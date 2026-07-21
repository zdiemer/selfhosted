# buildkit

In-cluster **rootless buildkitd**, so the [claude-workspace](../../dev/claude-workspace/) pod can build and push images without a docker daemon. Clients talk to it with `buildctl` over plain TCP:

```
workspace pod ──tcp──▶ buildkitd.buildkit.svc.cluster.local:1234
```

The workspace image bakes that address in as `BUILDKIT_HOST`, and every per-chart `build.sh` falls back from `docker` to `buildctl` automatically — so `./build.sh` works identically from a laptop (docker) and from the pod (remote build here).

## How a build works

`buildctl` streams the build context from the client's filesystem to buildkitd, which builds and **pushes directly to GHCR from inside the cluster**. Registry credentials are not stored here: the client forwards its own `~/.docker/config.json` auth per session (for the workspace, that file lives on its home PVC — see its README for the one-time PAT setup).

## Security posture

- **No mTLS on the listener** (upstream supports it). Anyone who can reach port 1234 can run builds and push with whatever credentials *they* forward. The gate is the NetworkPolicy: ingress only from the `claude` namespace (`networkPolicy.clientNamespace`), and the Service is ClusterIP-only. Single-user homelab tradeoff; if that ever changes, wire up upstream's mTLS example.
- **The scary-looking securityContext** (seccomp/AppArmor `Unconfined`, no `allowPrivilegeEscalation: false`) is the standard rootless-buildkit-on-k8s recipe: rootlesskit creates a user namespace and execs setuid `newuidmap`/`newgidmap`, which the default profiles block. Everything runs as uid 1000; nothing grants root on the node.
- Egress: DNS + HTTPS to public IPs only (pull bases, push to ghcr.io).

## Ubuntu 24.04 prerequisite

The nodes run Ubuntu 24.04, which defaults `kernel.apparmor_restrict_unprivileged_userns=1` — that forbids exactly the user namespace rootlesskit needs, and buildkitd crashloops with `operation not permitted`. `upgrade.sh` pre-flights this on every node (via tailscale ssh) and prints the fix:

```sh
tailscale ssh root@<node> 'printf "kernel.apparmor_restrict_unprivileged_userns=0\n" > /etc/sysctl.d/60-buildkit-userns.conf && sysctl --system >/dev/null'
```

## Cache

30Gi PVC (`cache.size`) on the default StorageClass, mounted at buildkitd's snapshot store. buildkitd's built-in GC keeps it bounded; if it ever fills anyway, tune GC via a `buildkitd.toml` ConfigMap (not wired up yet — deliberately, YAGNI).

## Deploy

```sh
./upgrade.sh   # creates the buildkit namespace, helm upgrade, rollout wait, worker smoke test
```

Version lockstep: `image.tag` here (`v0.24.0-rootless`) and `BUILDKIT_VERSION` in `dev/claude-workspace/Dockerfile` (`v0.24.0`) move together.
