<#
.SYNOPSIS
  Installs every Scoop app in config/groups.json on a connected Windows machine (a GitHub Actions runner),
  then packs what the offline machine needs for both install routes:

    scoop.zip                   Scoop itself (core/) and its buckets (buckets/<name>/) at the pinned commits
    tools-<group>-scoop.zip     Scoop's download cache for the group's apps, file names as Scoop wrote them
    tools-<group>-portable.zip  the installed trees (apps/<app>/) plus portable.json, for the no-Scoop route
    tools.facts.json            versions, bucket commits, sizes

  Run from the repo root in PowerShell 7:  .\scripts\collect-tools.ps1 -Stage D:\stage -Out out
  It installs Scoop for the current user and changes that user's PATH, so use a throwaway machine.
#>
[CmdletBinding()]
param(
  [string]$Config = 'config/groups.json',
  [string]$Stage = 'D:\stage',
  [string]$Out = 'out',
  [string[]]$Only            # limit to these groups (for quick test builds)
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repo = Split-Path -Parent $PSScriptRoot
$cfg = Get-Content (Join-Path $repo $Config) -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Force -Path $Stage, $Out | Out-Null
$Out = (Resolve-Path $Out).Path
$scoop = Join-Path $Stage 'scoop'
$sevenZip = (Get-Command 7z -ErrorAction SilentlyContinue).Source
if (-not $sevenZip) { $sevenZip = 'C:\Program Files\7-Zip\7z.exe' }

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }

# ---------------------------------------------------------------- Scoop and buckets
Step "Scoop into $scoop"
$env:SCOOP = $scoop
$installer = Join-Path $Stage 'install-scoop.ps1'
Invoke-WebRequest 'https://raw.githubusercontent.com/ScoopInstaller/Install/master/install.ps1' -OutFile $installer
& $installer -ScoopDir $scoop -RunAsAdmin
$env:PATH = "$scoop\shims;$env:PATH"
scoop config aria2-enabled false
scoop install main/git
if (-not (Test-Path "$scoop\apps\git\current\cmd\git.exe")) { throw 'git did not install' }

# Every bucket as a git checkout (the installer fetched main as a zip), then Scoop core as a checkout too.
scoop bucket rm main
foreach ($b in $cfg.buckets.PSObject.Properties) {
  scoop bucket add $b.Name $b.Value
  if (-not (Test-Path "$scoop\buckets\$($b.Name)\.git")) { throw "bucket $($b.Name) did not clone" }
}
scoop update
$commits = [ordered]@{}
$commits['scoop'] = (git -C "$scoop\apps\scoop\current" rev-parse HEAD 2>$null)
foreach ($b in $cfg.buckets.PSObject.Properties) { $commits[$b.Name] = git -C "$scoop\buckets\$($b.Name)" rev-parse HEAD }
$commits | Format-Table | Out-String | Write-Host

# ---------------------------------------------------------------- install every app
$groups = @($cfg.groups.PSObject.Properties | Where-Object { $_.Value.apps.Count -gt 0 -and (-not $Only -or $Only -contains $_.Name) })
$installed = [ordered]@{}   # group -> list of app names that made it
$failed = @()
foreach ($g in $groups) {
  Step "group $($g.Name)"
  $installed[$g.Name] = @()
  foreach ($a in $g.Value.apps) {
    $id = $a.app
    $name = $id.Split('/')[-1]
    for ($try = 1; $try -le 2; $try++) {
      scoop install $id
      if (Test-Path "$scoop\apps\$name\current\install.json") { break }
      Write-Host "  retry $id"
      scoop uninstall $name 2>$null | Out-Null
    }
    if (Test-Path "$scoop\apps\$name\current\install.json") { $installed[$g.Name] += $name }
    else { $failed += $id; Write-Host "::warning::$id failed to install and is left out of this release" }
  }
}

# ---------------------------------------------------------------- scoop.zip
Step 'scoop.zip'
$pack = Join-Path $Stage 'pack'
if (Test-Path $pack) { Remove-Item $pack -Recurse -Force }
$p = Join-Path $pack 'scoop'
New-Item -ItemType Directory -Force -Path "$p\buckets" | Out-Null
$coreDir = (Get-Item "$scoop\apps\scoop\current").Target
if (-not $coreDir) { $coreDir = "$scoop\apps\scoop\current" } elseif ($coreDir -is [array]) { $coreDir = $coreDir[0] }
robocopy $coreDir "$p\core" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy core failed ($LASTEXITCODE)" }
foreach ($b in $cfg.buckets.PSObject.Properties) {
  robocopy "$scoop\buckets\$($b.Name)" "$p\buckets\$($b.Name)" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy bucket $($b.Name) failed ($LASTEXITCODE)" }
}
Push-Location $p
& $sevenZip a -tzip -mx=5 -bso0 -bsp0 (Join-Path $Out 'scoop.zip') core buckets
if ($LASTEXITCODE) { throw '7z failed for scoop.zip' }
Pop-Location

# ---------------------------------------------------------------- helpers
function Get-Manifest($name) { Get-Content "$scoop\apps\$name\current\manifest.json" -Raw | ConvertFrom-Json }
function Arch($m, $key) {
  $a = $m.architecture.'64bit'
  if ($a -and $a.PSObject.Properties.Name -contains $key) { return $a.$key }
  return $m.$key
}
function AsList($v) { if ($null -eq $v) { return @() } if ($v -is [string]) { return @($v) } return @($v) }
function Placeholders([string]$s, $version) {
  if ($null -eq $s) { return $s }
  $s = $s -replace '\$persist_dir', '{app}' -replace '\$original_dir', '{app}' -replace '\$dir', '{app}' -replace '\$version', $version
  # $env:NAME -> %NAME%, expanded by Windows when the variable is read
  return [regex]::Replace($s, '\$env:([A-Za-z_][A-Za-z0-9_]*)', '%$1%')
}
$binaryExt = '.exe .dll .pdb .png .jpg .jpeg .gif .ico .bmp .node .pak .dat .bin .wasm .nupkg .zip .7z .gz .xz .ttf .otf .woff .woff2 .lib .a .o .so .pyd .msi .cab .jar .class .chm .mui .winmd .tlb .xbf .pri' -split ' '

# The installed trees record their build path in a few text files; list them so dtk can fix them.
function Find-Rewrites($tree, $roots) {
  $variants = foreach ($r in $roots) { $r; $r.Replace('\', '\\'); $r.Replace('\', '/') }
  $hits = @()
  Get-ChildItem $tree -Recurse -File -Force | Where-Object { $_.Length -lt 4MB -and $binaryExt -notcontains $_.Extension.ToLowerInvariant() } | ForEach-Object {
    try {
      $bytes = [IO.File]::ReadAllBytes($_.FullName)
      if ([Array]::IndexOf($bytes, [byte]0, 0, [Math]::Min($bytes.Length, 8000)) -ge 0) { return }
      $text = [Text.Encoding]::UTF8.GetString($bytes)
      foreach ($v in $variants) { if ($text.IndexOf($v, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $hits += $_.FullName.Substring($tree.Length + 1).Replace('\', '/'); break } }
    } catch { }
  }
  return $hits
}

# ---------------------------------------------------------------- per-group caches and trees
$facts = [ordered]@{ built = (Get-Date).ToUniversalTime().ToString('o'); commits = $commits; groups = [ordered]@{}; failed = $failed; assets = [ordered]@{} }
$allGroupApps = @($installed.Values | ForEach-Object { $_ })
$helpers = @(Get-ChildItem "$scoop\apps" -Directory | ForEach-Object Name | Where-Object { $_ -ne 'scoop' -and $allGroupApps -notcontains $_ })
Write-Host "helpers installed as dependencies (go with core): $($helpers -join ', ')"
$cacheFiles = @(Get-ChildItem "$scoop\cache" -File)

foreach ($g in $groups) {
  $gname = $g.Name
  Step "pack $gname"
  $apps = @($installed[$gname])
  $cacheApps = if ($gname -eq 'core') { $apps + $helpers } else { $apps }

  # Scoop route: cache files
  $mine = @($cacheFiles | Where-Object { $cacheApps -contains ($_.Name -split '#')[0] })
  $list = Join-Path $Stage "cache-$gname.txt"
  $mine.FullName | Set-Content $list
  Push-Location "$scoop\cache"
  & $sevenZip a -tzip -mx=0 -bso0 -bsp0 (Join-Path $Out "tools-$gname-scoop.zip") "@$list"
  if ($LASTEXITCODE) { throw "7z failed for tools-$gname-scoop.zip" }
  Pop-Location
  foreach ($a in $cacheApps) { if (-not ($mine | Where-Object { ($_.Name -split '#')[0] -eq $a })) { Write-Host "::warning::no cache file for $a" } }

  # Portable route: installed trees and portable.json
  $gp = Join-Path $pack "portable-$gname"
  New-Item -ItemType Directory -Force -Path "$gp\apps" | Out-Null
  $entries = @()
  $versions = [ordered]@{}
  foreach ($name in $apps) {
    $m = Get-Manifest $name
    $ver = $m.version
    $versions[$name] = $ver
    $src = "$scoop\apps\$name\$ver"
    if (-not (Test-Path $src)) { $src = (Get-Item "$scoop\apps\$name\current").Target | Select-Object -First 1 }
    $dst = "$gp\apps\$name"
    # robocopy copies junction targets, so persisted folders (settings, data) come along as real folders
    robocopy $src $dst /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /MT:8 | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy $name failed ($LASTEXITCODE)" }
    Remove-Item "$dst\install.json", "$dst\manifest.json" -ErrorAction SilentlyContinue
    $roots = @("$scoop\apps\$name\$ver", "$scoop\apps\$name\current", "$scoop\persist\$name")

    $bins = foreach ($b in (AsList (Arch $m 'bin'))) {
      if ($b -is [array] -or $b -is [System.Collections.IList]) { $t = [string]$b[0]; $al = if ($b.Count -gt 1) { [string]$b[1] } else { '' }; $ar = if ($b.Count -gt 2) { ($b[2..($b.Count - 1)] -join ' ') } else { '' } }
      else { $t = [string]$b; $al = ''; $ar = '' }
      if (-not $al) { $al = [IO.Path]::GetFileNameWithoutExtension($t) }
      [ordered]@{ path = $t.Replace('\', '/'); alias = $al.ToLowerInvariant(); args = (Placeholders $ar $ver) }
    }
    $paths = @(AsList (Arch $m 'env_add_path') | ForEach-Object { $_.Replace('\', '/') })
    $envs = [ordered]@{}
    $es = Arch $m 'env_set'
    if ($es) { foreach ($pp in $es.PSObject.Properties) { $envs[$pp.Name] = Placeholders ([string]$pp.Value) $ver } }
    $shortcuts = foreach ($s in (AsList (Arch $m 'shortcuts'))) {
      [ordered]@{ target = ([string]$s[0]).Replace('\', '/'); name = [string]$s[1]; args = if ($s.Count -gt 2) { Placeholders ([string]$s[2]) $ver } else { '' } }
    }
    $fonts = @()
    if ((Get-Content "$scoop\apps\$name\current\install.json" -Raw | ConvertFrom-Json).bucket -eq 'nerd-fonts') {
      $fonts = @(Get-ChildItem $dst -File | Where-Object { $_.Extension -in '.ttf', '.otf' } | ForEach-Object Name)
    }
    $rewrite = @(Find-Rewrites (Resolve-Path $dst).Path $roots)
    if ($rewrite) { Write-Host "  $name records its path in: $($rewrite -join ', ')" }
    $entries += [ordered]@{ name = $name; version = $ver; bins = @($bins); paths = $paths; env = $envs; shortcuts = @($shortcuts); rewrite = $rewrite; fonts = $fonts; roots = $roots }
  }
  if ($gname -eq 'core') {
    New-Item -ItemType Directory -Force -Path "$gp\shims" | Out-Null
    Copy-Item "$coreDir\supporting\shims\kiennq\shim.exe" "$gp\shims\shim.exe"
  }
  [ordered]@{ apps = $entries } | ConvertTo-Json -Depth 10 | Set-Content "$gp\portable.json" -Encoding utf8NoBOM
  Push-Location $gp
  $items = @('apps', 'portable.json') + @(if (Test-Path shims) { 'shims' })
  & $sevenZip a -tzip -mx=5 -mmt=on -bso0 -bsp0 (Join-Path $Out "tools-$gname-portable.zip") @items
  if ($LASTEXITCODE) { throw "7z failed for tools-$gname-portable.zip" }
  Pop-Location
  Remove-Item $gp -Recurse -Force

  $facts.groups[$gname] = [ordered]@{ apps = $versions; helpers = @(if ($gname -eq 'core') { $helpers }) }
}

foreach ($f in Get-ChildItem $Out -Filter '*.zip') { $facts.assets[$f.BaseName] = $f.Length }
$facts | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $Out 'tools.facts.json') -Encoding utf8NoBOM
Get-ChildItem $Out | Format-Table Name, @{ n = 'MB'; e = { [math]::Round($_.Length / 1MB) } } | Out-String | Write-Host
if ($failed) { Write-Host "::warning::left out: $($failed -join ', ')" }
