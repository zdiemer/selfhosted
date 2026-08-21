# crowdsec-machine-prune — stop dead agent pods from wedging live ones

A daily CronJob that runs `cscli machines prune` inside the CrowdSec LAPI pod.

```bash
./upgrade.sh              # install / update
./upgrade.sh --run-now    # install, then run it once and show the result
```

## Why

The upstream agent DaemonSet registers with the LAPI under its **pod name**
(`USERNAME` is a downward-API `fieldRef` on `metadata.name`). Pod names are
per-pod, not per-node, so every rollout, eviction and reboot burns one and
leaves the old registration behind forever. On 2026-08-21 the LAPI held 27
machines for a 7-node cluster — 21 of them tombstones.

An unreadable `cscli machines list` is the harmless half. The real problem is
that a stale registration **wedges the pod that owns it**. If an agent container
dies without its pod being replaced, the init container re-runs and tries to
claim the same name:

```
Error: cscli lapi register: api register http 403 Forbidden:
       API error: user 'crowdsec-agent-5q4sf': user already exist
```

There is no way out of that from inside the pod. The name is held by its own
dead predecessor, so the init container `CrashLoopBackOff`s forever and that
node quietly stops parsing Traefik logs. Nothing surfaces it: the DaemonSet
reports the pod as scheduled, and `cscli metrics` cannot show a gap for an agent
that never started. Two nodes sat in exactly this state before anyone noticed.

Pruning the tombstone frees the name, and the wedged init container's next
backoff retry succeeds. **This job is the self-heal path**, not just tidying.

## What it does not fix

The registration scheme itself. The durable answer is TLS agent authentication,
which uses certificates instead of a name-and-token and so cannot collide at
all. That is a larger change to how `infra/crowdsec` is configured; this makes
the current arrangement survivable until then.

## Safety

`--duration 24h` decides what counts as dead. Healthy agents heartbeat every few
seconds — the whole fleet was inside 20s when this was written — so the
threshold cannot be tripped by a busy node or a slow rollout. A node genuinely
offline for a day does get pruned, which is correct: its agent re-registers when
it returns, by the same mechanism described above.

`cscli machines prune` exits 0 and prints `No machines to prune.` when there is
nothing to do (verified against cscli v1.7.8), so the steady state is a job that
succeeds quietly rather than one that fails nightly once it has caught up.

## Where the privilege is

The job needs `pods/exec`, which is a shell in another pod, so it is worth
saying plainly why. `cscli machines` operates on the LAPI's **own database** —
a ReadWriteOnce PVC held by the LAPI pod. A second pod opening that SQLite file
concurrently is a corruption risk, not an alternative implementation. Exec is
how a human does this, and it is what the job does.

`pods/exec` cannot usefully be pinned by `resourceName`: the pod name carries
the ReplicaSet hash and changes on every crowdsec upgrade. The containment is
the namespace — the Role lives in `crowdsec`, and nothing else runs there.

## Verify

```bash
kubectl -n crowdsec get cronjob crowdsec-machine-prune
kubectl -n crowdsec exec deploy/crowdsec-lapi -- cscli machines list
```

Every row should be an agent that currently exists. Cross-check against:

```bash
kubectl -n crowdsec get pods -l type=agent
```

A row with a `⚠️` heartbeat older than a day means this job has not run, or has
been failing — check `kubectl -n crowdsec get jobs`.
