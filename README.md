# selfhosted

Helm charts and install scripts for the services running on my home k3s
cluster (10 nodes: 3 control-plane, 7 workers). External exposure goes
through [`infra/traefik/`](infra/traefik/) — the single ingress choke point,
its ACME DNS-01 certresolver holding a `*.zachd.duckdns.org` wildcard cert
(no cert-manager involved), and its access log — with the DuckDNS side of
that credential in [`infra/duckdns/`](infra/duckdns/). Some services are
additionally published on `diemer.codes` via
[`infra/cloudflared/`](infra/cloudflared/).

Each subfolder is a standalone project with its own chart, docs, and
install/upgrade scripts. Per-project secrets live in a gitignored
`values.local.yaml` alongside the tracked `values.yaml`.

The exception is [`scripts/k3s/`](scripts/k3s/), which isn't a project: it
operates on the nodes themselves (health, disk cleanup, rolling restarts, OS
and k3s upgrades) over `tailscale ssh`, so it lives at the repo root.

[`plans/`](plans/) is the other non-project folder: designs that are decided but
not yet deployed, usually blocked on hardware or a maintenance window. A plan is
deleted once its work ships and the reasoning has moved into the READMEs it
touched.

Some projects live in their own repo and are tracked here as submodules, so this
repo stays the full index of what runs on the cluster. Clone with
`git clone --recurse-submodules`; an existing clone catches up with
`git submodule update --init`.

## Projects

| Folder | What it is | Docs |
|---|---|---|
| [`minecraft/`](minecraft/) | Prominence II: Hasturian Era (Fabric 1.20.1) via the `itzg/minecraft` chart. BlueMap + Discord Integration add-ons, mc-backup sidecar. | [README bootstrap in values.yaml](minecraft/values.yaml), [ADDON_SETUP](minecraft/ADDON_SETUP.md), [CLIENT_SETUP](minecraft/CLIENT_SETUP.md) |
| [`minecraft/claude-bridge/`](minecraft/claude-bridge/) | Sandboxed Claude Code in a pod — players type `/claude <q>`, the bridge tails the server log + replies via RCON, can teleport on request, logs feature requests to `FEEDBACK.md`. | [minecraft/claude-bridge/README](minecraft/claude-bridge/README.md) |
| [`minecraft/claude-mod/`](minecraft/claude-mod/) | Tiny server-side Fabric mod that registers `/claude <prompt>` via Brigadier and prints a recognizable line for `claude-bridge` to pick up. Sideloaded into the PVC via `install.sh`. | [minecraft/claude-mod/README](minecraft/claude-mod/README.md) |
| [`discord/vocard/`](discord/vocard/) | Vocard music bot + Lavalink + MongoDB — slash-command music player for voice channels. Bot-only (no dashboard). | [discord/vocard/README](discord/vocard/README.md) |
| [`discord/smitele-bot/`](discord/smitele-bot/) → **submodule** | Smite-le — a Wordle-shaped Smite guessing game bot, plus the daily Hi-Rez match data collector that was previously a Windows Task Scheduler job. The corpus lives on an SMB share from the NAS (ReadWriteMany: the CronJob writes it, the bot reads it); the bot's own caches sit on a small PVC. Chart + source live in [zdiemer/smitele-bot](https://github.com/zdiemer/smitele-bot). | [zdiemer/smitele-bot README](https://github.com/zdiemer/smitele-bot#readme) |
| [`games/romm/`](games/romm/) | RomM — self-hosted ROM manager + in-browser EmulatorJS player, library mounted read-only over SMB from the NAS. | [games/romm/README](games/romm/README.md) |
| [`games/gamedex/`](games/gamedex/) → **submodule** | Gamedex — searchable browser for the Games Master List spreadsheet, mirrored live from a Dropbox shared link. Faceted search, no auth, PII columns stripped. Chart + source live in [zdiemer/gamedex](https://github.com/zdiemer/gamedex); the pin here is the deployed commit. | [zdiemer/gamedex README](https://github.com/zdiemer/gamedex#readme) |
| [`finance/money/`](finance/money/) → **submodule** | money — self-hosted personal finance dashboard: a Monte Carlo retirement/FI planning engine, interactive scenarios, and an auto-syncing monthly net-worth tracker (SimpleFIN + real home/vehicle valuations + collectibles). Gated behind Authelia at `money.zachd.duckdns.org`; single user, DuckDNS-only, personal numbers never committed to the (public) image. | [zdiemer/money README](https://github.com/zdiemer/money#readme) |
| [`media/jellyfin/`](media/jellyfin/) | Jellyfin — media server for `/mnt/vault/media` (NFS, read-only), VAAPI transcode pinned to the one node with a GPU. Not behind Authelia (TV/phone apps need the raw API); LAN NodePort `:30096` for smart TVs. | [media/jellyfin/README](media/jellyfin/README.md) |
| [`media/jellyseerr/`](media/jellyseerr/) | Jellyseerr — request/discovery frontend; signs users in with their Jellyfin account and hands approved requests to Radarr/Sonarr. | [media/jellyseerr/README](media/jellyseerr/README.md) |
| [`media/arr/`](media/arr/) | Prowlarr + Sonarr + Radarr + qBittorrent-inside-gluetun (PIA, port forwarding, real killswitch). Acquisition automation behind Jellyfin: imports are hardlinks into the shared NFS media volume. Admin UIs behind Authelia forward-auth. The VPN pod deliberately bypasses `infra/egress-proxy`. | [media/arr/README](media/arr/README.md) |
| [`auth/authelia/`](auth/authelia/) | Authelia — OIDC provider + (future) Traefik forward-auth. Shared login for every service in the cluster. | [auth/authelia/README](auth/authelia/README.md) |
| [`docs/paperless-ngx/`](docs/paperless-ngx/) | Paperless-ngx — self-hosted document management with OCR + full-text search. Bundles Postgres, Redis, Tika, and Gotenberg inline. | [docs/paperless-ngx/README](docs/paperless-ngx/README.md) |
| [`docs/stirling-pdf/`](docs/stirling-pdf/) | Stirling PDF — locally-processed toolkit for ~50 PDF operations (merge/convert/OCR/sign/redact). Gated behind Authelia forward-auth at the Traefik ingress. | [docs/stirling-pdf/README](docs/stirling-pdf/README.md) |
| [`web/kelsey-green/`](web/kelsey-green/) | kelsey.green — static Astro site, no image of our own: git-sync pulls the CI-built `deploy` branch and nginx serves it. Public via a Cloudflare tunnel (outbound-only) as well as the usual DuckDNS ingress. | [web/kelsey-green/README](web/kelsey-green/README.md) |
| [`web/old-diemer-codes/`](web/old-diemer-codes/) → **submodule** | old.diemer.codes — the 2019 Create React App personal site, kept exactly as it was. Inverts the usual submodule shape: the app repo is a frozen archive we don't modify, so the chart + Dockerfile live here and the source is the submodule under `site/`. Public via the shared Cloudflare tunnel. | [web/old-diemer-codes/README](web/old-diemer-codes/README.md) |
| [`web/talaria/`](web/talaria/) → **submodule** | talaria — auction watch platform (search, tracking, listing alerts): a Python backend, scrapers, and a frontend, with Postgres, Elasticsearch, Redis and Logstash in its own chart at `helm/talaria/`. The biggest thing on the cluster, and the only one whose secrets are sops-encrypted in-git rather than in a `values.local.yaml`. Chart + source live in [zdiemer/talaria](https://github.com/zdiemer/talaria). | [zdiemer/talaria README](https://github.com/zdiemer/talaria#readme) |
| [`web/talaria-deals/`](web/talaria-deals/) | talaria.deals — a single Ingress publishing the sibling `talaria` project's existing service through the shared Cloudflare tunnel. Lives here rather than in talaria's chart because that chart is in another repo; additive, so talaria keeps answering on DuckDNS. | [web/talaria-deals/README](web/talaria-deals/README.md) |
| [`web/apartment-watch/`](web/apartment-watch/) | Daily SF rental scraper → SMS. A CronJob scrapes Craigslist (plain HTTP) plus Zumper/Apartments.com/Zillow (Camoufox, to clear Akamai and PerimeterX), filters on rent/laundry/parking/neighborhood with a scored scam filter, and texts a digest of new matches through `infra/sms-relay`. No frontend and no Ingress. Stipulations live in a gitignored `criteria.yaml`. | [web/apartment-watch/README](web/apartment-watch/README.md) |
| [`infra/cloudflared/`](infra/cloudflared/) | Shared, domain-agnostic Cloudflare Tunnel connector. Publishes services on `diemer.codes` (auth/webdav/keepass/docs/pdf/games/romm) and `talaria.deals` through Traefik over an outbound-only tunnel; each app also keeps its DuckDNS ingress via an `ingress.cloudflareHosts` list. One tunnel, any number of zones. | [infra/cloudflared/README](infra/cloudflared/README.md) |
| [`infra/traefik/`](infra/traefik/) | **Load-bearing for the whole cluster.** The Traefik config overlay: the `duckdns` ACME DNS-01 certresolver every ingress here names, the http→https redirect, the `Recreate` rollout, and JSON access logging. Doesn't install Traefik — k3s does — but every change is a cluster-wide ingress outage. Split out of `infra/duckdns`. | [infra/traefik/README](infra/traefik/README.md) |
| [`infra/duckdns/`](infra/duckdns/) | **Load-bearing for the whole cluster.** Keeps `zachd.duckdns.org` pointed at the house (updater CronJob) and owns the DuckDNS token that Traefik's certresolver reads — copied into `kube-system` because a `secretKeyRef` can't cross namespaces. Moved out of the sibling `talaria` project. | [infra/duckdns/README](infra/duckdns/README.md) |
| [`infra/alloy/`](infra/alloy/) | The telemetry pipeline. **Two releases from one directory:** a DaemonSet shipping logs (Traefik access log, cloudflared, Authelia, CrowdSec detections, the egress proxy, plus pods labelled from their own `ingress.enabled`) and nine Prometheus scrape jobs, and a one-replica Deployment running blackbox probes of the public hostnames. Values-only against the upstream chart; no PVC, so nothing is pinned to a node. Every target is filtered to the local node so each is handled exactly once — which is why the DaemonSet must tolerate every taint. | [infra/alloy/README](infra/alloy/README.md) |
| [`infra/grafana-dashboards/`](infra/grafana-dashboards/) | Four Grafana dashboards and thirteen alert rules as code, pushed to Grafana Cloud over its HTTP API by `upgrade.sh`. Deploys nothing to the cluster. Datasource UIDs are resolved at push time rather than committed; `--verify` runs every panel query against live data, because a dashboard of empty panels reads as "nothing is happening" rather than "this query is wrong". Alerts evaluate but route to a deliberate dead end until wired to sms-relay. | [infra/grafana-dashboards/README](infra/grafana-dashboards/README.md) |
| [`infra/node-exporter/`](infra/node-exporter/) | Host CPU/memory/disk/NIC metrics for all ten nodes. Values-only, DaemonSet, `hostNetwork` — which is not optional, since `/proc/net` resolves inside the reading process's netns and a cluster-network pod would report its own veth. The **only** source of visibility for Minecraft `:25565` and Jellyfin `:30096`, neither of which traverses Traefik. Collectors are opt-in to hold the series budget. | [infra/node-exporter/README](infra/node-exporter/README.md) |
| [`infra/kube-state-metrics/`](infra/kube-state-metrics/) | Kubernetes object state as metrics: CronJob last-success, backup freshness, PVC fill denominators, pod restarts, CrashLoop reasons. Unfiltered it is ~15–20k series against a 10k free tier, so the collector and metric allowlists are the whole design — `upgrade.sh` fails the deploy above 3k. | [infra/kube-state-metrics/README](infra/kube-state-metrics/README.md) |
| [`infra/ingress-policy/`](infra/ingress-policy/) | **Load-bearing for the whole cluster.** A `ValidatingAdmissionPolicy` requiring every Ingress in every namespace to name `ingressClassName: traefik`, pin `router.entrypoints: websecure`, and either name a certresolver or declare that TLS ends at the Cloudflare edge. No pods — the API server evaluates it. Exists because tenants from other repos (whatnowgg, talaria, money) can't see this repo's conventions. Advisory (`Warn`) until the warnings go quiet. | [infra/ingress-policy/README](infra/ingress-policy/README.md) |
| [`infra/cluster-status/`](infra/cluster-status/) | **Tailnet-only** dashboard at `status.zachd.duckdns.org` — nodes, CPU/RAM/disk broken down pods-vs-k3s-vs-system, pod tables, deployment health, warnings. A read-only collector sidecar writes JSON; nginx serves it as a static page, so traffic never touches the k8s API. Was public at `status.diemer.codes` until the page's *content* — node names, pod inventory, raw event text — was weighed rather than just its API safety. Ported from talaria's authed `/admin/cluster`. | [infra/cluster-status/README](infra/cluster-status/README.md) |
| [`infra/priority-classes/`](infra/priority-classes/) | **Load-bearing for the whole cluster.** The three cluster-wide scheduling priorities, including the `platform-app` globalDefault that every pod here inherits without naming it. Pure policy, no workload. Moved out of talaria. | [infra/priority-classes/README](infra/priority-classes/README.md) |
| [`infra/renovate/`](infra/renovate/) | Self-hosted Renovate as a weekly CronJob — opens version-bump PRs across the zdiemer repos (helm values images, `CHART_VERSION` pins, Dockerfile `ARG`s, Chart.yaml `appVersion`, plus normal npm/pip/actions in the app repos). Shared behavior preset in [`renovate/default.json`](renovate/default.json). PRs propose, a human merges, `upgrade.sh` deploys. | [infra/renovate/README](infra/renovate/README.md) |
| [`infra/headlamp/`](infra/headlamp/) | Headlamp — Kubernetes dashboard on the LAN at `<node-ip>:30100`. Stock upstream chart, values only. **cluster-admin**: the login token is a full cluster credential, so it's deliberately never published externally. Moved out of talaria. | [infra/headlamp/README](infra/headlamp/README.md) |
| [`infra/egress-proxy/`](infra/egress-proxy/) | **Load-bearing for the whole cluster.** The single **egress** choke point — the counterpart to `infra/traefik`. A CONNECT-only squid that services opt into with `egress.proxy.enabled`, so outbound traffic is attributable (auth username = chart name, in every log line), counted, and movable off the home address by naming a different lane. `lane: direct` means "measured, but unchanged", which is what makes it safe to onboard one service at a time. Never a TLS-terminating proxy: smitele-bot's Cloudflare clearance and talaria's JA3/JA4 both depend on the client's own handshake reaching the origin. | [infra/egress-proxy/README](infra/egress-proxy/README.md) |
| [`infra/coredns-config/`](infra/coredns-config/) | **Load-bearing for the whole cluster.** The CoreDNS config overlay, via the `coredns-custom` ConfigMap k3s's Corefile already imports. Today it owns one thing: a **gated DNS query log** that produces the cluster's egress inventory — every outbound connection starts with a lookup, so this sees all of it, including from repos this one can't read. Default off, and meant to be: it's an audit instrument for a measurement window, not a pipeline. Applying it restarts cluster DNS, so `upgrade.sh` canaries the stanza on the real image first and auto-rolls-back. Collected by [`scripts/egress-audit.sh`](scripts/egress-audit.sh). | [infra/coredns-config/README](infra/coredns-config/README.md) |

## Conventions

- **One namespace per project** (`minecraft`, `discord`). Created manually
  once before `helm install`; never managed by a chart.
- **Secrets never hit git.** Tokens, passwords, and any user-identifying
  config live only in `values.local.yaml`. The `.gitignore` glob
  `**/values.local.yaml` covers every project. Watch out for the neighbouring
  `*-secret.yaml` glob: it is meant for rendered manifests, but it also
  swallows chart *templates* named that way, silently and without an error
  (`templates/ghcr-secret.yaml` never gets added). Name pull-secret templates
  something else — see `web/apartment-watch/templates/imagepullsecret.yaml`.
- **1Password is where those secrets actually live.** `values.local.yaml` is
  still the only thing helm reads, but it is now materialized rather than
  hand-kept: each project tracks a `values.local.tpl.yaml` holding nothing but
  `op://` references, and

      op inject -i values.local.tpl.yaml -o values.local.yaml -f

  reproduces the real file. That one-liner is the whole contract — it works in
  a standalone app-repo clone with no `scripts/` directory. `scripts/secrets.sh`
  is bulk convenience on top (`sync`, `pull`, `status`, `verify`, `publish`,
  `backup`) and no deploy path depends on it. The `.example` files stay: they
  document *shape and provenance*, which a template of references cannot.
  `infra/coredns-config` is a deliberate exception — its `values.local.yaml` is
  a mode switch, not a secret, and its *absence* is what closes the DNS audit
  window, so it is never materialized.
- **And it converges on its own.** A `secrets.sh sync` timer (every 15 min,
  [`scripts/systemd/`](scripts/systemd/)) pushes when only the file moved, pulls
  when only the vault moved, and refuses when both did — judged against a
  recorded hash of what the two last agreed on, not against mtimes. It runs
  unattended because [`scripts/op-session.sh`](scripts/op-session.sh) obtains a
  session without a terminal, which is also why the weekly backup regained its
  vault dump. Editing a secret is back to editing the file; the ritual around it
  is gone. The standalone app clones under `~/Code` are in scope by default —
  they are where secrets actually change, and they used to be invisible to
  everything but the backup.
- **Each project ships an `upgrade.sh`** that does the right pre-flight
  (e.g. Minecraft flushes the world to disk and triggers a backup before
  the helm upgrade). Prefer it over raw `helm upgrade`.
- **Availability conventions.** Written down once here because they repeat in
  nearly every chart, and because most of them are the kind of thing that looks
  optional until a node drain proves otherwise.

  *A pod behind a Service gets a `preStop` sleep.* Removing a pod from a
  Service's Endpoints and sending it SIGTERM are concurrent, not ordered, so
  Traefik can still be routing to a pod that has already begun shutting down and
  those requests become 502s. `maxUnavailable: 0` does not help — it guarantees
  a Ready *replacement*, not that this pod stopped receiving traffic. 3s for
  singletons, 5s for the multi-replica tier. Use the native
  `lifecycle.preStop.sleep` action rather than `exec: sh -c sleep`: most images
  here have read-only roots and several have no shell. It needs k8s ≥ 1.30.

  *`terminationGracePeriodSeconds` is set explicitly wherever it matters.* The
  30s default is not enough for anything that flushes on exit — databases,
  SQLite-backed apps, qBittorrent's resume data, and above all Minecraft, where
  a world save cut in half is real corruption. It must exceed the `preStop`
  sleep plus the app's own shutdown.

  *PDBs exist only where `replicas >= 2`, and are gated on it in the template.*
  A `minAvailable: 1` budget against a single-replica Deployment can never be
  satisfied by evicting that replica, so `kubectl drain` blocks forever rather
  than failing. Singletons are therefore deliberately unprotected by a budget;
  [`scripts/k3s/drain-preflight.sh`](scripts/k3s/drain-preflight.sh) reports
  what a drain will gap instead of pretending to prevent it.

  *Spread is `DoNotSchedule`, not a preference.* A `preferred` podAntiAffinity
  is a hint the scheduler may decline, so "the replicas are spread" becomes a
  prediction rather than a property — and it fails silently. With 10 nodes and
  2–3 replicas nothing makes spreading unsatisfiable. Always include
  `matchLabelKeys: [pod-template-hash]`, or a rollout measures its surge pod
  against the pods it is replacing, finds every node full, and stalls.

  *`Recreate` vs `RollingUpdate` follows the volume, not taste.* A single-writer
  RWO volume means `Recreate` — and on `truenas-iscsi` that is not a
  preference: RWO there is single-node *attach*, so a surge pod on another node
  blocks on `FailedAttachVolume` and the rollout wedges rather than fails.
  Charts with no volume, or with genuinely shared state, roll with
  `maxUnavailable: 0` — which is only meaningful if the container has a
  readiness probe, so add one before relying on it.

  *Downtime claims need a number.*
  [`scripts/measure-gap.sh`](scripts/measure-gap.sh) polls a host while an
  upgrade or drain runs and reports the outage windows. Two "zero-downtime"
  rollouts in this repo were silently broken before it existed. One caveat it
  now warns about: on an Authelia-gated host the forward-auth middleware answers
  before Traefik routes anywhere, so an unauthenticated probe measures Authelia
  rather than the service behind it.
- **Ingresses do not name a certresolver.** They set
  `traefik.ingress.kubernetes.io/router.tls: "true"` and pick up the cluster-wide
  default certificate from Traefik's TLSStore — the wildcard that
  [`infra/traefik-certs/`](infra/traefik-certs/) renews into a Secret. Traefik
  itself no longer runs ACME at all.

  The constraint that shaped all of this still holds and is worth knowing before
  adding a host: **DuckDNS can only write a TXT record at the account's own
  subdomain**, so a per-host cert for `foo.zachd.duckdns.org` cannot answer its
  own DNS-01 challenge. Every sub-subdomain rides the wildcard SAN instead.
  That is why the wildcard is not optional, and why per-Ingress
  `router.tls.domains.*` annotations used to exist — they are gone now because
  one default certificate covers every host.
- **Apps we write ourselves live in their own repo**, added back here as a
  submodule so this repo still lists everything on the cluster. The app repo owns
  its chart *and* its source together — `Chart.yaml` `appVersion` tracks
  `values.yaml` `image.tag`, so a release is one commit in one place. It builds to
  `ghcr.io/zdiemer/<name>` (public package: the cluster is multi-node, so every
  node pulls anonymously) and ships `build.sh` + `upgrade.sh`.
  [zdiemer/gamedex](https://github.com/zdiemer/gamedex) is the reference shape.
  Work in the app's own checkout and deploy from there — that checkout is the
  **source** for its `values.local.yaml`, and the submodule copy here is a
  materialize-on-demand artifact, never a second source.

  **The submodule worktrees are read-only** (`scripts/submodules-lock.sh`:
  `chmod -R a-w` plus a `no-push` remote, with matching deny rules in the tracked
  `.claude/settings.json`). An edit made inside one looks like it worked and can
  never ship, which is the kind of mistake worth making impossible rather than
  memorable. Unlock deliberately if you ever need to.

  Pins move on their own: [`scripts/sync-submodules.sh`](scripts/sync-submodules.sh)
  (daily timer) advances one only once the chart's `image.tag` at that commit
  matches the image the cluster is actually running, so the pin keeps meaning
  *what shipped* rather than *what is on the branch*. It commits locally and
  never pushes. `web/whatnowgg` deploys to a VPS instead of this cluster, so it
  tracks its newest `v*` tag; `web/talaria` pins every image to `:latest`, so its
  pin follows the branch head with that stated plainly; `web/old-diemer-codes/site`
  is a frozen archive and is skipped.

  The one exception is [`web/old-diemer-codes/`](web/old-diemer-codes/), where the
  app repo is a frozen 2019 archive that is deliberately not being modified: the
  chart and the Dockerfile live in *this* repo and the app is the submodule
  underneath, rather than the other way round. The submodule has to sit inside the
  chart directory because `docker build` cannot COPY from outside its context —
  which also means npm only ever runs in the build, so the submodule worktree is
  never dirtied. Everything else — GHCR, `build.sh`, `upgrade.sh`, appVersion
  tracking `image.tag` — is the same shape.
