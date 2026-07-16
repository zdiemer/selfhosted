# duckdns — public DNS + the cluster's TLS foundation

The cluster's dynamic-DNS updater and, less obviously, the thing that issues
**every HTTPS certificate in this repo**. It does two jobs off one credential:

- **DNS** — a CronJob tells DuckDNS every 5 minutes what the house's public IP
  is, keeping `zachd.duckdns.org` pointed at home across residential IP changes.
- **TLS** — a `HelmChartConfig` patches k3s's bundled Traefik with an ACME
  **DNS-01** certresolver named `duckdns`. Every ingress in this repo names that
  resolver, so this chart is load-bearing for HTTPS cluster-wide.

There is **no cert-manager** in this cluster; Traefik's own ACME client is the
whole story.

```
                    ┌─ updater CronJob ──▶ duckdns.org/update?ip=   (A record ⇒ home IP)
  duckdns-token ────┤
   (one secret)     └─ Traefik ──ACME DNS-01──▶ Let's Encrypt       (*.zachd.duckdns.org cert)
                                                     │
                                                     ▼
                                    every ingress: certresolver: duckdns
```

## Why DNS-01, and why a wildcard

DNS-01 (rather than HTTP-01) is what lets Traefik hold a **wildcard** cert and
means port 80 never has to be open to the internet just to renew.

The wildcard isn't a nicety — it's forced. **The DuckDNS API can only set a TXT
record at the account's top-level subdomain.** A per-host challenge for
`docs.zachd.duckdns.org` has nowhere to put its TXT record and will fail. So
services ride a wildcard SAN instead: one `*.zachd.duckdns.org` cert covers every
sub-subdomain, present and future. That's the pair you'll see in each service's
values:

```yaml
ingress:
  tls:
    certResolver: duckdns
    domain: zachd.duckdns.org
    wildcardSan: "*.zachd.duckdns.org"
```

The happy consequence: **adding a service needs no change here.** DuckDNS resolves
any `*.zachd.duckdns.org` label to the same A record, and the wildcard cert
already covers it. New host → it just works.

If you add a service and its cert won't issue, the missing `wildcardSan` is
almost always why.

## The secret exists twice, on purpose

`duckdns-token` is rendered into **both** `infra` (for the updater) and
`kube-system` (for Traefik). A `secretKeyRef` cannot cross namespaces, so each
reader needs a copy in its own — one token in `values.local.yaml`, two Secrets.

Both live objects predate this chart, from when this config lived in the
`talaria` repo. Helm won't touch a resource it didn't create, so `upgrade.sh`
stamps the ownership metadata on first run and adopts them. That's a no-op on
every run after.

## Deploy

```bash
cp values.local.yaml.example values.local.yaml   # then paste the DuckDNS token
./upgrade.sh                                      # installs into the `infra` namespace
```

`upgrade.sh` forces one updater run and shows its log, so a bad token fails in
front of you rather than quietly at 3am. It also diffs the Traefik config first
and warns before a redeploy.

Health:

```bash
kubectl -n infra get cronjob duckdns-updater
kubectl -n infra logs -l app.kubernetes.io/name=duckdns --tail=20   # expect "…: OK"
dig +short zachd.duckdns.org                                        # expect the house's IP
```

## Editing the Traefik config carefully

`valuesContent` is Traefik's **entire** value overlay, so it necessarily carries
settings that aren't DuckDNS's business — the ACME storage PVC, the http→https
redirect, the `Recreate` strategy. They're there because they share the object.

k3s's helm-controller redeploys Traefik whenever `valuesContent` changes, and
with `Recreate` that's a real cluster-wide ingress gap, not a rolling one. Issued
certs survive (`acme.json` is on the PVC), so a redeploy costs downtime, not a
re-issue — but keep edits deliberate. `upgrade.sh` diffs before applying.

Two names in there are easy to conflate:

- `certResolver` (`duckdns`) — our label for the resolver. Renaming it means
  editing every chart's `ingress.tls.certResolver` **and** forces a full re-issue.
- `...dnschallenge.provider=duckdns` — lego's provider ID. Fixed by lego; it stays
  `duckdns` no matter what the resolver is called.

## Rotating the token

Regenerate at [duckdns.org](https://www.duckdns.org), update `values.local.yaml`,
re-run `./upgrade.sh` — then **restart Traefik**:

```bash
kubectl rollout restart deployment traefik -n kube-system
```

The updater picks the new token up on its next tick, because every run is a fresh
pod. Traefik does not. It reads `DUCKDNS_TOKEN` into its environment at pod start,
and env vars are never refreshed afterwards — a *mounted* secret would track the
change, an env var can't. A running Traefik therefore keeps using the old token
until something restarts it.

What makes this easy to get wrong is the delay: certs are 90 days and Traefik
renews at 30 days remaining, so it won't touch DuckDNS for ~60 days. Skip the
restart and everything looks perfectly healthy right up until a renewal fails
DNS-01 against a token that no longer exists. Do it while you're thinking about it.

The restart is a brief ingress blip (`Recreate`, see above), but it re-issues
nothing — `acme.json` is on the PVC.

## History

This all lived in the sibling `talaria` project — the CronJob as a template in
talaria's chart, the Traefik config as a loose `kubectl apply` file. That made an
app repo the owner of cluster-wide DNS and TLS that a dozen unrelated services
quietly depended on. It moved here so the dependency is visible and the ownership
matches reality. talaria still *uses* `zachd.duckdns.org` (its ingress host, a CSP
entry, and email links) — it just no longer runs it.
