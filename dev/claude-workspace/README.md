# claude-workspace

Always-on Claude Code workspace on the cluster, replacing ephemeral SSH
sessions from the iOS terminal app. One pod, one `$HOME` on a PVC, four ways
in sharing that home:

- **`/term`** — [ttyd](https://github.com/tsl0922/ttyd) running
  `tmux new -A -s main`: the real Claude Code TUI. Every browser connection
  attaches to the *same* tmux session, so closing Safari — or iOS suspending
  it — leaves claude running; reopening `/term` lands back in the live
  session. Reachable on `claude.zachd.duckdns.org` — **tailnet only**, and
  additionally gated behind Authelia forward-auth (same pattern as
  `docs/stirling-pdf`). It used to be published on the Cloudflare tunnel at
  `claude.diemer.codes`; that put a web terminal on a cluster-admin pod on the
  open internet with a single boolean in the way. Two independent gates is the
  right number for a root shell. See `values.yaml` `ingress.cloudflareHosts`.
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
- **Signal / WhatsApp** — text the bot and it drives headless `claude -p`
  in this same `$HOME`; permission prompts round-trip as "reply 1/2/3"
  messages. The low-bandwidth surface: airline free-messaging wifi and
  slow data pass Signal/WhatsApp text when nothing else moves. See
  [Messaging surface](#messaging-surface).

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

**No Cloudflare step — this host is deliberately not on the tunnel.** Reaching
it requires being on the tailnet first (`infra/duckdns` is in `mode: tailnet`,
so `claude.zachd.duckdns.org` resolves to an unroutable 100.x address), then
passing Authelia. If a `claude.diemer.codes` Public Hostname still exists on
the tunnel from before the delisting, delete it: Traefik no longer has a
matching router, so it 404s rather than failing, and nothing tells you it is
still configured.

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
   app on the phone, set its custom server URL to
   `https://happy.zachd.duckdns.org` (tailnet only — the phone needs Tailscale
   up), then in tmux run `happy`. **Do not scan the QR**: the pod now reaches
   the relay over the cluster-local Service, so a QR minted here carries an
   in-cluster address the phone cannot resolve. Set the URL by hand.
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
- **Create repos + open PRs with `gh`** — the GitHub CLI is baked into the
  image (since v5). A one-time `gh auth login` (device flow) persists under
  `~/.config/gh` on the PVC, so Claude can scaffold and push new app repos
  (e.g. `finance/money`) without leaving the workspace. This is a **separate**
  credential from the GHCR PAT below (that one is packages-only): `gh` needs a
  token with `repo` + `read:org` scope.
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
3. **GitHub CLI** (`gh`, for creating repos / pushing from the pod): run
   `gh auth login` (choose GitHub.com → HTTPS → login with a web browser) and
   authorize the device code. Use a token/login with `repo` + `read:org` scope
   — the GHCR PAT above is packages-only and `gh` will reject it. Auth persists
   on the PVC at `~/.config/gh`.
4. **Repo**: clone this repo to `~/code/selfhosted`. The secrets it needs are
   no longer a manual step — see below.
5. **Gotcha**: kubectl/helm use the in-cluster SA only while `~/.kube/config`
   does not exist. If one ever lands on the PVC it silently takes precedence
   and everything breaks confusingly — `rm ~/.kube/config` is the fix.

### Secrets

Two paths, deliberately. The pod needs every chart's `values.local.yaml` or
deploys from here fail on `required` values — or worse, silently render a
secret empty.

**The relay is the bootstrap path.** `secret/selfhosted-secrets` (ns `claude`)
is a bundle the laptop publishes with
[`scripts/secrets.sh`](../../scripts/secrets.sh) `publish`. It is mounted into
the `init-home` initContainer, which unpacks it on **every pod start**, so a
restart is self-sufficient and needs nobody — this used to be a
`secrets.sh pull --from-cluster` you had to remember, and a pod that came back
without it looked fine right up until a deploy. `publish` still applies it
immediately when the pod is already running.

It grants the pod nothing new: this SA is cluster-admin (`templates/rbac.yaml`)
and can already read every Secret in the cluster, including the rendered chart
Secrets carrying these same values. It needs no credentials, which is exactly
why it stays even though the pod can now reach 1Password.

**1Password is the return path**, and it is optional
(`secrets.onePassword.enabled`, credentials in `values.local.yaml`). Without it
the relay is one-directional: a secret edited *here* sits on the PVC until the
laptop publishes over the top of it. With it, `scripts/secrets.sh` `pull`,
`push` and `sync` all work here —
[`scripts/op-session.sh`](../../scripts/op-session.sh) signs in unattended from
`~/.config/selfhosted/op-password`, adding the account on first use if the PVC
has never seen it. Turned on 2026-08-12; verified by pushing a change from the
pod and watching the laptop see it as `DRIFT (vault newer)`.

The one thing that does *not* work here is the NAS archive `push`/`sync` chain
onto a write. This image ships neither `age` nor `smbclient`, and the netpol
excludes RFC1918 anyway, so the pod has no route to the NAS by design. That
used to end a pod push with a bare `FAIL: age required` printed *after* the
vault write had already succeeded; `secrets.sh` now skips the archive where one
cannot be taken and says so. The vault holds the change either way, and the
laptop's next `push`/`sync`/weekly timer sweeps it into an archive.

The honest accounting, since this reverses an earlier decision recorded here:
the account password is now on the PVC. Against a cluster-admin SA that could
already read the rendered Secrets, what is genuinely new is reach into vault
items no chart has deployed yet. Everything stays behind Authelia. The earlier
objection assumed Service Accounts (a Teams/Business feature this account does
not have) were the only alternative to the password; the pty sign-in in
`scripts/lib/op-signin-pty.py` is the third option that did not exist then.

**Other repos:**
- gamedex / money / smitele-bot / sms-relay / whatnowgg (standalone clones): in
  scope by default now — `publish` bundles them, and they land beside each clone
  under `~/code` if the clone exists.
- talaria keeps secrets sops-encrypted in-git; the image ships `sops` (age
  support built in). Its age private key belongs in the vault, materialized to
  `~/.config/sops/age/keys.txt` (chmod 700 dir / 600 file) — sops' default
  search path, so it works in every shell and script with no env var (a
  `SOPS_AGE_KEY_FILE` export in `~/.bashrc` only reaches interactive shells).
  ⚠️ That key decrypts every talaria secret.

### Self-upgrade

`./upgrade.sh` from inside the pod works — helm applies server-side — but the
`Recreate` strategy then kills this very pod, so **your session dies at the
"Waiting for rollout" line**. That's expected: reconnect to `/term`,
`claude --resume`, then check `helm status claude-workspace -n claude` and
`kubectl -n claude rollout status deploy/claude-workspace`.

Helm used to be collateral damage in that: it writes the new revision as
`pending-upgrade`, applies, and only then marks it `deployed`, so the kubelet's
SIGTERM landing in between left a permanently pending revision and every later
upgrade failed with "another operation is in progress". `upgrade.sh` now
handles both ends of it:

- It **ignores SIGTERM around the helm call**. An ignored signal disposition
  survives `exec`, so helm inherits it and finishes inside the pod's
  termination grace period — 30s against an operation that needs
  milliseconds. The pod still goes down; it no longer takes the release record
  with it.
- On startup it **repairs an already-stuck release**: the pending revision is
  dropped and the previous one restored to `deployed`, then the normal upgrade
  re-applies and converges. Deliberately "forget the dead revision" rather than
  "assume it worked" — a killed run may or may not have applied its manifests,
  and re-applying is correct either way.

From the messaging surface the session itself survives, because the gateway
stores `session_id` and resumes it on the new pod. A `/term` tmux session does
not; that one needs `claude --resume`.

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

## Messaging surface

Signal (primary) and WhatsApp (optional) drive headless `claude -p` runs in
the pod's `$HOME`. Why headless is right *here* when it was wrong for
CloudCLI: this is the degraded-network fallback, not the primary surface —
`/term` and Happy keep the full harness, and the one interactive feature that
matters on a phone (permission prompts) is preserved by relaying it over chat.

```
you (Signal app) ──Signal servers──▶ signal-cli daemon ──unix socket──▶ ┐
you (WhatsApp)  ──WA servers──────▶ Baileys (in-process) ─────────────▶ messaging-gateway
                                                                        │ spawns per message
                                                          claude -p --resume <session>
                                                                        │ --permission-prompt-tool
                                                          approve-mcp ──unix socket──▶ gateway ──▶ "reply 1/2/3"
```

Everything is outbound (Baileys websocket, signal-cli to Signal's servers):
no new ingress, no netpol change, nothing new behind or around Authelia. The
**sender allowlist is the only auth** — anyone on it holds a shell on the
cluster, so it is your numbers only, set in values.local.yaml (vault item
`dev-claude-workspace`).

The gateway app lives in-repo at `gateway/` and is baked into the image at
`/opt/messaging-gateway` (small, single-consumer — same documented exception
as `web/apartment-watch` and `minecraft/claude-bridge`).

### One-time pairing

The spare number serves both networks. Register **before** flipping
`messaging.enabled` (the signal-cli daemon crash-loops accountless, and
registration needs the storage lock the daemon would hold):

```sh
# Signal — from /term (same image, same $HOME the daemon will use):
signal-cli -a +1<botnumber> register            # or `register --voice` for a voice call
signal-cli -a +1<botnumber> verify <sms-code>
# CAPTCHA required? Follow the URL signal-cli prints, then re-run register
# with the captcha token it produces.

# Then: add the messaging block to the vault item (see
# values.local.yaml.example), `scripts/secrets.sh publish` (or `sync` from the
# pod), flip messaging.enabled: true, ./upgrade.sh.

# WhatsApp (optional, after messaging.whatsapp.enabled: true) — pair by QR:
kubectl -n claude logs -f deploy/claude-workspace -c messaging-gateway
# scan the QR from the bot number's phone: WhatsApp → Linked Devices
```

Both registrations persist on the PVC (`~/.local/share/signal-cli`,
`~/.config/selfhosted/messaging-gateway/wa-auth/`) — pod restarts reconnect
without re-pairing.

### Chat commands

`!new` / `!clear` fresh session · `!resume [id]` continue the newest session for
the current cwd (this is the cross-surface handoff — start in tmux, `!resume`
from the plane) · `!cwd <repo|path>` switch repo (bare names resolve under
`~/code/`) · `!auto <30m|2h>` per-chat auto mode with an expiry (`--permission-mode
bypassPermissions` — no prompts at all). Prefer the duration: bare `!auto on`
still works and is still open-ended, but what it grants is unprompted root in
a cluster-admin pod, and a standing grant outlives the reason it was given.
Expiry is checked at every use, not swept by a timer, and the lapse is
announced rather than just quietly prompting again ·
`!more` the rest of a reply that was cut short (the four-chunk cap keeps a
runaway answer off a metered link, but the remainder is now kept rather than
discarded, and paging chains) · `!use <name>` / `!sessions` several named
threads in one chat, since there is only one chat with the bot and switching
context used to destroy the old thread · `!plan [on|off]` per-chat plan mode (`--permission-mode plan` — claude
researches and proposes, never edits; bare `!plan` turns it on, and it clears
`!auto`, which is the opposite instruction) ·
`!model opus|sonnet|haiku|fable|<id>|default` · `!effort
low|medium|high|xhigh|max|default` · `!bash <cmd>` shell command in the chat's
cwd, no model in the loop · `!usage [days]` token totals ·
`!stop` SIGTERM the running claude (and any `!bash`) · `!status` · `!help`.

`!bash` is the escape hatch for cheap questions — `kubectl get pods`,
`git log --oneline -5` — where a whole claude run plus a permission round-trip
costs four messages and a minute to answer one line. Owner-only, 1:1 only (a
group reads every byte, and its members are not on the personal allowlist), runs
under `bash -lc` in the chat's `!cwd`, bounded by `messaging.bash.timeoutSeconds`
and `maxOutputChars`. It is not a widening of the trust boundary — the same chat
can already `!auto on` — so it is on by default; `messaging.bash.enabled: false`
makes the model the only path to the shell.

`!usage [days]` (default `messaging.usageDays`) sums the `usage` block of every
assistant message in `~/.claude/projects/**.jsonl`: the rolling 5h window (the
subscription's limit block), today, and the last N days split by model. It reads
the PVC, not the network — there is no `claude usage` subcommand, no supported
endpoint for the subscription counters, and the interactive `/usage` panel is a
TUI this headless surface can't reach. So it counts **every** claude run in this
pod (chat, tmux, Happy) and nothing you ran anywhere else.

Runs default to **Opus 5 at medium effort** (`messaging.model` /
`messaging.effort`); `!model` and `!effort` override per chat and persist in the
state file. `!clear` is `!new` under a name that reads right in a chat — it
drops the session pointer so the next message starts cold. The transcript itself
stays on the PVC under `~/.claude`, so `!resume <id>` can still reach it.

Anything else is sent to claude. Replies are prefixed
`[repo · session · auto|plan?]` in a 1:1, chunked to ~1.9k (Signal) / ~2.9k
(WhatsApp) chars, max 4 chunks then truncated — bandwidth is the point.

While a run is going, three things say so, in increasing order of detail:

- Messages the gateway accepts are marked **read** on the sender's side, so a
  read receipt means "the bot has this", not "a packet arrived".
- Your message is **reacted to**: 👀 while it is the one being worked on, 🕒
  while it waits behind another run in the same chat, then ✅ or ❌.
  `messaging.reactions.*` turns this off or changes the emoji — it is the one
  part of this that shows up in a group whether the room asked or not.
- Reactions go the other way too: **answer a permission prompt by reacting to
  it** — 👍 allow, 👎 deny, 💯 allow all. Typing 1/2/3 is ambiguous once two
  prompts are open; a reaction names the message it answers. It needs the same
  credential as a `!` command, must be on the prompt itself, and an emoji
  outside that vocabulary does nothing — a reaction can never start a run or
  reach claude.
- **Attachments** work both ways (`messaging.attachments`). Photograph an
  error on a screen and send it: the media is saved under
  `~/.cache/messaging-gateway/inbox` (pruned to a week at startup — a cache,
  not an archive) and the run is told the paths, so claude reads it like any
  other file. Coming back, claude puts `[[send:/abs/path]]` alone on a line and
  the gateway strips the marker and delivers the file — a screenshot, a
  rendered chart, a diff too long to read as text. Signal takes it as `send --attachment`, with one wrinkle: the GraalVM native
  signal-cli build ships no ImageIO/AWT, and signal-cli reaches for them only
  when an attachment is typed as an image, to read its dimensions — so sending
  a photo by path fails with `Can't load library: awt`. The gateway retries
  those as an untyped data URI, which is identical bytes and still previews
  inline on the phone, just without dimensions attached. WhatsApp renders
  images inline and everything else as a document. Honoured in 1:1 chats only: the marker is parsed from text, and
  text is the one thing a room can steer.
- In a 1:1, a **status message** is sent when the run starts and edited in
  place as tool calls land (`Bash: helm upgrade …`, `Read src/router.ts`,
  `waiting for your approval`), collapsing to `✓ done · 14 tools · 1m42s` when
  the answer goes out. It is deliberately a separate message from the answer:
  the answer is chunked across up to four messages, so it has no single
  identity to edit, and WhatsApp refuses edits more than ~15 minutes after the
  original — a long run would lose the thread partway. Edits are throttled to
  one every `messaging.progress.editSeconds` for the same reason `!bash` output
  is capped: this is a metered connection and, on WhatsApp, an unofficial
  client. Groups get no status message — the room did not ask to watch, and a
  group run only has `WebFetch`/`WebSearch` to show.

The run itself is driven off `claude -p --output-format stream-json`, which is
also where the session id now comes from: it is written to the state file the
moment the run starts rather than when it finishes, which is what makes an
interrupted run recoverable (below).

Every run carries an appended system prompt (`--append-system-prompt`, so Claude
Code's own prompt still teaches it its tools) explaining the harness: the far
end is a phone, Markdown is not rendered so asterisks and backticks arrive
literally, and replies should lead with the answer and stay short. Override with
`messaging.systemPrompt`.

The bot's Signal profile is set with `signal-cli updateProfile --given-name …`
(or the daemon's `updateProfile` JSON-RPC while it holds the account lock).
Without it the account shows as **Unknown** in everyone's client.

### Restarts

This pod can redeploy itself (`build.sh && ./upgrade.sh` from `!bash` or an
`!auto on` chat), and `strategy: Recreate` means the old pod is gone before the
new image is pulled. Any run in flight simply stops. So the gateway says so at
both ends:

- On `SIGTERM`, every chat with a run going gets `⏳ gateway restarting — back
  shortly` and its status message is overwritten with the same. Strictly
  time-boxed (`messaging.restartNotice.shutdownGraceSeconds`) — the pod's
  termination grace period is the real ceiling and being killed mid-send is a
  normal outcome here, not something to handle.
- On boot, once each transport is actually connected, chats active in the last
  `restartNotice.withinMinutes` (default 60) get `✓ gateway back up`. A chat
  whose run was cut off is told that instead, and can say "continue" to pick it
  up: the session id was persisted at run start, so the partial transcript on
  the PVC is still reachable through `--resume`. Suppressed entirely if the
  last notice was under `dedupeMinutes` ago, so a crash-looping pod doesn't
  become a message per restart. Groups are never notified.

### Group chats

Off by default (`messaging.groups.enabled`), and the default is a statement:
**the allowlist decides who may drive this shell, not who reads the output.**
In a group, every member sees whatever claude prints. So a group run is a
different thing from a 1:1 run:

- **The room is the credential.** A group is authorised by ID
  (`signal.allowedGroups` / `whatsapp.allowedGroups`), and then *every member*
  may address the bot — the personal `allowedSenders` list is not consulted.
  This is the point: a shared room is useful only if the people in it can use
  it. Membership of a listed room does **not** grant a 1:1 conversation; a DM
  from someone not on `allowedSenders` is still dropped.
  With no groups listed the feature does nothing — it fails closed.
- **`!` commands stay owner-only, everywhere.** A room grant is permission to
  ask the bot things, not to repoint its cwd, switch its model, or wipe its
  session — which in a shared room would be everyone's settings changed by one
  person. Non-owner commands are ignored and logged.
- **Un-allowlisted rooms are dropped, not buffered.** Anyone can add this number
  to a group; holding context for rooms that will never get an answer is memory
  spent on strangers' conversations.

- **Mention-gated** (`groups.requireMention`) — the bot answers only when
  actually tagged: a real @-mention, or a reply to one of its own messages.
  Structured address only. Matching the bot's *name* in the message text was
  tried and removed — "claude" comes up in ordinary group conversation, and the
  bot answering that is the exact failure this gate exists to prevent.
  ⚠️ Signal mentions carry the sender's **ACI**, not the number, and signal-cli
  does not expose the account's own ACI over JSON-RPC (`listAccounts` returns
  the number only). The gateway reads it from
  `~/.local/share/signal-cli/data/accounts.json` at startup — if that lookup
  fails it logs `could not resolve own ACI` and **no @-mention will ever match**.
- **A hard tool ceiling** (`groups.allowedTools`, default `WebFetch
  WebSearch`) — no filesystem, no shell, no cluster. Anything outside the list
  is **denied outright, not prompted**: the room would see the prompt, only one
  member could answer it, and a "3 = allow all" from that member would quietly
  widen what everyone else can reach for the rest of the session.
- **The whole room is context** — every message is buffered and handed to the
  next run, not just the allowlisted sender's. "What did we decide?" is
  unanswerable otherwise. In memory only, newest `groups.contextLines`.
- **Rate-limited** (`groups.rateLimit` per `groups.rateWindowMinutes`) — this
  is a personal Claude subscription, and a group is the one surface where other
  people can spend it.
- **No banner** — group replies skip the `[repo · session · auto?]` prefix.
  That's workspace bookkeeping and means nothing to the rest of the room.

Auto mode is never available in a group, regardless of `!auto`.

### Permission relay contract

Without auto mode, claude runs with a read-only `--allowedTools` baseline
(`messaging.allowedTools`); any other tool triggers
`--permission-prompt-tool mcp__gw__approve` → the `gateway/src/approve-mcp.ts`
stdio MCP server → the gateway's approvals socket → a chat message. Reply
`1` allow once, `2` deny, `3` allow that tool for the rest of the session
(in-memory; resets with the pod, which is the safe direction). No reply for
`messaging.approvalTimeoutSeconds` (default 5m) denies.

**AskUserQuestion and ExitPlanMode arrive on this same socket but are not
permission requests** — they're claude talking to you, and the generic prompt
rendered them as truncated JSON with no way to answer:

- A question is sent as its options, numbered. Reply with a number (or `1,3`
  when it's multi-select), or type your own words for the "Other" branch. The
  answer goes back as the tool's `answers` field, keyed by question text; that
  field is the whole mechanism, and a bare "allow" without it is why an
  approved question used to come back as "user did not answer". Multiple
  questions are asked one at a time.
- A plan is sent as the plan text itself, de-marked for a surface that renders
  no markdown. `1` approves — and **clears `!plan` for the chat**, since
  approving a plan means "go do it" and leaving plan mode on sends the next
  message straight back into planning. Anything else keeps planning, with what
  you typed handed to claude as the reason.
- Neither offers `3 allow all`, and neither is eligible for it.

Two claude-CLI dependencies to re-verify on every image bump (the image
tracks `claude-code@latest` at build time):

- `--permission-prompt-tool` is accepted but **hidden from `--help`** as of
  2.1.224. If it's ever dropped, the fallback is a `PreToolUse` hook (via
  `--settings`) pointed at the same approvals socket.
- `--permission-mode bypassPermissions` spelling.

### Messaging gotchas

- **Baileys is pinned EXACTLY, never with a caret.** Upstream published
  `6.17.16` on 2025-03-04, *after* the 6.7.x line, so semver ranks it above the
  genuinely newer `6.7.24` (2026-07-29) and `^6.7.x` resolves **backwards** to a
  year-old client. WhatsApp refuses that client's stale protocol version at the
  handshake with a `405`, before pairing ever starts — the symptom is a
  reconnect loop that never prints a QR. The gateway also calls
  `fetchLatestBaileysVersion()` at connect, because the version baked into the
  library goes stale between releases and is refused the same way. Bump the pin
  deliberately; never restore the caret.
- **Signal identifies senders by ACI (UUID), not phone number.** Phone-number
  privacy has been the default since 2024, so `sourceNumber` is absent for most
  senders and the allowlist matches on whatever identifiers the envelope
  carries. Put both the E.164 number and the UUID in `allowedSenders`. Find the
  UUID in the daemon log: `kubectl -n claude logs deploy/claude-workspace -c
  signal-cli` prints `Envelope from: "Name" <uuid>`. The ACI is stable — it
  survives number changes, re-links, and reinstalls; only deleting the account
  and re-registering mints a new one.
- **Baileys ban risk**: unofficial WhatsApp client; Meta bans
  automation-smelling numbers. Dedicated number, low volume, default-off. A
  ban costs the number, not the account you actually use.
- **Never smoke-test the gateway with the default runtime dir.** `main.ts`
  unlinks `approve.sock` before binding it, so a second copy started in `/term`
  (or by a chat-driven claude run) deletes the socket the *live* gateway is
  listening on. The running server keeps the unlinked inode and notices nothing;
  every subsequent permission prompt dies with `gateway unreachable: connect
  ENOENT`, which strands the in-flight run read-only. It now refuses to start
  when the socket answers — pass `GW_RUNTIME_DIR=/tmp/gwtest GW_STATE_DIR=…
  GW_STDIN=true bun run src/main.ts` to test against a scratch path. The same
  applies to `bun test`, which binds a real socket to exercise the approval
  round trip; `tests/setup.ts` (preloaded via `bunfig.toml`) redirects both
  dirs to a temp path, so run the suite from `gateway/` with
  `bun test` / `bun run typecheck` and don't bypass that preload.
- **One surface per session at a time**: chat `!resume` of a session that
  tmux/Happy is actively driving interleaves jsonl writes. Hand off, don't
  share.
- **signal-cli staleness**: Signal's servers reject old clients — when
  receiving stops working after months of neglect, bump
  `SIGNAL_CLI_VERSION` in the Dockerfile and rebuild (pinned like ttyd,
  but it can't rot indefinitely).
- Turn the whole surface off with `messaging.enabled: false` (drops both
  containers and the Secret; pairing state stays on the PVC).

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
- **happy daemon**: the daemon is what lets the Happy app spawn NEW sessions in
  any `~/code` directory, not just attach to ones started in tmux. It now runs
  as its own always-on `happy-daemon` container (`happy.daemon.enabled`,
  `happy daemon start-sync`), so it comes back with the pod — no manual
  `happy daemon start` after a restart. Notes:
  - Phone-spawned sessions are **children of the `happy-daemon` container**, not
    the `term` one; `kubectl -n claude logs deploy/claude-workspace -c
    happy-daemon` and the daemon's own logs under `~/.happy/logs/` are where
    they surface (start-sync writes little to stdout).
  - It shares `~/.happy` with the interactive `happy` in `/term`; the singleton
    lock (`~/.happy/daemon.state.json.lock`) keeps them from fighting, and
    interactive `happy` defers to the running daemon. This holds only while
    every container runs the same happy version (they share one image) — a
    mismatch makes them kill and replace each other.
  - **Restarts** (`happy.restartNotice.enabled`): a deploy kills phone-spawned
    sessions, and they used to just vanish from the app. A `preStop` hook now
    pushes "restarting for a deploy" via `happy notify`, and the next boot
    pushes that it's back with a `happy resume <id>` for the session that
    dropped. It only fires when a session was genuinely live — the hook filters
    `sessions.json` on `lifecycleState: running` **and** `host == $HOSTNAME`,
    because every record left by a dead pod still says `running` (the process
    never got to update it), so without the host check a quiet restart would
    report twenty interrupted sessions. Liveness is recorded in a marker at
    shutdown rather than inferred at boot, which also means SIGKILL, an
    eviction or a node failure skips preStop and you get no notice — best
    effort, deliberately. The script is a ConfigMap mounted at
    `/etc/claude-workspace`, so it changes with a `helm upgrade` rather than an
    image rebuild.
  - Turn it off with `happy.daemon.enabled: false` (drops the container); the
    old manual `happy daemon start` from `/term` still works if you do.
- The chart holds no secrets at all, so `values.local.yaml` is just the
  ingress toggle. (The relay's master secret lives in `dev/happy-server`'s
  values.local.yaml, not here.)
