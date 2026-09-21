# Archive Team Warrior

One volunteer worker using spare capacity on an amd64 Linux worker node. It
follows Archive Team's automatic project selection, with two concurrent items
and the contribution nickname `zachd`. Change these in `values.yaml`.

## Scheduling and shutdown

`spare-compute` has priority **-1000**, below unclassified pods (0), batch
workers (1000), applications (10000), and core services (100000). Warrior never
preempts other pods. Higher-priority applications can preempt it when their
requests do not fit; the Deployment replaces it wherever capacity is available,
or leaves it Pending until capacity returns. Existing `batch-worker` pods also
have `preemptionPolicy: Never`, so they queue ahead of Warrior but cannot evict it.

Requests are 500m CPU, 1Gi memory and 5Gi ephemeral disk; limits are 2 CPU, 2Gi
memory and 20Gi disk. CPU requests determine contention shares, while the limit
caps bursting. Priority alone does **not** stop Warrior when existing pods get
busy: scheduler preemption is based on pending pods and resource requests, not
live CPU utilization. Keep application requests realistic and adjust Warrior's
limits to the headroom you want to donate.

The preStop hook calls Warrior's `/api/stop`, which stops new items and drains
active ones, and waits for the UI listener to exit before allowing container
termination. This avoids relying on signal forwarding through upstream's Python
launcher. Kubernetes allows **300 seconds total**, then forcibly terminates it.
That also bounds how long an application may wait for preempted capacity.
There is no PDB to obstruct eviction, and no liveness probe to interrupt a grab.

When Warrior finishes and its main process exits, the runtime can kill the
waiting hook before it returns, producing `FailedPreStopHook` / exit 137 for
the hook even after a successful drain. Check the application log for
`Runner has finished.` to distinguish this race from interrupted work. The
upstream container also logs `sudo: shutdown: command not found` on this path;
its process still exits. Both occurred in the successful live drain test.

Graceful completion is best effort: long items may exceed the deadline, and OOM,
hard node-pressure eviction or node failure can bypass it entirely. Interrupted
items are eventually reassigned by Archive Team. Work and UI settings are
ephemeral; no NAS storage or backup is needed. Do not mount over the image's
`/home/warrior/data`: it contains its wget binaries.

## Deploy and operate

```bash
../../infra/priority-classes/upgrade.sh
./upgrade.sh
kubectl -n life rollout status deployment/archiveteam-warrior --timeout=180s
kubectl -n life port-forward deployment/archiveteam-warrior 8001:8001
```

Open `http://localhost:8001`. There is no Ingress or Service; a NetworkPolicy
blocks pod ingress while port-forward supplies private administrative access.
Traffic goes directly to Archive Team projects, without the shared proxy/VPN.

The image uses upstream's `latest` with `Always` pull policy. This pulls on
container starts, **not continuously**. To fetch an updated image, or to check
graceful shutdown while watching logs:

```bash
kubectl -n life logs -f deployment/archiveteam-warrior
# In another terminal:
kubectl -n life rollout restart deployment/archiveteam-warrior
```

Updates use Recreate to avoid needing spare surge capacity. `upgrade.sh` does
not wait or roll back on Pending, which is normal for this workload. Pause with
`kubectl -n life scale deployment/archiveteam-warrior --replicas=0`; the next
chart upgrade restores one worker.

Upstream references: [container and configuration](https://github.com/ArchiveTeam/warrior-dockerfile),
[Warrior shutdown](https://wiki.archiveteam.org/index.php/ArchiveTeam_Warrior#How_can_I_shut_down_the_Warrior_without_losing_work?),
[Kubernetes preemption](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/).
