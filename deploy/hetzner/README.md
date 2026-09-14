# Hetzner VPN-only pilot

This override deploys one MartinCall application, a fresh TimescaleDB database, and a read-only
live IB Gateway on the existing `martincall-net` Docker network. It does not import a local
database, runtime data, or local API credentials.

On the first deployment Compose creates the named `martincall-timescale-data` volume. The override
also removes the root stack's local MartinCall runtime-secret and Codex bridge mounts; the only
required pilot secrets are the file-backed IBKR and maintenance VNC passwords created below.

The host bindings are intentionally limited to the Amnezia Docker bridge:

- terminal: `172.29.172.1:8000`;
- maintenance VNC: `172.29.172.1:5900`;
- PostgreSQL: root compose default `127.0.0.1:5432`;
- IBKR API: internal Docker network only, port `4003`.

Create `/opt/martincall/shared/data`, copy `server.env.example` to the release root as `.env`, replace
the password/build placeholders, and run the credential helper interactively:

```bash
sudo deploy/hetzner/set-ibkr-credentials.sh
```

The helper leaves the environment file root-only and gives the two file-backed secrets to the
pinned Gateway runtime UID `1000`, all with mode `0600`. The maintenance VNC password is a separate
eight-character ASCII password; do not reuse the IBKR or SSH password.
The one-shot `ib-gateway-settings-init` service gives the same runtime UID ownership of a newly
created persistent settings volume before Gateway starts.

Validate and start from the release root:

```bash
docker compose -f docker-compose.yml -f deploy/hetzner/docker-compose.yml config --quiet
docker compose -f docker-compose.yml -f deploy/hetzner/docker-compose.yml up -d --build
```

The example enables autonomous prospective capture for the exact ES futures-root identity and binds
`/opt/martincall/shared/data` at `/data`, so application rebuilds do not move the journal. Validate
the live owner and create a copyable snapshot archive from the release root:

```bash
./martincall-server.sh research-status
./martincall-server.sh research-export
```

Copy the reported archive and sibling manifest from
`/opt/martincall/shared/data/research/exports/` to the local external data tree, then run the manifest
verification command documented in `data/README.md` before analysis. This storage is separate from
the TimescaleDB volume; deleting or recreating the app container does not delete the host mount.

IBKR still requires its own login and two-factor confirmation. The terminal remains usable through
the VPN while Gateway authentication is pending, but IBKR data stays disconnected until login
completes. The Gateway supervisor makes only one login attempt at container startup. If that IB Key
request is missed, open **Settings → System Health** and press **New login** to request one fresh
full-authentication attempt. The adjacent **API RST** action only reconnects MartinCall's broker
sockets and does not create a new IB Key request. The private login-control port is exposed only to
the Compose network; it is never published on the host. VNC is a maintenance surface, not a public
application endpoint.
