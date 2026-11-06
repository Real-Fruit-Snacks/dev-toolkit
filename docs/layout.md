# How a release is laid out

This is the contract between the build scripts, the release and `dtk.exe`, the installer that runs on the
offline machine. Change it here first, then in the scripts.

## Groups

[config/groups.json](../config/groups.json) is the catalog. Each group lists the Scoop apps it installs, the
release assets it needs beyond those apps, and the VS Code extensions that go with it. `core` is always
installed. `wsl` is optional and only offered when WSL is already turned on.

## Release assets

Every file sits at the top level of the release. Anything of 1900 MiB or more is cut into
`<name>.part1`, `<name>.part2`, ... and `dtk.exe` reads the parts in order as one stream, so they are never
joined on disk. `SHA256SUMS.txt` lists every uploaded file, parts included.

| Asset | Built by | What is inside (paths relative to the zip root) |
|---|---|---|
| `dtk.exe`, `check.cmd`, `install.cmd` | `installer/` | The installer and its two entry points |
| `dev-toolkit.json` | `scripts/write-manifest.py` | Release manifest: groups, assets, parts, versions |
| `SHA256SUMS.txt`, `VERSIONS.md` | publish job | Hashes of every file; versions of everything inside |
| `scoop.zip` | `scripts/collect-tools.ps1` | `core/` (Scoop itself), `buckets/<name>/` (git checkouts at the pinned commit) |
| `tools-<group>-scoop.zip` | `scripts/collect-tools.ps1` | Scoop's download cache for the group's apps, file names exactly as Scoop writes them |
| `tools-<group>-portable.zip` | `scripts/collect-tools.ps1` | `apps/<app>/...` installed trees and `portable.json` (see below) |
| `rust-toolchain.zip` | `scripts/build-rust.ps1` | `rustup/` (RUSTUP_HOME) and `cargo/` (CARGO_HOME, with `bin/` and `advisory-db/`) |
| `crates.zip` | `scripts/build-crates.py` | A Cargo local registry: `index/` plus `<name>-<version>.crate` files at the root |
| `python.zip` | `scripts/build-python.ps1` | `cpython-3.14.<n>-windows-x86_64-none/` from python-build-standalone |
| `wheels-windows.zip` | `scripts/build-wheels.py` | Flat `.whl` files for CPython 3.14 on win_amd64, plus pure-Python wheels |
| `wheels-linux.zip` | `scripts/build-wheels.py` | Flat `.whl` files for CPython 3.14 on manylinux x86_64, plus pure-Python wheels (a complete set on its own) |
| `npm-cache.zip` | `scripts/build-npm-cache.mjs` | An npm cache folder (`_cacache/...`), with native packages for win32-x64 and linux-x64 |
| `nuget.zip` | `scripts/build-nuget-feed.py` | A NuGet v3 folder feed: `<id>/<version>/<id>.<version>.nupkg` (lowercase) with `.nupkg.sha512` and `.nuspec` |
| `vscode-extensions.zip` | `scripts/fetch-vscode-extensions.py` | `<group>/<publisher>.<name>-<version>.vsix` and `extensions.json` (install order per group) |
| `wsl-distro.tar.gz` | `scripts/build-distro.sh` | An Ubuntu 24.04 root filesystem that `wsl --import` accepts |

Each build script also writes a small `<asset>.facts.json` next to its output: version numbers, counts and
sizes for `VERSIONS.md` and the release manifest.

### portable.json

One per `tools-<group>-portable.zip`, for the route that installs without Scoop:

```json
{
  "apps": [
    {
      "name": "ripgrep",
      "version": "15.2.0",
      "bins":      [ { "path": "rg.exe", "alias": "rg", "args": "" } ],
      "paths":     [ "bin" ],
      "env":       { "GIT_INSTALL_ROOT": "{app}" },
      "shortcuts": [ { "target": "code.exe", "name": "Visual Studio Code", "args": "" } ],
      "rewrite":   [ "node_modules/npm/npmrc" ],
      "fonts":     [ "JetBrainsMonoNerdFont-Regular.ttf" ]
    }
  ],
  "buildRoots": [ "D:\\stage\\scoop\\apps\\ripgrep\\15.2.0", "D:\\stage\\scoop\\persist\\ripgrep" ]
}
```

Paths are relative to `apps/<name>/`. `{app}` is that folder once installed. `rewrite` lists text files that
contain one of the `buildRoots` paths, which `dtk.exe` replaces with the installed folder (as written, with
doubled backslashes, and with forward slashes).

## On the offline machine

`dtk.exe install` puts everything under one root, `%LOCALAPPDATA%\dev-toolkit` unless `--root` says
otherwise:

```
<root>\scoop\            Scoop route: SCOOP, with apps\, buckets\, cache\, shims\, persist\
<root>\portable\         Portable route: apps\<app>\ and shims\
<root>\rust\rustup\      RUSTUP_HOME
<root>\rust\cargo\       CARGO_HOME (bin\ on PATH, config.toml written here)
<root>\crates\           Cargo local registry
<root>\python\<build>\   Python 3.14 (it and its Scripts\ on PATH)
<root>\wheels\windows\   pip and uv find-links
<root>\wheels\linux\     find-links inside the WSL distro
<root>\npm-cache\        npm cache
<root>\nuget\            NuGet folder feed
<root>\wsl\              The imported distro's disk
<root>\installed.json    What was installed and every user setting changed, for uninstall
```

Config it writes (only keys it owns; existing files are merged, never replaced):

| File | Settings |
|---|---|
| `<root>\rust\cargo\config.toml` | crates-io replaced by the local registry; `net.offline = true`; GNU linker path |
| `%APPDATA%\pip\pip.ini` | `no-index`, `find-links`, `disable-pip-version-check` |
| `%APPDATA%\uv\uv.toml` | `offline`, `no-index`, `find-links`, `python-downloads = "never"` |
| `%USERPROFILE%\.npmrc` | `cache`, `offline`, `audit=false`, `fund=false`, `update-notifier=false` |
| `%APPDATA%\NuGet\NuGet.Config` | the local feed added, nuget.org disabled |
| VS Code `data\user-data\User\settings.json` | updates, telemetry and extension auto-update off |

User environment variables it sets: `PATH` additions, `CARGO_HOME`, `RUSTUP_HOME`, `DOTNET_CLI_TELEMETRY_OPTOUT`,
`DOTNET_NOLOGO`, `DOTNET_SKIP_FIRST_TIME_EXPERIENCE`, `DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE`,
`NUGET_CERT_REVOCATION_MODE=offline`, `UV_OFFLINE=1`, `UV_PYTHON_DOWNLOADS=never`, `DEV_TOOLKIT_ROOT`.

## Inside the WSL distro

The distro carries its own toolchains (Rust stable, Python 3.14 through uv, Node LTS, .NET SDK, build
tools) and a `dev-toolkit-link` command. `dtk.exe` runs it once after `wsl --import`, with the Windows root
as a `/mnt/<drive>/...` path, and it points Cargo, pip, uv, npm and NuGet inside the distro at the caches
under that root.
