# infra/traefik-certs

Renews the `*.zachd.duckdns.org` wildcard into a Secret on a schedule, so that
Traefik does not have to hold ACME state itself.

## Why

Traefik used to own the certificate: it ran the DNS-01 challenge and persisted
the result to `acme.json` on a **ReadWriteOnce local-path** PVC. That one file
set the availability ceiling for the entire cluster's ingress:

- **one replica** — two pods cannot hold an RWO volume, and two Traefiks racing
  on one `acme.json` corrupt it;
- **`Recreate` rollouts** — so every change to `infra/traefik` was a
  cluster-wide gap rather than a rolling update;
- **pinned to one node** — local-path meant the volume lived on a specific
  machine, so that machine could never be drained without taking all ingress
  down with it.

Moving issuance here makes Traefik stateless: it reads a Secret and never writes
one. That is what lets `infra/traefik` run three spread replicas behind a
PodDisruptionBudget with `maxUnavailable: 0`.

## How it avoids being pinned itself

No PVC anywhere. lego's data directory — its ACME **account key** plus the
issued certificate — is tarred into a Secret between runs and restored on the
next one. The account key is the part that genuinely has to persist: losing it
means re-registering with Let's Encrypt on every renewal, straight into a rate
limit.

The CronJob is three stages, as init containers plus one main container, because
init containers are a pod's only ordering primitive — regular containers start
in parallel, which would race the publish against the issuance:

| stage | image | does |
|---|---|---|
| `restore` | `alpine/kubectl` | lego state Secret → `emptyDir` (absent on first run, which is not an error) |
| `issue` | `goacme/lego` | `renew --days 30` if a cert exists, otherwise `run` |
| `publish` | `alpine/kubectl` | `emptyDir` → state Secret, then the TLS Secret |

The lego image ships a busybox shell with the binary at **`/lego`, not on
`PATH`**, which is what makes that branch possible without building an image of
our own. The branch is load-bearing: `renew` on a missing cert is an error, and
`run` on an existing one is a needless re-issue.

State is saved **before** the certificate. If the pod dies between the two,
having kept the account key and published nothing is recoverable; the reverse
would mean re-registering.

Traefik watches the Secret and picks up a new certificate **without
restarting**, so the job deliberately does not trigger a rollout — restarting
ingress to install a cert would give back the outage this exists to remove.

## Order of operations

```bash
./seed.sh                      # 1. copy the live cert out of Traefik's acme.json
./upgrade.sh                   # 2. install the renewal CronJob
./upgrade.sh --run-now         # 3. prove lego works, on a normal day
../traefik/upgrade.sh          # 4. the cutover
```

**Seeding first is what makes the cutover invisible.** The cutover points
Traefik at this Secret; if it does not exist yet, Traefik does not fail — it
serves its own self-signed certificate on every host, which is a browser warning
everywhere and nothing crashing to tell you. `infra/traefik/upgrade.sh` refuses
to apply unless the Secret is there.

Seeding also decouples "does the new plumbing work" from "can lego issue". The
seeded certificate keeps serving regardless of what the first CronJob run does,
so step 3 can be tested on a quiet weekday instead of being load-bearing during
a cutover.

`seed.sh` does **not** reconstruct lego's state — lego's data directory and
Traefik's `acme.json` are different formats, and converting the account key
between them is the kind of fiddly that fails quietly. lego registers its own
account and issues its own certificate on its first run. That is one extra
issuance, nowhere near Let's Encrypt's limits.

## The certresolver annotations can lag, and that is deliberate

Every Ingress in this repo (and in the submodule repos: talaria, whatnowgg,
money, smitele-bot, gamedex, sms-relay) still carries
`traefik.ingress.kubernetes.io/router.tls.certresolver: duckdns`, naming a
resolver that no longer exists after the cutover.

**This was tested against the live cluster rather than assumed.** An Ingress
pointing at a nonexistent resolver produces exactly one log line per router at
config-load time:

```
ERR Router uses a nonexistent certificate resolver  certificateResolver=doesnotexist routerName=...
```

and the router **still works** — TLS terminates, and the default certificate
from the TLSStore is served. It is not per-request, and it is not an outage.

So stripping those annotations is cleanup that silences ~26 log lines per
Traefik config reload, not a prerequisite. Keeping it out of the cutover is the
point: it means a change that touches 25 files across 7 repos does not have to
land in the same window as the change that can take all ingress down.

## Failure modes worth knowing

- **A failed renewal is not urgent.** The threshold is 30 days on a 90-day cert,
  so roughly eight weekly attempts happen before anything is user-visible.
  `backoffLimit: 0` — retrying an ACME order that failed for a real reason just
  burns rate limit.
- **`concurrencyPolicy: Forbid`** — two legos racing on one account key is the
  failure this design exists to avoid.
- **RBAC is scoped by `resourceNames`** to exactly the two Secrets. `create`
  cannot be restricted that way (no name exists at admission time), so it is a
  separate rule; `get`/`patch`/`update` are the narrow ones, and those are what
  an attacker would actually want in a namespace that also holds the DuckDNS
  token and Authelia's secrets.
- **DuckDNS can only set TXT at the account's own subdomain**, which is why the
  wildcard is not optional: a cert for `foo.zachd.duckdns.org` cannot be
  validated on its own, so sub-subdomains ride the wildcard SAN.
