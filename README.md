# dev-toolkit

Developer tools for a Windows machine that has no internet and won't give you admin rights: one GitHub release, rebuilt every Sunday, that you carry over and install as yourself.

It holds Git, VS Code and its extensions, PowerShell 7, Windows Terminal, a C and C++ compiler and the command-line tools I reach for, plus four languages that build real projects with no network: Rust with the popular crates, Python 3.14 with a wheelhouse, Node.js with an npm cache, and .NET with a NuGet feed. CMake, Ninja, SQLite, DuckDB, PostgreSQL and DBeaver come too, and an Ubuntu distro for WSL that uses the same caches.

## Install

On a connected machine, download the [latest release](https://github.com/Real-Fruit-Snacks/dev-toolkit/releases/latest) into one folder. With the GitHub CLI:

```powershell
gh release download --repo Real-Fruit-Snacks/dev-toolkit --dir dev-toolkit
```

Carry the folder to the offline machine, then run `check.cmd` and `install.cmd` from it, as yourself. Open a new terminal afterwards.

`check.cmd` says what the machine allows and which way the install will go. `install.cmd` checks every file it uses against `SHA256SUMS.txt`, installs everything under `%LOCALAPPDATA%\dev-toolkit` and writes the config that points each tool at the local caches. Both are thin wrappers around `dtk.exe`, which takes options:

```text
dtk install --groups rust,python   only these groups (core is always included)
dtk install --skip data,wsl        everything except these
dtk install --root D:\dev          another install folder
dtk verify                         check every file against SHA256SUMS.txt
dtk uninstall                      undo every setting it changed and delete the install folder
dtk exec -- cargo build            run a command with the environment a new terminal would get
```

This is what `install.cmd` printed in the v1.0.0 build's test, in a Windows container with no network, as a user who isn't an administrator, with PowerShell scripts limited to signed ones by group policy and only the core, C and C++, and database files in the folder (trimmed where it says so):

```text
dtk 1.0.0: installing dev-toolkit v1.0.0 into C:\Users\ContainerUser\AppData\Local\dev-toolkit
== This machine
  ok   Windows               Windows Server 2022 Datacenter build 20348.5773, 64-bit
  ok   Programs from         a program copied to C:\Users\ContainerUser\AppData\Local\dev-toolkit\.probe runs
  NO   PowerShell scripts    local PowerShell scripts can't run: File ...\probe.ps1 cannot be loaded. (trimmed)
  ok   PowerShell mode       FullLanguage
  info   MachinePolicy       AllSigned
  info C++ linker (MSVC)     no Visual Studio C++ tools -> Rust uses the GNU toolchain
  info WSL                   WSL isn't turned on (wsl --status failed)
  info Long paths            off (dtk works around it; some tools may hit the 260-character limit)
  info Free space            119.8 GB on C:\ (install folder C:\Users\ContainerUser\AppData\Local\dev-toolkit)
== This release (v1.0.0, in C:\mnt\kit\)
  ok   core                  Shell, Git, editor, a C compiler and command-line tools (978 MB)
  NO   rust                  Rust: stable MSVC and GNU toolchains, cargo tools, popular crates: missing rust-toolchain.zip, crates.zip
  (4 more groups whose files weren't copied over)
  ok   cpp                   C and C++ builds: CMake, Ninja, Make (the compiler, LLVM-MinGW, is in core) (51 MB)
  ok   data                  Databases and data: SQLite, DuckDB, PostgreSQL, DBeaver (503 MB)
== Verdict
   ok    Install will use the ready-unpacked tools (no Scoop; PowerShell scripts can't run here). Everything else is the same.
   The WSL group will be skipped: WSL isn't turned on, and turning it on needs admin.
== Plan: portable route; groups core, cpp, data
   release files for this install: 1.5 GB; expect about 3.7 GB installed
== Checking the files against SHA256SUMS.txt
   checked 4 files, 1.7 GB in 1s
   ok    Visual C++ runtime: not installed on this machine, so the kit's copy is on PATH
== Ready-unpacked tools: core
     25,114 files, 2.8 GB in 21s
   ok    7zip 26.05
   ok    git 2.56.0.4
   (19 more, all ok)
== Ready-unpacked tools: cpp
   (3 apps, all ok)
== Ready-unpacked tools: data
   (4 apps, all ok)
== VS Code
   (7 extensions, all ok)
== Done in 1.0 minutes
   Open a NEW terminal so it picks up the new PATH. Tools that are already open keep the old one.
```

### Which way it installs

`dtk.exe` tries before it chooses. It copies itself into the install folder and runs the copy, to see whether this machine runs programs from your profile at all. Then it runs a small PowerShell script the way Scoop would be run.

- **Scripts run:** the apps go in through [Scoop](https://scoop.sh), from a Scoop download cache that is already full, so `scoop list`, `scoop reset` and `scoop uninstall` work afterwards. `scoop update` and new installs need the internet.
- **Scripts are blocked** (an execution policy set by group policy, or Constrained Language mode): the same apps, unpacked as Scoop installed them on the build machine, with their paths rewritten and Scoop's own shim for each command. Nothing runs a script.
- **Programs from your profile are blocked** (AppLocker or WDAC rules for user folders): nothing can be installed this way, and `check.cmd` says so.

Either way, an app that fails to install through Scoop falls back to its unpacked copy, and every language, cache and setting below is the same.

### Picking groups

`core` always goes in. The others are optional, and you only need their files:

| Group | What it adds | Files it needs |
|---|---|---|
| core | Git, VS Code, PowerShell 7, Windows Terminal, LLVM-MinGW (clang), gh, ripgrep, fd, fzf, jq, yq, bat, delta, lazygit, just, hyperfine, starship, Neovim, less, JetBrains Mono Nerd Font | `dtk.exe`, `check.cmd`, `install.cmd`, `dev-toolkit.json`, `SHA256SUMS.txt`, `scoop.zip`, `tools-core-*.zip`, `vscode-extensions.zip` |
| rust | Stable Rust for MSVC and GNU, clippy, rustfmt, rust-analyzer, cargo-nextest, cargo-deny, cargo-audit, cargo-expand, bacon, and a local registry of the most downloaded crates | `rust-toolchain.zip`, `crates.zip` |
| python | Python 3.14 and uv, with a wheelhouse of the most downloaded PyPI projects | `tools-python-*.zip`, `python.zip`, `wheels-windows.zip` |
| node | Node.js LTS, with an npm cache of the most used packages | `tools-node-*.zip`, `npm-cache.zip` |
| dotnet | The .NET SDK (LTS), with a NuGet feed of the most downloaded packages, every SDK template and the publish packs | `tools-dotnet-*.zip`, `nuget.zip` |
| cpp | CMake, Ninja, GNU Make | `tools-cpp-*.zip` |
| data | SQLite, DuckDB, PostgreSQL, DBeaver | `tools-data-*.zip` |
| wsl | Ubuntu 24.04 with Rust, Python, Node and .NET, pointed at the same caches | `wsl-distro.tar.gz*`, `wheels-linux.zip` |

The `wsl` group goes in only when WSL is already turned on, since turning it on needs admin. Inside it, Rust, npm and NuGet use the Windows-side caches, so install those groups too to get them.

## What a release holds

v1.0.0 is 23.5 GiB in 35 files. Besides the tools and VS Code extensions:

- **Rust 1.99.1** for MSVC and GNU, with cargo-nextest, cargo-deny, cargo-audit, cargo-expand and bacon, the RustSec advisory database, and 4,078 crates in 8,723 versions.
- **Python 3.14.10** and uv, with 2,194 Windows wheels covering 1,930 of PyPI's most downloaded projects, and 2,184 Linux wheels for WSL.
- **Node.js 26.13.0** LTS, with an npm cache of 7,428 packages that 2,049 of the most used ones need, native builds included.
- **.NET SDK 10.0.403**, with a NuGet feed of 1,331 packages in 2,649 versions.
- **Ubuntu 24.04.5** for WSL, with the same languages inside.

Every version, and every file's SHA-256, is in [VERSIONS.md](VERSIONS.md) and the release's `SHA256SUMS.txt`.

## Working offline

The config `dtk.exe` writes points every tool at its local cache and turns off the update checks, so these work with the network cable out:

```powershell
cargo new hello; cd hello; cargo add serde --features derive; cargo add clap anyhow; cargo build
uv venv; uv pip install fastapi httpx pandas
python -m venv v; v\Scripts\pip install requests numpy pytest
npm init -y; npm install express typescript
dotnet new webapi -o api; dotnet add api package Newtonsoft.Json; dotnet build api
cmake -G Ninja -S . -B build; cmake --build build
initdb -D pgdata -U postgres -A trust; pg_ctl -D pgdata start
wsl -d dev-toolkit
```

- **Rust** uses the MSVC toolchain when Visual Studio's C++ tools are on the machine, and the GNU toolchain with LLVM-MinGW as linker when they aren't. `rustup default stable-x86_64-pc-windows-gnu` (or `-msvc`) switches. Cargo reads the crates from `<root>\crates` with `net.offline` on.
- **Python** packages come from `<root>\wheels\windows`, through pip (`pip.ini`) and uv (`uv.toml`), with downloads off.
- **npm** installs from its cache with `offline` set, so a package that isn't in it fails fast instead of waiting on the network.
- **.NET** restores from the local feed; nuget.org is turned off in your `NuGet.Config`.
- **VS Code** has the extensions for each group installed, with updates, telemetry, AI features and online lookups turned off in its settings.

The caches hold the most used packages and everything they depend on, not whole registries. [VERSIONS.md](VERSIONS.md) has the counts and versions for the latest release.

## What it changes

Everything goes under one folder, `%LOCALAPPDATA%\dev-toolkit` unless you pass `--root`. Outside it, `dtk.exe` changes only your own settings: user environment variables and PATH, the config files of Cargo, pip, uv, npm, NuGet and VS Code (only the keys it owns, with the original kept as `*.before-dev-toolkit`), per-user fonts, and Start menu shortcuts. Every change is recorded in `installed.json`, and `dtk uninstall` puts each one back. The full list is in [docs/layout.md](docs/layout.md).

## How a release is built

[release.yml](.github/workflows/release.yml) runs every Sunday at 08:00 UTC, or by hand from the Actions tab. Each part is built on its own runner and uploaded to a draft:

| Script | Builds |
|---|---|
| `scripts/collect-tools.ps1` | Installs every app with Scoop on a clean Windows runner, then packs Scoop's download cache and the installed trees, with the Visual C++ runtime and Scoop's shim |
| `scripts/build-rust.ps1` | The MSVC and GNU toolchains through rustup, the cargo tools, and the RustSec advisory database |
| `scripts/build-crates.py` | A Cargo local registry: the most downloaded crates, the newest version of each of their two newest compatible lines, and everything those need |
| `scripts/build-python.ps1` | Python 3.14 from python-build-standalone, through uv |
| `scripts/build-wheels.py` | Wheelhouses for Windows and Linux, resolved for the target platform rather than the build machine |
| `scripts/build-npm-cache.mjs` | An npm cache with native packages for Windows and Linux, checked by installing every seed offline |
| `scripts/build-nuget-feed.py` | A NuGet folder feed, restored with the SDK version the release ships |
| `scripts/fetch-vscode-extensions.py` | VS Code extensions from Open VSX, and CodeLLDB's self-contained build from its GitHub releases |
| `scripts/build-distro.sh` | The Ubuntu 24.04 WSL image, from Ubuntu's signed release, provisioned in a chroot |
| `installer/` | `dtk.exe`, for the .NET Framework 4.8 that Windows 11 and Windows 10 (1903 and later) come with |

Then `scripts/write-manifest.py` writes `dev-toolkit.json`, `SHA256SUMS.txt` and `VERSIONS.md`, and compares every hash with the digest GitHub recorded for the upload. The draft is installed and tested, and published only if every test passes. Files of 1900 MiB or more are cut into `.part1`, `.part2` and so on; `dtk.exe` reads the parts in place, so don't join them.

## Tested on

Every release, before it is published, in Windows Server 2022 containers with no network, as a user who isn't an administrator:

- the Scoop route with core, C and C++, and the databases, then `dtk uninstall`, checking that no user variable is left behind;
- the unpacked route, with PowerShell scripts limited to signed ones by group policy;
- each language on its own: a Rust project with serde, clap, `windows-sys` and C code built through the `cc` crate; Python venvs filled by pip and uv; an Express and TypeScript project from the npm cache; a console app, a web API and an xUnit test project from the NuGet feed;
- the WSL distro, unpacked and run in a chroot on Ubuntu with its network cut off, building Rust, Python, Node and .NET projects from the same caches.

Not tested: a real Windows 11 machine with company policy on it, a machine with Visual Studio's C++ tools (where Rust picks MSVC), and `wsl --import` itself, which the build runners can't run.

## Known issues

- VS Code's extensions come from Open VSX, so Microsoft's own C# Dev Kit and Pylance aren't there. C# uses the open-source build of the C# extension, and Python uses basedpyright.
- Long paths are off on most machines. `dtk.exe` works around the limit, but a deep `node_modules` or build folder can still hit 260 characters; `check.cmd` says whether they're on.
- A package that isn't in a cache needs a connected machine: `pip download`, `cargo vendor`, `npm pack` or `nuget install`, then carry the files over.
- Fonts are installed for your user only, so apps that read only the machine's fonts won't list JetBrains Mono.

## Configuration

What goes into each group is [config/groups.json](config/groups.json). The extra seeds for the caches are in `config/crates-extra.txt`, `config/pypi-extra.txt`, `config/npm-extra.txt` and `config/nuget-extra.txt`, and what each cache leaves out on purpose is in `config/pypi-exclude.txt` and `config/nuget-extra.txt`. How the release is laid out, and what the installer writes where, is [docs/layout.md](docs/layout.md).

## License

MIT, for the installer, scripts and config in this repo. The tools, packages and distro image in the releases keep their own licenses; [VERSIONS.md](VERSIONS.md) lists them.
