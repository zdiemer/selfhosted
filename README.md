# selfhosted

Helm charts and install scripts for the services running on my home k3s
cluster (single node, `zachd-ubuntu`). External exposure goes through the
sibling [`talaria`](../talaria) project's DuckDNS + cert-manager ingress.

Each subfolder is a standalone project with its own chart, docs, and
install/upgrade scripts. Per-project secrets live in a gitignored
`values.local.yaml` alongside the tracked `values.yaml`.

## Projects

| Folder | What it is | Docs |
|---|---|---|
| [`minecraft/`](minecraft/) | Prominence II: Hasturian Era (Fabric 1.20.1) via the `itzg/minecraft` chart. BlueMap + Discord Integration add-ons, mc-backup sidecar. | [README bootstrap in values.yaml](minecraft/values.yaml), [ADDON_SETUP](minecraft/ADDON_SETUP.md), [CLIENT_SETUP](minecraft/CLIENT_SETUP.md) |
| [`discord/vocard/`](discord/vocard/) | Vocard music bot + Lavalink + MongoDB — slash-command music player for voice channels. Bot-only (no dashboard). | [discord/vocard/README](discord/vocard/README.md) |
| [`games/romm/`](games/romm/) | RomM — self-hosted ROM manager + in-browser EmulatorJS player, library mounted read-only over SMB from the NAS. | [games/romm/README](games/romm/README.md) |

## Conventions

- **One namespace per project** (`minecraft`, `discord`). Created manually
  once before `helm install`; never managed by a chart.
- **Secrets never hit git.** Tokens, passwords, and any user-identifying
  config live only in `values.local.yaml`. The `.gitignore` glob
  `**/values.local.yaml` covers every project.
- **Each project ships an `upgrade.sh`** that does the right pre-flight
  (e.g. Minecraft flushes the world to disk and triggers a backup before
  the helm upgrade). Prefer it over raw `helm upgrade`.
