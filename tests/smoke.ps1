<#
.SYNOPSIS
  Proves an install works with no network: every tool answers, and each language builds a small
  project from the local caches. Run it in a fresh environment (dtk.exe exec -- powershell -File ...).
    powershell -File tests\smoke.ps1 -Groups core,rust,python -Config config\groups.json
#>
param(
  [string]$Groups = 'core',
  [string]$Config = (Join-Path (Split-Path -Parent $PSScriptRoot) 'config\groups.json')
)
$ErrorActionPreference = 'Continue'
$cfg = Get-Content $Config -Raw | ConvertFrom-Json
$selected = @('core') + @($Groups -split '[,; ]+' | Where-Object { $_ }) | Select-Object -Unique
$script:failures = @()
$work = Join-Path $env:TEMP ("dtk-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Head($t) { Write-Host "`n== $t" }
function Pass($t) { Write-Host "  ok    $t" }
function Fail($t, $detail) {
  Write-Host "  FAIL  $t"
  if ($detail) { ($detail -split "`n" | Select-Object -Last 25) | ForEach-Object { Write-Host "        $_" } }
  $script:failures += $t
}
# Runs a command line through cmd so .cmd shims and PATH lookup behave as in a terminal.
function Try-Cmd([string]$label, [string]$line, [string]$cwd = $work) {
  Push-Location $cwd
  $out = cmd /d /c "$line 2>&1"
  $code = $LASTEXITCODE
  Pop-Location
  $text = ($out | Out-String).Trim()
  if ($code -eq 0) { Pass "$label$(if ($text) { '  -> ' + ($text -split "`n")[0].Trim() })" } else { Fail "$label (exit $code)" $text }
  return ($code -eq 0)
}

Head "tools ($($selected -join ', '))"
foreach ($g in $selected) {
  $group = $cfg.groups.$g
  if (-not $group) { continue }
  foreach ($a in $group.apps) {
    $name = $a.app.Split('/')[-1]
    if ($a.check) { Try-Cmd $name $a.check | Out-Null }
    elseif ($a.where) {
      $hit = cmd /d /c "where $($a.where) 2>nul"
      if ($LASTEXITCODE -eq 0) { Pass "$name -> $(@($hit)[0])" } else { Fail "$name ($($a.where) not on PATH)" }
    }
    elseif ($a.font) {
      $fonts = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\Fonts') -Filter "*$($a.font)*" -ErrorAction SilentlyContinue
      if ($fonts) { Pass "$name ($($fonts.Count) font files)" } else { Fail "$name (no $($a.font) fonts installed for this user)" }
    }
  }
}

if ($selected -contains 'core') {
  Head 'VS Code extensions'
  $list = cmd /d /c "code --list-extensions 2>&1"
  $have = @($list | ForEach-Object { "$_".Trim().ToLowerInvariant() })
  foreach ($g in $selected) {
    foreach ($e in @($cfg.groups.$g.extensions)) {
      if (-not $e) { continue }
      if ($have -contains $e.ToLowerInvariant()) { Pass $e } else { Fail "$e is not installed" ($list | Out-String) }
    }
  }
}

if ($selected -contains 'rust') {
  Head 'Rust: a project with dependencies from the local registry'
  Try-Cmd 'rustc' 'rustc --version' | Out-Null
  Try-Cmd 'rustup default' 'rustup default' | Out-Null
  if (Try-Cmd 'cargo new' 'cargo new --quiet hello') {
    $p = Join-Path $work 'hello'
    # clap pulls in windows-sys (raw-dylib imports); cc compiles C in build.rs: the two things the GNU
    # toolchain can't do without LLVM-MinGW
    Try-Cmd 'cargo add serde, serde_json, anyhow, clap; cc as a build dependency' 'cargo add serde --features derive && cargo add serde_json anyhow && cargo add clap --features derive && cargo add --build cc' $p | Out-Null
    Set-Content (Join-Path $p 'src\main.rs') @'
use clap::Parser;
use serde::{Deserialize, Serialize};
#[derive(Serialize, Deserialize, Debug)]
struct Point { x: i32, y: i32 }
#[derive(Parser)]
struct Args { #[arg(long, default_value_t = 2)] times: i32 }
unsafe extern "C" { fn triple(x: i32) -> i32; }
fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    let p: Point = serde_json::from_str(r#"{"x":1,"y":2}"#)?;
    println!("rust ok {:?}, C says {}", p, unsafe { triple(args.times) });
    Ok(())
}
'@
    Set-Content (Join-Path $p 'build.rs') 'fn main() { cc::Build::new().file("src/triple.c").compile("triple"); }'
    Set-Content (Join-Path $p 'src\triple.c') '#include <stdio.h>
int triple(int x) { return 3 * x; }'
    Try-Cmd 'cargo run (offline: deps, windows-sys, C via cc)' 'cargo run --quiet' $p | Out-Null
    Try-Cmd 'cargo build --release' 'cargo build --quiet --release' $p | Out-Null
    Try-Cmd 'cargo clippy' 'cargo clippy --quiet' $p | Out-Null
  }
  # quick test builds carry only some of the cargo tools; a full release build fails if any is missing
  foreach ($t in @{ exe = 'cargo-nextest'; cmd = 'cargo nextest --version' }, @{ exe = 'cargo-audit'; cmd = 'cargo audit --version' },
                 @{ exe = 'cargo-deny'; cmd = 'cargo deny --version' }, @{ exe = 'bacon'; cmd = 'bacon --version' },
                 @{ exe = 'cargo-expand'; cmd = 'cargo expand --version' }) {
    if (Test-Path (Join-Path $env:CARGO_HOME "bin\$($t.exe).exe")) { Try-Cmd $t.cmd $t.cmd | Out-Null }
    else { Write-Host "  skip  $($t.exe) (not in this build)" }
  }
}

if ($selected -contains 'python') {
  Head 'Python: venvs filled from the wheelhouse'
  Try-Cmd 'python' 'python --version' | Out-Null
  if (Try-Cmd 'python -m venv' 'python -m venv v') {
    Try-Cmd 'pip install requests numpy pandas pytest' 'v\Scripts\python -m pip install --quiet requests numpy pandas pytest' | Out-Null
    Try-Cmd 'import them' 'v\Scripts\python -c "import requests, numpy, pandas, pytest; print(pandas.__version__)"' | Out-Null
  }
  if (Try-Cmd 'uv venv' 'uv venv --quiet u') {
    Try-Cmd 'uv pip install fastapi httpx' 'uv pip install --quiet --python u\Scripts\python.exe fastapi httpx' | Out-Null
    Try-Cmd 'import them' 'u\Scripts\python -c "import fastapi, httpx; print(fastapi.__version__)"' | Out-Null
  }
}

if ($selected -contains 'node') {
  Head 'Node.js: npm install from the cache'
  Try-Cmd 'node' 'node --version' | Out-Null
  $p = Join-Path $work 'web'
  New-Item -ItemType Directory -Force -Path $p | Out-Null
  if (Try-Cmd 'npm init' 'npm init -y' $p) {
    Try-Cmd 'npm install express typescript' 'npm install express typescript' $p | Out-Null
    Try-Cmd 'require express' 'node -e "require(''express''); console.log(''express ok'')"' $p | Out-Null
    Try-Cmd 'npx tsc' 'npx tsc --version' $p | Out-Null
  }
}

if ($selected -contains 'dotnet') {
  Head '.NET: projects restored from the local feed'
  Try-Cmd 'dotnet' 'dotnet --version' | Out-Null
  if (Try-Cmd 'dotnet new console' 'dotnet new console -o hello') {
    Try-Cmd 'dotnet add package Newtonsoft.Json' 'dotnet add hello package Newtonsoft.Json' | Out-Null
    Try-Cmd 'dotnet run' 'dotnet run --project hello' | Out-Null
  }
  if (Try-Cmd 'dotnet new webapi' 'dotnet new webapi -o api') { Try-Cmd 'dotnet build webapi' 'dotnet build api' | Out-Null }
  if (Try-Cmd 'dotnet new xunit' 'dotnet new xunit -o tests') { Try-Cmd 'dotnet test' 'dotnet test tests' | Out-Null }
}

if ($selected -contains 'cpp') {
  Head 'C and C++'
  Set-Content (Join-Path $work 'hello.c') "#include <stdio.h>`nint main(void) { puts(`"c ok`"); return 0; }"
  Try-Cmd 'clang hello.c' 'clang hello.c -o hello.exe && hello.exe' | Out-Null
  $p = Join-Path $work 'cm'
  New-Item -ItemType Directory -Force -Path $p | Out-Null
  Set-Content (Join-Path $p 'CMakeLists.txt') "cmake_minimum_required(VERSION 3.20)`nproject(hi CXX)`nadd_executable(hi main.cpp)"
  Set-Content (Join-Path $p 'main.cpp') "#include <iostream>`nint main() { std::cout << `"cmake ok`" << std::endl; }"
  Try-Cmd 'cmake + ninja + clang++' 'cmake -G Ninja -S . -B build -DCMAKE_CXX_COMPILER=clang++ && cmake --build build && build\hi.exe' $p | Out-Null
}

if ($selected -contains 'data') {
  Head 'Databases'
  Try-Cmd 'sqlite3' 'sqlite3 :memory: "select 40 + 2;"' | Out-Null
  Try-Cmd 'duckdb' 'duckdb -c "select 42 as answer"' | Out-Null
  $pg = Join-Path $work 'pg'
  if (Try-Cmd 'initdb' "initdb -D `"$pg`" -U postgres -A trust --no-locale -E UTF8") {
    # the server inherits pg_ctl's output handles and keeps them open until it stops, so pg_ctl writes to
    # files here: reading its output through a pipe would wait for the server to exit
    $ctlExe = (Get-Command pg_ctl -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    $ctl = Start-Process -FilePath $ctlExe -NoNewWindow -PassThru -RedirectStandardOutput "$work\pg_ctl.out" `
      -RedirectStandardError "$work\pg_ctl.err" -ArgumentList "-D `"$pg`" -o `"-p 54329`" -l `"$work\pg.log`" -w -t 60 start"
    $null = $ctl.Handle   # keeps the exit code readable
    $started = $ctl.WaitForExit(120000) -and $ctl.ExitCode -eq 0
    if ($started) {
      Pass 'pg_ctl start'
      Try-Cmd 'psql' 'psql -h 127.0.0.1 -p 54329 -U postgres -c "select version();"' | Out-Null
    } else {
      Fail 'pg_ctl start' ((Get-Content "$work\pg_ctl.out", "$work\pg_ctl.err", "$work\pg.log" -ErrorAction SilentlyContinue) -join "`n")
    }
    cmd /d /c "pg_ctl -D `"$pg`" -m fast -w -t 60 stop >nul 2>&1"
  }
}

Head 'Result'
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
if ($script:failures.Count) { Write-Host "$($script:failures.Count) failed: $($script:failures -join '; ')"; exit 1 }
Write-Host 'all passed'
exit 0
