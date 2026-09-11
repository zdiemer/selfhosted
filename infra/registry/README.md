# registry

The cluster's own OCI registry at `registry.zachd.duckdns.org`, in the `infra`
namespace. Every first-party image (`money`, `sms-relay`, `smitele-bot`,
`talaria-*`, `whatnowgg`, `gamedex`, `claude-workspace`, …) is pushed here by its
`build.sh` and pulled from here by every node. It is
[CNCF distribution](https://distribution.github.io/distribution/), values only.

## Why

Until this existed, every first-party image lived only on `ghcr.io/zdiemer/*`.
That made GitHub a *runtime* dependency: a pod rescheduling onto a node that
had never pulled that image — a drained node, a rebuilt one, a new one — needed
GHCR to answer, and an outage there, a package-permission change, or a PAT
expiring turned into pods stuck in `ImagePullBackOff`. The builds already ran
in-cluster ([`infra/buildkit`](../buildkit/)); this closes the loop so the
images they produce never leave the house.

Third-party images (`docker.io`, `ghcr.io/<upstream>`, `registry.k8s.io`) are
*not* proxied here. distribution's pull-through mode is one upstream per
instance and cannot accept pushes, so it would be a second deployment per
upstream for an outage that has never actually bitten. If that ever changes,
k3s's embedded registry mirror (Spegel) is the cheaper answer: nodes serve
each other whatever any of them has already pulled.

## Two paths in, one credential

```
build.sh (laptop / workspace / ARC) ──https──▶ Traefik ──▶ registry:5000   push
containerd on every node ──────────http (LAN)─────────────▶ 10.43.200.10:5000  pull
```

- **Push** goes through the normal ingress with the wildcard cert. No Authelia:
  docker and buildkit cannot complete an interactive login, so the gate is
  distribution's own htpasswd, checked on every request, pulls included.
- **Pull** never touches the ingress. Each node's
  `/etc/rancher/k3s/registries.yaml` mirrors `registry.zachd.duckdns.org` to the
  Service's **pinned ClusterIP** over plain HTTP on the cluster network, with
  the same credential. Two reasons this is not "just pull through the
  hostname": the public name resolves (from the LAN as well) to the VPS relay,
  so pulls would leave the house and come back; and
  [`infra/traefik`](../traefik/)'s edge rate limit is 25 r/s per source, which
  a multi-layer pull from one node exceeds on its own. If the mirror is ever
  unreachable containerd falls back to the hostname, so the slow path still
  works — it is just not the one anything should rely on.

The ClusterIP `10.43.200.10` is part of the contract: every node's file names
it, and helm cannot change a Service's IP in place. Don't.

There is exactly one user (`registry.auth`, from
`op://homelab/infra-registry`). Rotating it is `secrets edit infra/registry`
then `./upgrade.sh` — that re-hashes the htpasswd, rewrites every node, and
restarts k3s on each in turn — plus updating the two `~/.docker/config.json`
copies by hand (below).

## The node side: the `node-config` DaemonSet

containerd reads `registries.yaml` once, at k3s start, and a node cannot
resolve a Service name, so every node needs the file and a restart after any
change. A privileged, `hostPID` DaemonSet (see the header of
[`templates/node-config.yaml`](templates/node-config.yaml)) keeps the file equal
to the chart's Secret and restarts `k3s`/`k3s-agent` via `nsenter` when it
differs. It polls every five minutes; the steady state is "same, sleep".

Restarting k3s does not touch containerd or any running pod. Nodes take turns —
each waits for its slot in name order and for every schedulable node to be
Ready — so three control-plane restarts never overlap. If the unit is not back
within 90 s the previous file is restored and restarted, and that node stops
retrying until the Secret changes. A cordoned laptop gets the file whenever it
next runs the pod.

Watch a rollout with:

```sh
kubectl -n infra logs -l app.kubernetes.io/component=node-config -f --prefix
```

## Pushing from somewhere new

Add the credential to the docker config wherever `build.sh` runs. In the
workspace pod (no docker CLI):

```sh
eval "$(bash ~/code/selfhosted/scripts/op-session.sh ensure)"
PW="$(op read 'op://homelab/infra-registry/values.local.yaml' | python3 -c 'import sys,yaml;print(yaml.safe_load(sys.stdin)["registry"]["auth"]["password"])')"
python3 - "$PW" <<'PY'
import json, base64, sys, os
p = os.path.expanduser("~/.docker/config.json")
cfg = json.load(open(p)) if os.path.exists(p) else {"auths": {}}
cfg.setdefault("auths", {})["registry.zachd.duckdns.org"] = {
    "auth": base64.b64encode(f"zdiemer:{sys.argv[1]}".encode()).decode()}
json.dump(cfg, open(p, "w")); os.chmod(p, 0o600)
PY
unset PW
```

On a laptop with docker: `docker login registry.zachd.duckdns.org`.

## Reclaiming space

`storage.delete.enabled` is on, so a tag can be dropped with a `DELETE` on its
manifest digest, but distribution only frees the blobs when its garbage
collector runs — and that needs the registry stopped or read-only, which on a
single-attach RWO volume means a maintenance step, not a CronJob:

```sh
kubectl -n infra scale deploy/registry --replicas=0
kubectl -n infra run registry-gc --rm -it --restart=Never --image=docker.io/library/registry:3.1.1 \
  --overrides='{"spec":{"containers":[{"name":"gc","image":"docker.io/library/registry:3.1.1","command":["registry","garbage-collect","--delete-untagged","/etc/distribution/config.yml"],"volumeMounts":[{"name":"data","mountPath":"/var/lib/registry"},{"name":"config","mountPath":"/etc/distribution/config.yml","subPath":"config.yml"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"registry-data"}},{"name":"config","configMap":{"name":"registry-config"}}]}}'
kubectl -n infra scale deploy/registry --replicas=1
```

Half-finished uploads are purged automatically after a week.

## Deploy

```sh
./upgrade.sh   # helm upgrade, rollout, 401 gate check, probe push via the ingress, probe pull via the ClusterIP
```

The probe image (`zdiemer/registry-probe:<timestamp>`) is a one-file scratch
image; it is the smallest thing that proves the whole path, and it is cheap to
leave behind.
