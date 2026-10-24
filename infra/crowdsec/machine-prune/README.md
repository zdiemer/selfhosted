# CrowdSec machine registration cleanup

A daily CronJob runs `cscli machines prune --duration 24h --force` inside the
LAPI pod to remove abandoned agent identities. Healthy agents heartbeat
regularly; the age threshold protects active registrations.

```bash
./upgrade.sh              # install / update
./upgrade.sh --run-now    # install, then run once
```

The main CrowdSec release's `post-render.py` gives each password-registration
attempt a pod-name prefix plus a random UUID. Rebooting a node or losing local
credentials therefore does not collide with the previous identity. This job
bounds the retained identity count; agents no longer wait for its daily run to
recover from a name collision. The registration token and validation policy are
unchanged. Do not identify a current machine by exact pod name alone.

An agent offline for more than 24 hours can be pruned. If it returns with stale
credentials, replacing its pod establishes a fresh identity. This cleanup does
not implement certificate authentication or repair arbitrary LAPI outages.

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
