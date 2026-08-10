# duckdns — public DNS + the credential the cluster's TLS rides on

The cluster's dynamic-DNS updater and the owner of the token that issues
**every HTTPS certificate in this repo**. Two jobs, one credential:

- **DNS** — a CronJob tells DuckDNS every 5 minutes what the house's public IP
  is, keeping `zachd.duckdns.org` pointed at home across residential IP changes.
- **TOKEN** — the same DuckDNS token, copied into Traefik's namespace so its
  ACME **DNS-01** certresolver can answer a challenge. Break that Secret and
  nothing in the cluster renews.

The Traefik configuration itself — the certresolver, the ACME storage PVC, the
http→https redirect, access logging — lives in **[`../traefik`](../traefik)**.
It used to live here; see [History](#history).

There is **no cert-manager** in this cluster; Traefik's own ACME client is the
whole story.

```
                    ┌─ updater CronJob ──▶ duckdns.org/update?ip=   (A record ⇒ home IP)
  duckdns-token ────┤
   (one secret,     └─ Traefik ──ACME DNS-01──▶ Let's Encrypt       (*.zachd.duckdns.org cert)
    two copies)         ▲                            │
                        │                            ▼
             infra/traefik owns          every ingress: certresolver: duckdns
             the config that reads it
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

The `kube-system` copy predates this chart, from when this config lived in the
`talaria` repo. Helm won't touch a resource it didn't create, so `upgrade.sh`
stamps the ownership metadata on first run and adopts it. That's a no-op on
every run after.

`infra/traefik` references this Secret **by name**. Renaming it here breaks the
certresolver there.

## Deploy

```bash
cp values.local.yaml.example values.local.yaml   # then paste the DuckDNS token
./upgrade.sh                                      # installs into the `infra` namespace
```

`upgrade.sh` forces one updater run and shows its log, so a bad token fails in
front of you rather than quietly at 3am. It **no longer causes an ingress
outage** — that moved to `infra/traefik` along with the config.

Health:

```bash
kubectl -n infra get cronjob duckdns-updater
kubectl -n infra logs -l app.kubernetes.io/name=duckdns --tail=20   # expect "…: OK"
dig +short zachd.duckdns.org                                        # expect the house's IP
```

## Public or tailnet-only (`updater.mode`)

`updater.mode` decides what the A record points at, and with it whether
`*.zachd.duckdns.org` is reachable from the public internet at all.

- **`wan-echo`** (default, original behaviour) — send an empty `ip=`; DuckDNS
  records the request's source, i.e. the house's public IP. Every
  `*.zachd.duckdns.org` host is then reachable from the internet through
  whatever the router forwards.
- **`tailnet`** — send the first Ready cluster node's **Tailscale (100.x)**
  address instead. The names still resolve and still serve, but only to devices
  on the tailnet: `100.x` is CGNAT, unroutable from the public internet. This is
  the private/admin tier, and it's what lets the home router close its 80/443
  forwards entirely — the public path becomes Cloudflare-tunnel-only.

`tailnet` mode swaps the updater image for one with `kubectl` (`alpine/kubectl`)
and grants it a read-only `nodes` ClusterRole, so it can skip a node that isn't
Ready. It walks `updater.tailnet.candidates` in order — stable, so the record
doesn't flap between healthy nodes, but a dead lead node is skipped within one
tick. Keep that list matching `kubectl get nodes`.

**ACME is unaffected by the mode.** DNS-01 is outbound only: Traefik POSTs the
TXT record to the DuckDNS API and lego verifies it via 1.1.1.1/8.8.8.8. The A
record and any inbound port play no part in issuance or renewal, so the wildcard
keeps renewing after the cutover.

### Cutover runbook (wan-echo → tailnet)

The mode flip is safe and reversible on its own; closing the router is the step
that isn't. Do them separately, and only after the Cloudflare paths for anything
that used to be public via DuckDNS are confirmed working.

1. **Prereqs.** Every host that must stay publicly reachable is already on the
   Cloudflare tunnel and verified from off-LAN (jellyfin/requests/map, plus the
   Minecraft relay at `minecraft.diemer.codes`). See the plan.
2. Set `updater.mode: tailnet` in `values.yaml`, `./upgrade.sh`.
3. Wait one tick, then `dig +short zachd.duckdns.org @1.1.1.1` → expect a `100.x`
   address. From a tailnet device on cellular, load e.g.
   `https://docs.zachd.duckdns.org` and complete an Authelia login (session
   cookies and certs are unchanged — same names). From a non-tailnet network,
   the same URL must now fail to connect.
4. **Soak 24–48h.** The tailnet path is the only way you reach these now; be sure.
5. **MANUAL, at the router:** delete the port-forwards for 80, 443 and 25565.
   From an external host, `curl --max-time 5 https://<old-WAN-IP>` must time out;
   `*.diemer.codes` and `minecraft.diemer.codes` must still work.

Rollback at any point: `updater.mode: wan-echo`, `./upgrade.sh`, re-add the
router forwards. Propagates within one tick plus the ~60s DuckDNS TTL.

> The egress VPS's `egress-allow-home` set will now resolve `zachd.duckdns.org`
> to a `100.x` address. Harmless while `PUBLIC_FALLBACK=false` (nothing consults
> the set), but it must **stay** false in this mode — see
> [`../egress-proxy/vps/README.md`](../egress-proxy/vps/README.md).

## Editing the Traefik config

It isn't here any more — see [`../traefik`](../traefik). That chart's
`upgrade.sh` diffs before applying and warns you about the outage.

Two names are still easy to conflate when you get there:

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

The restart is a brief ingress blip (`Recreate` — see
[`../traefik`](../traefik)), but it re-issues nothing: `acme.json` is on the PVC.

## History

This all lived in the sibling `talaria` project — the CronJob as a template in
talaria's chart, the Traefik config as a loose `kubectl apply` file. That made an
app repo the owner of cluster-wide DNS and TLS that a dozen unrelated services
quietly depended on. It moved here so the dependency is visible and the ownership
matches reality. talaria still *uses* `zachd.duckdns.org` (its ingress host, a CSP
entry, and email links) — it just no longer runs it.

Then the Traefik half moved out again, to [`../traefik`](../traefik). Carrying
Traefik's entire config overlay inside the DNS chart was tolerable while it was
three ACME flags; it stopped being tolerable when access logging arrived and a
routine DNS change started meaning "brief cluster-wide ingress outage". Same
reasoning as the first move, one level finer: ownership should match what the
thing actually is.

