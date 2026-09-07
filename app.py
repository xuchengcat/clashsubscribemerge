"""Dynamic Clash/Mihomo subscription backed by a ZeroTier member address."""

import hmac
import ipaddress
import base64
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import requests
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

ROOT = Path(__file__).resolve().parent
# Render provides environment variables directly. Locally, load the ignored .env file.
load_dotenv(ROOT / ".env")
HOSTS_FILE = ROOT / "config" / "domains.txt"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not configured")
    return value


def read_domains() -> list[str]:
    domains = []
    for raw_line in HOSTS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            domains.append(line)
    if not domains:
        raise RuntimeError("config/domains.txt contains no domains")
    return domains


def load_base_config() -> dict[str, Any]:
    configured_path = Path(os.getenv("BASE_CONFIG_PATH", "config/base.yaml"))
    path = configured_path if configured_path.is_absolute() else ROOT / configured_path
    try:
        path.resolve().relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeError("BASE_CONFIG_PATH must point inside the application directory") from exc

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError("The base configuration must be a YAML object")
    return data


def strings_at(value: Any, field_names: set[str]) -> Iterable[str]:
    """Find address assignment fields in a ZeroTier member response."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in field_names and isinstance(child, list):
                yield from (item for item in child if isinstance(item, str))
            yield from strings_at(child, field_names)
    elif isinstance(value, list):
        for child in value:
            yield from strings_at(child, field_names)


def zerotier_ip_address() -> str:
    token = required_env("ZEROTIER_API_TOKEN")
    network_id = required_env("ZEROTIER_NETWORK_ID")
    member_id = required_env("ZEROTIER_MEMBER_ID")
    url = f"https://api.zerotier.com/api/v1/network/{network_id}/member/{member_id}"

    try:
        response = requests.get(
            url,
            headers={"Authorization": f"token {token}"},
            timeout=(5, 15),
        )
        response.raise_for_status()
        member = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("ZeroTier member lookup failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not obtain ZeroTier address") from exc

    requested_version = os.getenv("ZEROTIER_IP_VERSION", "auto").strip().lower()
    if requested_version not in {"auto", "4", "6"}:
        raise RuntimeError("ZEROTIER_IP_VERSION must be auto, 4, or 6")

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []

    # This controller exposes the member's usable IPv6 address as
    # physicalAddress. Other controllers expose assignments under config.
    physical_address = member.get("physicalAddress")
    if isinstance(physical_address, str):
        try:
            addresses.append(ipaddress.ip_address(physical_address))
        except ValueError:
            # Standard ZeroTier controllers can use this field for a node ID.
            pass

    for candidate in strings_at(member, {"v6assignments", "ipassignments"}):
        try:
            address = ipaddress.ip_interface(candidate).ip
        except ValueError:
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
        if address not in addresses:
            addresses.append(address)

    preferred_versions = [6, 4] if requested_version == "auto" else [int(requested_version)]
    for version in preferred_versions:
        for address in addresses:
            if address.version == version:
                return str(address)

    logger.warning("ZeroTier response did not include a requested IP assignment")
    raise HTTPException(status_code=502, detail="No requested IP assignment found for ZeroTier member")


def formatted_subscription_url(format_name: str) -> str:
    """Build a provider URL without retaining stale format parameters."""
    source = required_env("UPSTREAM_SUBSCRIPTION_URL")
    parsed = urlsplit(source)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"flag", "sr"}
    ]
    if format_name == "clash":
        query.append(("flag", "clash"))
    elif format_name == "shadowrocket":
        query.extend((("flag", "shadowrocket"), ("sr", "conf")))
    else:
        raise ValueError(f"Unknown subscription format: {format_name}")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def fetch_subscription(url: str, headers: dict[str, str]) -> requests.Response:
    """Fetch an upstream subscription with bounded retries and token-safe logs."""
    last_error: requests.RequestException | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, timeout=(10, 30))
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            status = exc.response.status_code if exc.response is not None else "network"
            logger.warning(
                "Subscription request attempt %s failed (status=%s)", attempt + 1, status
            )
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise HTTPException(status_code=502, detail="Could not obtain upstream subscription") from last_error


def upstream_config() -> dict[str, Any]:
    """Fetch the original Clash/Mihomo YAML subscription when configured."""
    if not os.getenv("UPSTREAM_SUBSCRIPTION_URL", "").strip():
        return load_base_config()
    url = formatted_subscription_url("clash")

    try:
        response = fetch_subscription(
            url,
            {
                # Match Clash Meta for Android so the provider returns its
                # full Meta-compatible profile rather than a downgraded one.
                "User-Agent": os.getenv(
                    "UPSTREAM_USER_AGENT", "ClashMetaForAndroid/2.11.24"
                ),
                "Accept": "application/yaml, text/yaml, text/plain, */*",
            },
        )
        data = yaml.safe_load(response.text)
    except yaml.YAMLError as exc:
        logger.warning("Upstream subscription YAML parsing failed")
        raise HTTPException(status_code=502, detail="Could not obtain upstream subscription") from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=502,
            detail="Upstream subscription is not a Clash/Mihomo YAML configuration",
        )
    return data


def decode_base64(value: str | bytes) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def upstream_shadowrocket_proxies() -> list[dict[str, Any]]:
    """Fetch and decode the provider's native Shadowrocket VMess subscription."""
    try:
        response = fetch_subscription(
            formatted_subscription_url("shadowrocket"),
            {
                "User-Agent": os.getenv(
                    "SHADOWROCKET_UPSTREAM_USER_AGENT", "Shadowrocket/2.2.68"
                ),
                "Accept": "text/plain, */*",
            },
        )
        decoded = decode_base64(response.content.strip()).decode("utf-8-sig")
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("Shadowrocket subscription decoding failed")
        raise HTTPException(
            status_code=502, detail="Could not obtain Shadowrocket subscription"
        ) from exc

    proxies = []
    for line in decoded.splitlines():
        if not line.startswith("vmess://"):
            continue
        try:
            parsed = urlsplit(line)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            credential = decode_base64(parsed.netloc).decode("utf-8")
            method_uuid, endpoint = credential.rsplit("@", 1)
            method, uuid = method_uuid.split(":", 1)
            server, port = endpoint.rsplit(":", 1)
            proxy: dict[str, Any] = {
                "name": unquote(query.get("remark") or query.get("remarks") or server),
                "type": "vmess",
                "server": server.strip("[]"),
                "port": int(port),
                "uuid": uuid,
                "alterId": int(query.get("alterId", 0)),
                "cipher": method or "auto",
                "network": "ws" if query.get("obfs") == "websocket" else "tcp",
                "udp": True,
            }
            if proxy["network"] == "ws":
                proxy["ws-opts"] = {
                    "path": query.get("path") or "/",
                    "headers": {"Host": query.get("obfsParam", "")},
                }
            if query.get("tls") not in (None, "", "none"):
                proxy["tls"] = True
                proxy["servername"] = query.get("peer") or query.get("obfsParam")
            proxies.append(proxy)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=502, detail="Invalid VMess node in Shadowrocket subscription"
            ) from exc
    if not proxies:
        raise HTTPException(
            status_code=502, detail="Shadowrocket subscription contains no VMess nodes"
        )
    return proxies


def shadowrocket_vmess_proxy(proxy: dict[str, Any]) -> str:
    """Convert one Clash VMess node to Shadowrocket's configuration syntax."""
    required = ("name", "server", "port", "uuid")
    missing = [key for key in required if proxy.get(key) in (None, "")]
    if missing:
        raise RuntimeError(f"VMess proxy is missing required fields: {', '.join(missing)}")

    fields = [
        "vmess",
        str(proxy["server"]),
        str(proxy["port"]),
        f'username={proxy["uuid"]}',
        f'alterId={proxy.get("alterId", 0)}',
        f'method={proxy.get("cipher", "auto")}',
    ]

    if proxy.get("network") == "ws":
        ws_options = proxy.get("ws-opts") or {}
        ws_headers = ws_options.get("headers") or proxy.get("ws-headers") or {}
        ws_path = ws_options.get("path") or proxy.get("ws-path") or "/"
        fields.extend(("ws=true", f"ws-path={ws_path}"))
        if ws_headers.get("Host"):
            fields.append(f'ws-headers=Host:{ws_headers["Host"]}')

    if proxy.get("tls"):
        fields.append("tls=true")
        server_name = proxy.get("servername") or proxy.get("sni")
        if server_name:
            fields.append(f"sni={server_name}")
        if proxy.get("skip-cert-verify"):
            fields.append("skip-cert-verify=true")

    if proxy.get("udp", True):
        fields.append("udp-relay=true")
    return f'{proxy["name"]} = ' + ", ".join(fields)


def shadowrocket_config(
    address: str, clash_config: dict[str, Any], proxies: list[dict[str, Any]]
) -> str:
    """Build a Shadowrocket profile solely from live upstream responses."""
    if not isinstance(proxies, list) or not proxies:
        raise RuntimeError("The upstream Clash configuration contains no proxies")
    unsupported = sorted(
        {str(proxy.get("type", "missing")) for proxy in proxies if proxy.get("type") != "vmess"}
    )
    if unsupported:
        raise RuntimeError(
            "Shadowrocket conversion does not yet support proxy types: "
            + ", ".join(unsupported)
        )
    proxy_names = [str(proxy.get("name", "")) for proxy in proxies]
    if any(not name for name in proxy_names) or len(proxy_names) != len(set(proxy_names)):
        raise RuntimeError("Upstream proxy names must be non-empty and unique")

    clash_groups = clash_config.get("proxy-groups") or []
    if not isinstance(clash_groups, list) or not clash_groups:
        raise RuntimeError("The upstream Clash configuration contains no proxy groups")
    group_names = {str(group.get("name", "")) for group in clash_groups}
    proxy_name_set = set(proxy_names)
    usable_names = [
        name
        for name in proxy_names
        if not name.startswith(("剩余流量：", "套餐到期：", "过滤掉"))
    ] or proxy_names
    rebuilt_groups = []
    for group in clash_groups:
        name = str(group.get("name", ""))
        group_type = str(group.get("type", "select"))
        options = [str(item) for item in (group.get("proxies") or [])]
        options = [item for item in options if item in proxy_name_set | group_names | {"DIRECT", "REJECT", "PROXY"}]
        if not options:
            options = usable_names.copy()
        extra = []
        for key in ("url", "interval", "tolerance", "timeout"):
            if group.get(key) is not None:
                extra.append(f"{key}={group[key]}")
        rebuilt_groups.append(f"{name} = {group_type}," + ",".join(options + extra))

    canonical_groups = {name.upper(): name for name in group_names}
    rules = []
    for raw_rule in clash_config.get("rules") or []:
        line = str(raw_rule)
        parts = line.split(",")
        if parts[0].strip().upper() == "MATCH":
            parts[0] = "FINAL"
        policy_index = -2 if parts[-1].strip() == "no-resolve" else -1
        policy = parts[policy_index].strip()
        if policy.upper() in canonical_groups:
            parts[policy_index] = canonical_groups[policy.upper()]
        rules.append(",".join(parts))

    hosts = clash_config.get("hosts") or {}
    if not isinstance(hosts, dict):
        raise RuntimeError("The upstream Clash hosts key must be a YAML object")
    hosts = {str(name): str(value) for name, value in hosts.items()}
    hosts.update({domain: address for domain in read_domains()})
    dns = clash_config.get("dns") or {}
    nameservers = dns.get("nameserver") or ["system"] if isinstance(dns, dict) else ["system"]
    lines = [
        "# Generated from live Clash and Shadowrocket subscriptions",
        "[General]",
        "bypass-system = true",
        "udp-policy-not-supported-behaviour = REJECT",
        f'ipv6 = {str(bool(clash_config.get("ipv6", True))).lower()}',
        "prefer-ipv6 = false",
        "dns-server = " + ", ".join(str(item) for item in nameservers),
        "fallback-dns-server = system",
        "dns-direct-system = false",
        "icmp-auto-reply = true",
        "always-reject-url-rewrite = false",
        "private-ip-answer = true",
        "dns-direct-fallback-proxy = false",
        # Managed host mappings must also apply when a domain matches a proxy
        # policy; otherwise Shadowrocket delegates its resolution remotely.
        "use-local-host-item-for-proxy = true",
        "",
        "[Proxy]",
        *map(shadowrocket_vmess_proxy, proxies),
        "",
        "[Proxy Group]",
        *rebuilt_groups,
        "",
        "[Rule]",
        *rules,
        "",
        "[Host]",
        *(f"{name} = {value}" for name, value in hosts.items()),
    ]
    return "\n".join(lines) + "\n"


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/s/{subscription_token}", response_class=Response)
def subscription(subscription_token: str) -> Response:
    expected_token = required_env("SUBSCRIPTION_TOKEN")
    if not hmac.compare_digest(subscription_token, expected_token):
        raise HTTPException(status_code=404, detail="Not found")

    address = zerotier_ip_address()
    # Retain every upstream setting and change only the selected host mappings.
    config = upstream_config()
    hosts = config.setdefault("hosts", {})
    if not isinstance(hosts, dict):
        raise RuntimeError("The base configuration's hosts key must be a YAML object")
    hosts.update({domain: address for domain in read_domains()})

    payload = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    return Response(
        content=payload,
        media_type="text/yaml; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/shadowrocket/{subscription_token}", response_class=Response)
def shadowrocket_subscription(subscription_token: str) -> Response:
    expected_token = required_env("SUBSCRIPTION_TOKEN")
    if not hmac.compare_digest(subscription_token, expected_token):
        raise HTTPException(status_code=404, detail="Not found")

    payload = shadowrocket_config(
        zerotier_ip_address(), upstream_config(), upstream_shadowrocket_proxies()
    )
    return Response(
        content=payload,
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="shadowrocket.conf"',
        },
    )
