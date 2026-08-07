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

## Three things that were quietly broken

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

**The ACME delay flag was deprecated.** Traefik warned on every start:
`delayBeforeCheck is now deprecated, please use propagation.delayBeforeChecks
instead.` It still worked, but a deprecated ACME flag is exactly the sort of
thing that vanishes in a routine k3s bump and takes cluster-wide TLS with it.
Now rendered as `...dnschallenge.propagation.delaybeforechecks`.

## Do not turn `filters.retryattempts` back on

Setting it `true` makes the chart render a **bare, valueless** CLI flag:

```
--accesslog.filters.retryattempts
```

Traefik 3.6.7 accepts that at startup, reports it in its args, serves traffic
normally, and increments its Prometheus counters — while writing **no access log
at all**. No error, no warning, nothing on stderr. It cost a full debugging pass
to find, because every observable signal said the config had applied.

The template only emits filter keys that carry a value now. If you ever want
retry logging back, verify that lines actually appear before believing it works.

## Two reconcile traps worth knowing

**Helm adopts on install without writing.** The first
`helm upgrade --install` against a pre-existing object *takes ownership* and
records the new manifest in the release — but does not push the content. The
next upgrade then diffs new-against-new, produces an empty patch, and changes
nothing. Symptom: `helm get manifest` shows your change, the live object doesn't
have it. Fix is to `kubectl apply` the rendered manifest once; after that the
release and the object agree and normal upgrades work.

**helm-controller can only fetch the chart from some nodes.** The `HelmChart` CR
pins an exact tarball:

```
https://%{KUBERNETES_API}%/static/charts/traefik-38.0.201+up38.0.2.tgz
```

That URL resolves through the `kubernetes` Service, which round-robins across
all three control-plane apiservers — and each server only serves the chart
version *its own k3s release* bundles. This cluster's control plane is split
(`v1.34.3+k3s3` on `zachd-ubuntu`, `v1.34.6+k3s1` on the two laptops), and only
`zachd-ubuntu` still has 38.x. The other two answer **404**.

So roughly two out of three reconcile attempts fail. The job retries forever
(`backoffLimit: 1000`), so a config change *does* land eventually — at an
unpredictable moment, as an unannounced cluster-wide outage. To land it
deliberately, delete the failing job pod until an attempt succeeds:

```bash
kubectl -n kube-system delete pod -l job-name=helm-install-traefik --force
```

**The real fix is aligning k3s versions across the control plane**
(`scripts/k3s/k3s-upgrade.sh`). Until then, every change here is a coin flip on
timing.

## The startup middleware race is expected

For a few seconds after Traefik starts, its log fills with:

```
ERR error="middleware \"auth-authelia-forwardauth@kubernetescrd\" does not exist"
```

This is a race, not a fault: the `kubernetesingress` provider builds routers
from Ingress objects before the `kubernetescrd` provider has finished syncing
the `Middleware` CRDs those routers reference. It resolves itself within
seconds and the errors stop.

It matters only because it is not free: during that window, requests to every
Authelia-gated host get a 500. `updateStrategy: Recreate` means each config
change spends that window with no old pod still serving. Expect a handful of
5xx in the access log around any run of `./upgrade.sh` — those are this, not a
regression.

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
