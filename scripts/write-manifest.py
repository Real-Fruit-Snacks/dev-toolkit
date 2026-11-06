#!/usr/bin/env python3
"""Writes the release's dev-toolkit.json, SHA256SUMS.txt and VERSIONS.md from what the build jobs made.

Inputs: config/groups.json, the <asset>.json files upload-assets.py wrote (--meta), and the
*.facts.json files the build scripts wrote (--facts). With --check-release it also compares every
uploaded file's SHA-256 with the digest GitHub recorded for it on the draft, and fails on any difference.

    python scripts/write-manifest.py --tag v1.0.0 --meta meta --facts facts --out release-meta [--check-release]
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys

REPO = "Real-Fruit-Snacks/dev-toolkit"


def load_dir(d: pathlib.Path, suffix: str) -> dict:
    out = {}
    if d.is_dir():
        for p in sorted(d.rglob(f"*{suffix}")):
            out[p.name[: -len(suffix)]] = json.loads(p.read_text(encoding="utf-8-sig"))
    return out


def mib(n: int) -> str:
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--config", default="config/groups.json")
    ap.add_argument("--meta", required=True)
    ap.add_argument("--facts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--check-release", action="store_true")
    a = ap.parse_args()

    cfg = json.loads(pathlib.Path(a.config).read_text(encoding="utf-8"))
    meta = load_dir(pathlib.Path(a.meta), ".json")
    facts = load_dir(pathlib.Path(a.facts), ".facts.json")
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    tools = facts.get("tools", {})
    vsix = facts.get("vscode-extensions", {})
    shipped_ext = set()
    for e in vsix.get("extensions", []) if isinstance(vsix.get("extensions"), list) else []:
        if isinstance(e, dict) and e.get("id"):
            shipped_ext.add(e["id"].lower())

    groups = {}
    problems = []
    for name, g in cfg["groups"].items():
        installed = tools.get("groups", {}).get(name, {}).get("apps", {})
        apps = []
        for app in g.get("apps", []):
            app_name = app["app"].split("/")[-1]
            if not tools or app_name in installed:
                apps.append(app["app"])
            else:
                problems.append(f"{app['app']} is not in this release (it failed to install on the build machine)")
        exts = [e for e in g.get("extensions", []) if not shipped_ext or e.lower() in shipped_ext]
        for e in g.get("extensions", []):
            if shipped_ext and e.lower() not in shipped_ext:
                problems.append(f"VS Code extension {e} is not in this release")
        assets = list(g.get("assets", []))
        missing = [x for x in assets if x not in meta]
        if g.get("apps"):
            missing += [x for x in (f"tools-{name}-scoop", f"tools-{name}-portable") if x not in meta]
        if missing:
            if g.get("required"):
                sys.exit(f"the required group {name} is missing assets: {', '.join(missing)}")
            problems.append(f"group {name} is left out: its assets didn't build ({', '.join(missing)})")
            continue
        groups[name] = {
            "title": g.get("title", name),
            "required": bool(g.get("required")),
            "optional": bool(g.get("optional")),
            "apps": apps,
            "assets": assets,
            "extensions": exts,
        }

    manifest = {
        "version": a.tag,
        "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "repo": REPO,
        "groups": groups,
        "assets": {k: {"files": v["files"], "size": v["size"]} for k, v in sorted(meta.items())},
        "facts": {k: trim(v) for k, v in facts.items()},
    }
    mf = out / "dev-toolkit.json"
    mf.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    sums = {}
    for v in meta.values():
        sums.update(v["sha256"])
    sums["dev-toolkit.json"] = hashlib.sha256(mf.read_bytes()).hexdigest()
    versions = out / "VERSIONS.md"
    versions.write_text(render_versions(a.tag, manifest, meta, facts, problems), encoding="utf-8")
    sums["VERSIONS.md"] = hashlib.sha256(versions.read_bytes()).hexdigest()
    (out / "SHA256SUMS.txt").write_text("".join(f"{h}  {n}\n" for n, h in sorted(sums.items())), encoding="utf-8")

    for p in problems:
        print(f"::warning::{p}")
    print(f"dev-toolkit.json: {len(groups)} groups, {len(meta)} assets, {len(sums)} files in SHA256SUMS.txt")

    if a.check_release:
        check_release(a.tag, meta)


def check_release(tag: str, meta: dict) -> None:
    """Every file a job uploaded must be on the draft with the digest the job computed."""
    rels = json.loads(subprocess.check_output(["gh", "api", f"repos/{REPO}/releases?per_page=30"]))
    rel = next((r for r in rels if r["tag_name"] == tag), None)
    if not rel:
        sys.exit(f"no release with tag {tag}")
    assets = {}
    page = 1
    while True:
        batch = json.loads(subprocess.check_output(["gh", "api", f"repos/{REPO}/releases/{rel['id']}/assets?per_page=100&page={page}"]))
        if not batch:
            break
        for x in batch:
            assets[x["name"]] = (x.get("digest") or "").replace("sha256:", "")
        page += 1
    bad = 0
    for v in meta.values():
        for f, h in v["sha256"].items():
            got = assets.get(f)
            if got is None:
                print(f"  MISSING  {f}")
                bad += 1
            elif got and got != h:
                print(f"  DIFFERS  {f}")
                bad += 1
    if bad:
        sys.exit(f"{bad} uploaded file(s) don't match")
    print(f"all {sum(len(v['sha256']) for v in meta.values())} uploaded files match GitHub's digests")


def trim(v):
    """The facts without their long lists (every seed, every wheel): the manifest keeps the counts."""
    if isinstance(v, dict):
        return {k: trim(x) for k, x in v.items() if not (isinstance(x, list) and len(x) > 50)}
    if isinstance(v, list):
        return [trim(x) for x in v]
    return v


def render_versions(tag: str, m: dict, meta: dict, facts: dict, problems: list) -> str:
    L = [f"# dev-toolkit {tag}", "", f"Built {m['built']} by the weekly release workflow. Every file and its SHA-256 is in `SHA256SUMS.txt`.", ""]
    tools = facts.get("tools", {})
    if tools:
        L += ["## Tools (Scoop apps)", "", "| Group | App | Version |", "|---|---|---|"]
        for g, info in tools.get("groups", {}).items():
            for app, ver in info.get("apps", {}).items():
                L.append(f"| {g} | {app} | {ver} |")
        commits = tools.get("commits", {})
        if commits:
            L += ["", "Scoop and its buckets at: " + ", ".join(f"{k} `{(v or '')[:10]}`" for k, v in commits.items()), ""]
    rt = facts.get("rust-toolchain")
    if rt:
        L += ["## Rust", "", f"- {rt.get('msvc')} (MSVC) and {rt.get('gnu')} (GNU)", f"- {rt.get('rustup')}"]
        for t, v in (rt.get("tools") or {}).items():
            L.append(f"- {t} {v}")
        L.append(f"- RustSec advisory database as of {rt.get('advisoryDb')}")
    cr = facts.get("crates")
    if cr:
        L += [f"- crates: {cr.get('crates', cr.get('crate_count', '?'))} crates, {cr.get('versions', cr.get('version_count', '?'))} versions"]
    L.append("")
    py = facts.get("python")
    if py:
        L += ["## Python", "", f"- Python {py.get('version')} ({py.get('build')}), pip {py.get('pip')}"]
        for plat in ("windows", "linux"):
            w = facts.get(f"wheels-{plat}")
            if w:
                seeds = w.get("seeds") if isinstance(w.get("seeds"), dict) else {}
                L.append(f"- {plat} wheels: {w.get('wheels', '?')} files for {seeds.get('ok', '?')} of PyPI's most downloaded projects and what they depend on")
        L.append("")
    npm = facts.get("npm-cache")
    if npm:
        seeds = npm.get("seeds") if isinstance(npm.get("seeds"), dict) else {}
        cache = npm.get("cache") if isinstance(npm.get("cache"), dict) else {}
        L += ["## Node.js", "", f"- npm cache: {cache.get('packages', '?')} packages ({cache.get('tarballs', '?')} tarballs), from {seeds.get('ok', '?')} of npm's most used packages and their dependencies, with native builds for win32-x64 and linux-x64", ""]
    nu = facts.get("nuget")
    if nu:
        L += ["## .NET", "", f"- NuGet feed: {nu.get('package_count', nu.get('packages', '?'))} packages, {nu.get('version_count', nu.get('versions', '?'))} versions (SDK {nu.get('sdk', '?')}, {nu.get('tfm', '?')})", ""]
    wsl = facts.get("wsl-distro")
    if wsl:
        tools_ = wsl.get("tools") or {}
        L += ["## WSL distro", "",
              f"- Ubuntu {wsl.get('ubuntu')} ({(wsl.get('image') or {}).get('name', '')}), default user `{wsl.get('user', 'dev')}`",
              f"- Rust {wsl.get('rust')}, Python {wsl.get('python')} (uv {wsl.get('uv')}), Node {wsl.get('node')} (npm {wsl.get('npm')}), .NET SDK {wsl.get('dotnetSdk')}",
              "- " + ", ".join(f"{k} {v}" for k, v in tools_.items()), ""]
    vs = facts.get("vscode-extensions")
    if vs and isinstance(vs.get("extensions"), list):
        L += ["## VS Code extensions", "", "From Open VSX, except CodeLLDB, which is its maker's self-contained Windows build from GitHub.", "",
              "| Extension | Version | Licence |", "|---|---|---|"]
        copyleft = []
        for e in vs["extensions"]:
            lic = e.get("licenceDetected") or e.get("licence") or e.get("license", "")
            L.append(f"| {e.get('id')} | {e.get('version')} | {lic} |")
            if "GPL" in str(lic).upper():
                ns, _, name = str(e.get("id")).partition(".")
                copyleft.append(f"- {e.get('id')} is {lic}; its source is linked from https://open-vsx.org/extension/{ns}/{name}")
        L.append("")
        if copyleft:
            L += copyleft + [""]
    L += ["## Files", "", "| Asset | Size | Files |", "|---|---|---|"]
    for k, v in sorted(meta.items()):
        L.append(f"| {k} | {mib(v['size'])} | {len(v['files'])} |")
    if problems:
        L += ["", "## Left out of this build", ""] + [f"- {p}" for p in problems]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
