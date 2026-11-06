#!/usr/bin/env python3
"""Builds a tiny release (ripgrep and jq, ready-unpacked) and runs dtk.exe against it in a sandbox
profile: install by the portable route, run the tools through the shims, then uninstall.

It checks dtk itself (verify, unzip, shims, PATH, variables, path rewriting, uninstall) in a minute,
without Scoop, containers or the big caches, and without touching the real user profile.

    python tests/mini-kit.py --dtk path\\to\\dtk.exe --work path\\to\\scratch
"""
import argparse
import hashlib
import io
import json
import pathlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile

RG = "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-pc-windows-msvc.zip"
JQ = "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-windows-amd64.exe"
SHIM = "https://raw.githubusercontent.com/ScoopInstaller/Scoop/master/supporting/shims/kiennq/shim.exe"
BUILD_ROOT = r"D:\stage\scoop\apps\ripgrep\14.1.1"


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "dev-toolkit-test"})) as r:
        return r.read()


def run(cmd, **kw):
    print("$", " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run(cmd, text=True, capture_output=True, **kw)
    print(r.stdout[-3000:], r.stderr[-2000:], sep="", flush=True)
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dtk", required=True)
    ap.add_argument("--work", required=True)
    a = ap.parse_args()
    work = pathlib.Path(a.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    kit = work / "kit"
    kit.mkdir(parents=True)
    home = work / "home"

    rg = zipfile.ZipFile(io.BytesIO(get(RG)))
    portable = {"apps": [
        {"name": "ripgrep", "version": "14.1.1",
         "bins": [{"path": "rg.exe", "alias": "rg", "args": ""}],
         "paths": [], "env": {"RIPGREP_HOME_TEST": "{app}"}, "shortcuts": [],
         "rewrite": ["settings.txt"], "fonts": [],
         "roots": [BUILD_ROOT, r"D:\stage\scoop\apps\ripgrep\current", r"D:\stage\scoop\persist\ripgrep"]},
        {"name": "jq", "version": "1.7.1",
         "bins": [{"path": "jq.exe", "alias": "jq", "args": ""}],
         "paths": ["."], "env": {}, "shortcuts": [], "rewrite": [], "fonts": [], "roots": []},
    ]}
    with zipfile.ZipFile(kit / "tools-core-portable.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("shims/shim.exe", get(SHIM))
        for info in rg.infolist():
            name = info.filename.split("/", 1)[1] if "/" in info.filename else ""
            if name and not info.is_dir():
                z.writestr("apps/ripgrep/" + name, rg.read(info))
        z.writestr("apps/ripgrep/settings.txt", f"home = {BUILD_ROOT}\nhome_json = {BUILD_ROOT.replace(chr(92), chr(92) * 2)}\n")
        z.writestr("apps/jq/jq.exe", get(JQ))
        z.writestr("portable.json", json.dumps(portable))

    manifest = {
        "version": "v0.0.0-mini",
        "groups": {"core": {"title": "mini core", "required": True, "apps": ["main/ripgrep", "main/jq"], "assets": [], "extensions": []}},
        "assets": {"tools-core-portable": {"files": ["tools-core-portable.zip"], "size": (kit / "tools-core-portable.zip").stat().st_size}},
    }
    (kit / "dev-toolkit.json").write_text(json.dumps(manifest))
    sums = "".join(f"{hashlib.sha256((kit / f).read_bytes()).hexdigest()}  {f}\n" for f in ("tools-core-portable.zip",))
    (kit / "SHA256SUMS.txt").write_text(sums)
    dtk = kit / "dtk.exe"
    shutil.copy(a.dtk, dtk)

    common = ["--home", str(home), "--root", str(home / "root")]
    fails = []
    r = run([str(dtk), "install", "--route", "portable", "--from", str(kit), *common])
    if r.returncode != 0:
        fails.append(f"install exit {r.returncode}")
    r = run([str(dtk), "exec", *common, "--", "rg", "--version"])
    if r.returncode != 0 or "ripgrep 14.1.1" not in r.stdout:
        fails.append("rg through the shim")
    r = run([str(dtk), "exec", *common, "--", "jq", "--version"])
    if r.returncode != 0 or "jq-1.7.1" not in r.stdout:
        fails.append("jq")
    settings = (home / "root" / "portable" / "apps" / "ripgrep" / "settings.txt").read_text()
    want = str(home / "root" / "portable" / "apps" / "ripgrep")
    if want not in settings or BUILD_ROOT in settings or want.replace("\\", "\\\\") not in settings:
        fails.append("path rewrite: " + settings)
    env = json.loads((home / "profile" / "user-environment.json").read_text())
    if env.get("RIPGREP_HOME_TEST") != want:
        fails.append(f"env var: {env.get('RIPGREP_HOME_TEST')}")
    if "portable\\shims" not in env.get("Path", ""):
        fails.append("shims not on PATH")
    r = run([str(dtk), "uninstall", *common])
    env = json.loads((home / "profile" / "user-environment.json").read_text())
    if r.returncode != 0 or env.get("RIPGREP_HOME_TEST") or "dev-toolkit" in env.get("Path", "") or (home / "root").exists():
        fails.append(f"uninstall left things behind: {env}")
    if fails:
        sys.exit("FAILED: " + "; ".join(fails))
    print("mini kit: all passed")


if __name__ == "__main__":
    main()
