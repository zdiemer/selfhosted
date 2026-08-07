# traefik

Cluster-wide Traefik configuration: the ACME certresolver, the http→https
redirect, the rollout strategy, and access logging.

**This chart does not install Traefik.** k3s does, from a bundled `HelmChart`.
This chart owns exactly one object — a `HelmChartConfig` named `traefik` in
`kube-system` — which k3s's helm-controller merges into that release.

```
                       ┌─ ACME DNS-01 ─▶ Let's Encrypt   (*.zachd.duckdns.org)
internet ──▶ Traefik ──┤
   :443                └─ access log ──▶ infra/alloy ──▶ Grafana Cloud
                                │
                                └─ routes by Host header ──▶ 20 Ingresses
```

Traefik is the **single choke point for every external HTTP request** in the
cluster, whichever repo owns the workload behind it. That is what makes one
access log a complete record of external ingress, and it is why changes here
are cluster-wide rather than local.

## Every change here is an outage

helm-controller redeploys Traefik whenever `valuesContent` changes. With
`updateStrategy: Recreate` and a ReadWriteOnce `acme.json` volume, the old pod
is fully gone before the new one starts — roughly 10–30s during which **nothing**
in the cluster is reachable over HTTP.

Issued certs survive (`acme.json` is on the PVC), so this is downtime, not a
re-issue, and it does not spend Let's Encrypt rate limit.

`./upgrade.sh` diffs live against rendered, prints the diff, and asks before
proceeding. Take the prompt seriously.

## Relationship to infra/duckdns

Split apart deliberately. `infra/duckdns` owns **DNS** (the updater CronJob) and
**the token Secret**; this chart owns **Traefik**. They are coupled at exactly
one point:

- duckdns renders `duckdns-token` into *both* `infra` and `kube-system`, because
  a `secretKeyRef` cannot cross namespaces. This chart's certresolver reads the
  `kube-system` copy by name (`acme.tokenSecret.name`).

So renaming that Secret in `infra/duckdns` breaks the certresolver here.

**Rotating the DuckDNS token needs a Traefik restart.** Traefik reads
`DUCKDNS_TOKEN` into its environment at pod start and never re-reads it. A
mounted secret would refresh; an env var cannot. Update the token, re-run
`infra/duckdns/upgrade.sh`, then:

```bash
kubectl rollout restart deployment traefik -n kube-system
```

Skip that and everything looks perfectly healthy for ~60 days, until a renewal
fails against a token Traefik no longer has.

## Two things that were quietly broken

**`deployment.strategy` was never a real key.** The config carried
`deployment.strategy.type: Recreate` for its whole life. The Traefik chart reads
**`updateStrategy`**, so the live Deployment sat on the chart default
(`RollingUpdate`, `maxSurge: 1`, `maxUnavailable: 0`) the entire time. It never
visibly broke because a rollout only wedges when the replacement pod is
scheduled to a *different* node: `maxSurge: 1` starts it before the original
terminates, both want the same ReadWriteOnce local-path volume, and the new pod
blocks on `FailedAttachVolume` while the old one refuses to die. Fixed here.

**There were no access logs at all.** Traefik ran without any `--accesslog`
flag, producing about two log lines per day. There was no way to answer "is
something scanning me", "who hit that endpoint", or "why did that 502".

## Access logs

JSON, every status code, shipped off-node by `infra/alloy`.

Two settings are load-bearing and should not be relaxed casually:

- **`fields.headers.defaultmode: drop`** — this is what keeps session cookies,
  `Authorization`, and Authelia's `Remote-User`/`Remote-Email` identity headers
  out of logs that leave the house. The allowlist is only `User-Agent`,
  `Referer`, `X-Forwarded-For`.
- **`addInternals: false`** — Traefik's own ping/dashboard routers are health
  noise, not ingress.

`X-Forwarded-For` is on the allowlist because it is usually the *only* real
client IP available: klipper-lb and the Cloudflare tunnel both mean `ClientHost`
is frequently an internal hop.

**Query strings are logged.** Traefik's `RequestPath` includes them, and these
logs go to a third party. Nothing in this cluster puts a credential in a query
string today — sms-relay's webhook is HMAC-signed in the body, Authelia's `?rd=`
is just a redirect target — but a service that accepted `?token=` would leak it.
Drop `RequestPath` in `values.yaml` if that ever changes.

## Usage

First time only — move the config off the `duckdns` release:

```bash
./handover.sh     # no outage: only Helm bookkeeping annotations change
```

Then, and for every change after:

```bash
./upgrade.sh      # shows the diff, asks, then takes the outage
```

Verify:

```bash
# strategy actually applied
kubectl -n kube-system get deploy traefik -o jsonpath='{.spec.strategy.type}{"\n"}'

# structured access logs flowing
kubectl -n kube-system logs deploy/traefik --tail=5 | jq .

# and that no credential headers are present
kubectl -n kube-system logs deploy/traefik --tail=50 \
  | jq 'keys' | grep -iE 'cookie|authorization|remote-' && echo LEAK || echo clean
```

## Deliberately not configured here

- **The dashboard.** `--api.dashboard=true` is on, but the `traefik` entrypoint
  is absent from the LoadBalancer Service, so it is unreachable from outside.
  Leave it that way.
- **Prometheus metrics.** k3s already passes `--metrics.prometheus=true` on the
  `metrics` entrypoint (`:9100`). `infra/alloy` scrapes the pod IP directly, so
  that port never needs to reach the Service, let alone the LoadBalancer.

## Known limitation

Traefik runs as **one replica**, and cannot currently run as more: `acme.json`
is on a ReadWriteOnce local-path volume, so two pods can never hold it at once.
It is a single point of failure for all ingress *and* all TLS in the cluster.
Fixing it needs RWX storage or a different cert store — a real project, not a
values change.
