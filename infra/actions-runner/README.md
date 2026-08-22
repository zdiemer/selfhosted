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

## What a job gets

`ghcr.io/actions/actions-runner` — Ubuntu with the runner, git, docker CLI,
and not much else. `actions/setup-python`, `setup-node`, `setup-uv` download
what they need on first use (no hosted toolcache here, so the first run of each
is a little slower; later runs hit the runner image's cache only for as long as
that pod lives, i.e. one job). A docker daemon is present via the dind sidecar
so `docker build` and `docker/build-push-action` work unchanged.

Worth doing next, per workflow: `docker/setup-buildx-action` with
`driver: remote` and `endpoint: tcp://buildkitd.buildkit.svc.cluster.local:1234`
so image builds use the in-cluster buildkitd and its persistent layer cache
instead of a cold dind every job. Then `runners.dind` can go to false and the
runner pod stops being privileged.

## Failure modes

- **Jobs sit on "Waiting for a runner"** with listeners Running: the PAT has
  expired or lost a repo. `kubectl -n arc-<repo> logs -l app.kubernetes.io/component=runner-scale-set-listener`.
- **Listener CrashLoops right after upgrade**: controller and scale set from
  different ARC versions. `upgrade.sh` pins both to `ARC_VERSION`.
- **A runner pod is Pending**: `resources.requests` don't fit; maxRunners ×
  requests is the worst case per repo.
