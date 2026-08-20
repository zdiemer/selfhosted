# cluster-status — tailnet cluster dashboard

Live at **<https://status.zachd.duckdns.org>** — tailnet only. Node
CPU/RAM/disk with a pods-vs-k3s-vs-system breakdown, per-node pod tables, a
problem-pods roll-up, Deployment/StatefulSet/DaemonSet health, top consumers, and
events.

Since it stopped being public it also carries the things that were a bad idea to
publish and are the reason anyone opens it: **node identity** (internal and
tailnet IPs, role, k3s version, taints, cordon state, allocatable vs capacity),
the **routing table** for every Ingress in the cluster with its exposure and
auth posture, **PersistentVolumeClaims** with live fill, **CronJobs and job
failures**, **per-pod images, age, QoS and OOMKilled**, a **namespace roll-up**,
and the **URL behind each uptime probe**.

Ported from talaria's authed `/admin/cluster` page. That page's collector was
already fully generic — no talaria names, no filters — and its only coupling was
plumbing: talaria's `Handler` framework, its k8s client, protobuf, and structlog.
This drops all four and emits plain JSON. **The measurement logic is otherwise
deliberately unchanged**: it was the valuable part and it was already right.

talaria keeps its own page. This one is a separate, read-only view.

**It used to be public**, at `status.diemer.codes` over the Cloudflare tunnel,
on the reasoning that the page is static JSON so public traffic never touches
the k8s API. That reasoning is sound about the *API* and beside the point about
the *content*: the page publishes node names, the full pod inventory,
namespaces, deployment health and raw event text — a free map of the cluster for
anyone deciding whether it is worth attacking. Nothing was gained by that being
public, because the audience for this page was always us. See
`values.yaml` `ingress.cloudflareHosts` for the full note.

Delisting cost no availability: the tailnet path does not depend on the
Cloudflare tunnel, so this page still answers when the tunnel is the broken
thing — which is exactly when a status dashboard earns its keep.

```
  collector sidecar ──reads k8s API──▶ /data/status.json  (emptyDir)
   (the only thing                            │
    talking to k8s)          nginx ───serves──┤  index.html + status.json
                                              │
                                    Ingress ──┴──▶ status.zachd.duckdns.org
                                                   (tailnet only — no tunnel)
```

## Why a sidecar, not a CronJob

Network rates are computed by diffing kubelet's *cumulative* rx/tx counters
against the previous sample, held in memory. A CronJob starts fresh every run, so
it would never have a previous sample and every rate would be `null` forever. The
collector has to be a long-lived process.

## Payload size, and gzip

`status.json` is ~155KB — most of it per-pod rows whose key names repeat 250
times, so it compresses about 8×. It is served `no-store` and re-fetched by every
open tab every 15s, which is what makes that ratio worth having.

**nginx's stock config ships `gzip` commented out**, so this went over the wire
uncompressed for as long as the page existed. `default.conf` now turns it on.
Three things there are easy to get wrong and are commented in place: `text/html`
is always gzipped and must not be listed in `gzip_types`; `gzip_vary` matters
because Traefik sits in front; and level 5 rather than 9, because 9 buys ~3% more
on JSON for double the CPU on a container with a 10m request.

The collector logs `scrape_done seconds=… bytes=…` every cycle, which is the
measurement for both this and the interval below:

```bash
kubectl -n infra logs -l app.kubernetes.io/name=cluster-status -c collector --tail=5 | grep scrape_done
curl -s -o /dev/null -w 'plain %{size_download}\n' https://status.zachd.duckdns.org/status.json
curl -s -H 'Accept-Encoding: gzip' -o /dev/null -w 'gzip  %{size_download}\n' https://status.zachd.duckdns.org/status.json
```

Payload growth is kept down by omitting empty keys rather than writing
`"restartReason": null` on 250 pods (`prune()` in `extras.py`; the page reads
every optional field as `x || ''`). Image strings are deliberately *not* interned
into a lookup table — gzip already dedupes them, and a table would make the data
model non-obvious for ~10KB.

## Two scrape intervals

Ingresses, PVCs, PVs, Jobs and CronJobs change on a **deploy**, not on a tick.
Fetching them every 15s would be a 30% larger API bill for data that is identical
three cycles out of four, so `collector.slowIntervalSeconds` (60s) gives them
their own lane via `slow_api_get`. Steady state is ~17 calls per cycle with a +5
spike every fourth — about 7% more traffic rather than 30% — and
`intervalSeconds` stays at 15, so the page's freshness promise is unchanged.

Nothing time-sensitive rides on the slow lane. The number on the storage panel
that actually moves — how full a claim is — comes from kubelet's
`/stats/summary`, which is fetched every cycle with everything else. A failed
slow fetch keeps the previous value rather than blanking the panel, which is the
same choice `main()` makes about `status.json` as a whole.

## Service checks (uptime)

Pod status answers "is the process running", which is not the same question as
"does the service work". `infra/sms-relay` is the case that motivated this: its
pod can be `1/1 Running` and its `/api/health` green while the Android phone
that actually puts messages on the air is asleep, off wifi, or between IP
addresses — every other section of this page stays green through all of it.

`serviceChecks` in values is a list of HTTP endpoints the collector probes once
per cycle, in series, inside the scrape loop:

```yaml
serviceChecks:
  - name: SMS relay
    url: http://sms-relay.infra.svc.cluster.local:8000/api/health
    expectStatus: 200
    timeoutSeconds: 5
```

Keep `timeoutSeconds` well under `collector.intervalSeconds` — a hung check
delays the whole scrape, and an unreachable host burns the full timeout (a dead
ClusterIP port takes the entire budget before it gives up).

A check distinguishes three outcomes, because they mean different things:
`ok` (answered as expected), a `status` code (**reachable but unhealthy** — the
service is up and wrong), and a `reason` like `URLError` (**unreachable** — no
answer at all).

**The URL used to be withheld unconditionally**, because checks point at
ClusterIPs and LAN addresses and publishing them on the tunnel would have handed
the internet an internal host inventory. On the tailnet the target is the most
useful column in the table: "SMS relay is down" and "*this* ClusterIP is refusing
on :8000" are different diagnoses and only the second says where to look. So it
is published, behind `publish.serviceCheckUrls`.

Setting that flag to `false` restores the old behaviour exactly — the field is
never written, so it cannot leak through a later ingress mistake. And
`upgrade.sh` now treats *URLs published* **plus** *a public host on the Ingress*
as a combination worth shouting about at deploy time, which is the interlock
that makes leaving it on safe to forget about.

Turn the whole section off with `publish.uptime: false`.

**Why the phone itself isn't checked.** Probing the Android gateway directly
would need its basic-auth credentials in this collector, and it would publish
"the owner's phone is off the network" to the open internet — a fact about a
person, not about the cluster. The relay's own health endpoint is the right
altitude. (If that ever changes: the gateway app answers `/health` with a **500**
and reports real status on `/` instead, so point any such check at `/`.)

## Why nothing serves from an API

nginx only ever serves static files off local disk, so a page view never reaches
the Kubernetes API. An always-on API querying k8s per request would hand every
viewer a way to hammer the API server and every kubelet's `/stats/summary`,
unauthenticated. The collector is the only thing that talks to k8s, on its own
fixed schedule, no matter how much traffic arrives.

This argument was originally written as "this page is PUBLIC". Delisting removed
the internet from the threat model; it did not make an unauthenticated
k8s-querying API a good idea, and the property that makes this safe — serving
cannot amplify into API load — is worth having against a tailnet full of our own
browser tabs too.

The cost is that data is up to `collector.intervalSeconds` (15s) stale — so the
page says how old it is rather than pretending to be live.

## What's public, and where to change it

Everything the collector writes is readable by every viewer the moment it's
written. `values.yaml` has the switches, all **on** by default:

| Flag | Covers | Why you might turn it off |
|---|---|---|
| `publish.events` | Kubernetes event messages, Normal and Warning | Benign on a healthy cluster, but the text is unbounded and on a bad day routinely names Secrets, images and paths (`secret "x" not found`, `Failed to pull image ghcr.io/… unauthorized`). Least safe exactly when the page is most useful. |
| `publish.nodeVersions` | `kernelVersion`, `osImage`, `kubeletVersion`, `containerRuntimeVersion` | Publishing exact versions tells anyone which CVEs apply to these nodes. |
| `publish.nodeAddresses` | each node's internal and tailnet IP | Names are an inventory; addresses are a map you can act on. Nothing else on this page has ever carried an IP. |
| `publish.podImages` | first container `image:tag` per pod | A version inventory of every piece of software in the house — the `nodeVersions` argument one layer in. The two should move together. |
| `publish.ingresses` | host, path, backend, TLS, auth, exposure | The strongest disclosure here by some distance: a prioritised target list, not merely an inventory. Also the most useful new panel for the audience that can reach the page. That tension is why it has its own flag. |
| `publish.storage` | PVC name, class, capacity, fill | Capacities are dull; claim names are app and sometimes person names, and a fill percentage says which service is about to break without anyone touching it. |
| `publish.jobs` | CronJob schedules, failed Jobs | A schedule is a timetable of when this cluster is busy and when the backups run — information about the house rather than about Kubernetes. |
| `publish.serviceCheckUrls` | the target of each uptime probe | Withheld unconditionally while the page was public. See "Service checks" above. |

Redaction happens **at collection**, not in the page or the ingress — a field
that's off is never written to `status.json` at all, so it can't leak through a
later misconfiguration.

Also readable by nature, behind no flag: node names, and every
pod/namespace/deployment/PVC/CronJob name in the cluster — effectively an
inventory of what runs here, plus the namespace roll-up, which is a sum of
figures already on the page and so has deliberately **no** flag of its own. A
switch that redacts nothing quietly suggests the rest are decorative too.

**The payload used to carry no IPs at all.** It does now, behind
`publish.nodeAddresses` — that line was true of the public page and is the single
clearest example of what changed when the audience did.

`upgrade.sh` prints what's actually being published on every deploy — including
the probe URLs, spelled out rather than implied by a flag — so a flag that was
meant to be off is visible at deploy time.

## RBAC

Read-only: `nodes` list, `nodes/proxy` + `nodes/stats` get, `pods` list,
`events` list, `persistentvolumeclaims`/`persistentvolumes` list,
`deployments`/`statefulsets`/`daemonsets` list, `jobs`/`cronjobs` list, and
`ingresses` list.

Still narrower than talaria's backend role this came from — but the gap has
closed. That role also carried `pods/log`, `jobs` and `cronjobs`; the jobs lane
now needs the last two, so **`pods/log` is what remains**, and it is the one that
matters: log text is unbounded application output, a different class of
disclosure from object metadata, and it is not going on a page.

Every rule is `list` or `get`, and the five added for the routing, storage and
jobs panels are all plain object reads — none is a proxy or subresource verb, so
none of them widens what this ServiceAccount can *reach*, only what it can
enumerate.

`nodes/proxy` is still the powerful one — it's what reaches kubelet's
`/stats/summary` for the CPU/RAM/disk/network numbers, and it's why no
metrics-server is needed. It also means this ServiceAccount can proxy to
kubelets, so nothing else should borrow it. That warning points at exactly one
grant, and still does.

## A bug fixed on the way over

talaria's collector read network counters from kubelet's **top-level**
`node.network.rxBytes`/`txBytes`. On this cluster kubelet reports `name: ""` with
those fields absent and only a per-interface list populated — so the read returns
`None`, and **talaria's network column has always been blank**. Here the collector
falls back to the busiest physical interface, skipping the overlay and tunnel
devices (`flannel*`, `cni*`, `veth*`, `tailscale*`, …) whose traffic is already
counted on the uplink. Rates now populate on all 7 nodes.

If talaria's page ever needs it, that fix is `iface_counters()` in the collector.

## Deploy

```bash
./upgrade.sh    # installs into `infra`
```

There's no `values.local.yaml` — no secrets. Both the collector scripts and the
page files ship as ConfigMaps, so there's no image to build and no GHCR package;
a `checksum/` annotation rolls the pods when either changes.

Before deploying, compile the Python — a `|` block scalar swallows an
indentation mistake silently, and `helm template` will not notice:

```bash
mkdir -p /tmp/cs && helm template cluster-status infra/cluster-status | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-collector"):
        for k, v in d["data"].items(): open("/tmp/cs/" + k, "w").write(v)
'
python3 -m py_compile /tmp/cs/collect.py /tmp/cs/extras.py
node --check /tmp/page/app.js     # see "Editing the page" for the extract
```

Verify after:

```bash
curl -s https://status.zachd.duckdns.org/status.json | python3 -m json.tool | head -30
kubectl -n infra logs -l app.kubernetes.io/name=cluster-status -c collector --tail=20

for r in ingresses persistentvolumeclaims persistentvolumes jobs cronjobs; do
  kubectl auth can-i list $r --as=system:serviceaccount:infra:cluster-status
done
kubectl auth can-i create pods --as=system:serviceaccount:infra:cluster-status  # must be "no"
```

Network rates need **two** scrapes before they appear — expect `null` for the
first ~15s after a restart. That's the diff working as designed, not a fault.

Watch that **both replicas produce the same payload size**. A divergence means
one of them is silently failing a fetch and serving a shorter page to half the
tabs — which is invisible from a browser, because you only ever hit one.

## The verdict badge

The page opens on the answer, not the evidence. `verdict()` works it out from
exactly the data every card below is drawn from, so the badge can never
disagree with them, and it splits into two tiers:

| | what counts | colour |
|---|---|---|
| **bad** | node NotReady, probed service down, pod CrashLoopBackOff, any filesystem ≥ 90%, any PVC ≥ 90%, node under memory/disk/PID/network pressure | red |
| **warn** | failed / pending / unknown pods, any filesystem ≥ 80%, any PVC ≥ 80%, unbound PVC, cordoned node, failed Job | amber |
| **good** | none of the above | green |

**Failed pods are deliberately not critical.** Most of them here are CronJob
pods that exited non-zero hours ago and are being kept for their logs. A red
banner for those trains you to ignore red banners, which costs you the one that
matters.

**Staleness outranks everything.** nginx keeps serving the last `status.json`
after the collector dies, so without that check the page would sit there saying
"All clear" about a cluster it had stopped being able to see — the single worst
thing a status page can do. Past three collector intervals the badge goes grey
and says so, and a failed `fetch` does the same rather than leaving the last
good answer up.

The badge holds one line; anything else the verdict found goes on the `also:`
line beneath it. That is not just a nicety — a `title` tooltip would have hidden
it from every phone, which is most of this page's traffic.

## Editing the page

`templates/web-configmap.yaml` holds four keys: `index.html` (the shell — head,
og tags, masthead, `<div id="root">`), `app.css`, `app.js`, and the nginx config.
No build step, no framework.

**This used to say that past ~1000 lines the page "wants a real repo and a
build", and that was reconsidered rather than ignored.** The problem that line
described was navigability, and splitting files fixes navigability; a build does
not. What a build would have cost, against a chart whose stated value is having
no image and no CI: a Dockerfile, a GHCR package, a renovate entry, a workflow, a
version pin in `values.yaml`, the local iteration loop below, and the
`checksum/web` annotation in `deployment.yaml` that makes an edit roll the pods.
A ConfigMap is a map of keys and nginx already serves a directory, so the split
was free.

The same reasoning applies on the collector side: `collect.py` keeps every
function that talks to Kubernetes, and `extras.py` holds only pure parsers for
the newer panels — no network, no globals, no environment. That is what makes
them testable against a saved `status.json` and why there is no circular import.

All three page files are `no-cache`. That is not new, but it now covers three
files rather than one: a deploy can briefly pair a new `index.html` with a
not-yet-revalidated `app.js`, and `no-cache` bounds that window to one
round-trip rather than the hours heuristic caching would give.

Everything from `status.json` is HTML-escaped before it reaches `innerHTML` —
event messages, Job failure messages, image strings, taints and Ingress hosts are
all arbitrary text from namespaces this chart does not control, so `esc()` is not
optional. Keep it that way.

### On a phone

This is mostly read on a phone, and three things there are load-bearing:

- **Tables become records below 700px.** Every `<td>` carries a `data-l`
  attribute holding its column's header, which CSS pulls in front of the value
  as a label. Add a column, add its `data-l` — a cell without one renders with a
  blank label. They *flow* rather than stacking one per line: stacking was the
  first attempt and multiplies each table's height by its column count, which
  with 71 deployments and a node's 63 pods turned the page into 21,000px of
  scroll.
- **Long, healthy workload lists fold.** 71 Deployments, all Available, is one
  fact and 71 rows. A `<details>` collapses them — but only when nothing is
  wrong, because a card that hides a Failure behind a click is worse than no
  card.
- **`min-width: 0` on the grid and flex children.** The default is
  `min-width: auto`, i.e. "never shrink below your content", so one long mono
  bar label used to push its track wider than its allotted space and the whole
  document sideways with it. This page rendered 593px wide on a 390px phone for
  as long as it had existed. Anything new that holds an unbreakable string wants
  the same treatment, or it will do it again.

Every new table follows all three. Specifically: `.opt` is on the pod **Image**
and **QoS** columns, the routing **Path** and **Backend**, the storage **Class**
and **Phase**, the namespace **request** columns, and the problem-pod **Owner** —
in each case the longest string on the row and the least useful at a glance. The
routing table is the one to check hardest at 390px: hosts and backends are long
unbreakable strings, which is exactly what produced the 593px-wide bug.

**Anything the reader opens has to survive a poll.** `render()` replaces the
whole of `#root` every 15s, and a `<details>` keeps its open state in the DOM —
so every card that had been opened snapped shut on the next scrape, which is
long enough to start reading and short enough to be maddening. Open state lives
in `folds` beside `expanded` and `eventFilter`, for the same reason: the markup
is rebuilt from data, and what the reader chose to look at is not part of that
data. The listener is on `toggle` rather than a click on the `<summary>`,
because a `<details>` also opens by keyboard and by find-in-page.

Storage, routing, CronJobs and namespaces all fold behind `<details>` on the same
rule as the workload lists — and all of them force open the moment something is
wrong (an unbound or ≥80% claim, an Ingress with no TLS or no auth answer, a
CronJob whose last run failed). Namespaces fold unconditionally above 8 rows,
which looks like an exception and isn't: a namespace has no failure state to
hide, so the rule is satisfied vacuously rather than forgotten.

`.opt` marks the longest, least useful thing on a line — the OS string on a node
row, the `· req N` tail on a bar label. It is *dropped* on narrow screens rather
than ellipsised, because truncating a line takes the figure at the end of it
(the percentage) with it, and that is the one anybody came for. It is dropped
inside `.node-bars` at every width: those three bars share one node's worth of
space and never have room for the tail.

Iterating on this without a deploy loop: render the chart, drop `index.html` next
to a real `status.json`, and serve the directory.

```bash
mkdir -p /tmp/page && helm template cluster-status infra/cluster-status | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-web"):
        for k, v in d["data"].items():
            if k != "default.conf": open("/tmp/page/" + k, "w").write(v)
'
curl -s https://status.zachd.duckdns.org/status.json > /tmp/page/status.json
python3 -m http.server 8899 -d /tmp/page
```

**Exercise the unhappy paths by hand.** A healthy cluster never renders the warn
branches, which is how they ship broken. Copy `status.json` and edit it to
inject a PVC at 94%, a `Pending` PVC with `usedBytes: null`, an Ingress with no
certresolver and no `ingress.zachd/*` annotation, a CronJob whose
`lastSuccessfulTime` predates its `lastScheduleTime`, a pod with
`restartReason: OOMKilled`, and a cordoned node with two taints. Each should
colour correctly, force its `<details>` open, and reach the verdict badge.

## Icons and the preview card

`brand/` holds the favicon, the four home-screen sizes, the manifest and the
1200×630 preview card. They are **generated, not hand-made**: run
`python3 scripts/gen-brand.py status` from the repo root after changing the
mark, then redeploy. `brand/icon.svg` is the authority on the artwork and the
Pillow drawing calls in that script are a transcription of it — a change has to
land in both, which the SVG's own comment says out loud.

They ship as a second ConfigMap (`templates/brand-configmap.yaml`, `binaryData`
via `.Files.Get | b64enc`) because this chart has no image to bake them into.

The 1MiB ceiling applies **per object**, and there are three ConfigMaps here, so
the code has more room than it looks: `-brand` ~199KB, `-web` ~112KB, `-collector`
~48KB. The shared budget that actually binds is the **Helm release Secret**,
which holds every rendered manifest gzipped and base64'd and is capped at 1MiB
too — currently ~235KB, about 22%, of which the base64 PNGs are most of it
because they barely compress. The thing to watch when adding anything large is
`brand/og.png` at 111KB, not the code.

Two things about the nginx side that are not obvious:

- The location block **names each file** rather than matching an extension.
  Everything else on this server falls through to the page shell, and a
  `\.png$` pattern would start 404ing whatever gets added later instead of
  quietly serving the page. The list is the contract with the ConfigMap.
- `manifest.webmanifest` gets its own block purely for `default_type`. nginx's
  `mime.types` has no `.webmanifest`, so it served as
  `application/octet-stream` and Chrome silently declined to install the app
  while the file itself fetched perfectly. A `types {}` block would have been
  the obvious fix and is a trap — inside a location it *replaces* the inherited
  map rather than adding to it, taking `image/png` off the icons with it.

The card carries no numbers on purpose. A static card baked with "246 pods" is
a photograph of one afternoon, and on a page whose whole subject is freshness a
stale figure is worse than none — so it shows the three series the page draws
instead, which never go out of date. (`smite.diemer.codes` solves the same
problem the other way, by rendering its card live from the last snapshot;
nothing here can, because nothing here runs but nginx.)
