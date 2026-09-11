<#
Turn this guest into a self-hosted GitHub Actions runner, for one or more repos.

Run by the win11 chart's provision hook at first logon (see values.yaml
section provision), or by hand from the sysprep CD on a guest that already exists.
Idempotent either way: a runner that is already configured is left alone.

WHY THIS EXISTS RATHER THAN A RUNNER POD. infra/actions-runner scales GitHub's
Linux runner as pods, which is strictly better -- ephemeral, scale-to-zero, no
state between jobs. It cannot do Windows: a Windows runner pod needs a Windows
node, and every node in this cluster is Linux. A KubeVirt VM is not something a
runner scale set can schedule either. So the Windows runner is the ordinary
agent installed inside the guest, registered as a service, long-lived.

WHY ONE AGENT PER REPO. A runner registers against exactly one scope. A
personal account has no org scope, so the only scope available is the repo --
there is no way to make a single agent serve romnas, vxp and zodemu. Each repo
therefore gets its own install directory under C:\actions-runner and its own
service (config.cmd names it actions.runner.<owner>-<repo>.<name>, so the names
never collide). They share the machine, the toolchains and the Defender
exclusions; GitHub does not know they are the same box.

WHAT THAT COSTS. These runners are NOT ephemeral. Their work directories, their
environment and anything a job leaves behind persist into the next job -- and
now across repos as well, which is a second reason the rule below is absolute.
That is acceptable for a private repo running its own code and is the reason the
Linux side stays on pods. Do not point a public repo at it -- a pull request
from a fork would execute untrusted code on a persistent box inside this
cluster.

Expected environment (set from provision.env in values):
  GITHUB_OWNER              required   e.g. zdiemer
  GITHUB_REPO               required   one repo, or several: "romnas,vxp,zodemu"
  GITHUB_TOKEN              required   fine-grained PAT, Administration: RW on
                                       every repo listed above
  PROVISION_TOOLCHAINS      optional   comma-separated: msvc, rust, dotnet
  RUNNER_LABELS             optional   default "win11"
  RUNNER_NAME               optional   default the computer name
  RUNNER_VERSION            optional   default the latest release
  RUNNER_SERVICE_USER       optional   local account to run the service as
  RUNNER_SERVICE_PASSWORD   optional   its password
  RUNNER_RECONFIGURE        optional   "1" discards local config and re-registers
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

# Machine scope, because a service does not inherit an interactive session's
# environment: whatever a job needs on PATH has to be on the machine's.
function Add-MachinePath($dir) {
  $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  if ($machinePath -notlike "*$dir*") {
    Write-Host "==> Adding $dir to the machine PATH"
    [Environment]::SetEnvironmentVariable('Path', "$machinePath;$dir", 'Machine')
  }
  $Env:PATH = "$Env:PATH;$dir"
}

$owner  = Need 'GITHUB_OWNER'
$token  = Need 'GITHUB_TOKEN'
# One repo or a list. Commas, spaces or both.
$repos  = (Need 'GITHUB_REPO') -split '[,\s]+' | Where-Object { $_ }
$labels = if ($Env:RUNNER_LABELS) { $Env:RUNNER_LABELS } else { 'win11' }
$name   = if ($Env:RUNNER_NAME)   { $Env:RUNNER_NAME }   else { $Env:COMPUTERNAME }
$base   = 'C:\actions-runner'
$chains = if ($Env:PROVISION_TOOLCHAINS) { $Env:PROVISION_TOOLCHAINS -split '[,\s]+' | Where-Object { $_ } } else { @() }

$api = @{
  Accept                 = 'application/vnd.github+json'
  Authorization          = "Bearer $token"
  'X-GitHub-Api-Version' = '2022-11-28'
}

Write-Host "==> Runners for: $($repos -join ', ')"
if ($chains) { Write-Host "==> Toolchains: $($chains -join ', ')" }

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
Add-MachinePath $sevenZipDir

# ---------------------------------------------------------------------------
# 5. Toolchains.
#
# These belong to the MACHINE, not to a workflow step. On a hosted runner a
# toolchain is in the image; here the box is persistent, so installing one per
# job would pay a multi-GB download forever and, for VS Build Tools, would not
# work at all inside a job's time budget. Opt in per guest with
# PROVISION_TOOLCHAINS -- a guest that only runs Python CI wants none of this.
# ---------------------------------------------------------------------------
$vswhere = Join-Path ${Env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
function Test-Msvc {
  if (-not (Test-Path $vswhere)) { return $false }
  $found = & $vswhere -products '*' -latest -prerelease `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
  return [bool]$found
}

if ($chains -contains 'msvc') {
  if (Test-Msvc) {
    Write-Host '==> MSVC build tools already present'
  } else {
    # The C++ workload, which is what supplies link.exe and the Windows SDK.
    # rustc finds them by itself (it runs the same vswhere lookup the cc crate
    # does), so nothing downstream has to source vcvars.
    Write-Host '==> Installing Visual Studio Build Tools (VC++ workload) -- this takes a while'
    $vs = Join-Path $Env:TEMP 'vs_BuildTools.exe'
    Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vs_BuildTools.exe' -OutFile $vs
    $p = Start-Process -FilePath $vs -Wait -PassThru -ArgumentList @(
      '--quiet', '--wait', '--norestart', '--nocache',
      '--add', 'Microsoft.VisualStudio.Workload.VCTools', '--includeRecommended'
    )
    # 3010 is "installed, wants a reboot" and is a success; the tools work
    # without one. Anything else is a real failure.
    if ($p.ExitCode -notin @(0, 3010)) { throw "vs_BuildTools.exe exited $($p.ExitCode)" }
    if (-not (Test-Msvc)) { throw 'vs_BuildTools.exe reported success but vswhere cannot find the VC tools' }
    Write-Host '    installed'
  }
}

if ($chains -contains 'rust') {
  # Machine-wide CARGO_HOME/RUSTUP_HOME rather than the installing user's
  # profile: the runner service may run as a different account, and a toolchain
  # it cannot see is not installed as far as CI is concerned.
  $rustRoot   = 'C:\rust'
  $cargoHome  = Join-Path $rustRoot 'cargo'
  $rustupHome = Join-Path $rustRoot 'rustup'
  [Environment]::SetEnvironmentVariable('CARGO_HOME',  $cargoHome,  'Machine')
  [Environment]::SetEnvironmentVariable('RUSTUP_HOME', $rustupHome, 'Machine')
  $Env:CARGO_HOME  = $cargoHome
  $Env:RUSTUP_HOME = $rustupHome

  if (Test-Path (Join-Path $cargoHome 'bin\rustup.exe')) {
    Write-Host '==> rustup already present'
  } else {
    if (-not (Test-Msvc)) {
      # Not fatal: rustup installs fine, and the failure would only show up at
      # the first link. Say so loudly now rather than in a release job.
      Write-Warning 'Installing rust without the MSVC build tools -- an MSVC-target build will fail to link. Add msvc to PROVISION_TOOLCHAINS.'
    }
    Write-Host '==> Installing rustup (stable-x86_64-pc-windows-msvc)'
    New-Item -ItemType Directory -Force -Path $rustRoot | Out-Null
    $init = Join-Path $Env:TEMP 'rustup-init.exe'
    Invoke-WebRequest -OutFile $init `
      -Uri 'https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe'
    # --no-modify-path because rustup would edit the installing user's PATH,
    # and the service account is what matters here; Add-MachinePath below does
    # the right one. -y takes the defaults and skips the MSVC prompt.
    & $init -y --no-modify-path --profile minimal --default-toolchain stable-x86_64-pc-windows-msvc
    if ($LASTEXITCODE -ne 0) { throw "rustup-init exited $LASTEXITCODE" }
    # cargo writes into CARGO_HOME (registry cache, installed binaries) as
    # whoever runs the build, so the tree has to be writable by more than the
    # account that installed it. S-1-5-32-545 is BUILTIN\Users, by SID because
    # the group name is localised.
    & icacls $rustRoot /grant '*S-1-5-32-545:(OI)(CI)M' /T /Q | Out-Null
  }
  Add-MachinePath (Join-Path $cargoHome 'bin')
}

if ($chains -contains 'dotnet') {
  $dotnetDir = Join-Path $Env:ProgramFiles 'dotnet'
  $installed = $false
  if (Test-Path (Join-Path $dotnetDir 'dotnet.exe')) {
    $installed = [bool](& (Join-Path $dotnetDir 'dotnet.exe') --list-sdks | Where-Object { $_ -like '8.*' })
  }
  if ($installed) {
    Write-Host '==> .NET 8 SDK already present'
  } else {
    Write-Host '==> Installing the .NET 8 SDK'
    $ps1 = Join-Path $Env:TEMP 'dotnet-install.ps1'
    Invoke-WebRequest -Uri 'https://dot.net/v1/dotnet-install.ps1' -OutFile $ps1
    # Execution policy is RemoteSigned by now, so a file carrying a zone marker
    # would be refused. Invoke-WebRequest does not normally write one; this
    # costs nothing and removes the question.
    Unblock-File -Path $ps1 -ErrorAction SilentlyContinue
    & $ps1 -Channel '8.0' -InstallDir $dotnetDir
    if (-not (Test-Path (Join-Path $dotnetDir 'dotnet.exe'))) { throw 'dotnet-install.ps1 installed no dotnet.exe' }
  }
  [Environment]::SetEnvironmentVariable('DOTNET_ROOT', $dotnetDir, 'Machine')
  $Env:DOTNET_ROOT = $dotnetDir
  Add-MachinePath $dotnetDir
}

# ---------------------------------------------------------------------------
# 6. Migrate the old single-runner layout.
#
# Before this script took a repo list the one runner lived directly in
# C:\actions-runner. That is now where the per-repo directories go, so the old
# install has to be retired -- and it cannot simply be moved, because the
# service's binPath points into it. Tear it down locally (`remove --local`
# needs no server round trip) and let the loop below re-register it under its
# own directory; --replace there means it reclaims the same runner name rather
# than leaving an offline duplicate in the repo's settings.
# ---------------------------------------------------------------------------
if (Test-Path (Join-Path $base '.runner')) {
  Write-Host '==> Retiring the old single-repo runner in C:\actions-runner'
  Get-Service -Name 'actions.runner.*' -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Service -Name $_.Name -Force -ErrorAction SilentlyContinue }
  & (Join-Path $base 'config.cmd') remove --local
  # Keep the tree: _work holds caches worth nothing but costing a re-download,
  # and a stale bin\ next to the new directories is harmless. Only the config
  # had to go.
  Rename-Item -Path $base -NewName "actions-runner.pre-multirepo.$(Get-Date -Format yyyyMMddHHmmss)"
}

New-Item -ItemType Directory -Force -Path $base | Out-Null

# ---------------------------------------------------------------------------
# 7. One runner per repo.
# ---------------------------------------------------------------------------
foreach ($repo in $repos) {
  Write-Host ''
  Write-Host "=== $owner/$repo ==="
  $root = Join-Path $base $repo

  # "Already configured" is checked against the SERVER, not just the local
  # file. GitHub deletes a registration that has not connected in a while, and
  # the runner then fails every start with "The runner registration has been
  # deleted from the server, please re-configure" while .runner still sits on
  # disk. A re-run that trusts the file alone reports success and changes
  # nothing, which is the least useful thing it could do.
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
      # A transient API failure must not be read as "the server forgot us" --
      # that would tear down a perfectly good runner over a blip.
      Write-Warning "Could not confirm registration with GitHub: $($_.Exception.Message)"
    }
    if (-not $known) {
      Write-Host "==> Configured locally, but $owner/$repo has no runner named $name -- reconfiguring"
      $configured = $false
    }
  }
  if ($configured -eq $false -and (Test-Path (Join-Path $root '.runner'))) {
    $old = Get-Service -Name "actions.runner.$owner-$repo.*" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($old) { Stop-Service -Name $old.Name -Force -ErrorAction SilentlyContinue }
    & (Join-Path $root 'config.cmd') remove --local
  }

  if ($configured) {
    Write-Host '==> Runner already configured -- leaving it alone'
  } else {
    # Version resolved at run time for the same reason infra/actions-runner
    # pins its image to :latest -- GitHub refuses a runner more than a few
    # releases behind at registration, so a pin is how a fleet silently stops
    # taking jobs. Less pressing here, because a configured runner
    # self-updates, but a rebuild months from now should still start current.
    $version = if ($Env:RUNNER_VERSION) {
      $Env:RUNNER_VERSION.TrimStart('v')
    } else {
      (Invoke-RestMethod -Uri 'https://api.github.com/repos/actions/runner/releases/latest' -Headers $api).tag_name.TrimStart('v')
    }
    Write-Host "==> Installing actions-runner $version to $root"

    New-Item -ItemType Directory -Force -Path $root | Out-Null
    # One download serves every repo: the zip is cached in TEMP by version.
    $zip = Join-Path $Env:TEMP "actions-runner-win-x64-$version.zip"
    if (-not (Test-Path $zip)) {
      Invoke-WebRequest -OutFile $zip `
        -Uri "https://github.com/actions/runner/releases/download/v$version/actions-runner-win-x64-$version.zip"
    }
    Expand-Archive -Path $zip -DestinationPath $root -Force

    # A registration token, not the PAT. It is what config.cmd wants, it is
    # minted from the PAT, and it expires in an hour -- which is why the PAT
    # has to be reachable from inside the guest at all rather than a token
    # being baked into the chart. It is per repo, so this is minted per repo.
    Write-Host "==> Minting a registration token for $owner/$repo"
    $reg = Invoke-RestMethod -Method Post -Headers $api `
      -Uri "https://api.github.com/repos/$owner/$repo/actions/runners/registration-token"

    # --replace so a rebuilt guest reclaims its own name instead of piling up
    # offline runners in the repo's settings. The same $name in every repo is
    # fine: a runner name only has to be unique within its scope, and every
    # scope here is a different repo.
    $cfg = @(
      '--unattended', '--replace',
      '--url',    "https://github.com/$owner/$repo",
      '--token',  $reg.token,
      '--name',   $name,
      '--labels', $labels,
      '--work',   '_work',
      '--runasservice'
    )
    # Without an explicit account the service runs as NETWORK SERVICE, which
    # has no user profile -- and toolchain installers that a job runs
    # (setup-uv, setup-python, setup-dotnet) write into one. Running as the
    # local admin the answer file already created is what makes those steps
    # behave like they do on a hosted runner.
    if ($Env:RUNNER_SERVICE_USER) {
      $account = if ($Env:RUNNER_SERVICE_USER -match '\\') { $Env:RUNNER_SERVICE_USER } else { ".\$($Env:RUNNER_SERVICE_USER)" }
      $cfg += @('--windowslogonaccount', $account)
      if ($Env:RUNNER_SERVICE_PASSWORD) { $cfg += @('--windowslogonpassword', $Env:RUNNER_SERVICE_PASSWORD) }
    }

    Write-Host "==> config.cmd --name $name --labels $labels"
    Push-Location $root
    try {
      & (Join-Path $root 'config.cmd') @cfg
      if ($LASTEXITCODE -ne 0) { throw "config.cmd exited $LASTEXITCODE" }
    } finally { Pop-Location }
  }

  # Real-time scanning of a build tree is the single largest tax on Windows CI
  # -- every file a compiler or an unpacker touches is scanned synchronously.
  # The work directory holds nothing but checked-out source and build output.
  try {
    Add-MpPreference -ExclusionPath (Join-Path $root '_work') -ErrorAction Stop
    Write-Host '==> Defender exclusion added for the work directory'
  } catch {
    Write-Warning "Could not add a Defender exclusion: $($_.Exception.Message)"
  }

  # config.cmd names the service after the scope, so this matches exactly one.
  $svc = Get-Service -Name "actions.runner.$owner-$repo.*" -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $svc) { throw "config.cmd reported success but installed no service for $owner/$repo" }
  Set-Service -Name $svc.Name -StartupType Automatic
  if ($svc.Status -ne 'Running') { Start-Service -Name $svc.Name }
  Write-Host "==> $($svc.Name) is $((Get-Service -Name $svc.Name).Status)"
  Write-Host "==> Shows under https://github.com/$owner/$repo/settings/actions/runners as '$name'"
}

Write-Host ''
Write-Host "==> Done. $($repos.Count) runner(s): $($repos -join ', ')"
