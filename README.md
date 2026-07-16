# selfhosted

Helm charts and install scripts for the services running on my home k3s
cluster (10 nodes: 3 control-plane, 7 workers). External exposure goes
through the sibling `talaria` project's DuckDNS + cert-manager ingress.

Each subfolder is a standalone project with its own chart, docs, and
install/upgrade scripts. Per-project secrets live in a gitignored
`values.local.yaml` alongside the tracked `values.yaml`.

## Projects

| Folder | What it is | Docs |
|---|---|---|
| [`minecraft/`](minecraft/) | Prominence II: Hasturian Era (Fabric 1.20.1) via the `itzg/minecraft` chart. BlueMap + Discord Integration add-ons, mc-backup sidecar. | [README bootstrap in values.yaml](minecraft/values.yaml), [ADDON_SETUP](minecraft/ADDON_SETUP.md), [CLIENT_SETUP](minecraft/CLIENT_SETUP.md) |
| [`minecraft/claude-bridge/`](minecraft/claude-bridge/) | Sandboxed Claude Code in a pod — players type `/claude <q>`, the bridge tails the server log + replies via RCON, can teleport on request, logs feature requests to `FEEDBACK.md`. | [minecraft/claude-bridge/README](minecraft/claude-bridge/README.md) |
| [`minecraft/claude-mod/`](minecraft/claude-mod/) | Tiny server-side Fabric mod that registers `/claude <prompt>` via Brigadier and prints a recognizable line for `claude-bridge` to pick up. Sideloaded into the PVC via `install.sh`. | [minecraft/claude-mod/README](minecraft/claude-mod/README.md) |
| [`discord/vocard/`](discord/vocard/) | Vocard music bot + Lavalink + MongoDB — slash-command music player for voice channels. Bot-only (no dashboard). | [discord/vocard/README](discord/vocard/README.md) |
| [`games/romm/`](games/romm/) | RomM — self-hosted ROM manager + in-browser EmulatorJS player, library mounted read-only over SMB from the NAS. | [games/romm/README](games/romm/README.md) |
| [`games/gamedex/`](games/gamedex/) | Gamedex — searchable browser for the Games Master List spreadsheet, mirrored live from a Dropbox shared link. Faceted search, no auth, PII columns stripped. | [games/gamedex/README](games/gamedex/README.md) |
| [`auth/authelia/`](auth/authelia/) | Authelia — OIDC provider + (future) Traefik forward-auth. Shared login for every service in the cluster. | [auth/authelia/README](auth/authelia/README.md) |
| [`docs/paperless-ngx/`](docs/paperless-ngx/) | Paperless-ngx — self-hosted document management with OCR + full-text search. Bundles Postgres, Redis, Tika, and Gotenberg inline. | [docs/paperless-ngx/README](docs/paperless-ngx/README.md) |
| [`docs/stirling-pdf/`](docs/stirling-pdf/) | Stirling PDF — locally-processed toolkit for ~50 PDF operations (merge/convert/OCR/sign/redact). Gated behind Authelia forward-auth at the Traefik ingress. | [docs/stirling-pdf/README](docs/stirling-pdf/README.md) |
| [`web/kelsey-green/`](web/kelsey-green/) | kelsey.green — static Astro site, no image of our own: git-sync pulls the CI-built `deploy` branch and nginx serves it. Public via a Cloudflare tunnel (outbound-only) as well as the usual DuckDNS ingress. | [web/kelsey-green/README](web/kelsey-green/README.md) |
| [`infra/cloudflared/`](infra/cloudflared/) | Shared, domain-agnostic Cloudflare Tunnel connector. Publishes services on `diemer.codes` (auth/webdav/keepass/docs/pdf/games/romm) through Traefik over an outbound-only tunnel; each app also keeps its DuckDNS ingress via an `ingress.cloudflareHosts` list. Reusable for more domains. | [infra/cloudflared/README](infra/cloudflared/README.md) |

## Conventions

- **One namespace per project** (`minecraft`, `discord`). Created manually
  once before `helm install`; never managed by a chart.
- **Secrets never hit git.** Tokens, passwords, and any user-identifying
  config live only in `values.local.yaml`. The `.gitignore` glob
  `**/values.local.yaml` covers every project.
- **Each project ships an `upgrade.sh`** that does the right pre-flight
  (e.g. Minecraft flushes the world to disk and triggers a backup before
  the helm upgrade). Prefer it over raw `helm upgrade`.
