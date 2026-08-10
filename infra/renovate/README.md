# renovate

Self-hosted [Renovate](https://docs.renovatebot.com/) as a weekly CronJob
(Saturday 05:30). It opens version-bump PRs across the zdiemer repos; a human
merges; each project's `upgrade.sh` deploys. Renovate never touches the
cluster — it only writes git.

Why self-hosted rather than the Mend-hosted GitHub App: the app means a third
party with write access to every private repo (including `money`). The runner
here stays on the cluster, and the operational cost is one small chart in a
repo that already has thirty of them.

## How configuration is split

- **This chart** owns *where and when* Renovate runs: the schedule, the
  explicit repository list (`autodiscover: false` — onboarding a repo is a
  values.yaml edit, visible in git), and the PAT.
- **`renovate/default.json`** (repo root) owns *how updates behave*
  everywhere: rate limits, 3-day soak (14 for DB images), majors gated behind
  the dependency dashboard, submodules and `ghcr.io/zdiemer/*` off-limits.
  Other repos extend it as `github>zdiemer/selfhosted//renovate/default`.
- **Each repo's `renovate.json`** adds repo-specific managers — this repo's
  covers the `# renovate:` annotation comments on `CHART_VERSION=` pins,
  Dockerfile `ARG`s, combined `image:` strings, and Chart.yaml `appVersion`.

## Deliberately unmanaged

minecraft's MODRINTH/MODS block (unsortable IDs, moves with the client pack),
`claude-code@latest`, `OP_VERSION`/`BAKERY_REF`, kelsey-green's git-sync
`deploy` branch, submodule pointers (`scripts/sync-submodules.sh` owns those),
and every first-party `ghcr.io/zdiemer/*` image.

## Secrets

`values.local.yaml` (gitignored, `op://homelab/infra-renovate`):

```yaml
github:
  pat: github_pat_...
```

Fine-grained PAT scoped to the repos in `repositories:`, permissions:
Contents RW, Pull requests RW, Issues RW (dependency dashboard),
Workflows RW (action bumps), Metadata R. It is NOT the laptop's `gh` token.

## Rollout / operations

- `dryRun: "full"` in values.yaml logs what would happen without opening PRs.
  Flip to `""` once the detection log looks right.
- Manual run: `kubectl -n renovate create job --from=cronjob/renovate renovate-manual`
- A missed/failed run is visible as a stale dependency dashboard and a quiet
  Saturday; check `kubectl -n renovate logs job/...`.
