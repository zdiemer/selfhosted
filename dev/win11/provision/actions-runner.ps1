<#
Turn this guest into a self-hosted GitHub Actions runner.

Run by the win11 chart's provision hook at first logon (see values.yaml
section provision), or by hand from the sysprep CD on a guest that already exists.
Idempotent either way: a runner that is already configured is left alone.

WHY THIS EXISTS RATHER THAN A RUNNER POD. infra/actions-runner scales GitHub's
Linux runner as pods, which is strictly better -- ephemeral, scale-to-zero, no
state between jobs. It cannot do Windows: a Windows runner pod needs a Windows
node, and every node in this cluster is Linux. A KubeVirt VM is not something a
runner scale set can schedule either. So the Windows runner is the ordinary
agent installed inside the guest, registered as a service, long-lived.

WHAT THAT COSTS. This runner is NOT ephemeral. Its work directory, its
environment and anything a job leaves behind persist into the next job. That is
acceptable for a private repo running its own code and is the reason the Linux
side stays on pods. Do not point a public repo at it -- a pull request from a
fork would execute untrusted code on a persistent box inside this cluster.

Expected environment (set from provision.env in values):
  GITHUB_OWNER              required   e.g. zdiemer
  GITHUB_REPO               required   e.g. romnas
  GITHUB_TOKEN              required   fine-grained PAT, Administration: RW
  RUNNER_LABELS             optional   default "win11"
  RUNNER_NAME               optional   default the computer name
  RUNNER_VERSION            optional   default the latest release
  RUNNER_SERVICE_USER       optional   local account to run the service as
  RUNNER_SERVICE_PASSWORD   optional   its password
#>

$ErrorActionPreference = 'Stop'

# Invoke-WebRequest renders a progress bar per chunk in Windows PowerShell, and
# it dominates the runtime of a large download -- the runner zip goes from
# minutes to seconds with this off. Not cosmetic.
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Need($name) {
  $v = [Environment]::GetEnvironmentVariable($name)
  if ([string]::IsNullOrWhiteSpace($v)) { throw "$name is required -- set it in provision.env" }
  return $v
}

$owner  = Need 'GITHUB_OWNER'
$repo   = Need 'GITHUB_REPO'
$token  = Need 'GITHUB_TOKEN'
$labels = if ($Env:RUNNER_LABELS) { $Env:RUNNER_LABELS } else { 'win11' }
$name   = if ($Env:RUNNER_NAME)   { $Env:RUNNER_NAME }   else { $Env:COMPUTERNAME }
$root   = 'C:\actions-runner'

$api = @{
  Accept                 = 'application/vnd.github+json'
  Authorization          = "Bearer $token"
  'X-GitHub-Api-Version' = '2022-11-28'
}

# ---------------------------------------------------------------------------
# 1. Wait for the network.
#
# At first logon this script runs seconds after the virtio guest tools
# installed the NIC, and DHCP has usually not finished. Every step below is a
# web request, so failing here would look like a broken PAT rather than a race.
# ---------------------------------------------------------------------------
Write-Host '==> Waiting for network'
$deadline = (Get-Date).AddMinutes(5)
while ((Get-Date) -lt $deadline) {
  try { Invoke-RestMethod -Uri 'https://api.github.com/zen' -TimeoutSec 10 | Out-Null; break }
  catch { Start-Sleep -Seconds 5 }
}
if ((Get-Date) -ge $deadline) { throw 'No route to api.github.com after 5 minutes' }

# ---------------------------------------------------------------------------
# 2. Execution policy.
#
# A client Windows 11 defaults to `Restricted`, which refuses to run ANY
# script. That is not just an inconvenience for this file -- it breaks jobs. A
# workflow `run:` step is written to a temp .ps1 under _work\_temp and
# dot-sourced, so under Restricted every PowerShell step fails with
# UnauthorizedAccess. The hosted windows-latest image ships permissive, which
# is why no workflow ever mentions it.
#
# RemoteSigned rather than Bypass: local unsigned scripts (which is what the
# runner generates) are allowed, while anything arriving from the internet zone
# still has to be signed. LocalMachine scope so it applies to the service
# account, not just whoever ran this.
# ---------------------------------------------------------------------------
$policy = Get-ExecutionPolicy -Scope LocalMachine
if ($policy -in @('Restricted', 'Undefined', 'AllSigned')) {
  Write-Host "==> Execution policy is $policy -- setting LocalMachine to RemoteSigned"
  Set-ExecutionPolicy -Scope LocalMachine -ExecutionPolicy RemoteSigned -Force
} else {
  Write-Host "==> Execution policy is already $policy"
}

# ---------------------------------------------------------------------------
# 3. Git.
#
# NOT optional, and the reason is easy to miss: actions/checkout falls back to
# downloading a tarball over the REST API when git is absent, so a workflow
# appears to work while silently losing history, submodules and tags. The
# hosted windows-latest image ships git preinstalled and workflows assume it --
# the same trap infra/actions-runner's README documents for node and python.
# ---------------------------------------------------------------------------
if (Get-Command git -ErrorAction SilentlyContinue) {
  Write-Host "==> git already present: $(git --version)"
} else {
  Write-Host '==> Installing Git for Windows'
  # Resolved at run time rather than pinned: this runs on a fresh guest, and a
  # pin would mean installing a knowingly stale git on every rebuild.
  $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest' -Headers $api
  $asset = $rel.assets | Where-Object { $_.name -like 'Git-*-64-bit.exe' } | Select-Object -First 1
  if (-not $asset) { throw 'No 64-bit Git for Windows installer in the latest release' }
  $exe = Join-Path $Env:TEMP $asset.name
  Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $exe
  # /VERYSILENT is Inno Setup's; NOICONS keeps a build agent's desktop clean.
  Start-Process -FilePath $exe -ArgumentList '/VERYSILENT','/NORESTART','/NOCANCEL','/SP-','/NOICONS' -Wait
  $Env:PATH = "$Env:PATH;$Env:ProgramFiles\Git\cmd"
  Write-Host "    installed: $(& "$Env:ProgramFiles\Git\cmd\git.exe" --version)"
}

# ---------------------------------------------------------------------------
# 4. 7-Zip.
#
# Same trap as git, and the same root cause: windows-latest preinstalls a pile
# of tooling that workflows never think to declare. romnas locates it with
# shutil.which("7z"), so the directory has to be on the MACHINE path -- the
# service does not inherit an interactive session's environment.
# ---------------------------------------------------------------------------
$sevenZipDir = Join-Path $Env:ProgramFiles '7-Zip'
if (Get-Command 7z -ErrorAction SilentlyContinue) {
  Write-Host '==> 7z already present'
} else {
  Write-Host '==> Installing 7-Zip'
  $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/ip7z/7zip/releases/latest' -Headers $api
  $asset = $rel.assets | Where-Object { $_.name -match '^7z.*-x64\.exe$' } | Select-Object -First 1
  if (-not $asset) { throw 'No x64 7-Zip installer in the latest release' }
  $exe = Join-Path $Env:TEMP $asset.name
  Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $exe
  Start-Process -FilePath $exe -ArgumentList '/S' -Wait
}
# Idempotent, and machine scope so the runner service sees it too.
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
if ($machinePath -notlike "*$sevenZipDir*") {
  Write-Host "==> Adding $sevenZipDir to the machine PATH"
  [Environment]::SetEnvironmentVariable('Path', "$machinePath;$sevenZipDir", 'Machine')
}
$Env:PATH = "$Env:PATH;$sevenZipDir"

# ---------------------------------------------------------------------------
# 5. The runner itself.
#
# Version resolved at run time for the same reason infra/actions-runner pins
# its image to :latest -- GitHub refuses a runner more than a few releases
# behind at registration, so a pin is how a fleet silently stops taking jobs.
# Less pressing here than there, because a configured runner self-updates, but
# a rebuild months from now should still start from something current.
# ---------------------------------------------------------------------------
#
# "Already configured" is checked against the SERVER, not just the local file.
# GitHub deletes a registration that has not connected in a while, and the
# runner then fails every start with "The runner registration has been deleted
# from the server, please re-configure" while .runner still sits on disk. A
# re-run that trusts the file alone reports success and changes nothing, which
# is the least useful thing it could do. `config.cmd remove --local` drops the
# local config without needing the server to still know about it.
# ---------------------------------------------------------------------------
$configured = Test-Path (Join-Path $root '.runner')
if ($configured -and $Env:RUNNER_RECONFIGURE -eq '1') {
  Write-Host '==> RUNNER_RECONFIGURE=1 -- discarding the local config'
  $configured = $false
} elseif ($configured) {
  $known = $true
  try {
    $known = [bool]((Invoke-RestMethod -Headers $api `
      -Uri "https://api.github.com/repos/$owner/$repo/actions/runners").runners |
      Where-Object { $_.name -eq $name })
  } catch {
    # A transient API failure must not be read as "the server forgot us" -- that
    # would tear down a perfectly good runner over a blip.
    Write-Warning "Could not confirm registration with GitHub: $($_.Exception.Message)"
  }
  if (-not $known) {
    Write-Host "==> Configured locally, but $owner/$repo has no runner named $name -- reconfiguring"
    $configured = $false
  }
}
if ($configured -eq $false -and (Test-Path (Join-Path $root '.runner'))) {
  $old = Get-Service -Name 'actions.runner.*' -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($old) { Stop-Service -Name $old.Name -Force -ErrorAction SilentlyContinue }
  & (Join-Path $root 'config.cmd') remove --local
}

if ($configured) {
  Write-Host '==> Runner already configured -- leaving it alone'
} else {
  $version = if ($Env:RUNNER_VERSION) {
    $Env:RUNNER_VERSION.TrimStart('v')
  } else {
    (Invoke-RestMethod -Uri 'https://api.github.com/repos/actions/runner/releases/latest' -Headers $api).tag_name.TrimStart('v')
  }
  Write-Host "==> Installing actions-runner $version to $root"

  New-Item -ItemType Directory -Force -Path $root | Out-Null
  $zip = Join-Path $Env:TEMP "actions-runner-win-x64-$version.zip"
  if (-not (Test-Path $zip)) {
    Invoke-WebRequest -OutFile $zip `
      -Uri "https://github.com/actions/runner/releases/download/v$version/actions-runner-win-x64-$version.zip"
  }
  Expand-Archive -Path $zip -DestinationPath $root -Force

  # A registration token, not the PAT. It is what config.cmd wants, it is
  # minted from the PAT, and it expires in an hour -- which is why the PAT has
  # to be reachable from inside the guest at all rather than a token being
  # baked into the chart.
  Write-Host "==> Minting a registration token for $owner/$repo"
  $reg = Invoke-RestMethod -Method Post -Headers $api `
    -Uri "https://api.github.com/repos/$owner/$repo/actions/runners/registration-token"

  # --replace so a rebuilt guest reclaims its own name instead of piling up
  # offline runners in the repo's settings.
  $cfg = @(
    '--unattended', '--replace',
    '--url',    "https://github.com/$owner/$repo",
    '--token',  $reg.token,
    '--name',   $name,
    '--labels', $labels,
    '--work',   '_work',
    '--runasservice'
  )
  # Without an explicit account the service runs as NETWORK SERVICE, which has
  # no user profile -- and toolchain installers that a job runs (setup-uv,
  # setup-python) write into one. Running as the local admin the answer file
  # already created is what makes those steps behave like they do on a hosted
  # runner.
  if ($Env:RUNNER_SERVICE_USER) {
    $account = if ($Env:RUNNER_SERVICE_USER -match '\\') { $Env:RUNNER_SERVICE_USER } else { ".\$($Env:RUNNER_SERVICE_USER)" }
    $cfg += @('--windowslogonaccount', $account)
    if ($Env:RUNNER_SERVICE_PASSWORD) { $cfg += @('--windowslogonpassword', $Env:RUNNER_SERVICE_PASSWORD) }
  }

  Write-Host "==> config.cmd --name $name --labels $labels"
  & (Join-Path $root 'config.cmd') @cfg
  if ($LASTEXITCODE -ne 0) { throw "config.cmd exited $LASTEXITCODE" }
}

# ---------------------------------------------------------------------------
# 6. Defender.
#
# Real-time scanning of a build tree is the single largest tax on Windows CI --
# every file a compiler or an unpacker touches is scanned synchronously. The
# work directory holds nothing but checked-out source and build output.
# ---------------------------------------------------------------------------
try {
  Add-MpPreference -ExclusionPath (Join-Path $root '_work') -ErrorAction Stop
  Write-Host '==> Defender exclusion added for the work directory'
} catch {
  Write-Warning "Could not add a Defender exclusion: $($_.Exception.Message)"
}

# ---------------------------------------------------------------------------
# 7. Make sure it is actually running.
# ---------------------------------------------------------------------------
$svc = Get-Service -Name 'actions.runner.*' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $svc) { throw 'config.cmd reported success but installed no service' }
Set-Service -Name $svc.Name -StartupType Automatic
if ($svc.Status -ne 'Running') { Start-Service -Name $svc.Name }
Write-Host "==> $($svc.Name) is $((Get-Service -Name $svc.Name).Status)"
Write-Host "==> Done. The runner shows under https://github.com/$owner/$repo/settings/actions/runners as '$name'."
