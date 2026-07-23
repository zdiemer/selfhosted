# happy-server

Self-hosted [Happy](https://github.com/slopus/happy) relay: the sync backend
that lets the Happy iOS/Android/web apps remote-control the `claude` sessions
running in [`dev/claude-workspace`](../claude-workspace/) — full Claude Code
harness (plan mode, hooks, skills, real permission prompts), phone UI.

One pod, self-contained: embedded PGlite database + blob files on a `/data`
PVC. No Postgres, no Redis, no S3. Clients E2E-encrypt everything before it
reaches the server, so the relay stores only ciphertext.

## Why there is NO Authelia in front of this (⚠️ read before "fixing")

Every other web surface in this repo sits behind Authelia forward-auth. This
one deliberately does not: the phone app speaks REST + WebSocket directly to
the API and cannot complete an interactive forward-auth flow. Happy's own
security model replaces it — devices pair via QR code, keys never leave the
devices, and the server never sees plaintext. Adding the forward-auth
middleware here would break the app while protecting nothing the E2E crypto
doesn't already cover. What the ingress *does* still get: TLS via the DuckDNS
wildcard cert, same as everything else.

## First install

```sh
# 1. Image (upstream ships no server image; we build from the monorepo)
./build.sh
# First push only: set ghcr.io/zdiemer/happy-server package → Public.

# 2. Local values: master secret + ingress off
cp values.local.yaml.example values.local.yaml
# put `openssl rand -base64 48` output into masterSecret, keep ingress.enabled
# commented/false for now

# 3. Install
kubectl create namespace happy
helm install happy-server . -n happy -f values.yaml -f values.local.yaml
kubectl -n happy get pods -w

# 4. Smoke test
kubectl -n happy port-forward svc/happy-server 3005:3005
#   curl -i http://localhost:3005/   → any HTTP answer (even 404) = server up

# 5. Expose
# flip ingress.enabled: true in values.local.yaml, then
./upgrade.sh
```

One-time Cloudflare step (per `infra/cloudflared` README): Zero Trust →
Networks → Tunnels → the shared tunnel → Public Hostnames → add
`happy.diemer.codes` → `https://traefik.kube-system.svc.cluster.local:443`
with **No TLS Verify ON**. (`happy.zachd.duckdns.org` needs nothing — the
wildcard SAN cert and DuckDNS A record already cover it.)

## Wiring up the clients

- **CLI (workspace pod)**: `dev/claude-workspace` sets
  `HAPPY_SERVER_URL=https://happy.zachd.duckdns.org` on the term container —
  nothing to do there. In tmux, run `happy` instead of `claude`; first run
  prints a QR code.
- **iOS app**: install Happy from the App Store, set the custom server URL to
  `https://happy.diemer.codes` (or the duckdns host) in the app's server
  settings **before** pairing, then scan the QR from the terminal.
- **Web**: `app.happy.engineering` pointed at the same custom server URL
  (E2E crypto means the hosted web client never sees plaintext either).

## Day-2 notes

- **Upgrading**: bump `HAPPY_REF` in `build.sh` and `image.tag` in
  `values.yaml` together, `./build.sh`, then `./upgrade.sh`. The CLI pin in
  the claude-workspace Dockerfile should track roughly the same release —
  wildly mismatched client/server versions are the first thing to suspect
  after an upgrade.
- **Master secret**: rotating `HANDY_MASTER_SECRET` invalidates existing
  device sessions — expect to re-pair every client.
- **Backup**: the PVC is ciphertext, but it's also the pairing state; losing
  it means re-pairing all devices (conversation history lives on the
  workspace PVC in `~/.claude`, not here).
- **readOnlyRootFilesystem**: on by default; the server writes only to /data
  and /tmp. If an upstream bump starts writing into its package dir, flip
  `security.readOnlyRootFilesystem: false` in values.local.yaml and note the
  version here.
