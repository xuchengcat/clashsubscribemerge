# ZeroTier dynamic Hosts subscription for Clash Meta

This service returns a complete Mihomo/Clash YAML subscription. For every subscription request it fetches your existing Clash/Mihomo YAML subscription, queries one ZeroTier member, and maps every domain in `config/domains.txt` to that member's current IP address under `hosts`.

The ZeroTier API token stays in Render environment variables and is never included in the returned subscription.

## Deploy on Render

1. Create a new **Web Service** from this Git repository. Render detects `render.yaml`.
2. Set these secret environment variables in the Render dashboard:

   - `ZEROTIER_API_TOKEN`: a newly generated ZeroTier API token.
   - `ZEROTIER_NETWORK_ID`: the ZeroTier network ID.
   - `ZEROTIER_MEMBER_ID`: the target member ID.
   - `ZEROTIER_IP_VERSION`: `auto` (default, IPv6 then IPv4), `4`, or `6`.
   - `UPSTREAM_SUBSCRIPTION_URL`: the base provider URL without `flag` or `sr` parameters. The service adds the required format parameters.
   - `UPSTREAM_USER_AGENT`: `ClashMetaForAndroid/2.11.24` (the default). This requests the same profile format as the Android app.
   - `SHADOWROCKET_UPSTREAM_USER_AGENT`: `Shadowrocket/2.2.68` (the default).
   - `SUBSCRIPTION_TOKEN`: a random URL-safe secret at least 32 characters long.

3. Deploy. Confirm `https://<service>.onrender.com/healthz` returns `{"status":"ok"}`.
4. Add this URL as a Clash Meta profile subscription:

   ```text
   https://<service>.onrender.com/s/<SUBSCRIPTION_TOKEN>
   ```

5. Enable periodic profile updates in Clash Meta (six hours is a sensible starting interval).

`render.yaml` uses Render's Free plan for initial testing. Change `plan: free` to your preferred paid plan before relying on scheduled updates; free web services can sleep after inactivity.

## Configure the returned Clash profile

Set `UPSTREAM_SUBSCRIPTION_URL` to preserve your existing nodes, proxy groups, rules, and DNS settings. The service changes only its `hosts` mapping; any entries for domains in `config/domains.txt` are replaced with the current ZeroTier member address. `ZEROTIER_IP_VERSION=auto` prefers IPv6 and falls back to IPv4; set it to `4` or `6` to require a specific address family. The service accepts an IP-formatted `physicalAddress` (as returned by your controller) as well as standard ZeroTier assignment fields.

If no upstream URL is set, the service instead returns `config/base.yaml`. This fallback is useful for development, but its committed example is direct-only and is not a replacement for a normal node subscription.

For Clash, the service adds `flag=clash` and expects Clash/Mihomo YAML. For
Shadowrocket, it adds `flag=shadowrocket&sr=conf`, decodes the provider's native
node subscription, and combines those nodes with groups and rules from the Clash
response.

Edit `config/domains.txt` to add or remove hostnames. Use one hostname per line; blank lines and lines beginning with `#` are ignored.

IP values are emitted as YAML strings by PyYAML, which is the expected representation in the `hosts` map.

## Local test

PowerShell:

```powershell
Copy-Item .env.example .env
# Edit .env and enter a newly rotated ZeroTier token and the four remaining values.
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

Then request `http://127.0.0.1:8000/s/<random secret>`.

For Shadowrocket, add
`http://127.0.0.1:8000/shadowrocket/<random secret>` as a remote configuration.
Every request fetches both provider formats. Native Shadowrocket nodes populate
`[Proxy]`; the Clash response supplies `[Proxy Group]`, `[Rule]`, DNS settings, and
existing hosts. The service then updates the `[Host]` entries listed in
`config/domains.txt` with the current ZeroTier address. Local configuration files
are not used as generation templates.

The generated `[Host]` section is placed at the end of the profile so Shadowrocket
recognizes and applies the dynamic mappings.
`use-local-host-item-for-proxy = true` is emitted explicitly so those mappings also
apply when a domain is routed through a proxy policy instead of `DIRECT`.

To create a file for local import testing from the configured remote subscription:

```bash
python3 scripts/generate_shadowrocket.py
```

The output is `local/shadowrocket.generated.conf`. The `local` directory is ignored
by Git because the generated profile contains node credentials. Use `--output` to
select another destination.

The converter currently accepts VMess nodes, including WebSocket and TLS options.
If the upstream contains another protocol, the request fails explicitly instead of
silently publishing an incomplete subscription.

The application automatically loads `.env` only for local execution. Render ignores
this file and receives the configured values from its environment-variable settings.

## Security and operation

- Revoke the API token previously placed in `changehost.bat`; it was exposed and should not be reused.
- Keep this repository private if its base configuration reveals server names or proxy information.
- The URL token protects the subscription endpoint. Treat the full subscription URL as a secret; anyone with it can retrieve the returned configuration.
- The endpoint does not persist generated output. Therefore restarts and deploys cannot lose the current host address.
- The service deliberately does not use `physicalAddress`: that field is a ZeroTier member ID, not an IPv6 address.
