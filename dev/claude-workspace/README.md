# claude-workspace

Always-on Claude Code workspace on the cluster, replacing ephemeral SSH
sessions from the iOS terminal app. One pod, one `$HOME` on a PVC, three ways
in sharing that home:

- **`/term`** — [ttyd](https://github.com/tsl0922/ttyd) running
  `tmux new -A -s main`: the real Claude Code TUI. Every browser connection
  attaches to the *same* tmux session, so closing Safari — or iOS suspending
  it — leaves claude running; reopening `/term` lands back in the live
  session. Gated behind Authelia forward-auth (same pattern as
  `docs/stirling-pdf`) on `claude.zachd.duckdns.org` and, via the shared
  Cloudflare tunnel, `claude.diemer.codes`.
- **[Happy](https://github.com/slopus/happy) app** (iOS/Android/web) — run
  `happy` instead of `claude` in tmux and the phone gets full remote control
  of that same real-harness session (plan mode, permission prompts as push
  notifications), relayed E2E-encrypted through the self-hosted
  [`dev/happy-server`](../happy-server/) on `happy.zachd.duckdns.org`.
  This replaced CloudCLI (claudecodeui), which drove claude through headless
  mode and lost most of the harness (plan mode, hooks, skills); nothing
  serves `/` anymore.
- **`bakery.zachd.duckdns.org`** — [bakery](https://github.com/seemethere/bakery)
  (npm `pi-web-agent`): a second web coding-agent harness, on its own
  subdomain rather than a path (see [Bakery surface](#bakery-surface) for why).
  It runs its own agent against the same `~/code` repos, with state on the
  PVC, behind the same Authelia gate on its own duckdns host.

## What persists, what doesn't

The home PVC holds `~/.claude` (OAuth credentials + conversation history),
`~/.happy` (Happy pairing keys), `~/.ssh`, and repos under `~/code` — all
of it survives pod deletes and reschedules, on any node (no hostPath, no
nodeSelector; the subscription login lives on the PVC, which is what frees
this chart from claude-bridge's node pin).

The tmux server itself is in-memory: a **pod restart drops tmux sessions and
any live claude process**. Recovery is cheap — the conversation jsonl is on
the PVC, so open `/term` and `claude --resume` (or `claude -c` for the most
recent; both work under `happy` too).

## First install

```sh
# 1. Image (once per Dockerfile change; needs docker login ghcr.io)
./build.sh
# First push only: set ghcr.io/zdiemer/claude-workspace package → Public.

# 2. Install with ingress off
kubectl create namespace claude
helm install claude-workspace . -n claude -f values.yaml
kubectl -n claude get pods -w

# 3. Smoke test the ports
kubectl -n claude port-forward svc/claude-workspace 7681:7681 5173:5173 3141:3141
#   http://localhost:7681/term → tmux prompt echoes keystrokes
#   http://localhost:3141/healthz → bakery server "ok"
#   (bakery web on 5173 refuses a localhost Host header — allowedHosts is set
#    to the ingress host — so verify it end-to-end after the ingress is up)

# 4. Expose
cp values.local.yaml.example values.local.yaml   # ingress.enabled: true
./upgrade.sh
```

One-time Cloudflare step (per `infra/cloudflared` README): Zero Trust →
Networks → Tunnels → the shared tunnel → Public Hostnames → add
`claude.diemer.codes` → `https://traefik.kube-system.svc.cluster.local:443`
with **No TLS Verify ON**.

## First use (all from the phone, in-browser)

1. `https://claude.zachd.duckdns.org/term` → Authelia 2FA → tmux prompt.
2. Run `claude` → it prints an OAuth URL. Open it in a second tab, authorize
   with the claude.ai subscription account, paste the code back. Credentials
   land in `~/.claude/.credentials.json` on the PVC — this is the only login
   ever needed.
3. Git: `ssh-keygen -t ed25519 -C claude-workspace`, add
   `~/.ssh/id_ed25519.pub` to GitHub, then clone into `~/code/`. The key
   persists on the PVC. (NetworkPolicy allows egress 443 + 22 to public IPs
   only.)
4. Happy pairing (needs `dev/happy-server` deployed first): install the Happy
   app on the phone, set its custom server URL to `https://happy.diemer.codes`
   (or the duckdns host), then in tmux run `happy` — scan the QR it prints.
   Pairing keys land in `~/.happy` on the PVC. From then on, `happy` instead
   of `claude` = same session, controllable from the phone with push
   notifications for permission prompts.

## Cluster powers

Since image v2 the workspace is a full operations seat, not just a dev shell.
Three capabilities, three mechanisms:

- **Deploy anything with helm/kubectl** — the pod's ServiceAccount is bound to
  **cluster-admin** (`rbac.clusterAdmin`, see the ⚠️ in values.yaml). kubectl
  and helm pick up the in-cluster SA token automatically; there is no
  kubeconfig file anywhere.
- **Build + push images to GHCR** — `buildctl` against the in-cluster rootless
  buildkitd ([`infra/buildkit`](../../infra/buildkit/)); every per-chart
  `build.sh` falls back from docker to buildctl automatically. Push auth comes
  from `~/.docker/config.json` on the PVC (setup below).
- **Node maintenance (`scripts/k3s/`)** — a `tailscaled` container (userspace
  networking, unprivileged) joins the pod to the tailnet so
  `tailscale ssh root@<node>` works. The `tailscale` CLI in the image is a
  wrapper pointing at the shared socket in `/tmp/tailscale/`.

⚠️ Together these make Authelia forward-auth the ONLY thing between the
internet and cluster-admin + root-on-every-node. Never disable
`auth.forwardAuth` or expose the Service any other way while
`rbac.clusterAdmin` / `tailscale.enabled` are on.

### One-time setup (from `/term`)

1. **Tailscale**: `tailscale up --ssh=false --hostname=claude-workspace
   --accept-dns=false`, open the printed URL, authorize. Then in the Tailscale
   admin console: approve the node (if approval is on) **and make sure the ACL
   `ssh` rules allow this node as a source for `root@` the k3s nodes** — a
   healthy `tailscale status` with a failing `tailscale ssh` means ACLs, not
   the pod. State persists on the PVC.
2. **GHCR PAT** (classic PAT with `write:packages`; there is no docker CLI in
   the pod, so write the auth file directly):

   ```sh
   read -rs GHCR_PAT
   printf '{"auths":{"ghcr.io":{"auth":"%s"}}}\n' \
     "$(printf 'zdiemer:%s' "$GHCR_PAT" | base64 -w0)" > ~/.docker/config.json
   chmod 600 ~/.docker/config.json; unset GHCR_PAT
   ```
3. **Repo + local values**: clone this repo to `~/code/selfhosted`, then from
   the laptop run [`scripts/sync-local-values.sh`](../../scripts/sync-local-values.sh)
   to copy every gitignored `values.local.yaml` into the pod — without them,
   deploys from the pod fail on `required` values or silently render secrets
   empty. Re-run after any local-values change.
4. **Gotcha**: kubectl/helm use the in-cluster SA only while `~/.kube/config`
   does not exist. If one ever lands on the PVC it silently takes precedence
   and everything breaks confusingly — `rm ~/.kube/config` is the fix.
5. **Other repos' secrets** (sync-local-values.sh only covers this repo):
   - gamedex (standalone clone): copy its values from the laptop —
     `tar czf - -C ~/Code/gamedex values.local.yaml | kubectl -n claude exec -i deploy/claude-workspace -c term -- tar xzf - -C /home/node/code/gamedex`
   - talaria keeps secrets sops-encrypted in-git; the image ships `sops`
     (age support built in), but the age private key must be copied to the
     pod at `~/.config/sops/age/keys.txt` (chmod 700 dir / 600 file) —
     sops' default search path, so it works in every shell and script with
     no env var (a `SOPS_AGE_KEY_FILE` export in `~/.bashrc` only reaches
     interactive shells). ⚠️ That key decrypts every talaria secret —
     copying it makes Authelia the only thing guarding them.

### Self-upgrade

`./upgrade.sh` from inside the pod works — helm applies server-side — but the
`Recreate` strategy then kills this very pod, so **your session dies at the
"Waiting for rollout" line**. That's expected: reconnect to `/term`,
`claude --resume`, then check `helm status claude-workspace -n claude` and
`kubectl -n claude rollout status deploy/claude-workspace`.

If helm was killed in the narrow window before it finished bookkeeping, the
release is stuck `pending-upgrade` and the next upgrade fails with "another
operation is in progress". Fix: `helm rollback claude-workspace -n claude`
and re-run, or delete the stuck revision secret
`sh.helm.release.v1.claude-workspace.v<N>` in the claude namespace.

Image-only refresh (tag unchanged): `kubectl -n claude rollout restart
deploy/claude-workspace` — same session-death caveat.

## Bakery surface

[Bakery](https://github.com/seemethere/bakery) is a second web coding-agent
harness, vendored into the image (`image.tag` v3+) and served at
`bakery.zachd.duckdns.org`. It's two processes — a Bun API/WebSocket **server**
(`PI_WEB_PORT` 3141) and a **Vite web** UI (5173) — running as two containers
that share the same `$HOME` PVC as everything else.

**Why a subdomain, not `/bakery`.** Bakery's web client has no base-path
support (Vite `base` is unset) and talks to its server at an *absolute* origin
it computes from `window.location` (defaulting to `:3141`). The one override,
`VITE_PI_WEB_API_BASE`, is baked at serve time. So bakery can only live at the
root of a host, and only *one* host per build — which is why it's a subdomain
and, unlike `/` and `/term`, is **not** published on the `diemer.codes`
Cloudflare tunnel. A path mount or a second domain would each require patching
and rebuilding bakery; the subdomain avoids that fork entirely.

**How it's wired.** `bakery.apiBase` is baked into the web client as
`https://bakery.zachd.duckdns.org`, so REST + WebSocket calls are *same-origin*.
The ingress path-splits that one host: `/api` (including the
`/api/sessions/<id>/ws` WebSocket) → the server, everything else → Vite. Because
it's same-origin under `zachd.duckdns.org`, Authelia's SSO cookie
(`auth/authelia` `sessionDomain`) and the server's CORS/WS origin check both
line up with no extra config, and `default_policy: two_factor` gates it with no
new access-control rule. The host rides the existing `*.zachd.duckdns.org`
wildcard DNS + cert — nothing to add in DuckDNS or Cloudflare.

**No bakery token by default.** `PI_WEB_AUTH_TOKEN` is left unset: Authelia
forward-auth is the only gate, exactly like `/` and `/term`. Set
`bakery.authToken` (values.local.yaml) only for defense-in-depth — it's passed
to the server *and* baked into the client (`VITE_PI_WEB_AUTH_TOKEN`), so the
browser authenticates with no Settings-dialog entry.

**First use (one-time).** Bakery runs its own coding agent, which keeps
credentials in `~/.pi` on the PVC (separate from `~/.claude`). After the pod is
up, open `https://bakery.zachd.duckdns.org` through Authelia and complete
bakery's in-app agent login; or run its CLI from `/term`
(`cd /opt/bakery && bun run bakery` — the login persists in `~/.pi`). Sessions,
artifacts, and metadata live under `bakery.dataDir`
(`~/.pi-web-agent`), also on the PVC.

**Caveats.**
- It runs the Vite **dev** server as the permanent surface (HMR off) — that's
  what upstream ships (`bun run dev:lan`); there is no production build path.
- The two bakery containers run with `readOnlyRootFilesystem: false`
  (`bakery.readOnlyRootFilesystem`) because bun/vite write transpile caches into
  `/opt/bakery`. The other surfaces keep the read-only rootfs.
- Upstream has no published image and no release tags, so the Dockerfile pins
  `BAKERY_REF` to the exact commit the current image was built from. Bump it
  deliberately (then `./build.sh`), same as the claude CLI.
- Turn the whole surface off with `bakery.enabled: false` (drops both
  containers, the two service ports, the ingress host, and the netpol rule).

## Day-2 notes

- **Upgrading claude/happy**: bump the pin in `Dockerfile`, `./build.sh`,
  then `kubectl -n claude rollout restart deploy/claude-workspace` (static
  tag + `pullPolicy: Always`). Schedule around live claude sessions — a
  restart kills tmux. Keep the `happy` pin roughly in step with
  happy-server's `HAPPY_REF` (see that chart's README).
- **Upgrading bakery**: bump `BAKERY_REF` in the `Dockerfile` (and bump
  `image.tag`, since it's a static tag), `./build.sh`, then roll the pod.
  Bakery deps are `bun install`ed at build time, so a rebuild is required for
  any bakery change — a plain rollout restart re-pulls the same tag and won't
  pick up a new ref unless the tag also moved.
- **readOnlyRootFilesystem**: on by default; claude, happy, and tmux all
  write under `$HOME`/`/tmp` only (the bakery containers opt out via
  `bakery.readOnlyRootFilesystem` — bun/vite write caches into /opt/bakery).
  If a tool upgrade starts writing into its npm package dir, flip
  `security.readOnlyRootFilesystem: false` in
  values.local.yaml and note the version here.
- **happy daemon**: `happy daemon start` (from `/term` or the phone) lets the
  Happy app spawn NEW sessions in any `~/code` directory, not just attach to
  ones started in tmux. Like tmux, the daemon dies on pod restart — restart
  it with the same command; pairing keys on the PVC survive.
- The chart holds no secrets at all, so `values.local.yaml` is just the
  ingress toggle. (The relay's master secret lives in `dev/happy-server`'s
  values.local.yaml, not here.)
