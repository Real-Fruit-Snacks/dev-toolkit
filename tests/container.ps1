<#
.SYNOPSIS
  Installs a release inside a Windows Server Core container, as the built-in non-admin user
  (ContainerUser), with no network at all, then runs tests\smoke.ps1 the same way.

    .\tests\container.ps1 -Kit D:\kit -Groups core,cpp,data
    .\tests\container.ps1 -Kit D:\kit -Groups core -StrictPolicy     # group policy says AllSigned during install
    .\tests\container.ps1 -Kit D:\kit -Groups core -Uninstall         # also prove uninstall cleans up

  -Kit is a folder with the release files (dtk.exe, install.cmd, dev-toolkit.json, SHA256SUMS.txt and the
  assets the groups need). Needs Docker with Windows containers (GitHub's windows-2022 runners have it).
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$Kit,
  [string]$Groups = 'core',
  [ValidateSet('auto', 'scoop', 'portable')][string]$Route = 'auto',
  [string]$ExpectRoute,
  [switch]$StrictPolicy,
  [switch]$Uninstall,
  [string]$Image = 'mcr.microsoft.com/windows/servercore:ltsc2022',
  [string]$Name = 'dtk-test'
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$Kit = (Resolve-Path $Kit).Path
function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Exec([string]$user, [string[]]$cmd) {
  $a = @('exec', '-e', 'DTK_NO_PAUSE=1')
  if ($user) { $a += @('-u', $user) }
  $a += $Name
  & docker @a @cmd | Out-Host   # to the console, so the function returns only the exit code
  return $LASTEXITCODE
}

docker rm -f $Name 2>$null | Out-Null
Step "container $Name (no network)"
docker run -d --name $Name --network none --storage-opt size=120G `
  -v "${Kit}:C:\mnt\kit:ro" -v "$repo\tests:C:\mnt\tests:ro" -v "$repo\config:C:\mnt\config:ro" `
  $Image ping -t localhost | Out-Null
if ($LASTEXITCODE) { throw 'docker run failed' }
$rc = 0
try {
  Exec $null @('cmd', '/c', 'ping -n 1 -w 1000 1.1.1.1 >nul && echo NETWORK IS UP && exit 1 || echo no network, as intended') | Out-Null
  Exec 'ContainerUser' @('cmd', '/c', 'whoami & whoami /groups | findstr /c:"S-1-5-32-544" && echo ADMIN && exit 1 || echo not an administrator, as intended')
  if ($LASTEXITCODE) { throw 'ContainerUser is an administrator?' }

  if ($StrictPolicy) {
    Step 'group policy: PowerShell scripts must be signed (AllSigned)'
    Exec $null @('reg', 'add', 'HKLM\SOFTWARE\Policies\Microsoft\Windows\PowerShell', '/v', 'EnableScripts', '/t', 'REG_DWORD', '/d', '1', '/f') | Out-Null
    Exec $null @('reg', 'add', 'HKLM\SOFTWARE\Policies\Microsoft\Windows\PowerShell', '/v', 'ExecutionPolicy', '/t', 'REG_SZ', '/d', 'AllSigned', '/f') | Out-Null
  }

  Step "install.cmd --groups $Groups as ContainerUser"
  $args_ = @('cmd', '/c', "C:\mnt\kit\install.cmd --groups $Groups" + $(if ($Route -ne 'auto') { " --route $Route" } else { '' }))
  $installRc = Exec 'ContainerUser' $args_
  Write-Host "install exit code: $installRc"
  if ($installRc -ne 0) { $rc = 1 }

  if ($ExpectRoute) {
    $rec = docker exec -u ContainerUser $Name cmd /c 'type %LOCALAPPDATA%\dev-toolkit\installed.json' | Out-String
    if ($rec -notmatch "`"route`":\s*`"$ExpectRoute`"") { Write-Host "FAIL: expected the $ExpectRoute route"; $rc = 1 } else { Write-Host "ok: took the $ExpectRoute route" }
  }

  if ($StrictPolicy) {
    # lift it again so the test script itself can run
    Exec $null @('reg', 'delete', 'HKLM\SOFTWARE\Policies\Microsoft\Windows\PowerShell', '/f') | Out-Null
  }

  Step 'smoke tests as ContainerUser, with a fresh environment'
  $smokeRc = Exec 'ContainerUser' @('C:\mnt\kit\dtk.exe', 'exec', '--', 'powershell', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', 'C:\mnt\tests\smoke.ps1', '-Groups', $Groups, '-Config', 'C:\mnt\config\groups.json')
  Write-Host "smoke exit code: $smokeRc"
  if ($smokeRc -ne 0) { $rc = 1 }

  if ($Uninstall) {
    Step 'uninstall'
    $u = Exec 'ContainerUser' @('C:\mnt\kit\dtk.exe', 'uninstall')
    $left = docker exec -u ContainerUser $Name cmd /c 'reg query HKCU\Environment' | Out-String
    Write-Host $left
    if ($u -ne 0 -or $left -match 'dev-toolkit|CARGO_HOME|SCOOP') { Write-Host 'FAIL: uninstall left settings behind'; $rc = 1 }
    else { Write-Host 'ok: uninstall restored the environment' }
  }
}
finally {
  if ($rc) {
    Step 'install.log'
    docker exec -u ContainerUser $Name cmd /c 'type %LOCALAPPDATA%\dev-toolkit\install.log' 2>$null
  }
  docker rm -f $Name | Out-Null
}
exit $rc
