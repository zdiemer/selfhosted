# headlamp — Kubernetes dashboard

Stock upstream [Headlamp](https://headlamp.dev/) chart, so this folder is values
+ scripts only — no `Chart.yaml`, same shape as [`minecraft/`](../../minecraft/).

Reach it on the LAN at **`http://<any-node-ip>:30100`** (NodePort). Get a login
token with `./token.sh`.

It lived in the sibling talaria project, which had nothing to do with it —
Headlamp reads the entire cluster, not one app.

## ⚠️ This is a cluster-admin dashboard

`clusterRoleBinding.create: true` binds Headlamp's ServiceAccount to
**cluster-admin**. That's what lets the UI see and edit everything, and it means:

- **A token from `./token.sh` is a full cluster credential.** Treat it like a root
  password. It's short-lived (~1h) but total while valid.
- **NodePort 30100 is open on every node IP on the LAN**, with no ingress, no
  Authelia, no tunnel. Headlamp demands a token before doing anything, so what's
  exposed is the login page rather than the cluster — but there's no second lock.
- **It is deliberately not published externally.** No ingress, no
  `cloudflareHosts`. A cluster-admin dashboard has no business on the public
  internet, and [`infra/cluster-status`](../cluster-status/) already covers the
  read-only "how is the cluster doing" case for anyone outside.

## Deploy

```bash
./upgrade.sh    # adds the helm repo, ensures the namespace, upgrade --install
./token.sh      # login token
```

`upgrade.sh` is `--install`, so it both bootstraps and upgrades. talaria's version
had separate `install`/`upgrade`/`uninstall` subcommands for what is one
idempotent operation; the uninstall path is `helm uninstall headlamp -n headlamp`
if you ever want it.

## Getting the token onto another device

```bash
./token-server.sh          # prints a URL; open it on the phone, copy the token
```

This serves a **cluster-admin token over plaintext HTTP with no authentication**.
Anyone who can reach this host on that port while it's running gets it. Two
changes from talaria's original, both because of that sentence:

- **The URL carries a random path.** The original served at `/` on a fixed port,
  so anything sweeping the LAN would be handed a cluster-admin token by simply
  connecting. Now a sweep gets a 404. That's obscurity, not authentication — but
  it means the URL has to be given to you rather than stumbled upon.
- **It exits after serving the token once** (or `--timeout` seconds, default 300).
  The original ran until Ctrl-C, so the window stayed open as long as you forgot
  about the terminal. `--serve-forever` restores the old behaviour if you want it.

If you're only ever grabbing the token on this machine, skip it entirely and use
`./token.sh`.
