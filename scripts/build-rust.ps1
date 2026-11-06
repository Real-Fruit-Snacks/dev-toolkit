<#
.SYNOPSIS
  Builds rust-toolchain.zip: stable Rust for both Windows targets (MSVC and GNU) with clippy, rustfmt,
  rust-src and rust-analyzer, the offline docs, a few cargo tools, and the RustSec advisory database.

    rustup/   RUSTUP_HOME (both toolchains; self-update and auto-install turned off)
    cargo/    CARGO_HOME (bin/ with the rustup proxies and the cargo tools, advisory-db/)

  The cargo tools are built with the C runtime linked in, so they run on machines without the
  Visual C++ redistributable. Needs a Windows machine with Visual Studio's C++ tools (GitHub's runners have them).
    .\scripts\build-rust.ps1 -Stage D:\stage-rust -Out out
#>
[CmdletBinding()]
param(
  [string]$Stage = 'D:\stage-rust',
  [string]$Out = 'out',
  [string[]]$Tools = @('cargo-nextest', 'cargo-expand', 'bacon', 'cargo-deny', 'cargo-audit'),
  [switch]$NoDocs
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
New-Item -ItemType Directory -Force -Path $Stage, $Out | Out-Null
$Out = (Resolve-Path $Out).Path
$rust = Join-Path $Stage 'rust'
if (Test-Path $rust) { Remove-Item $rust -Recurse -Force }
$env:RUSTUP_HOME = "$rust\rustup"
$env:CARGO_HOME = "$rust\cargo"
$env:PATH = "$env:CARGO_HOME\bin;$env:PATH"
$sevenZip = (Get-Command 7z -ErrorAction SilentlyContinue).Source
if (-not $sevenZip) { $sevenZip = 'C:\Program Files\7-Zip\7z.exe' }

Step 'rustup and the MSVC toolchain'
$init = Join-Path $Stage 'rustup-init.exe'
Invoke-WebRequest 'https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe' -OutFile $init
$components = 'clippy,rustfmt,rust-src,rust-analyzer'
$msvcComponents = if ($NoDocs) { $components } else { "$components,rust-docs" }
& $init -y --no-modify-path --profile minimal --default-toolchain stable-x86_64-pc-windows-msvc -c $msvcComponents
if ($LASTEXITCODE) { throw 'rustup-init failed' }

Step 'the GNU toolchain (links on its own, for machines without Visual Studio)'
rustup toolchain install stable-x86_64-pc-windows-gnu --profile minimal -c $components
if ($LASTEXITCODE) { throw 'GNU toolchain install failed' }
rustup default stable-x86_64-pc-windows-msvc
rustup set auto-self-update disable
rustup set auto-install disable
if ($LASTEXITCODE) { Write-Host "::warning::this rustup has no auto-install setting" }

Step 'cargo tools'
$env:RUSTFLAGS = '-C target-feature=+crt-static'
foreach ($t in $Tools) {
  cargo install --locked $t
  if ($LASTEXITCODE) { Write-Host "  retry $t"; cargo install --locked $t }
  if ($LASTEXITCODE) { throw "cargo install $t failed" }
}
Remove-Item Env:\RUSTFLAGS

Step 'RustSec advisory database (cargo audit --no-fetch, cargo deny --disable-fetch)'
git clone --depth 1 https://github.com/rustsec/advisory-db "$env:CARGO_HOME\advisory-db"
if ($LASTEXITCODE) { throw 'advisory-db clone failed' }
# cargo-deny keeps its own copy; let it fetch one into CARGO_HOME now
$probe = Join-Path $Stage 'deny-probe'
cargo new --quiet $probe
Push-Location $probe
cargo deny init 2>$null
cargo deny fetch db
if ($LASTEXITCODE) { Write-Host '::warning::cargo deny fetch db failed; cargo deny will need --disable-fetch and a db-path' }
Pop-Location
Remove-Item $probe -Recurse -Force

Step 'trim'
foreach ($d in 'registry', 'git', '.package-cache', '.global-cache') { Remove-Item "$env:CARGO_HOME\$d" -Recurse -Force -ErrorAction SilentlyContinue }
foreach ($d in 'downloads', 'tmp') { Remove-Item "$env:RUSTUP_HOME\$d" -Recurse -Force -ErrorAction SilentlyContinue }

Step 'facts'
$facts = [ordered]@{
  built = (Get-Date).ToUniversalTime().ToString('o')
  rustup = (rustup --version 2>$null | Select-Object -First 1)
  msvc = (rustc +stable-x86_64-pc-windows-msvc --version)
  gnu = (rustc +stable-x86_64-pc-windows-gnu --version)
  components = $components
  docs = -not $NoDocs
  tools = [ordered]@{}
  advisoryDb = (git -C "$env:CARGO_HOME\advisory-db" log -1 --format=%cs)
}
foreach ($t in $Tools) {
  $exe = Get-ChildItem "$env:CARGO_HOME\bin" -Filter "$t*.exe" | Select-Object -First 1
  $facts.tools[$t] = (& cargo install --list | Select-String "^$t v([^:]+):" | ForEach-Object { $_.Matches[0].Groups[1].Value })
}
$facts | ConvertTo-Json -Depth 5 | Write-Host

Step 'rust-toolchain.zip'
Push-Location $rust
& $sevenZip a -tzip -mx=7 -mmt=on -bso0 -bsp0 (Join-Path $Out 'rust-toolchain.zip') rustup cargo
if ($LASTEXITCODE) { throw '7z failed' }
Pop-Location
$facts.size = (Get-Item (Join-Path $Out 'rust-toolchain.zip')).Length
$facts | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $Out 'rust-toolchain.facts.json') -Encoding utf8NoBOM
Write-Host "rust-toolchain.zip: $([math]::Round($facts.size / 1MB)) MB"
