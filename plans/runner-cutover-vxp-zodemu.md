# runner-cutover-vxp-zodemu

**The code is committed; the cutover is not done.** vxp and zodemu are private,
so every Actions minute they burn is billed, and both were pointed at self-hosted
runners on 2026-09-11 — vxp's Linux leg at the ARC scale sets, vxp's Windows leg
and zodemu's entire release job at the agent in the `dev/win11` guest.

Status: **blocked on two things that need a human at a keyboard** — a
fine-grained PAT can only be edited in GitHub's web UI, and the win11 guest
needs an RDP session to run its provisioning script. Everything either of those
gates is listed below, in order.

## What already shipped

| Repo | Commit | What it did |
|---|---|---|
| `selfhosted` | `5654c32` | `GITHUB_REPO` takes a list; the guest installs one runner per repo under `C:\actions-runner\<repo>`; `PROVISION_TOOLCHAINS` installs MSVC / rustup / .NET 8; `vxp` added to `infra/actions-runner/values.yaml` |
| `vxp` | `402e5cd` | CI matrix is now `[arc, win11, macos-latest]` |
| `zodemu` | `0db5d34` | release job `runs-on: win11`, `pwsh` → `powershell`, `gh release` → `softprops/action-gh-release` |

The vault entry is already updated too: `op://homelab/dev-win11` carries
`GITHUB_REPO: "romnas,vxp,zodemu"` and `PROVISION_TOOLCHAINS: "msvc,rust,dotnet"`.
Nothing about the secret needs touching again.

**Until the steps below run, both repos' workflows queue forever.** A job asking
for a `runs-on:` label no runner offers is not an error — GitHub holds it for 24
hours and then cancels it. There is no failure notification to look for.

## Step 1 — add `vxp` to the PAT (blocked on the GitHub web UI)

One fine-grained PAT serves both ARC and the guest (verified: same token,
sha256 fingerprint `b8d6787a87f5`, in `op://homelab/infra-actions-runner` and
`op://homelab/dev-win11`). It can already mint registration tokens for `romnas`
and `zodemu` — HTTP 201 for both — and **404s on `vxp`**, because `vxp` is not in
its "Only select repositories" list.

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained
tokens → the runner token → Repository access → add `zdiemer/vxp`. Permissions
stay as they are: Administration **Read and write** (that is what runner
registration needs) plus Metadata Read.

There is no API for this. Fine-grained PAT repository lists are UI-only, which
is why this step cannot be automated from the pod.

Verify, from anywhere with the token:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $TOK" -H 'Accept: application/vnd.github+json' \
  https://api.github.com/repos/zdiemer/vxp/actions/runners/registration-token
```

`201` means step 2 will work. `404` means the list did not save.

## Step 2 — create the `arc-vxp` scale set (needs step 1)

```sh
eval "$(bash ~/code/selfhosted/scripts/op-session.sh ensure)"
~/code/selfhosted/infra/actions-runner/upgrade.sh
```

This is idempotent across the existing scale sets; the only new release is
`arc-vxp` in namespace `arc-vxp`. Confirm a listener appears and stays `Running`:

```sh
kubectl get pods -n arc-vxp
```

A listener that crash-loops is the PAT: it long-polls with that token, and a
404 on the repo shows up as a restart loop, not a helm failure.

Running this before step 1 leaves a broken release behind, so don't.

## Step 3 — reprovision the win11 guest (blocked on RDP)

The guest currently runs **one** runner, registered to `romnas` only. This step
retires it and registers three.

```sh
eval "$(bash ~/code/selfhosted/scripts/op-session.sh ensure)"
~/code/selfhosted/dev/win11/upgrade.sh     # new provision.ps1 into the sysprep Secret
virtctl restart win11 -n dev               # the CD is rebuilt at every VM start
```

Then, **inside the guest over RDP** (the script auto-runs at first logon only,
so on an existing guest it has to be started by hand):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\provision.ps1
```

It takes a while — the VS Build Tools install is most of it. What it does, in
order: stops the old service, `config.cmd remove --local`, renames
`C:\actions-runner` to `actions-runner.pre-multirepo.<timestamp>`, installs the
toolchains, then registers one runner per repo.

Verify in the guest:

```powershell
Get-Service -Name 'actions.runner.zdiemer-*' | Select-Object Name, Status
rustup --version; dotnet --version
```

Three services, all `Running`. A runner registers against exactly one scope and
a personal account has no org scope, which is why it is three services and not
one; `config.cmd` names them `actions.runner.<owner>-<repo>.<name>`, so they
cannot collide.

If `virtctl restart` happens without step 3's `upgrade.sh` first, the guest
comes back with the **old** provision.ps1 on `D:` and nothing changes.

## Step 4 — prove it

- vxp: push anything, or re-run CI. All three legs should go green;
  `arc` and `win11` are self-hosted, `macos-latest` is still metered and that is
  deliberate — macOS has no self-hosted answer here.
- zodemu: the release workflow is `workflow_dispatch` + tag. **Do not cut a real
  tag as the test.** Dispatch it against an existing tag first; if rustup is
  missing the toolchain step throws with a pointer to this file's step 3 rather
  than failing halfway through a build.

## When this is done

Delete this file. The durable parts already live in `dev/win11/README.md` (one
agent per repo, the toolchain table, how to re-run provisioning on an existing
guest, and the list of ways the guest is not `windows-latest`) and in the
comments in `infra/actions-runner/values.yaml` — including why zodemu is
deliberately *absent* from the ARC list.
