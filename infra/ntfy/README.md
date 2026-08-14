# ntfy

Self-hosted push notifications at `ntfy.zachd.duckdns.org`, in the `infra`
namespace. Publish with an HTTP POST, subscribe from a phone, a browser, or a
shell script:

```bash
curl -u alerts:"$PW" -d "Backup finished" https://ntfy.zachd.duckdns.org/backups
```

## Why this and not sms-relay

[`infra/sms-relay`](../sms-relay/) already delivers messages to a phone, and it
stays the right answer for anything that has to arrive as a *text message* — a
message to someone who isn't running an app, or an alert that has to survive
the house's internet being the thing that's broken.

Everything else was over-paying for that. sms-relay's path runs through a real
handset with a real carrier, so each message costs a send, arrives without
formatting, and cannot carry a title, a priority, a tap-through link, or an
attachment. Most of what the cluster wants to say is a CronJob that failed or a
disk crossing 85% — worth a notification, not worth an SMS.

ntfy is the cheap tier: unlimited, structured, per-topic, and free to subscribe
to from more than one device. The two are complements, and the split is roughly
"can this wait for me to unlock my phone?".

## Topics and who can use them

Access is **deny-all by default** (`auth-default-access: deny-all`). This host
is public on DuckDNS, and an ntfy server without that setting is a message queue
anyone on the internet can publish to and read from — including reading every
notification the cluster has sent, since topics are just strings and there is
nothing secret about guessing one.

So there are exactly two accounts, seeded from the vault:

| User | Role | Can do |
|---|---|---|
| `zach` | admin | read-write on every topic, including ones that don't exist yet |
| `alerts` | user | **write-only** on `*` |

`alerts` is the credential every automated publisher holds. Write-only is the
point of it: a leaked publisher credential should be able to send a
notification, not read back the history of everything the cluster has ever told
me. Admin is the subscriber side, and it covers new topics without an ACL
change — which matters, because otherwise the first alert from a new source is
the one that silently goes nowhere.

Topics are created by publishing to them. The convention is one per source:
`deploy`, `backups`, `alerts`, `cron`.

## Getting it on a phone

**Android** — install ntfy (F-Droid or Play), Settings → *Default server* →
`https://ntfy.zachd.duckdns.org`, then sign in as `zach` under *Manage users*.
The app holds its own long-lived connection to this server and no third party
is involved.

**iOS** — same server and login, but the delivery path is different and worth
understanding before trusting it. iOS has no background push except APNs, and
APNs certificates are per-app: the App Store ntfy client can only be woken by
ntfy.sh. `upstream-base-url: https://ntfy.sh` in `values.yaml` is what bridges
that — on publish, this server forwards a *poll request* upstream so ntfy.sh can
wake the app, which then fetches the actual message from here.

**The message body never leaves the cluster.** What ntfy.sh learns is a sha256
of the topic name and the timing of every notification. That is a real
disclosure and it is the price of instant delivery on an iPhone; set
`ntfy.upstreamBaseUrl: ""` to opt out and accept that iOS notifications arrive
whenever the app next polls.

## Not behind Authelia

Nearly every host in this repo is gated by Authelia forward-auth. This one
deliberately isn't, for the same reason [`media/jellyfin`](../../media/jellyfin/)
isn't: every client is an API client. The phone apps send
`Authorization: Bearer`, `curl` publishes with basic auth, Grafana posts a
webhook — none of them can complete an interactive browser login, so a
forward-auth middleware would turn every one of them into a 302 to a login page
they can't render.

ntfy's own deny-all ACL is the gate instead, and unlike a wrapped app it is one
the clients were designed for.

## Users are seeded, not clicked

ntfy has no declarative user store: accounts and ACL rows are written by the
`ntfy user` and `ntfy access` CLI into `user.db`. Left alone that makes account
setup a `kubectl exec` nobody writes down, and a password rotation something
that happens in exactly one place and is then forgotten.

`templates/secret-seed.yaml` renders a `seed.sh` from `ntfy.users` in the vault
and the container runs it in the background of the server process on every
start, so the vault stays authoritative — rotate a password there, deploy, done.

Two constraints shaped that:

- **The CLI refuses a database that doesn't exist yet** ("auth-file does not
  exist; please start the server at least once to create it"). Seeding therefore
  strictly follows the server. An initContainer runs before it, and a Job would
  need a second attach of a single-node RWO volume — both are also two writers
  against one SQLite file. Backgrounding the seeder inside the server's own
  container keeps a single writer and gets the ordering for free.
- **`ntfy user change-role` clears that user's ACL rows.** Grants are re-applied
  after it, in that order, every start.

The server still `exec`s as PID 1 so it receives SIGTERM directly; a shell
parent would swallow it and the pod would die on the grace period instead.

## Storage

One 5Gi `truenas-iscsi` PVC at `/var/lib/ntfy`, holding `cache.db` (the
replayable message log), `user.db` (accounts and ACL) and the attachment
directory. SQLite wants real fsync, and RWO on `truenas-iscsi` is single-node
*attach*, which is why the Deployment is `Recreate` — a surge pod on another
node would block on `FailedAttachVolume` and wedge the rollout.

`cache-duration: 12h` is what makes a phone that was off all morning still
receive the night's messages when it reconnects; it is deliberately longer than
any subscriber's reconnect window.

Losing this volume is losing every account, not just history — it belongs in the
backup set rather than being treated as a cache.

## Deploying

```bash
./upgrade.sh
```

`upgrade.sh` waits for the seeder to finish, prints the ACL it actually wrote,
and — given `NTFY_SMOKE_USER` / `NTFY_SMOKE_PASS` — publishes a real message
through the public hostname. That last check is the one that matters: a
notification server that is `Running` and rejecting every publish looks
identical to a healthy one from `kubectl get pods`, and the ClusterIP curl that
would be easier to write passes just as happily when the ingress is wrong, the
cert is stale, or the ACL denies the publisher.

## Follow-ups

- **Grafana alerts.** [`infra/grafana-dashboards`](../grafana-dashboards/) has
  thirteen alert rules routing to a deliberate dead end. A contact point posting
  to `https://ntfy.zachd.duckdns.org/alerts` as `alerts` is what this server was
  stood up for; that change belongs in that chart.
- **`scripts/notify-failure.sh`** currently goes through sms-relay. Most of what
  it reports is the cheap tier.
- **Metrics.** `enable-metrics` is on at `:9090` on the Service, but
  [`infra/alloy`](../alloy/) doesn't scrape it yet — that needs a tenth scrape
  job there, and the series budget checked against
  [`infra/kube-state-metrics`](../kube-state-metrics/)'s ceiling.
- **Blackbox probe.** `ntfy.zachd.duckdns.org` isn't in alloy's probe list.
- **Web push** (browser notifications with the tab closed) needs a VAPID
  keypair in `web-push-*` config. Not wired; the phone apps cover it.
