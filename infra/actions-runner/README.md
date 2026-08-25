# actions-runner — self-hosted GitHub Actions runners

[Actions Runner Controller](https://github.com/actions/actions-runner-controller)
(ARC) on the cluster, one runner scale set per **private** repo, scaling to
zero between jobs. Private repos are the only ones GitHub meters, and they are
the only ones that should ever get a self-hosted runner (see `values.yaml`).

```
GitHub ◀─long-poll─ listener pod   ns arc-talaria
GitHub ◀─long-poll─ listener pod   ns arc-money
GitHub ◀─long-poll─ listener pod   ns arc-…            │ job queued
                                                       ▼
                                    runner pod (+ dind sidecar), gone after
                           ▲
   arc-systems: gha-runner-scale-set-controller watches AutoscalingRunnerSets
```

## Deploy

1. Create a fine-grained PAT (`values.local.yaml.example` says which
   permissions) and store it: `secrets new infra/actions-runner`.
2. `./upgrade.sh`. Installs the controller, then this chart (a namespace,
   Secret and ServiceAccount per repo), then a scale set per repo in `repos:`. Each appears under that repo's Settings → Actions →
   Runners as `arc` (Idle, until a job arrives).
3. In each repo's workflow: `runs-on: arc`. That is the whole migration.

Add a repo: append to `repos:` in values.yaml, add it to the PAT's repository
list, `./upgrade.sh`. Remove one: delete the entry, `./upgrade.sh` uninstalls
its release and namespace.

## Linux only — where Windows jobs go

Everything here is a pod, so everything here is Linux. A Windows runner pod
would need a Windows node and all ten in this cluster are Linux, and a KubeVirt
VM is not something a runner scale set can schedule. `runs-on: arc` therefore
never satisfies a Windows job.

Windows is the ordinary runner agent installed inside the `dev/win11` guest and
registered as a service — `runs-on: win11`. See that chart's README §A Windows
Actions runner. It is long-lived rather than ephemeral, which is exactly why
the same public-repo prohibition applies there, harder.

macOS has no self-hosted answer at all: no mac in the cluster, and Apple's
licensing rules out a VM. Those legs stay on GitHub's meter.

## What a job gets

`ghcr.io/actions/actions-runner` — Ubuntu with the runner, git, docker CLI,
and not much else. **No node, no python**: `ubuntu-latest` has them preinstalled
and workflows written against it lean on that silently (whatnowgg's
`node --test` did). `actions/setup-python`, `setup-node`, `setup-uv` download
what they need on first use, every job — a runner pod lives for exactly one.

It *does* ship `sudo`, passwordless for the `runner` user, so a workflow that
apt-installs system libraries works unchanged (romnas installs Qt runtime libs
that way). Verified against `ghcr.io/actions/actions-runner:latest`.

**Image builds** go to the in-cluster buildkitd, not a daemon on the runner:

```yaml
- uses: docker/setup-buildx-action@v4
  with:
    driver: remote
    endpoint: tcp://buildkitd.buildkit.svc.cluster.local:1234
```

then `docker buildx build` / `docker/build-push-action` as usual. `infra/buildkit`'s
NetworkPolicy admits the `arc-*` namespaces for this. Persistent layer cache
on buildkitd's PVC (drop any `cache-from/to: type=gha`), a memory-hungry build
lands on buildkitd's 16Gi rather than the runner's limit, and the runner pod
is not privileged. `runners.dind: true` is the fallback for a workflow that
still runs plain `docker build`/`docker run` — a privileged dind sidecar with
a cold cache every job.

**Timing-sensitive tests** will be slower than on a hosted runner: these pods
share nodes with everything else. talaria's "5000 categories in under 15ms"
came in at 17.

## Failure modes

- **Jobs sit on "Waiting for a runner"** with listeners Running: the PAT has
  expired or lost a repo. `kubectl -n arc-<repo> logs -l app.kubernetes.io/component=runner-scale-set-listener`.
- **Listener CrashLoops right after upgrade**: controller and scale set from
  different ARC versions. `upgrade.sh` pins both to `ARC_VERSION`.
- **A runner pod is Pending**: `resources.requests` don't fit; maxRunners ×
  requests is the worst case per repo.
