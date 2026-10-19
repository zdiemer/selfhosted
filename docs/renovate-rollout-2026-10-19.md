# Renovate rollout — 2026-10-19

The sweep covered 46 open Renovate PRs across 11 repositories: 43 merged and
three incompatible pin proposals closed. No open Renovate PRs remained after
this inventory was processed. Dependency Dashboard issues remain open because
they are ongoing Renovate control surfaces; approval-gated and rate-limited
future proposals were not bulk-approved. Unrelated human PRs and legacy
Dependabot PRs were left alone.

## Deployed applications

| Repository/application | Result | Validation |
| --- | --- | --- |
| selfhosted / Authelia | 4.39.28, schema migration completed | Ready, public HTTP 200 |
| whatnowgg | v1.0.106 on the production droplet; matching cluster image | 599 backend and 133 frontend tests; both public endpoints HTTP 200 |
| gamedex | Python 3.15, FastAPI 0.143.3, Uvicorn 0.54.0 | Image build; live version checks and `/api/health` 200 |
| sms-relay | Python 3.15, uv 0.13.1, refreshed native dependency lock | 36 tests; Ready and live runtime version checks |
| talaria | Seven dependency PRs, including Next, PyJWT and gRPC security updates | 1,943 backend and 596 frontend tests; lint/types; all workloads Ready; both ingress endpoints HTTP 200 |
| kelsey-dot-green | Astro 7.4.0 and Node 26.12.0 build runtime | Local build, successful deployment CI, git-sync refresh, public HTTP 200 |
| rachelfreeman | Next 16.3.8, Payload and all plugins 3.90.0, Sharp 0.35.5, Stripe 22.6.2, Node 22.23.3 | Full image builds and CI; two Ready app replicas and public HTTP 200 |
| money | Vite 6.4.4, Node 22.23.3, uv 0.13.1 and refreshed Python dependency lock; Python 3.14 retained | 278 tests; full image build; live Python 3.14.8, HTTP 200, and Camoufox successfully opened about:blank |
| selfhosted / claude-bridge | MCP 1.30.0 | 19 tests, image build, Ready, Minecraft log stream/whitelist refresh active |
| selfhosted / claude-workspace | v40: MCP SDK 1.31.0, Bun 1.4.3, Tailscale 1.104.1, Helm 3.22.1, signal-cli 0.14.9 and other tools | Disposable image pod passed 171 gateway tests and binary version checks; delegated workspace Ready on v40 |

Talaria's frontend image now copies `.python-version` before generating its
bindings. Without that pin, `uv` selected Python 3.15 in an otherwise Python
3.14 application build and failed on native dependencies. Eight pre-existing
type errors/stale annotations were repaired before merging its PRs.

Rachel's old GHCR pull credential returned 403. The first attempted deployment
rolled back without replacing the existing healthy replicas. The successful
rollout uses `registry.zachd.duckdns.org/zdiemer/rachelfreeman:renovate-20261019`.
CI now publishes to that registry as well as GHCR, using repository-scoped
GitHub Actions secrets. Its PostgreSQL pre-upgrade dump is private at
`~/.local/state/renovate-backups/rachel-pre-payload390.sql.gz`; gzip integrity
was checked. Interactive checkout/payment and authenticated admin edits were
not exercised.

## Compatibility decisions

- Smite #23: closed the Python 3.15 proposal. Pipfile/Pipfile.lock require 3.11;
  all derived ML images must migrate together. Renovate remains within 3.11.
- RomNAS #12: closed the proposal replacing `>=3.11` with `==3.15.0`. Preserve
  the supported Python range instead of turning it into a runtime pin.
- Rachel #10: closed the proposal pinning the Node compatibility range to 26.
  The Dockerfile owns its installed Node runtime; engine-range pins are disabled.
- Money #16 was merged with the other PRs, but its Python 3.15 runtime was
  subsequently reverted: a real Camoufox launch fails in fpgen 1.3.0 with
  `binascii.Error: Incomplete Base85 group`. Python image updates are constrained
  below 3.15 until upstream fingerprint decoding supports it. The other upgrades
  remain. Native-library lock refreshes and deterministic test corrections stay.
- Workspace kubectl stays at 1.35.9, matching K3s; the proposed 1.37.1 exceeds
  Kubernetes' supported one-minor client skew. Buildctl stays at 0.32.2 with
  the deployed BuildKit daemon. Renovate bounds prevent independent incompatible
  bumps. See https://kubernetes.io/releases/version-skew-policy/.
- Apartment Watch is retired. Its Camoufox PR was merged and its image built,
  but no application was resurrected; further Renovate proposals are disabled.

## Libraries, CI and repository state

RomNAS merged gdown, cryptography, Pydantic and Ruff updates. Local lint/types
and 2,083 tests passed (21 skipped); Linux CI passed. Its Windows CI leg was
still queued during the sweep, so Windows validation was not claimed.
SimpleScraper merged Python 3.15 and refreshed Playwright/greenlet/aiohttp locks;
its environment resolves and source compiles. Neither has a standalone cluster
service to restart.

The workspace gateway passed type checks and 171 tests both locally and inside
the new image. Selfhosted checks passed: 51 chart lints, 27 renders, availability
policy for 42 charts (12 require secret values), and 35 appVersion comparisons.
Submodule pins were advanced using `scripts/sync-submodules.sh --commit` after
checking deployed image tags; Talaria's SHA was verified separately because its
chart uses `latest`.

## Primary workspace restart

The primary workspace's old Helm revision 72 had been stuck `pending-upgrade`
since October 18. Its rendered Deployment spec was checked against the Ready
live Deployment before marking that revision deployed and the previous revision
superseded. No workload was restarted during that metadata repair.

Both workspace upgrades preserve their existing Helm chart/configuration and
change only the image, avoiding accidental enablement of newer chart features
in the delegated profile. Signal state backups are on the workspace PVCs under
`~/.local/state/upgrade-backups/signal-pre-0149-20261019.tgz` where state exists.

The primary self-restart is owned by an independent Job so killing the current
workspace cannot leave Helm pending again. Inspect its durable verification:

```sh
kubectl -n claude logs job/renovate-workspace-rollout
kubectl -n claude get job renovate-workspace-rollout
helm status claude-workspace -n claude
```

The Job waits for the `renovate-workspace-rollout` ConfigMap's `go` flag, then
performs an atomic upgrade to v40 and checks rollout and the running Bun version.
A successful run ends with `VERIFIED: primary workspace v40 rollout completed`.
The disposable image-check Job and rollout Job are automatically removed after
their retention periods. Delegated local secret files are now excluded from
future Helm chart packages as well as the primary local secret file.
