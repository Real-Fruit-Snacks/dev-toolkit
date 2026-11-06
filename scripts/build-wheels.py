#!/usr/bin/env python3
"""Download a wheelhouse of popular PyPI packages for offline installs.

Fills one flat folder with wheels for CPython 3.14 on Windows (win_amd64) or on Linux (manylinux x86_64,
for the Ubuntu 24.04 WSL distro), so that on the offline machine

    pip install --no-index --find-links <folder> <package>
    uv pip install --offline --no-index --find-links <folder> <package>

work without a network. The host does not have to match the target: pip is given the target's Python
version, implementation, ABI and platform tags, and its environment markers are evaluated for the target
as well (see PIP_SHIM).

Seeds are the top N projects by downloads plus a curated list (config/pypi-extra.txt). Each seed is
resolved on its own, because seeds conflict with each other, into a per-worker temporary folder, and the
result is merged into --out by file name. A seed with no compatible wheel, a failed resolve or a closure
larger than --max-mb is skipped and recorded in the facts file; one that resolves to an older release
than PyPI's newest (its newest needs a dependency without a wheel) is kept and flagged there. Problems
that would affect every seed
(no network, pip missing, marker patch not working, most seeds failing, a wheel the target cannot
install) stop the build with a non-zero exit code.

Examples:
    python scripts/build-wheels.py --platform windows --exclude config/pypi-exclude.txt
    python scripts/build-wheels.py --platform linux --top 200 --out stage/wheels-linux --zip stage/wheels-linux.zip

Needs Python 3.9 or later with pip 22 or later. Nothing is installed; pip only downloads.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import fnmatch
import itertools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOP_URL = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json"
PYPI_JSON = "https://pypi.org/pypi/{}/json"
USER_AGENT = "dev-toolkit build-wheels"
MIB = 1024 * 1024

SEED_TIMEOUT = 1200  # seconds for one seed's pip run; a resolve that hangs is recorded as a skip
ATTEMPTS = 3  # pip runs per seed when the failure looks like a network problem
MIN_OK_SHARE = 0.6  # fewer tried seeds than this succeeding means something systemic is wrong
MAX_LOST_SHARE = 0.02  # more seeds than this lost to network or unexpected errors also fails the build

# pip evaluates environment markers (sys_platform == "win32", python_version < "3.11", ...) against the
# interpreter it runs in, even with --platform and --python-version. This runs pip with the marker
# environment replaced by the target's, so a Linux runner building for Windows still gets colorama for
# pytest and pywinpty for Jupyter, and a Python 3.12 runner gets the dependency set of 3.14.
PIP_SHIM = """
import json, os, sys
target = json.loads(os.environ["BUILD_WHEELS_MARKERS"])
from pip._vendor.packaging import markers
host_environment = markers.default_environment
def default_environment():
    env = host_environment()
    env.update(target)
    return env
markers.default_environment = default_environment
from pip._internal.cli.main import main
sys.exit(main(sys.argv[1:]))
"""

# Signs in pip's output that a failure came from the network or a damaged download, not the package.
NETWORK_RE = re.compile(
    r"Could not fetch URL|Max retries exceeded|ConnectionError|ConnectTimeout|ReadTimeoutError|"
    r"IncompleteRead|ProtocolError|RemoteDisconnected|HTTP error 5\d\d|50\d Server Error|"
    r"THESE PACKAGES DO NOT MATCH THE HASHES|Connection broken"
)
NOT_FOUND_RE = re.compile(r"No matching distribution found for (\S+)")
NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

_print_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def annotate(kind: str, message: str) -> None:
    """Log a warning or error, and surface it as an annotation when running in GitHub Actions."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        log(f"::{kind}::{message}")
    else:
        log(f"{kind.upper()}: {message}")


def normalize(name: str) -> str:
    """PEP 503 project name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str:
    m = NAME_RE.match(requirement)
    return normalize(m.group(1)) if m else normalize(requirement.strip())


def mib(n: int) -> str:
    return f"{n / MIB:,.1f} MiB"


class Target:
    """The interpreter and platform the wheels are for, as pip arguments and a marker environment."""

    def __init__(self, platform: str, python: str, glibc: str) -> None:
        parts = python.split(".")
        if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts) or parts[0] != "3":
            raise SystemExit(f"--python must look like 3.14 or 3.14.2, not {python!r}")
        self.platform = platform
        self.python = ".".join(parts[:2])
        self.full = ".".join(parts) if len(parts) == 3 else self.python + ".0"
        self.minor = int(parts[1])
        self.abi = "cp3" + parts[1]
        if platform == "windows":
            self.platform_tags = ["win_amd64"]
            self.markers = {"os_name": "nt", "sys_platform": "win32", "platform_system": "Windows",
                            "platform_machine": "AMD64"}
        else:
            m = re.fullmatch(r"2\.(\d+)", glibc)
            if not m or int(m.group(1)) < 5:
                raise SystemExit(f"--glibc must look like 2.39, not {glibc!r}")
            ceiling = int(m.group(1))
            # pip does not expand manylinux_2_39 to the older tags a 2.39 system also runs, so list them
            # all, newest first, plus the legacy aliases of PEP 600 (2014 = 2_17, 2010 = 2_12, 1 = 2_5).
            tags = [f"manylinux_2_{n}_x86_64" for n in range(ceiling, 4, -1)]
            tags += [f"{alias}_x86_64" for alias, n in (("manylinux2014", 17), ("manylinux2010", 12),
                                                         ("manylinux1", 5)) if n <= ceiling]
            self.platform_tags = tags
            self.markers = {"os_name": "posix", "sys_platform": "linux", "platform_system": "Linux",
                            "platform_machine": "x86_64"}
        self.markers.update({
            "python_version": self.python,
            "python_full_version": self.full,
            "implementation_name": "cpython",
            "implementation_version": self.full,
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_version": "",
        })
        self.platform_set = set(self.platform_tags)

    def pip_args(self) -> list[str]:
        args = ["--only-binary=:all:", "--python-version", self.full, "--implementation", "cp",
                "--abi", self.abi, "--abi", "abi3", "--abi", "none"]
        for tag in self.platform_tags:
            args += ["--platform", tag]
        return args

    def wheel_kind(self, filename: str) -> str | None:
        """'pure', 'abi3', the target ABI (e.g. 'cp314') or 'binary' (py3-none-<platform>) if the target
        installs this wheel, else None."""
        parts = filename[:-4].split("-")
        if not filename.endswith(".whl") or len(parts) not in (5, 6):
            return None
        best = None
        for py, abi, plat in itertools.product(*(p.split(".") for p in parts[-3:])):
            if not self._tag_supported(py, abi, plat):
                continue
            kind = "pure" if plat == "any" else "abi3" if abi == "abi3" else abi if abi == self.abi else "binary"
            if best is None or kind == "pure":
                best = kind
        return best

    def _tag_supported(self, py: str, abi: str, plat: str) -> bool:
        if plat != "any" and plat not in self.platform_set:
            return False
        m = re.fullmatch(r"(cp|py)3(\d*)", py)
        if not m:
            return False
        minor = int(m.group(2)) if m.group(2) else None
        if abi == self.abi:
            return m.group(1) == "cp" and minor == self.minor
        if abi == "abi3":
            return m.group(1) == "cp" and minor is not None and 2 <= minor <= self.minor
        if abi == "none":
            if m.group(1) == "cp":
                return minor == self.minor
            return minor is None or minor <= self.minor
        return False


class Seed:
    def __init__(self, requirement: str, index: int) -> None:
        self.requirement = requirement
        self.name = requirement_name(requirement)
        self.index = index
        self.rank: int | None = None  # position in the top list
        self.extra = False  # listed in the curated file

    def key(self) -> str:
        """Name plus extras, version and marker, so "uvicorn" and "uvicorn[standard]" stay separate."""
        m = NAME_RE.match(self.requirement)
        rest = self.requirement[m.end():] if m else ""
        return self.name + re.sub(r"\s+", "", rest).lower()

    def origin(self) -> dict:
        d: dict = {}
        if self.rank is not None:
            d["rank"] = self.rank
        if self.extra:
            d["extra"] = True
        return d


def read_list(path: Path) -> list[str]:
    """Non-empty lines of a text file, with # comments removed."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def fetch_top(n: int) -> tuple[list[str], str]:
    request = urllib.request.Request(TOP_URL, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.load(response)
            break
        except Exception as e:  # noqa: BLE001 - any failure here is retried, then fatal
            if attempt == 3:
                raise SystemExit(f"could not fetch the top package list from {TOP_URL}: {e}") from e
            log(f"fetching the top list failed ({e}); retrying")
            time.sleep(10 * attempt)
    rows = data.get("rows") or []
    names = [row["project"] for row in rows[:n]]
    if len(names) < n:
        annotate("warning", f"the top list has only {len(names)} projects, fewer than --top {n}")
    if n and not names:
        raise SystemExit(f"the top list from {TOP_URL} has no rows")
    return names, str(data.get("last_update", ""))


def newest_release(name: str) -> str | None:
    """PyPI's newest release of a project, or None if the lookup fails; it is only informational."""
    request = urllib.request.Request(PYPI_JSON.format(name), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return str(json.load(response)["info"]["version"])
    except Exception:  # noqa: BLE001
        return None


def older(version: str, latest: str) -> bool:
    try:
        from pip._vendor.packaging.version import Version
        return Version(version) < Version(latest)
    except Exception:  # noqa: BLE001 - no pip packaging or an odd version string
        return version != latest


def build_seeds(top: list[str], extra: list[str], exclude: list[str]) -> tuple[list[Seed], list[dict]]:
    seeds: dict[str, Seed] = {}
    for line in extra:
        seed = Seed(line, len(seeds))
        seed.extra = True
        seeds.setdefault(seed.key(), seed)
    for rank, name in enumerate(top, 1):
        seed = Seed(name, len(seeds))
        seed = seeds.setdefault(seed.key(), seed)
        if seed.rank is None:
            seed.rank = rank
    patterns = [normalize(p) for p in exclude]
    kept, excluded = [], []
    for seed in seeds.values():
        hit = next((p for p in patterns if fnmatch.fnmatchcase(seed.name, p)), None)
        if hit:
            excluded.append({"seed": seed.requirement, "pattern": hit, **seed.origin()})
        else:
            kept.append(seed)
    return kept, excluded


def retry_os(action, *args):
    """Run a file operation, retrying briefly: on Windows a virus scanner can hold a fresh file open."""
    for attempt in range(1, 6):
        try:
            return action(*args)
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(attempt)


def reset_dir(path: Path) -> None:
    if path.exists():
        retry_os(shutil.rmtree, path)
    path.mkdir(parents=True)


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def detail_lines(output: str, limit: int = 600) -> str:
    """The lines of pip's output that say what went wrong, kept short for the log and the facts file."""
    lines = [line.rstrip() for line in output.splitlines()]
    picked = [line[len("ERROR: "):] for line in lines if line.startswith("ERROR: ")]
    if "The conflict is caused by:" in output:
        start = lines.index(next(line for line in lines if "The conflict is caused by:" in line))
        picked += [line.strip() for line in lines[start + 1:start + 4] if line.strip()]
    if not picked:
        picked = [line for line in lines if line.strip()][-3:]
    # pip's "Cannot install x==1, x==2, ..." lines can list every version; keep each line short.
    return shorten(" | ".join(dict.fromkeys(shorten(p, 200) for p in picked)), limit)


def unavailable(output: str) -> list[str]:
    """Names pip lists under "no matching distributions available for your environment" when a resolve
    fails because some dependency has no compatible wheel at all."""
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if "no matching distributions available for your environment" in line:
            names = []
            for following in lines[i + 1:]:
                if not following.startswith((" ", "\t")) or not following.strip():
                    break
                names.append(requirement_name(following))
            return names
    return []


def classify(output: str, seed: Seed) -> tuple[str, str]:
    """Turn a failed pip run into (reason, detail)."""
    if "ResolutionTooDeep" in output:
        return "too-deep", detail_lines(output)
    missing = [name for name in unavailable(output) if name != seed.name]
    if missing:
        return "dependency-no-wheel", f"no compatible wheel for {', '.join(missing)}"
    if unavailable(output):
        return "no-wheel", detail_lines(output)
    if "ResolutionImpossible" in output or "conflicting dependencies" in output:
        return "conflict", detail_lines(output)
    m = NOT_FOUND_RE.search(output)
    if m and "Could not fetch URL" not in output:
        missing = requirement_name(m.group(1))
        if missing == seed.name:
            return "no-wheel", detail_lines(output)
        return "dependency-no-wheel", f"no compatible wheel for {missing}: " + detail_lines(output)
    if NETWORK_RE.search(output):
        return "network", detail_lines(output)
    return "error", detail_lines(output)


class Build:
    def __init__(self, args: argparse.Namespace, target: Target) -> None:
        self.args = args
        self.target = target
        self.out: Path = args.out
        self.work: Path = args.out.parent / f".wheels-{target.platform}.work"
        self.cache: Path = args.cache_dir or self.work / "pip-cache"
        self.max_bytes = args.max_mb * MIB if args.max_mb > 0 else None
        self.env = dict(os.environ, BUILD_WHEELS_MARKERS=json.dumps(target.markers), PYTHONUTF8="1",
                        PYTHONIOENCODING="utf-8")
        self.merge_lock = threading.Lock()
        self.slots: queue.Queue[int] = queue.Queue()
        for slot in range(args.jobs):
            self.slots.put(slot)
        self.ok: dict[int, dict] = {}
        self.skipped: dict[int, dict] = {}
        self.done = 0
        self.started = time.monotonic()

    def pip(self, requirement: str, dest: Path) -> tuple[int, str]:
        cmd = [sys.executable, "-c", PIP_SHIM, "--isolated", "download", requirement, "--dest", str(dest),
               *self.target.pip_args(), "--cache-dir", str(self.cache), "--progress-bar", "off",
               "--disable-pip-version-check", "--no-input", "--timeout", "60", "--retries", "5"]
        try:
            p = subprocess.run(cmd, cwd=self.work, env=self.env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                               timeout=SEED_TIMEOUT)
            return p.returncode, p.stdout
        except subprocess.TimeoutExpired as e:
            out = e.output or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            return -1, out + f"\n(timed out after {SEED_TIMEOUT}s)"

    def canary(self) -> None:
        """One small download that fails fast if pip, the network or the marker patch is broken.

        The marker below is true only for the target, so a pip that ignored the patch would download
        nothing when the host differs from the target."""
        t = self.target
        requirement = (f'requests; sys_platform == "{t.markers["sys_platform"]}" '
                       f'and python_full_version == "{t.full}"')
        dest = self.work / "canary"
        reset_dir(dest)
        rc, output = self.pip(requirement, dest)
        if rc != 0:
            log(output)
            raise SystemExit("the canary download (requests) failed; see pip's output above")
        if not any(dest.glob("requests-*.whl")):
            log(output)
            raise SystemExit("the canary download got no requests wheel: pip did not evaluate markers "
                             "for the target, so the marker patch no longer works with this pip")
        shutil.rmtree(dest)

    def run_seed(self, seed: Seed, total: int) -> None:
        slot = self.slots.get()
        dest = self.work / f"w{slot}"
        try:
            t0 = time.monotonic()
            for attempt in range(1, ATTEMPTS + 1):
                reset_dir(dest)
                rc, output = self.pip(seed.requirement, dest)
                if rc == 0:
                    break
                if rc == -1:
                    reason, detail = "timeout", detail_lines(output)
                    break
                reason, detail = classify(output, seed)
                if reason != "network" or attempt == ATTEMPTS:
                    break
                log(f"retry {seed.requirement}: network trouble ({detail[:120]})")
                time.sleep(15 * attempt)
            seconds = time.monotonic() - t0
            if rc == 0:
                self.finish_ok(seed, dest, seconds, total)
            else:
                self.record_skip(seed, reason, detail, total)
        finally:
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            self.slots.put(slot)

    def finish_ok(self, seed: Seed, dest: Path, seconds: float, total: int) -> None:
        files = sorted(dest.iterdir())
        stray = [f.name for f in files if f.suffix != ".whl"]
        if stray:
            self.record_skip(seed, "error", "pip saved files that are not wheels: " + ", ".join(stray), total)
            return
        if not files:
            self.record_skip(seed, "not-for-platform", "its environment marker excludes this platform", total)
            return
        size = sum(f.stat().st_size for f in files)
        if self.max_bytes and size > self.max_bytes:
            self.record_skip(seed, "too-large", f"closure of {len(files)} wheels is {mib(size)}, over "
                             f"--max-mb {self.args.max_mb}", total, size=size)
            return
        version = ""
        for f in files:
            name, ver = f.name.split("-")[:2]
            if normalize(name) == seed.name:
                version = ver
        # A seed can resolve to an old release when its newest one needs a dependency that has no
        # compatible wheel; pip then backtracks silently. Record how far behind it is.
        latest = newest_release(seed.name) if version else None
        behind = latest if latest and older(version, latest) else None
        added = 0
        with self.merge_lock:
            for f in files:
                target = self.out / f.name
                if target.exists():
                    retry_os(f.unlink)
                else:
                    retry_os(os.replace, f, target)
                    added += 1
            entry = {"seed": seed.requirement, "version": version, "wheels": len(files), "bytes": size,
                     **seed.origin()}
            if behind:
                entry["latest"] = behind
            self.ok[seed.index] = entry
            note = f" (latest is {behind})" if behind else ""
            self.progress(f"ok    {seed.requirement} {version}{note}: {len(files)} wheels, {mib(size)}, "
                          f"{added} new, {seconds:.1f}s", total)

    def record_skip(self, seed: Seed, reason: str, detail: str, total: int, size: int | None = None) -> None:
        with self.merge_lock:
            entry = {"seed": seed.requirement, "reason": reason, "detail": detail, **seed.origin()}
            if size is not None:
                entry["bytes"] = size
            self.skipped[seed.index] = entry
            self.progress(f"skip  {seed.requirement}: {reason}: {detail[:200]}", total)

    def progress(self, message: str, total: int) -> None:
        """Log one finished seed; call with merge_lock held."""
        self.done += 1
        log(f"[{self.done:>{len(str(total))}}/{total}] {message}")
        if self.done % 100 == 0 and self.done < total:
            elapsed = time.monotonic() - self.started
            eta = elapsed / self.done * (total - self.done)
            log(f"  -- {self.done}/{total} seeds in {elapsed / 60:.1f} min, {len(self.ok)} ok, "
                f"{len(self.skipped)} skipped, about {eta / 60:.0f} min to go")


def audit(out: Path, target: Target) -> tuple[list[dict], dict, list[str]]:
    """List the wheels in out, count them by kind and return any the target would not install."""
    wheels, kinds, bad = [], {}, []
    for f in sorted(out.iterdir()):
        kind = target.wheel_kind(f.name)
        if kind is None:
            bad.append(f.name)
            continue
        kinds[kind] = kinds.get(kind, 0) + 1
        wheels.append({"file": f.name, "bytes": f.stat().st_size})
    return wheels, kinds, bad


def write_zip(path: Path, out: Path) -> None:
    """Wheels are already compressed, so store them; fixed timestamps keep the zip reproducible."""
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for f in sorted(out.iterdir()):
            info = zipfile.ZipInfo(f.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            with f.open("rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    os.replace(tmp, path)


def pip_version() -> str:
    p = subprocess.run([sys.executable, "-m", "pip", "--version"], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
    m = re.match(r"pip (\d+)(\.\d+)*", p.stdout)
    if p.returncode != 0 or not m:
        raise SystemExit(f"pip is not available for {sys.executable}: {p.stdout.strip()}")
    if int(m.group(1)) < 22:
        raise SystemExit(f"pip 22 or later is needed, found {m.group(0)}")
    return m.group(0).split()[1]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--platform", required=True, choices=["windows", "linux"])
    p.add_argument("--top", type=int, default=2000, help="seeds from the top of the PyPI download list (default 2000)")
    p.add_argument("--extra", type=Path, default=REPO / "config" / "pypi-extra.txt",
                   help="curated seeds, one requirement per line (default config/pypi-extra.txt)")
    p.add_argument("--exclude", type=Path, help="seeds never to try: names or fnmatch patterns, one per line")
    p.add_argument("--python", default="3.14", help="target CPython, 3.14 or 3.14.2 (default 3.14)")
    p.add_argument("--glibc", default="2.39", help="newest glibc on the Linux target (default 2.39, Ubuntu 24.04)")
    p.add_argument("--out", type=Path, help="flat folder for the wheels (default out/wheels-<platform> in the repo)")
    p.add_argument("--max-mb", type=int, default=400,
                   help="skip a seed whose own download closure is larger than this many MiB; 0 for no limit (default 400)")
    p.add_argument("--jobs", type=int, default=8, help="seeds resolved at once (default 8)")
    p.add_argument("--facts", type=Path, help="facts file (default <out>/../wheels-<platform>.facts.json)")
    p.add_argument("--zip", type=Path, help="also write the wheels into this zip, flat, uncompressed")
    p.add_argument("--cache-dir", type=Path, help="pip cache to use and keep, e.g. shared by both platforms "
                   "(default a temporary one that is deleted)")
    p.add_argument("--clean", action="store_true", help="delete the wheels already in --out first")
    args = p.parse_args(argv)
    if args.jobs < 1 or args.top < 0:
        p.error("--jobs must be at least 1 and --top at least 0")
    args.out = (args.out or REPO / "out" / f"wheels-{args.platform}").resolve()
    args.facts = (args.facts or args.out.parent / f"wheels-{args.platform}.facts.json").resolve()
    if args.cache_dir:
        args.cache_dir = args.cache_dir.resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = parse_args(argv)
    target = Target(args.platform, args.python, args.glibc)
    started = time.monotonic()
    date = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    pip = pip_version()
    log(f"build-wheels: {args.platform}, CPython {target.full} ({target.abi}), pip {pip} on Python "
        f"{sys.version.split()[0]} ({sys.platform})")
    log(f"platform tags: {' '.join(target.platform_tags)}")

    if not args.extra.is_file():
        raise SystemExit(f"--extra file not found: {args.extra}")
    extra = read_list(args.extra)
    exclude = read_list(args.exclude) if args.exclude else []
    top, top_date = fetch_top(args.top) if args.top else ([], "")
    seeds, excluded = build_seeds(top, extra, exclude)
    log(f"seeds: {len(seeds)} to try ({len(top)} from the top list, updated {top_date or 'n/a'}; "
        f"{len(extra)} curated; {len(excluded)} excluded)")
    if not seeds:
        raise SystemExit("no seeds to download")

    out = args.out
    if out.exists():
        entries = list(out.iterdir())
        if entries and not args.clean:
            raise SystemExit(f"{out} is not empty; pass --clean to delete its wheels first")
        for f in entries:
            if f.suffix != ".whl" or not f.is_file():
                raise SystemExit(f"{out} holds something other than wheels ({f.name}); not cleaning it")
            f.unlink()
    out.mkdir(parents=True, exist_ok=True)

    build = Build(args, target)
    reset_dir(build.work)
    build.cache.mkdir(parents=True, exist_ok=True)
    try:
        build.canary()
        log(f"canary ok; resolving {len(seeds)} seeds with {args.jobs} workers")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(build.run_seed, seed, len(seeds)) for seed in seeds]
            try:
                for future in concurrent.futures.as_completed(futures):
                    future.result()  # re-raises unexpected errors in the worker, which stop the build
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    finally:
        shutil.rmtree(build.work, ignore_errors=True)
        if args.cache_dir is None:
            shutil.rmtree(build.cache, ignore_errors=True)

    wheels, kinds, bad = audit(out, target)
    total_bytes = sum(w["bytes"] for w in wheels)
    ok = [build.ok[i] for i in sorted(build.ok)]
    skipped = [build.skipped[i] for i in sorted(build.skipped)]
    reasons: dict[str, int] = {}
    for entry in skipped:
        reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + 1
    behind = [e for e in ok if "latest" in e]
    elapsed = round(time.monotonic() - started)

    facts = {
        "asset": f"wheels-{args.platform}",
        "platform": args.platform,
        "python": target.python,
        "python_full_version": target.full,
        "abi": target.abi,
        "platform_tags": target.platform_tags,
        "date": date,
        "pip": pip,
        "top": {"source": TOP_URL, "last_update": top_date, "requested": args.top, "used": len(top)},
        "max_mb": args.max_mb,
        "seeds": {"tried": len(seeds), "ok": len(ok), "skipped": len(skipped), "excluded": len(excluded),
                  "skip_reasons": dict(sorted(reasons.items())), "ok_behind_latest": len(behind)},
        "wheels": len(wheels),
        "bytes": total_bytes,
        "wheel_kinds": dict(sorted(kinds.items())),
        "largest_seeds": sorted(ok, key=lambda e: -e["bytes"])[:20],
        "largest_wheels": sorted(wheels, key=lambda w: -w["bytes"])[:10],
        "elapsed_seconds": elapsed,
        "ok": ok,
        "skipped": skipped,
        "excluded": excluded,
    }
    args.facts.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.facts.with_name(args.facts.name + ".tmp")
    tmp.write_text(json.dumps(facts, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, args.facts)

    log("")
    log(f"wheels: {len(wheels)} files, {mib(total_bytes)} in {out}")
    log("kinds: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    reason_text = ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())) or "none"
    log(f"seeds: {len(ok)} ok, {len(skipped)} skipped ({reason_text}), {len(excluded)} excluded")
    if behind:
        log("older than PyPI's latest: " + ", ".join(f"{e['seed']} {e['version']} < {e['latest']}" for e in behind))
    log("largest seeds: " + ", ".join(f"{e['seed']} {mib(e['bytes'])}" for e in facts["largest_seeds"][:10]))
    log(f"facts: {args.facts}")
    log(f"took {elapsed / 60:.1f} min")
    for entry in skipped:
        if entry.get("extra") and entry["reason"] != "not-for-platform":
            annotate("warning", f"curated seed {entry['seed']} skipped: {entry['reason']}: {entry['detail'][:300]}")

    failures = []
    if bad:
        failures.append(f"{len(bad)} files the target would not install, e.g. {', '.join(bad[:5])}")
    tried = len(ok) + len(skipped)
    if len(ok) < MIN_OK_SHARE * tried:
        failures.append(f"only {len(ok)} of {tried} seeds succeeded")
    lost = reasons.get("network", 0) + reasons.get("error", 0)
    if lost > max(5, MAX_LOST_SHARE * tried):
        failures.append(f"{lost} seeds failed with network or unexpected errors")
    if failures:
        for message in failures:
            annotate("error", message)
        return 1

    if args.zip:
        log(f"writing {args.zip}")
        args.zip.parent.mkdir(parents=True, exist_ok=True)
        write_zip(args.zip.resolve(), out)
        log(f"zip: {mib(args.zip.stat().st_size)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
