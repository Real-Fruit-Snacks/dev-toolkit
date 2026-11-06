# Security

## Supported versions

The latest release only. A new one is built every Sunday from `main`; older releases get no fixes.

## Reporting

Please don't put the details of a vulnerability in a public issue. Open an issue that says you have a
security report, without the details, and I'll reply with a private way to send it.

Useful to include: the release tag, the file or script, and what it did that it shouldn't.

## Checking what you downloaded

The release files aren't signed. What is checked:

- `dtk.exe install` hashes every file it is about to use and compares it with `SHA256SUMS.txt` before
  it unpacks anything, and stops on a mismatch. `dtk.exe verify` checks the whole folder.
- The weekly build compares the SHA-256 that GitHub recorded for every uploaded file with the hash the
  build computed, and publishes only when they all match.
- Every release is installed in a Windows container with no network, as a user who isn't an
  administrator, and tested before it is published.

That proves the files are the ones the build made, not that an upstream tool is what its maker meant to
ship. The tools come from Scoop's manifests, which pin each download's hash; the Rust toolchains from
rustup; Python from python-build-standalone; packages from crates.io, PyPI, npm and nuget.org, checked
against those registries' own hashes where they publish them.

## What the install changes

Everything goes under one folder (`%LOCALAPPDATA%\dev-toolkit` unless you pick another). Outside it,
`dtk.exe` changes only your own user settings: user environment variables and PATH, the config files of
pip, uv, npm, NuGet, Scoop and VS Code (only the keys it owns; the original is kept as
`*.before-dev-toolkit`), per-user fonts and Start menu shortcuts. Each change is listed in
`installed.json`, and `dtk.exe uninstall` undoes them. Nothing needs or asks for admin rights.

## Out of scope

The bundled tools themselves. Report those to their projects; [VERSIONS.md](VERSIONS.md) lists them.
