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
| selfhosted / claude-workspace | v40: MCP SDK 1.31.0, Bun 1.4.3, Tailscale 1.104.1, Helm 3.22.1, signal-cli 0.14.9 and other tools | Disposable image pod passed 171 gateway tests and binary version checks; both workspaces Ready on v40 after Signal schema-marker repair |

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
still queued at the follow-up check: its self-hosted `WIN11` runner is offline
although the KubeVirt guest is Running. The Windows service is configured for
automatic startup but cannot log on: Service Control Manager event 7038 reports
that its local account password has expired. A service-start attempt confirmed
the failure. At that checkpoint no account policy was changed and Windows validation
was not claimed. The subsequent CI repair is recorded below.
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

## Post-restart follow-up

The independent rollout Job completed and Helm revision 73 is deployed. A
follow-up found the Signal sidecar crash-looping while the other seven workspace
containers and the delegated workspace were healthy. Binary-version and gateway
unit checks had not exercised the persisted Signal account migration.

Signal's SQLite database passed `integrity_check`, but `user_version` was 28
while all version 29 and 30 changes already existed. The 0.14.9 migration tried
to add `sticker.position` again and exited. Compared against the upstream
[AccountDatabase migration](https://github.com/AsamK/signal-cli/blob/v0.14.9/lib/src/main/java/org/asamk/signal/manager/storage/AccountDatabase.java),
the repair verified both integer sticker columns and their defaults, both BLOB
columns, the unique storage-ID constraint, and the recipient PNI-signature
column. It also verified version 31's blocked-at columns were absent.

A consistent SQLite backup was saved with mode 0600 at
`~/.local/state/upgrade-backups/signal-before-schema-marker-repair-20261019.db`.
An exclusive transaction corrected only the version marker to 30, leaving the
normal application migration responsible for version 31. No account records
were deleted and no Signal relinking was required.

The next automatic container restart completed the migration to schema 31;
`integrity_check` remained `ok`. The primary workspace returned to 8/8 Ready,
and gateway logs confirmed its connection to the Signal daemon. The delegated
workspace remained 1/1 Ready without restarts. All ten nodes are Ready, no
Deployment is below its requested available replica count, and the public
Talaria, WhatNow and Kelsey endpoints returned HTTP 200 at the follow-up.
All 11 repositories matched their upstream branches before this documentation
commit; a fresh GitHub search returned zero open Renovate-labelled PRs.

## CI follow-up

Talaria's latest CI run [39208458732](https://github.com/zdiemer/talaria/actions/runs/39208458732)
passed both backend and frontend jobs. Kelsey's latest build/publish run
[39208243995](https://github.com/zdiemer/kelsey-dot-green/actions/runs/39208243995)
also passed. The failure notifications corresponded to earlier commits: Talaria's
eight type errors and Kelsey's missing `libatomic.so.1` were already fixed.

For RomNAS, the Windows runner's actual local service account had expired.
Disabled password aging for that account, retaining its existing password, and
started the runner service. GitHub reported it online and immediately dispatched
the queued Windows job. Provisioning now reconciles this setting for local
runner service accounts on existing installations too; domain and built-in
accounts and machine-wide policy are excluded. The new PowerShell block passed
the Windows parser and was executed successfully on the guest. The prevention
fix is committed as `61d74df` in selfhosted.

RomNAS CI run [39208561396](https://github.com/zdiemer/romnas/actions/runs/39208561396)
subsequently completed successfully on both Windows and Linux, including lint,
types, configuration validation, pytest and CLI smoke tests. The Windows suite
ran for roughly 19 minutes, consistent with the previous successful run; it was
slow rather than stalled. All three repositories' latest CI runs are green.

## Talaria CI result-reporting investigation

A later run, [39278429663](https://github.com/zdiemer/talaria/actions/runs/39278429663),
remained `in_progress` after its runners exited. Retrieved job logs confirmed
1,943 backend and 596 frontend tests passed, followed by cleanup. Normal and
force cancellation said the run was not running/completed, while rerun returned
"already running". The API responses were inconsistent.

Added `workflow_dispatch` in Talaria commit `49ad790`, allowing independent
manual validation runs. Its push triggered fresh run
[39281760900](https://github.com/zdiemer/talaria/actions/runs/39281760900).
Captured both runner logs before ARC cleaned them up. Both explicitly reported
`Job result after all job steps finish: Succeeded`, submitted job completion
with result `Succeeded`, and exited with code 0 at 16:01 UTC. The broker GET
cancellation warnings occurred during shutdown after successful completion.
REST and GraphQL still reported the fresh jobs as queued at the final check.

Evidence points to GitHub result-reporting inconsistency; no execution failure,
OOM, or premature ARC deletion was found. This does not establish a general
GitHub outage. The external check status remains unresolved, despite successful
execution. Private diagnostics are preserved under
`~/.cache/talaria-ci-diagnostics/`; no runner logs or credentials are committed.

## Submodule pin follow-up

Updated Talaria from `30dd048` to `49ad790` (bounded BuildKit cache and manual CI
trigger). Its shell script passes `bash -n`; the live builder already has the
24 GiB cache volume and matching garbage-collection settings.

Updated Money's nested SimpleScraper pin from `6d43cef` to `ea6bee1`, recorded in
Money commit `9d5db86`. SimpleScraper's source is unchanged; its standalone build
and lockfile updates do not change Money's Python 3.14 runtime. All 278 Money
backend tests passed, as did chart lint and the lightweight image build.

Runtime validation exposed an existing Chromium mismatch: Camoufox changed
Playwright after Chromium was installed. Money now installs Chromium after all
Python browser dependencies. Built and deployed
`submodule-20261019-browserfix` (image index digest
`sha256:f053a30d2c3ea151c44c9a98f88c95e689cabd4e8533c01d77abfe9428ffa472`),
Helm revision 111. Chromium and Camoufox both launched and rendered a local test
page under the production security settings, first in an isolated pod and then
in the deployed pod. Money is Ready with HTTP 200 on `/api/health`.

A final remote check found a newer Smitele commit, `36ac9a6`, published after the
initial pin audit. That application change is outside these two pin updates;
its recorded revision remains `194b754` pending separate rollout validation.

## Epic wishlist recovery

Gamedex commit `18d8c28` replaces the old Epic Store GraphQL wishlist request
with the current frontend's platform REST API, using the existing launcher
OAuth token and the already-installed `curl_cffi` browser TLS client. Ordinary
`requests` received a Cloudflare 403; the legacy GraphQL resolver returned
`notLoggedIn` even after establishing a website session. The platform API
returned the account's complete 10-item wishlist; titles resolve through the
Epic catalog API. No new credentials or persistent browser cookies are needed.

Invalid, partial, or failed snapshots now raise a sanitized wishlist-stage
error, preserving cached entries instead of replacing them with an empty list.
Eleven regression tests pass, including preservation after a second-page
failure and continued execution of other sync stages. The tests also run in
CI; [run 39306225691](https://github.com/zdiemer/gamedex/actions/runs/39306225691)
passed.

Deployed `epic-wishlist-20261019`, Helm revision 590, image index digest
`sha256:3ef13936d21bd7d0e54bee494d2decd38f6b70cface42647cbd8745d726c4588`.
The new pod is Ready and `/api/health` returns HTTP 200. Its normal background
sync saved all 10 wishlist entries and completed at 18:55 UTC with account
status `linked` and no error. Advanced the selfhosted Gamedex pin accordingly.
