# hatch

A read-mostly REST/JSON cluster API for an external AI agent on the tailnet.

`hatch` is an agent that lives outside this cluster and can make HTTP requests.
It should be able to answer *"is anything broken, and why"* and fix the small
things, without holding a kubeconfig, an SSH key or a 1Password session. This
chart is the surface it talks to: the grant is a reviewable list of endpoints
rather than an RBAC role handed to a third party.

`https://hatch.zachd.duckdns.org` — tailnet-only, bearer-token authenticated.

## Architecture

FastAPI on `python:3.12-slim`, two replicas, no volume, no state that outlives
a pod. Three sources sit behind it, and which one answers matters:

| Source | Serves | Why not the others |
|---|---|---|
| `infra/cluster-status` `status.json` | node CPU/RAM/disk/network, the collector's own view | Already collected every 15s. Proxying it is why **hatch does not need `nodes/proxy`** — the one grant cluster-status warns nothing else should borrow. |
| The Kubernetes API | pods, workloads, events, pod logs, the mutations | Live and filterable; `status.json` is a snapshot with no log access. |
| Grafana Cloud (Viewer token) | PromQL, LogQL, alert state | Nothing in-cluster stores history — `infra/alloy` is a shipper with a write-only token. |

`GET /v1/cluster/summary` is the intended first call: a few KB.
`GET /v1/cluster/status` is the full collector document — **~165KB, roughly 45k
tokens** — so it takes a `?section=` projection and defaults to a subset.
`?section=index` lists the sections with their byte sizes first.

`/openapi.json` is served unauthenticated and is how the agent discovers the
surface. `/docs` and `/redoc` are off: an agent does not need HTML.

## Auth

`Authorization: Bearer <key>` or `X-API-Key`. Keys come from the chart Secret
and map 1:1 to a caller name recorded on every action; the comparison is
`hmac.compare_digest` against **every** configured key, because a dict lookup
leaks key material through timing (ported from `infra/sms-relay`).

**Never send the key as a query parameter.** Traefik's JSON access log records
`RequestPath` *including* the query string, and those logs ship to Grafana
Cloud, so a key in a URL is a key in someone else's log store.

Keys carry scopes, `read` and `act`. One key with both is the default; to hand
the agent read-only access, issue a second key with `"scopes":["read"]` and no
chart change is needed.

If `HATCH_API_KEYS` is empty or unparseable the pod **exits at startup** rather
than serving an unauthenticated cluster API. That check is in the app rather
than a helm `required` on purpose — see the comment in `templates/secret.yaml`.

## Why there is no tailnet ipAllowList

The obvious third layer would be a Traefik `ipAllowList` middleware on
`100.64.0.0/10`. It does not work here, and this was measured rather than
assumed.

Traefik's Service is `externalTrafficPolicy: Cluster` behind klipper-lb, so a
tailnet request is SNAT'd through the svclb pod before Traefik sees it. A
request to `status.zachd.duckdns.org` from the tailnet was logged as:

```
"ClientHost":"10.42.7.199"   "RequestHost":"status.zachd.duckdns.org"
```

`10.42.7.0/24` is `zachd-ubuntu-1`'s pod CIDR — the node DuckDNS points at.
Traefik never observes a `100.64/10` source, so that middleware would 403 every
real request. (`infra/traefik/values.yaml` says as much itself: "klipper-lb …
mean ClientHost is frequently an internal hop, not the actual origin.")

So the layers are: **tailnet-only by DNS** (`infra/duckdns` `updater.mode:
tailnet` resolves `*.zachd.duckdns.org` to an unroutable CGNAT address), then
the bearer key, then the deny-lists. `ingress.cloudflareHosts` is empty and
must stay empty — the tunnel would put a cluster-mutation API on the public
internet behind one token.

## The two deny-lists

**Logs** — `logs.denyNamespaces` (`auth`, `finance`). Log text is unbounded
application output, and everything hatch returns ends up in an LLM provider's
context. `logs.redact` scrubs credential-shaped strings from every line before
it leaves the pod and matters more than the namespace list, because it covers
the cases nobody predicts.

**Actions** — `actions.denyWorkloads` / `denyNamespaces`, matched *before* any
API call. Entries are `name`, `namespace/name` or `Kind:namespace/name`, glob
allowed. The chart appends this release itself, so hatch cannot be talked into
restarting hatch: that would kill the request mid-flight, so no result line is
ever written and the caller sees a connection reset it cannot tell apart from
a crash.

A denial returns 403 naming **both** the workload and the rule that matched —
an agent that receives a bare 403 retries forever. `POST /v1/actions/check`
dry-runs the guard under the `read` scope, and reports the specific rule even
while `actions.enabled` is false, so the deny-list can be verified before
mutations are ever turned on.

Not enabled, because they were not asked for: `actions.cooldown`,
`actions.notify`, and split read/act keys are values knobs defaulted off. With
no cooldown, nothing stops a confused agent restart-looping a workload, and the
first you would hear of it is `/v1/audit` or Loki.

## RBAC, and what is deliberately absent

Two ClusterRoles on one ServiceAccount. `hatch-act` is **not rendered at all**
unless `actions.enabled`, so turning actions off removes the authority rather
than just the code path, and `kubectl auth can-i` gives the honest answer.

Never granted: `secrets`, `configmaps`, `pods/exec`, `pods/attach`,
`pods/portforward`, `nodes/proxy`, `nodes/stats`, anything in
`rbac.authorization.k8s.io`, `nodes` patch (no cordon/drain), and `create` on
anything. `upgrade.sh` asserts the first few after every deploy.

Two honest limits:

- **`pods/log` is cluster-wide.** The namespace deny-list is enforced in
  application code, not by RBAC — expressing "everywhere except two" in RBAC
  means enumerating every namespace, which is the allow-list model this chart
  did not take.
- **`patch` on a Deployment is `patch` on its image too.** RBAC has no
  field-level scope. The narrowing is that `k8s.py` only ever sends the one
  `restartedAt` annotation patch, and that `rbac.act.mode: namespaced` (the
  default) makes the API server enforce *where* a mutation may land.

Scaling uses the `deployments/scale` subresource, never a patch of the parent.

One trap worth knowing, because it fails silently rather than loudly: the
generated Kubernetes client negotiates a patch `Content-Type` with
`select_header_content_type([json-patch, merge-patch, strategic-merge, …])`,
which returns `content_types[0]` — so patches go out as
`application/json-patch+json`, and `_content_type=` is not an accepted kwarg.
`json-patch` at least *errors* on a dict body. `merge-patch` would be accepted
and would **replace** the whole pod-template annotations map, deleting the
target chart's own `checksum/*` and rolling it for unrelated reasons. `k8s.py`
therefore keeps a separate `ApiClient` whose default header forces strategic
merge, since `__call_api` applies `default_headers` last. `app/tests/
test_patch_content_type.py` pins all three facts.

## Audit

One JSON line to stdout per mutation, per denial, and per pod-log read — the
log read is the sensitive read, so it belongs in the same trail. The pod
carries `logging.zachd/external-ingress`, so `infra/alloy` ships it to Loki
with no Alloy change. **That stream is the audit record**, not debug output.

```
{namespace="infra", app="hatch"} | json | event=~"action|denied|logs"
```

`GET /v1/audit` reads an in-memory ring buffer, and says so in its own payload:
with two replicas, two consecutive calls can return two different halves of the
history and a restart loses one. Loki is the source of truth.
`audit.source: loki` merges both and dedupes on `auditId`.

Not chosen: a shared PVC (an RWX volume would couple the monitoring tool to
`democratic-csi`, which is on the action deny-list precisely because it is
load-bearing), or Service `sessionAffinity` (it pins per *Traefik* pod, and
there are three of those).

## Secrets

`op://homelab/infra-hatch/values.local.yaml` — see `values.local.yaml.example`.
Two entries: the API key(s), and a Grafana Cloud token.

The Grafana token is a **third, new** credential. Not `infra-alloy`'s `glc_`
token (write-only — it cannot run a query) and not `infra-grafana-dashboards`'
`glsa_` **Admin** token (it can rewrite every dashboard and alert rule). Make a
service account `hatch-readonly`, role **Viewer**.

UIDs are resolved from `/api/frontend/settings`, which the least-privileged
token can read. Never promote this token to Admin to clear a 403.

Datasources are matched by **exact UID**, because the failure mode is silence.
Measured on this stack with one LogQL query:

```
grafanacloud-logs            -> HTTP 200, 4 streams
grafanacloud-usage-insights  -> HTTP 200, 0 streams
```

Three Loki datasources exist, and two Prometheus. The wrong pick is not an
error — it is an empty answer with a success code.

## Operating

```bash
eval "$(../../scripts/op-session.sh ensure)"
./build.sh      # bump image.tag AND Chart.yaml appVersion first
./upgrade.sh    # rolls, then asserts the RBAC negative space and probes /healthz
```

`build.sh` refuses to push over an existing tag. That is stricter than most
charts here and deliberate: the Deployment uses `imagePullPolicy: IfNotPresent`
so hatch can still start on a node that cannot reach GHCR, which is only safe
if a cached layer is guaranteed to be the image the tag names.

Rate-limit budget worth knowing: the Traefik `websecure` entrypoint rate-limits
at 25 req/s average, 100 burst, and all non-Cloudflare traffic shares one
bucket. An agent fanning out fifty concurrent log queries will 429 itself, and
a Traefik 429 looks to the agent like an application fault. Keep concurrency
low.

## Phases

1. **Read-only** — `actions.enabled: false`, `grafana.enabled: false`. Health,
   cluster summary/status, nodes, workloads, pods, events, logs.
2. **History** — create the Grafana Viewer service account, set
   `grafana.enabled: true`. Adds metrics, logs and alert queries.
3. **Actions** — `actions.enabled: true` with `rbac.act.mode: namespaced` and
   `allowNamespaces` narrowed to one namespace at first, widened one at a time.
