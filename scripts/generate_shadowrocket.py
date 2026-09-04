"""Generate a local Shadowrocket profile from a Clash YAML configuration."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import (  # noqa: E402
    shadowrocket_config,
    upstream_config,
    upstream_shadowrocket_proxies,
    zerotier_ip_address,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "local" / "shadowrocket.generated.conf",
    )
    args = parser.parse_args()

    config = upstream_config()
    proxies = upstream_shadowrocket_proxies()
    payload = shadowrocket_config(zerotier_ip_address(), config, proxies)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    print(
        f"Generated {args.output} with {len(proxies)} proxies from live subscriptions"
    )


if __name__ == "__main__":
    main()
