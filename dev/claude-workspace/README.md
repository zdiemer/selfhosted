# claude-workspace

Always-on Claude Code workspace on the cluster, replacing ephemeral SSH
sessions from the iOS terminal app. One pod, one `$HOME` on a PVC, two web
surfaces sharing that home:

- **`/`** — [CloudCLI (claudecodeui)](https://github.com/siteboon/claudecodeui):
  mobile-friendly session browser + chat UI. Auto-discovers everything in
  `~/.claude`, so it sees (and can resume) sessions started in the terminal.
- **`/term`** — [ttyd](https://github.com/tsl0922/ttyd) running
  `tmux new -A -s main`: the real Claude Code TUI. Every browser connection
  attaches to the *same* tmux session, so closing Safari — or iOS suspending
  it — leaves claude running; reopening `/term` lands back in the live
  session.

Both are gated behind Authelia forward-auth (same pattern as
`docs/stirling-pdf`) on `claude.zachd.duckdns.org` and, via the shared
Cloudflare tunnel, `claude.diemer.codes`.

## What persists, what doesn't

The home PVC holds `~/.claude` (OAuth credentials + conversation history),
`~/.cloudcli` (CloudCLI's sqlite), `~/.ssh`, and repos under `~/code` — all
of it survives pod deletes and reschedules, on any node (no hostPath, no
nodeSelector; the subscription login lives on the PVC, which is what frees
this chart from claude-bridge's node pin).

The tmux server itself is in-memory: a **pod restart drops tmux sessions and
any live claude process**. Recovery is cheap — the conversation jsonl is on
the PVC, so open `/term` and `claude --resume` (or `claude -c` for the most
recent). CloudCLI sessions are those same jsonl files.

## First install

```sh
# 1. Image (once per Dockerfile change; needs docker login ghcr.io)
./build.sh
# First push only: set ghcr.io/zdiemer/claude-workspace package → Public.

# 2. Install with ingress off
kubectl create namespace claude
helm install claude-workspace . -n claude -f values.yaml
kubectl -n claude get pods -w

# 3. Smoke test both ports
kubectl -n claude port-forward svc/claude-workspace 3001:3001 7681:7681
#   http://localhost:3001   → CloudCLI loads
#   http://localhost:7681/term → tmux prompt echoes keystrokes

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
4. Open `/` and complete CloudCLI's first-run (it keeps its own local account
   in `~/.cloudcli/auth.db`; redundant behind Authelia but harmless —
   register once, the browser remembers).

## Day-2 notes

- **Upgrading claude/CloudCLI**: bump the pin in `Dockerfile`, `./build.sh`,
  then `kubectl -n claude rollout restart deploy/claude-workspace` (static
  tag + `pullPolicy: Always`). Schedule around live claude sessions — a
  restart kills tmux.
- **readOnlyRootFilesystem**: on by default; CloudCLI's writes are pointed
  under `$HOME` via `DATABASE_PATH`. If a CloudCLI upgrade starts writing
  into its package dir, flip `security.readOnlyRootFilesystem: false` in
  values.local.yaml and note the version here.
- The chart holds no secrets at all, so `values.local.yaml` is just the
  ingress toggle.
