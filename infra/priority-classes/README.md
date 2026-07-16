# priority-classes — cluster-wide scheduling priority

Three PriorityClasses, one of which is the **globalDefault that every pod on this
cluster inherits**. No image, no workload: pure policy.

| Class | Value | For |
|---|---|---|
| `platform-core` | 100000 | Stateful things whose eviction costs data or a slow recovery — databases, search, caches, game servers |
| `platform-app` | 10000 | **globalDefault.** Anything that doesn't name a class |
| `batch-worker` | 1000 | Interruptible queue workers. `preemptionPolicy: Never` — evictable, but never evicts |

## Why it's here

These lived in the sibling talaria project's chart, which meant an app repo
owned the default scheduling policy for the whole cluster. At the time of the
move, **55 pods across 11 namespaces** ran under `platform-app` — only 15 of them
talaria's. The other 40 were minecraft, web, docs, infra, discord, keda, games,
auth, kube-system and headlamp.

Nothing in this repo names a `priorityClassName`. Every chart here inherits
`platform-app` silently, and would have kept inheriting it from a chart in
another repo. Same story as [`infra/duckdns`](../duckdns/).

talaria still *uses* `platform-core` and `batch-worker` — its postgres,
elasticsearch, redis and KEDA workers name them in seven places. It just no
longer defines them.

## Two things that will bite you

**`value` is immutable.** Kubernetes rejects any change to `value` on an existing
PriorityClass. Renumbering means delete-and-recreate, and for the globalDefault
that means the hazard below. `upgrade.sh` pre-flights this and fails with a clear
message rather than a wall of API error.

**Deleting the globalDefault is quietly destructive.** A pod's priority is stamped
into its spec at admission, so *running* pods keep whatever they were admitted
with. Delete `platform-app` and nothing appears to break — but every pod
scheduled afterwards admits at **priority 0** while everything still running sits
at 10000. The next restart makes a workload preemptible by every pod that hasn't
restarted yet, and `platform-core` (100000) outranks it outright. The symptom is
inexplicable evictions, hours or days later, with no obvious cause.

`helm uninstall priority-classes` does exactly that. `upgrade.sh` refuses to
finish if no globalDefault exists afterwards.

## Deploy

```bash
./upgrade.sh    # installs into `infra`
```

It adopts the live objects (they were created by talaria's release, and Helm
won't take over a resource it didn't create), checks the immutable values haven't
drifted, and prints the resulting policy plus the globalDefault.

## How the migration was done

Worth recording, because the obvious order is wrong. Removing the template from
talaria's chart and upgrading would have made Helm **delete** the PriorityClasses
— they were in its release manifest — taking the globalDefault with them.

1. `kubectl annotate priorityclass … helm.sh/resource-policy=keep` — so talaria's
   upgrade could not delete them.
2. Remove the template from talaria's chart; `helm upgrade talaria`. All three
   survived.
3. Stamp Helm ownership (`meta.helm.sh/release-name=priority-classes`) and
   `helm upgrade --install` here, which adopted them with no spec change.
4. Remove the `keep` annotation, so this chart genuinely owns them rather than
   leaving invisible state that would silently block future deletions.

Same shape as the PVC-retain dance: protect first, move second.
