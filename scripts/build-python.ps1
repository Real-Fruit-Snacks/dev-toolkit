<#
.SYNOPSIS
  Builds python.zip: CPython from python-build-standalone (the builds uv uses), unpacked and ready to run
  from any folder. pip is included; the EXTERNALLY-MANAGED marker is removed so pip can install into it.
    .\scripts\build-python.ps1 -Version 3.14 -Out out
#>
[CmdletBinding()]
param(
  [string]$Version = '3.14',
  [string]$Stage = 'D:\stage-python',
  [string]$Out = 'out'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
New-Item -ItemType Directory -Force -Path $Stage, $Out | Out-Null
$Out = (Resolve-Path $Out).Path
$sevenZip = (Get-Command 7z -ErrorAction SilentlyContinue).Source
if (-not $sevenZip) { $sevenZip = 'C:\Program Files\7-Zip\7z.exe' }

# uv's own static build, so this doesn't depend on the runner's Python
$uvZip = Join-Path $Stage 'uv.zip'
Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $uvZip
Expand-Archive $uvZip -DestinationPath "$Stage\uv" -Force
$uv = Get-ChildItem "$Stage\uv" -Recurse -Filter uv.exe | Select-Object -First 1

$installs = Join-Path $Stage 'pythons'
if (Test-Path $installs) { Remove-Item $installs -Recurse -Force }
$env:UV_PYTHON_INSTALL_DIR = $installs
& $uv.FullName python install $Version
if ($LASTEXITCODE) { throw "uv python install $Version failed" }
$dir = Get-ChildItem $installs -Directory | Where-Object Name -like "cpython-$Version*-windows-x86_64-none" | Select-Object -First 1
if (-not $dir) { throw "no cpython-$Version build in $installs" }
$py = Join-Path $dir.FullName 'python.exe'

& $py -m pip --version
if ($LASTEXITCODE) { & $py -m ensurepip --default-pip; if ($LASTEXITCODE) { throw 'no pip' } }
Get-ChildItem $dir.FullName -Recurse -Filter 'EXTERNALLY-MANAGED' | Remove-Item -Force
& $py -c "import sys, ssl, sqlite3, venv; print(sys.version)"
if ($LASTEXITCODE) { throw 'python smoke test failed' }

Push-Location $installs
& $sevenZip a -tzip -mx=7 -mmt=on -bso0 -bsp0 (Join-Path $Out 'python.zip') $dir.Name
if ($LASTEXITCODE) { throw '7z failed' }
Pop-Location

$facts = [ordered]@{
  built = (Get-Date).ToUniversalTime().ToString('o')
  build = $dir.Name
  version = (& $py -c "import platform; print(platform.python_version())")
  pip = ((& $py -m pip --version) -split ' ')[1]
  uv = ((& $uv.FullName --version) -split ' ')[1]
  size = (Get-Item (Join-Path $Out 'python.zip')).Length
}
$facts | ConvertTo-Json | Set-Content (Join-Path $Out 'python.facts.json') -Encoding utf8NoBOM
$facts | ConvertTo-Json | Write-Host
