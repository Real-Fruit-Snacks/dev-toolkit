#!/usr/bin/env python3
"""Uploads a build job's release files to the draft release.

Files of 1900 MiB or more are cut into <name>.part1, .part2, ... first (GitHub refuses 2 GiB and up).
For every logical asset it writes <meta>/<asset>.json with its files, their SHA-256 and the total size;
the manifest job turns those into dev-toolkit.json and SHA256SUMS.txt and checks them against the
digests GitHub recorded.

    python scripts/upload-assets.py --tag v1.0.0 --out out --meta meta [--no-upload]
"""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

PART = 1900 * 1024 * 1024
SUFFIXES = (".tar.gz", ".zip", ".exe", ".cmd")


def logical_name(name: str) -> str:
    for s in (".tar.gz", ".zip"):
        if name.endswith(s):
            return name[: -len(s)]
    return name  # dtk.exe, check.cmd, install.cmd keep their names


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def split(path: pathlib.Path) -> list:
    parts = []
    with open(path, "rb") as src:
        n = 0
        while True:
            n += 1
            part = path.with_name(f"{path.name}.part{n}")
            written = 0
            with open(part, "wb") as dst:
                while written < PART:
                    block = src.read(min(8 << 20, PART - written))
                    if not block:
                        break
                    dst.write(block)
                    written += len(block)
            if written == 0:
                part.unlink()
                break
            parts.append(part)
            if written < PART:
                break
    path.unlink()
    return parts


def upload(tag: str, files: list) -> None:
    for f in files:
        for attempt in range(1, 6):
            r = subprocess.run(["gh", "release", "upload", tag, str(f), "--clobber"])
            if r.returncode == 0:
                print(f"  uploaded {f.name}", flush=True)
                break
            print(f"  upload of {f.name} failed (try {attempt}), retrying", flush=True)
            time.sleep(15 * attempt)
        else:
            sys.exit(f"could not upload {f.name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()
    out = pathlib.Path(a.out)
    meta = pathlib.Path(a.meta)
    meta.mkdir(parents=True, exist_ok=True)

    for path in sorted(p for p in out.iterdir() if p.is_file() and p.name.endswith(SUFFIXES)):
        name = logical_name(path.name)
        size = path.stat().st_size
        files = split(path) if size >= PART else [path]
        if len(files) > 1:
            print(f"{path.name}: {size / 2**20:.0f} MiB -> {len(files)} parts", flush=True)
        hashes = {f.name: sha256(f) for f in files}
        if not a.no_upload:
            upload(a.tag, files)
        (meta / f"{name}.json").write_text(json.dumps(
            {"files": [f.name for f in files], "sha256": hashes, "size": size, "file": path.name}, indent=2))
        print(f"{name}: {len(files)} file(s), {size / 2**20:.0f} MiB", flush=True)


if __name__ == "__main__":
    main()
