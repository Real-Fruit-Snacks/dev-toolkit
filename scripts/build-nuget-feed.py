#!/usr/bin/env python3
"""Build the NuGet folder feed that ships as nuget.zip.

Seeds are the most downloaded packages on nuget.org (latest stable version of each) plus
config/nuget-extra.txt. Every seed is restored on its own in a throwaway project for --tfm, because
seeds often conflict with each other, and all restores share one packages folder. The projects carry
RuntimeIdentifiers for every --rids entry, so RID-specific dependencies come along too.

The SDK's own project templates (every language, plus a few option variants) and a handful of
projects that publish self-contained, trimmed, single-file and ReadyToRun for each RID are restored
the same way. That pulls in what the SDK downloads on demand: runtime packs, apphosts, ILLink and
Crossgen2 and the native AOT compiler (best effort: AOT also needs a C++ linker on the machine that
publishes). Packs that exist per build-machine RID (Crossgen2, apphosts, ILCompiler) are then fetched
for every --rids entry, so the Windows machine gets win-x64 tools even though CI runs on Linux.

The feed is <id>/<version>/{<id>.<version>.nupkg, <id>.<version>.nupkg.sha512, <id>.nuspec}, in
lowercase as NuGet writes its packages folder, and NuGet reads that layout as a local source.
Packages that only failed or skipped restores pulled in are left out. Finally every successful
restore runs again with the new feed as the only source, into an empty packages folder, and must
resolve exactly the same packages; anything missing fails the build here, not on the offline machine.

Usage:
  python3 scripts/build-nuget-feed.py [--top N] [--extra FILE] [--tfm TFM] [--rids RID,RID]
                                      [--out DIR] [--work DIR] [--facts FILE] [--jobs J]
                                      [--windows-tfm TFM|none] [--no-verify]

Needs the .NET SDK that supports --tfm on PATH and access to nuget.org. It writes only to --out,
--work and --facts: every dotnet command gets its own NuGet.Config, packages folder, HTTP cache and
CLI home through environment variables set for that child process alone.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import datetime as dt
import fnmatch
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import quoteattr

REPO = Path(__file__).resolve().parent.parent
SERVICE_INDEX = 'https://api.nuget.org/v3/index.json'
NUGET_ORG = 'https://api.nuget.org/v3/index.json'
USER_AGENT = 'dev-toolkit-build-nuget-feed'
RESTORE_TIMEOUT = 45 * 60      # seconds; a few popular packages are several hundred MB
NETWORK_TRIES = 3              # attempts per restore when the failure looks like the network

# NuGet log codes the seed logic reacts to.
NETWORK_CODES = {'NU1301', 'NU1801'}               # source unreachable
TOOL_CODES = {'NU1212', 'NU1213'}                  # package type does not fit a PackageReference
COMPAT_CODES = {'NU1201', 'NU1202', 'NU1701'}      # no assets for the target framework
NETWORK_TEXT = re.compile(
    r'unable to load the service index|an error occurred while sending the request|'
    r'the ssl connection could not be established|the operation has timed out|'
    r'connection (?:was )?(?:reset|refused)|name or service not known|no such host is known|'
    r'response status code does not indicate success: (?:408|429|5\d\d)', re.I)

# Option sets restored on top of each template's defaults; they pull in packages the plain template
# does not (Identity + EF Core, Microsoft.Identity.Web, WebAssembly, MSTest.Sdk and its extensions).
# A variant the installed SDK does not know is recorded as skipped.
TEMPLATE_VARIANTS = {
    'mvc': [['--auth', 'Individual'], ['--auth', 'Individual', '--use-local-db'],
            ['--auth', 'SingleOrg'], ['--auth', 'Windows']],
    'webapp': [['--auth', 'Individual'], ['--auth', 'Individual', '--use-local-db'],
               ['--auth', 'SingleOrg']],
    'webapi': [['--auth', 'SingleOrg'], ['--auth', 'Windows']],
    'blazor': [['--interactivity', 'Auto', '--auth', 'Individual'], ['--interactivity', 'WebAssembly'],
               ['--auth', 'Individual', '--use-local-db']],
    'blazorwasm': [['--auth', 'Individual'], ['--pwa']],
    'mstest': [['--sdk'], ['--test-runner', 'MSTest'], ['--coverage-tool', 'coverlet'],
               ['--sdk', '--test-runner', 'MSTest', '--extensions-profile', 'AllMicrosoft']],
}
LANG_DIR = {'C#': 'cs', 'F#': 'fs', 'VB': 'vb'}

# Projects that make the SDK download what self-contained, trimmed, single-file, ReadyToRun and
# native AOT publishing need: runtime packs, apphosts, ILLink, Crossgen2 and ILCompiler.
PUBLISH = ('<SelfContained>true</SelfContained><PublishSingleFile>true</PublishSingleFile>'
           '<PublishReadyToRun>true</PublishReadyToRun>')
PACKS = {
    'console': ('Microsoft.NET.Sdk', '<OutputType>Exe</OutputType>' + PUBLISH + '<PublishTrimmed>true</PublishTrimmed>'),
    'web': ('Microsoft.NET.Sdk.Web', PUBLISH + '<PublishTrimmed>true</PublishTrimmed>'),
    'desktop': ('Microsoft.NET.Sdk', '<OutputType>WinExe</OutputType><UseWPF>true</UseWPF>'
                '<UseWindowsForms>true</UseWindowsForms>' + PUBLISH),
    'aot': ('Microsoft.NET.Sdk', '<OutputType>Exe</OutputType><PublishAot>true</PublishAot>'),
}

PROJECT = '''<Project Sdk="{sdk}">
  <PropertyGroup>
    <TargetFramework>{tfm}</TargetFramework>
    {props}
  </PropertyGroup>
  <ItemGroup>
    {items}
  </ItemGroup>
</Project>
'''


class Fatal(Exception):
    """A systemic problem: the build stops with this message."""


_log_lock = threading.Lock()
_t0 = time.monotonic()


def log(msg):
    with _log_lock:
        s = int(time.monotonic() - _t0)
        print(f'[{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}] {msg}', flush=True)


def rmtree(path):
    def retry(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    if Path(path).exists():
        shutil.rmtree(path, onexc=retry)


def norm_version(v):
    """NuGet's normalized version string, lowercased, as used for packages folder names."""
    v = v.strip().strip('[]()').split(',')[0].strip().split('+')[0]
    core, dash, label = v.partition('-')
    nums = [str(int(p)) if p.isdigit() else p for p in core.split('.')]
    while len(nums) < 3:
        nums.append('0')
    if len(nums) == 4 and nums[3] == '0':
        nums.pop()
    return ('.'.join(nums) + dash + label).lower()


def first_line(text, limit=300):
    line = next((l.strip() for l in str(text).splitlines() if l.strip()), '')
    return line if len(line) <= limit else line[:limit - 3] + '...'


def human(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if n < 1024 or unit == 'GiB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024


# --- nuget.org API ------------------------------------------------------------------------------

def http_json(url, tries=4):
    """GET a JSON document; None on 404. Retries other failures, then gives up loudly."""
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'gzip'})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    data = gzip.decompress(data)
            return json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = f'HTTP {e.code}'
        except (urllib.error.URLError, OSError, ValueError) as e:
            err = str(e)
        if attempt == tries:
            raise Fatal(f'{url}: {err}')
        time.sleep(5 * attempt)


class NuGetApi:
    def __init__(self):
        index = http_json(SERVICE_INDEX)
        if not index:
            raise Fatal(f'no service index at {SERVICE_INDEX}')
        res = index.get('resources', [])

        def find(*types):
            for t in types:
                for r in res:
                    if r.get('@type') == t:
                        return r['@id'].rstrip('/')
            raise Fatal(f'{SERVICE_INDEX} lists no {types[0]}')
        self.search_url = find('SearchQueryService/3.5.0', 'SearchQueryService/3.0.0-rc', 'SearchQueryService')
        self.flat_url = find('PackageBaseAddress/3.0.0')

    def search(self, q, skip=0, take=20):
        query = urllib.parse.urlencode({'q': q, 'skip': skip, 'take': take,
                                        'prerelease': 'false', 'semVerLevel': '2.0.0'})
        doc = http_json(f'{self.search_url}?{query}')
        if doc is None:
            raise Fatal(f'search failed: {self.search_url}?{query}')
        return doc.get('data', [])

    def versions(self, package_id):
        doc = http_json(f'{self.flat_url}/{package_id.lower()}/index.json')
        return doc.get('versions', []) if doc else []


# --- seeds --------------------------------------------------------------------------------------

@dataclasses.dataclass
class Seed:
    id: str
    version: str
    types: list
    source: str                # top, extra or top+extra
    status: str = 'pending'    # ok or skipped
    reason: str = ''
    notes: list = dataclasses.field(default_factory=list)
    systemic: bool = False     # failed for a reason that is probably not the package's fault


def top_seeds(api, n):
    """The n most downloaded packages. An empty search query comes back roughly in download order;
    fetching a few more and sorting by totalDownloads fixes the tail."""
    want = min(n + max(100, n // 10), 4000)     # the search service pages no further than skip=3000
    hits, skip = [], 0
    while len(hits) < want and skip <= 3000:
        page = api.search('', skip, min(1000, want - len(hits)))
        if not page:
            break
        hits += page
        skip += len(page)
    hits.sort(key=lambda d: -int(d.get('totalDownloads') or 0))
    seeds = []
    for d in hits[:n]:
        types = [t.get('name') for t in d.get('packageTypes') or [] if t.get('name')]
        seeds.append(Seed(d['id'], d['version'], types, 'top'))
    return seeds


def read_extra(path):
    """Returns ([(id, version spec or None)], [(pattern, reason)]) from nuget-extra.txt."""
    wanted, excludes = [], []
    for n, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        text, _, comment = raw.partition('#')
        parts = text.split()
        if not parts:
            continue
        if parts[0].startswith('!'):
            pattern = (parts[0][1:] or (parts[1] if len(parts) > 1 else '')).strip()
            if not pattern or len(parts) > (1 if parts[0][1:] else 2):
                raise Fatal(f'{path}:{n}: expected "!Pattern  # reason"')
            excludes.append((pattern, comment.strip() or f'excluded in {path.name}'))
        elif len(parts) <= 2:
            wanted.append((parts[0], parts[1] if len(parts) == 2 else None))
        else:
            raise Fatal(f'{path}:{n}: expected "Id" or "Id Version"')
    return wanted, excludes


def resolve_extra(api, package_id, spec):
    """Seed for one nuget-extra.txt line, or (id, reason) when it cannot be used."""
    found = api.search(f'packageid:{package_id}', take=1)
    hit = next((d for d in found if d['id'].lower() == package_id.lower()), None)
    if spec is None:
        if not hit:
            return package_id, 'no stable version on nuget.org'
        version = hit['version']
    elif '*' in spec:
        prefix = spec.split('*')[0]
        stable = [v for v in api.versions(package_id) if '-' not in v and v.startswith(prefix)]
        if not stable:
            return package_id, f'no stable version matching {spec} on nuget.org'
        version = stable[-1]           # the flat container lists versions in ascending order
    else:
        version = spec
    types = [t.get('name') for t in (hit or {}).get('packageTypes') or [] if t.get('name')]
    return Seed(hit['id'] if hit else package_id, version, types, 'extra')


# --- the build ----------------------------------------------------------------------------------

@dataclasses.dataclass
class Restore:
    """What one dotnet restore left behind."""
    rc: int | None
    output: str
    logs: list
    packages: set      # (id, version), lowercase: libraries and downloadDependencies
    downloads: set     # (name, version) of downloadDependencies (SDK packs and PackageDownload)

    @property
    def timed_out(self):
        return self.rc is None


@dataclasses.dataclass
class Job:
    """A successful restore, replayed against the finished feed."""
    name: str
    target: Path
    props: tuple
    packages: set
    warnings: frozenset   # NuGet warning codes of the original restore


class Build:
    def __init__(self, args):
        self.args = args
        self.tfm = args.tfm
        self.rids = [r.strip() for r in args.rids.split(',') if r.strip()]
        if not self.rids:
            raise Fatal('--rids is empty')
        wt = args.windows_tfm.replace('{tfm}', self.tfm)
        self.windows_tfm = None if wt.lower() == 'none' else wt
        self.out = Path(args.out).resolve()
        self.work = Path(args.work).resolve() if args.work else self.out.parent / 'nuget-work'
        self.facts_path = Path(args.facts).resolve() if args.facts else self.out.parent / 'nuget.facts.json'
        self.packages = self.work / 'packages'
        self.projects = self.work / 'projects'
        self.logdir = self.work / 'logs'
        self.config = self.work / 'nuget.config'
        self.dotnet = shutil.which('dotnet')
        if not self.dotnet:
            raise Fatal('dotnet is not on PATH')
        self.env = self.child_env(self.packages, self.work / 'http-cache')
        self.lock = threading.Lock()
        self.needed = set()     # packages that successful restores resolved
        self.failed = set()     # packages that failed or skipped restores resolved
        self.downloads = set()  # SDK pack downloads of successful restores
        self.replay = []        # successful restores, for the offline check
        self.sdk = self.runtime = self.host_rid = ''

    # -- processes --

    def child_env(self, packages, http_cache):
        env = dict(os.environ)
        for key in ('NUGET_FALLBACK_PACKAGES', 'RestoreConfigFile', 'RestorePackagesPath', 'MSBuildSDKsPath'):
            env.pop(key, None)
        env.update({
            'NUGET_PACKAGES': str(packages),
            'NUGET_HTTP_CACHE_PATH': str(http_cache),
            'NUGET_PLUGINS_CACHE_PATH': str(self.work / 'plugins-cache'),
            'NUGET_XMLDOC_MODE': 'skip',               # the .nupkg keeps the XML docs; no need to extract them
            'DOTNET_CLI_HOME': str(self.work / 'cli-home'),
            'DOTNET_NOLOGO': '1',
            'DOTNET_CLI_TELEMETRY_OPTOUT': '1',
            'DOTNET_SKIP_FIRST_TIME_EXPERIENCE': '1',
            'DOTNET_GENERATE_ASPNET_CERTIFICATE': 'false',
            'DOTNET_ADD_GLOBAL_TOOLS_TO_PATH': 'false',
            'DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE': '1',
            'DOTNET_CLI_UI_LANGUAGE': 'en',
            'MSBUILDDISABLENODEREUSE': '1',
            'MSBUILDTERMINALLOGGER': 'off',
            'UseSharedCompilation': 'false',
        })
        return env

    def run(self, cmd, cwd, env=None, timeout=None):
        """(exit code or None on timeout, combined output)."""
        try:
            p = subprocess.run(cmd, cwd=cwd, env=env or self.env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', timeout=timeout)
            return p.returncode, p.stdout
        except subprocess.TimeoutExpired as e:
            out = e.output or ''
            return None, out.decode('utf-8', 'replace') if isinstance(out, bytes) else out
        except OSError as e:
            raise Fatal(f'cannot run {cmd[0]}: {e}')

    def restore(self, target, props=(), *, env=None, config=None, packages=None, force=False):
        root = target if target.is_dir() else target.parent
        cmd = [self.dotnet, 'restore', str(target), '--configfile', str(config or self.config),
               '--packages', str(packages or self.packages), '-nologo', '-v:q',
               *[f'-p:{p}' for p in props]]
        if force:
            cmd.append('--force')
        for attempt in range(1, NETWORK_TRIES + 1):
            for obj in [p for p in root.rglob('obj') if p.is_dir()]:
                rmtree(obj)
            rc, out = self.run(cmd, root, env, RESTORE_TIMEOUT)
            res = read_assets(root, rc, out)
            network = rc and (any(l.get('code') in NETWORK_CODES for l in res.logs) or NETWORK_TEXT.search(out))
            if not network or attempt == NETWORK_TRIES:
                return res
            log(f'  network trouble restoring {root.name}, retrying ({attempt}/{NETWORK_TRIES})')
            time.sleep(20 * attempt)

    def record(self, res, ok, name=None, target=None, props=()):
        with self.lock:
            if ok:
                self.needed |= res.packages
                self.downloads |= res.downloads
                if target is not None:
                    codes = frozenset(l.get('code') for l in res.logs if l.get('level') == 'Warning')
                    self.replay.append(Job(name, target, tuple(props), set(res.packages), codes))
            else:
                self.failed |= res.packages

    def save_log(self, name, res):
        self.logdir.mkdir(parents=True, exist_ok=True)
        (self.logdir / f'{name}.log').write_text(res.output, encoding='utf-8')

    # -- setup --

    def prepare(self):
        rc, out = self.run([self.dotnet, '--version'], self.work)
        if rc != 0:
            raise Fatal(f'dotnet --version failed:\n{out}')
        self.sdk = out.strip().splitlines()[-1].strip()
        rc, out = self.run([self.dotnet, '--info'], self.work)
        m = re.search(r'^\s*RID:\s*(\S+)', out or '', re.M)
        self.host_rid = m.group(1) if m else ''
        log(f'.NET SDK {self.sdk} on {self.host_rid or "unknown RID"}; tfm {self.tfm}, rids {",".join(self.rids)}'
            f', windows tfm {self.windows_tfm or "none"}')

        for d in (self.projects, self.packages, self.logdir):
            rmtree(d)
        self.projects.mkdir(parents=True)
        self.packages.mkdir(parents=True)
        self.config.write_text(nuget_config({'nuget.org': NUGET_ORG}), encoding='utf-8')
        # The SDK resolver (for Sdk="Name/version" projects) reads NuGet.Config from the project's
        # folders rather than --configfile, so the same file sits above every project.
        shutil.copyfile(self.config, self.projects / 'NuGet.Config')
        # Stop MSBuild from importing anything from folders above the work dir.
        rids = ';'.join(self.rids)
        (self.projects / 'Directory.Build.props').write_text(f'''<Project>
  <!-- Shared by the throwaway projects of build-nuget-feed.py. -->
  <PropertyGroup>
    <RuntimeIdentifiers Condition="'$(DtkNoRids)' != 'true'">{rids}</RuntimeIdentifiers>
    <EnableWindowsTargeting>true</EnableWindowsTargeting>
    <NuGetAudit>false</NuGetAudit>
    <ManagePackageVersionsCentrally>false</ManagePackageVersionsCentrally>
    <NoWarn>$(NoWarn);NU1605;NU1608</NoWarn>
  </PropertyGroup>
</Project>
''', encoding='utf-8')
        for name in ('Directory.Build.targets', 'Directory.Packages.props'):
            (self.projects / name).write_text('<Project />\n', encoding='utf-8')

    def write_project(self, path, tfm, items=(), sdk='Microsoft.NET.Sdk', props=''):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PROJECT.format(sdk=sdk, tfm=tfm, props=props, items='\n    '.join(items)), encoding='utf-8')
        return path

    # -- publish packs --

    def restore_packs(self):
        specs = []
        for rid in self.rids:
            specs.append((f'console-{rid}', 'console', self.tfm, rid, True))
            specs.append((f'web-{rid}', 'web', self.tfm, rid, True))
            if rid.startswith('win'):
                specs.append((f'desktop-{rid}', 'desktop', f'{self.tfm}-windows', rid, False))
                if self.windows_tfm:
                    specs.append((f'windows-sdk-{rid}', 'desktop', self.windows_tfm, rid, False))
            specs.append((f'aot-{rid}', 'aot', self.tfm, rid, False))      # best effort

        def one(spec):
            name, kind, tfm, rid, required = spec
            sdk, props = PACKS[kind]
            proj = self.write_project(self.projects / 'packs' / name / f'{name}.csproj', tfm, sdk=sdk,
                                      props=f'<RuntimeIdentifier>{rid}</RuntimeIdentifier>' + props)
            res = self.restore(proj, ('DtkNoRids=true',))
            why = problem(res)
            self.record(res, why is None, f'packs {name}', proj, ('DtkNoRids=true',))
            if why:
                self.save_log(f'packs-{name}', res)
                if required:
                    raise Fatal(f'restoring the {name} publish project failed: {why[0]}: {why[1]}\n{tail(res.output)}')
                log(f'  packs {name}: skipped ({why[0]}: {why[1]})')
                return name, None
            log(f'  packs {name}: ok')
            return name, res

        # The first one doubles as the preflight: if it fails, nothing else will work either.
        first = one(specs[0])
        for name, version in sorted(first[1].downloads):
            if name.lower().startswith('microsoft.netcore.app.runtime.') and '.mono.' not in name.lower():
                self.runtime = norm_version(version)
        with cf.ThreadPoolExecutor(self.args.jobs) as ex:
            rest = list(ex.map(one, specs[1:]))
        done = [first] + rest
        return [n for n, r in done if r], [n for n, r in done if not r]

    def mirror_host_packs(self, api):
        """Fetch per-RID SDK packs (Crossgen2, apphost, ILCompiler...) for every --rids entry."""
        rids = {r.lower(): r for r in self.rids}
        have = {(n.lower(), norm_version(v)) for n, v in self.downloads} | self.needed
        want = set()
        for name, version in self.downloads:
            parts = name.split('.')
            for i, part in enumerate(parts):
                if part.lower() in rids:
                    for other in self.rids:
                        cand = '.'.join(parts[:i] + [other] + parts[i + 1:])
                        if (cand.lower(), norm_version(version)) not in have:
                            want.add((cand, norm_version(version)))
        exists = []
        for cand, version in sorted(want):
            if version in {norm_version(v) for v in api.versions(cand)}:
                exists.append((cand, version))
            else:
                log(f'  {cand} {version} is not on nuget.org; not needed')
        if not exists:
            return []
        items = [f'<PackageDownload Include={quoteattr(n)} Version={quoteattr(f"[{v}]")} />' for n, v in exists]
        proj = self.write_project(self.projects / 'packs' / 'mirror' / 'mirror.csproj', self.tfm, items)
        res = self.restore(proj, ('DtkNoRids=true',))
        why = problem(res)
        self.record(res, why is None, 'packs mirror', proj, ('DtkNoRids=true',))
        if why:
            self.save_log('packs-mirror', res)
            raise Fatal(f'fetching per-RID packs failed: {why[0]}: {why[1]}')
        for n, v in exists:
            log(f'  + {n} {v}')
        return [f'{n} {v}' for n, v in exists]

    # -- templates --

    def list_templates(self):
        rc, out = self.run([self.dotnet, 'new', 'list', '--type', 'project'], self.work, timeout=600)
        if rc != 0:
            raise Fatal(f'dotnet new list failed:\n{tail(out)}')
        templates = parse_template_table(out)
        if not templates:
            raise Fatal(f'could not read the template list:\n{tail(out)}')
        return templates

    def restore_templates(self):
        jobs = []
        for short, langs in self.list_templates():
            jobs += [(short, lang, []) for lang in langs]
            if 'C#' in langs:
                jobs += [(short, 'C#', args) for args in TEMPLATE_VARIANTS.get(short, [])]
        created, skipped = [], []
        for i, (short, lang, args) in enumerate(jobs, 1):     # one at a time: dotnet new shares a cache
            label = ' '.join([short, f'({lang})', *args])
            d = self.projects / 'templates' / f'{i:02d}-{short}-{LANG_DIR.get(lang, "x")}'
            why = self.dotnet_new(short, lang, args, d)
            if why:
                skipped.append({'template': label, 'reason': why})
                log(f'  template {label}: skipped ({why})')
            else:
                created.append((label, d))
        log(f'  created {len(created)} template projects; restoring them')

        def one(item):
            label, d = item
            target = template_target(d)
            why = None
            for props in ((), ('DtkNoRids=true',)):         # browser-wasm projects reject extra RIDs
                res = self.restore(target, props)
                why = problem(res)
                self.record(res, why is None, f'template {label}', target, props)
                if why is None:
                    return label, None, bool(props)
                self.save_log(f'template-{d.name}', res)
            return label, f'{why[0]}: {why[1]}', False

        ok = []
        with cf.ThreadPoolExecutor(self.args.jobs) as ex:
            for label, why, no_rids in ex.map(one, created):
                if why:
                    skipped.append({'template': label, 'reason': why})
                    log(f'  template {label}: restore failed ({why})')
                else:
                    ok.append(label)
                    log(f'  template {label}: ok' + (' (without extra RIDs)' if no_rids else ''))
        if not any(l.startswith('console (C#)') for l in ok):
            raise Fatal('the C# console template did not restore; see the logs folder in the work dir')
        return ok, skipped

    def dotnet_new(self, short, lang, args, d):
        cmd = [self.dotnet, 'new', short, '-o', str(d), '-n', 'Sample', '--no-restore', '-lang', lang, *args]
        out = ''
        for extra in (['-f', self.tfm], []):     # not every template has a --framework option
            rmtree(d)
            rc, out = self.run(cmd + extra, self.work, timeout=600)
            if rc == 0:
                return None
        return error_line(out) or 'dotnet new failed'

    # -- seeds --

    def collect_seeds(self, api):
        top = top_seeds(api, self.args.top) if self.args.top > 0 else []
        log(f'top {len(top)} packages by downloads: ' + ', '.join(s.id for s in top[:8]) + (' ...' if len(top) > 8 else ''))
        wanted, excludes = read_extra(Path(self.args.extra))
        skipped = []
        with cf.ThreadPoolExecutor(8) as ex:
            extras = list(ex.map(lambda w: resolve_extra(api, *w), wanted))
        seeds = {s.id.lower(): s for s in top}
        explicit = {w[0].lower() for w in wanted}
        for e in extras:
            if not isinstance(e, Seed):
                skipped.append(Seed(e[0], '', [], 'extra', 'skipped', e[1]))
                continue
            key = e.id.lower()
            if key in seeds:
                seeds[key].source = 'top+extra'
                seeds[key].version = e.version      # same as the top version unless the line pins one
            else:
                seeds[key] = e
        for key, s in list(seeds.items()):
            if key in explicit:
                continue
            if 'DotnetPlatform' in s.types and 'Dependency' not in s.types:
                # Workload manifests and the latest runtime, apphost and targeting packs. The publish
                # projects already fetched the pack versions this SDK uses; other versions only help
                # a different SDK, and workloads cannot be installed from a feed like this anyway.
                skipped.append(Seed(s.id, s.version, s.types, s.source, 'skipped',
                                    'SDK pack or workload manifest (the feed has the packs this SDK uses)'))
                del seeds[key]
                continue
            for pattern, reason in excludes:
                if fnmatch.fnmatchcase(key, pattern.lower()):
                    skipped.append(Seed(s.id, s.version, s.types, s.source, 'skipped', f'excluded: {reason}'))
                    del seeds[key]
                    break
        log(f'{len(seeds)} seeds ({len(top)} top, {len(wanted)} extra lines); {len(skipped)} left out before restoring')
        for s in skipped:
            log(f'  left out {" ".join(filter(None, (s.id, s.version)))}: {s.reason}')
        return list(seeds.values()), skipped, len(top)

    def restore_seed(self, seed, index):
        d = self.projects / 'seeds' / f'{index:04d}'
        proj = d / 'seed.csproj'
        kind = 'download' if seed.types and 'Dependency' not in seed.types else 'reference'
        tfm, rids, first, tried = self.tfm, True, None, set()
        while (kind, tfm, rids) not in tried:
            tried.add((kind, tfm, rids))
            props = () if rids else ('DtkNoRids=true',)
            self.write_project(proj, tfm, seed_items(seed, kind))
            res = self.restore(proj, props)
            why = problem(res, seed.id if kind == 'reference' else None)
            if why is None and kind == 'download':
                extra = self.tool_rid_packages(seed)
                if extra:            # a RID-specific .NET tool: fetch its per-RID packages too
                    self.write_project(proj, tfm, seed_items(seed, kind, extra))
                    res = self.restore(proj, props)
                    why = problem(res)
            if why is None:
                self.record(res, True, f'seed {seed.id}', proj, props)
                seed.status = 'ok'
                seed.notes = [n for n, on in (('as PackageDownload', kind == 'download'),
                                              (f'with {tfm}', tfm != self.tfm),
                                              ('without RID graphs', not rids)) if on]
                return seed
            self.record(res, False)
            self.save_log(f'seed-{d.name}', res)
            code, msg, about_seed = why
            first = first or why
            if code in TOOL_CODES and kind == 'reference':
                kind = 'download'
            elif code in COMPAT_CODES and self.windows_tfm and tfm == self.tfm:
                tfm = self.windows_tfm
            elif rids and not about_seed and code not in COMPAT_CODES | TOOL_CODES | {'timeout'}:
                rids = False            # maybe only a RID-specific dependency is missing
        code, msg, _ = first
        seed.status, seed.reason = 'skipped', f'{code}: {msg}'
        seed.systemic = code in NETWORK_CODES or code in ('timeout', 'restore') or code.startswith('MSB')
        return seed

    def tool_rid_packages(self, seed):
        """Per-RID packages a .NET tool points to (DotnetToolSettings.xml, .NET 10 and later)."""
        found = set()
        root = self.packages / seed.id.lower() / norm_version(seed.version) / 'tools'
        for f in root.glob('*/*/DotnetToolSettings.xml'):
            try:
                elements = ET.parse(f).iter()
            except (ET.ParseError, OSError):
                continue
            for el in elements:
                if el.tag.rsplit('}', 1)[-1] == 'RuntimeIdentifierPackage':
                    rid, pid = el.get('RuntimeIdentifier'), el.get('Id')
                    if pid and rid in (*self.rids, 'any'):
                        found.add((pid, el.get('Version') or seed.version))
        return sorted(found)

    def restore_seeds(self, seeds):
        total, done, systemic = len(seeds), 0, 0
        ex = cf.ThreadPoolExecutor(self.args.jobs)
        try:
            starts = {}

            def timed(seed, i):
                starts[seed.id] = time.monotonic()
                return self.restore_seed(seed, i)
            futures = [ex.submit(timed, s, i) for i, s in enumerate(seeds, 1)]
            for f in cf.as_completed(futures):
                s = f.result()
                done += 1
                systemic += s.systemic
                secs = time.monotonic() - starts.get(s.id, time.monotonic())
                if s.status == 'ok':
                    note = f' [{", ".join(s.notes)}]' if s.notes else ''
                    log(f'[{done}/{total}] ok   {s.id} {s.version}{note} ({secs:.0f}s)')
                else:
                    log(f'[{done}/{total}] skip {s.id} {s.version}: {s.reason}')
                if done >= 20 and systemic * 2 > done:
                    raise Fatal(f'{systemic} of the first {done} seeds failed for reasons that look systemic '
                                '(network, timeouts, MSBuild errors); stopping')
        finally:
            ex.shutdown(wait=True, cancel_futures=True)

    # -- feed --

    def write_feed(self):
        # Everything in the packages folder goes in, except packages that only failed or skipped
        # restores resolved. That keeps packages no assets file lists: MSBuild SDKs (Sdk="X/1.0")
        # and versions NuGet downloaded while resolving and then replaced with a higher one. The
        # offline restore walks the same graph, so it may ask for them too.
        dropped_only = self.failed - self.needed
        present = {}
        for id_dir in self.packages.iterdir():
            if id_dir.is_dir():
                for v_dir in id_dir.iterdir():
                    if v_dir.is_dir():
                        present[(id_dir.name, v_dir.name)] = v_dir
        if self.out.exists():
            entries = list(self.out.iterdir())
            if any(not e.is_dir() or e.name != e.name.lower() for e in entries):
                raise Fatal(f'refusing to delete {self.out}: it does not look like a feed this script wrote')
            rmtree(self.out)
        self.out.mkdir(parents=True)
        kept, dropped, incomplete, size = [], [], [], 0
        for (pid, ver), src in sorted(present.items()):
            if (pid, ver) in dropped_only:
                dropped.append(f'{pid} {ver}')
                continue
            files = [f'{pid}.{ver}.nupkg', f'{pid}.{ver}.nupkg.sha512', f'{pid}.nuspec']
            if not all((src / f).is_file() for f in files) or not (src / '.nupkg.metadata').is_file():
                incomplete.append(f'{pid} {ver}')
                continue
            dst = self.out / pid / ver
            dst.mkdir(parents=True)
            for f in files:
                try:                        # same disk: a hard link saves copying gigabytes
                    os.link(src / f, dst / f)
                except OSError:
                    shutil.copyfile(src / f, dst / f)
                size += (dst / f).stat().st_size
            kept.append((pid, ver, (dst / files[0]).stat().st_size))
        unattributed = sorted(f'{p} {v}' for p, v in present if (p, v) not in self.needed and (p, v) not in self.failed)
        missing = sorted(f'{p} {v}' for p, v in self.needed - set(present))
        log(f'feed: {len(kept)} package versions, {human(size)}; dropped {len(dropped)} only used by '
            f'skipped seeds; {len(incomplete)} incomplete in the packages folder')
        if unattributed:
            log(f'  kept {len(unattributed)} not in any assets file (MSBuild SDKs, versions read while resolving): '
                + ', '.join(unattributed[:15]) + (' ...' if len(unattributed) > 15 else ''))
        if incomplete:
            log('  incomplete, not copied: ' + ', '.join(incomplete[:15]))
        if missing:
            log(f'  WARNING {len(missing)} resolved packages are not in the packages folder: ' + ', '.join(missing[:15]))
        for pid, ver, n in sorted(kept, key=lambda k: -k[2])[:12]:
            log(f'  largest: {pid} {ver} {human(n)}')
        return kept, size

    def verify(self):
        """Restore every successful project again with the feed as the only source."""
        vpk = self.work / 'verify-packages'
        rmtree(vpk)
        vpk.mkdir(parents=True)
        cfg = self.work / 'verify.config'
        cfg.write_text(nuget_config({'dev-toolkit': str(self.out)}), encoding='utf-8')
        shutil.copyfile(cfg, self.projects / 'NuGet.Config')
        env = self.child_env(vpk, self.work / 'verify-http-cache')
        failures = []

        def one(job):
            res = self.restore(job.target, job.props, env=env, config=cfg, packages=vpk, force=True)
            why = problem(res)
            if why:
                self.save_log('verify-' + re.sub(r'[^\w.-]+', '_', job.name), res)
                return job, f'{why[0]}: {why[1]}'
            # NU1603 and friends: a dependency's lower bound is missing and NuGet took a higher
            # version. The result may even be the same, but the offline restore would warn.
            new = [l for l in res.logs if l.get('level') == 'Warning' and str(l.get('code', '')).startswith('NU16')
                   and l.get('code') not in job.warnings]
            if new:
                return job, f'{new[0].get("code")}: {first_line(new[0].get("message"))}'
            if res.packages != job.packages:
                lost = sorted(f'{p} {v}' for p, v in job.packages - res.packages)
                new = sorted(f'{p} {v}' for p, v in res.packages - job.packages)
                return job, f'resolved differently; missing {lost[:5]}, instead {new[:5]}'
            return job, None

        jobs = sorted(self.replay, key=lambda j: j.name)
        log(f'verifying: restoring {len(jobs)} projects again from the feed alone')
        try:
            with cf.ThreadPoolExecutor(self.args.jobs) as ex:
                for n, (job, why) in enumerate(ex.map(one, jobs), 1):
                    if why:
                        failures.append(f'{job.name}: {why}')
                        log(f'  FAILED {job.name}: {why}')
                    elif n % 100 == 0:
                        log(f'  {n}/{len(jobs)} ok')
        finally:
            shutil.copyfile(self.config, self.projects / 'NuGet.Config')
            rmtree(vpk)
        if failures:
            raise Fatal(f'{len(failures)} of {len(jobs)} restores fail against the finished feed:\n  '
                        + '\n  '.join(failures[:40]))
        log(f'verified: all {len(jobs)} restores resolve the same packages from the feed alone')
        return len(jobs)


# --- helpers that do not need the build state ---------------------------------------------------

def nuget_config(sources):
    adds = '\n'.join(f'    <add key={quoteattr(k)} value={quoteattr(v)} />' for k, v in sources.items())
    return f'''<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear />
{adds}
  </packageSources>
  <disabledPackageSources>
    <clear />
  </disabledPackageSources>
  <fallbackPackageFolders>
    <clear />
  </fallbackPackageFolders>
  <packageSourceMapping>
    <clear />
  </packageSourceMapping>
</configuration>
'''


def seed_items(seed, kind, extra=()):
    tag = 'PackageReference' if kind == 'reference' else 'PackageDownload'
    items = [f'<{tag} Include={quoteattr(seed.id)} Version={quoteattr(f"[{seed.version}]")} />']
    items += [f'<PackageDownload Include={quoteattr(i)} Version={quoteattr(f"[{v}]")} />' for i, v in extra]
    return items


def read_assets(root, rc, output):
    logs, packages, downloads = [], set(), set()
    for f in root.rglob('project.assets.json'):
        try:
            doc = json.loads(f.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        logs += doc.get('logs') or []
        for key, lib in (doc.get('libraries') or {}).items():
            if lib.get('type') == 'package' and '/' in key:
                pid, ver = key.split('/', 1)
                packages.add((pid.lower(), norm_version(ver)))
        for fw in ((doc.get('project') or {}).get('frameworks') or {}).values():
            for d in fw.get('downloadDependencies') or []:
                downloads.add((d['name'], norm_version(d['version'])))
                packages.add((d['name'].lower(), norm_version(d['version'])))
    return Restore(rc, output or '', logs, packages, downloads)


def problem(res, seed_id=None):
    """None when the restore is usable, else (code, message, whether it is about the seed itself)."""
    if res.timed_out:
        return 'timeout', f'restore did not finish in {RESTORE_TIMEOUT // 60} minutes', False
    mine = (seed_id or '').lower()
    errors = [l for l in res.logs if l.get('level') == 'Error']
    errors.sort(key=lambda l: (l.get('libraryId') or '').lower() != mine)
    if errors:
        e = errors[0]
        return e.get('code') or 'error', first_line(e.get('message')), (e.get('libraryId') or '').lower() == mine
    if res.rc:
        m = re.search(r'error ([A-Z]+\d+): (.+?)(?: \[[^\]]*\])?\s*$', res.output, re.M)
        if m:
            return m.group(1), first_line(m.group(2)), False
        return 'restore', error_line(res.output) or f'dotnet restore exited with {res.rc}', False
    if mine and any(l.get('code') == 'NU1701' and (l.get('libraryId') or '').lower() == mine for l in res.logs):
        return 'NU1701', 'only .NET Framework assets (would restore through AssetTargetFallback)', True
    return None


def error_line(output):
    lines = [l.strip() for l in (output or '').splitlines() if l.strip()]
    errs = [l for l in lines if re.search(r'\berror\b', l, re.I)]
    return first_line((errs or lines or [''])[0])


def tail(output, n=15):
    return '\n'.join((output or '').rstrip().splitlines()[-n:])


def parse_template_table(out):
    """[(short name, [languages])] from the table `dotnet new list` prints."""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if re.fullmatch(r'\s*-{3,}(?:\s+-{3,})+\s*', line) and i > 0:
            spans = [m.start() for m in re.finditer(r'-+', line)] + [None]
            header = [lines[i - 1][spans[k]:spans[k + 1]].strip() for k in range(len(spans) - 1)]
            try:
                c_short, c_lang = header.index('Short Name'), header.index('Language')
            except ValueError:
                return []
            result = []
            for row in lines[i + 1:]:
                if not row.strip():
                    break
                cells = [row[spans[k]:spans[k + 1]].strip() for k in range(len(spans) - 1)]
                short = cells[c_short].split(',')[0].strip()
                langs = [l.strip('[] ') for l in cells[c_lang].split(',') if l.strip('[] ')] or ['C#']
                if short:
                    result.append((short, langs))
            return result
    return []


def template_target(d):
    solutions = sorted(d.glob('*.sln')) + sorted(d.glob('*.slnx'))
    if len(solutions) == 1:
        return solutions[0]
    projects = sorted(p for p in d.glob('*.*proj') if p.suffix in ('.csproj', '.fsproj', '.vbproj'))
    return projects[0] if len(projects) == 1 else d


# --- main ---------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description='Build the NuGet folder feed for nuget.zip.')
    ap.add_argument('--top', type=int, default=1000, help='most downloaded packages to seed (default 1000)')
    ap.add_argument('--extra', default=str(REPO / 'config' / 'nuget-extra.txt'), help='extra seeds file')
    ap.add_argument('--tfm', default='net10.0', help='target framework of the throwaway projects')
    ap.add_argument('--rids', default='win-x64,linux-x64', help='runtime identifiers to cover, comma-separated')
    ap.add_argument('--out', default=str(REPO / 'stage' / 'nuget'), help='feed folder to write (the zip root)')
    ap.add_argument('--work', help='scratch folder (default <out>/../nuget-work)')
    ap.add_argument('--facts', help='facts file (default <out>/../nuget.facts.json)')
    ap.add_argument('--jobs', type=int, default=4, help='parallel restores (default 4)')
    ap.add_argument('--windows-tfm', default='{tfm}-windows10.0.19041.0',
                    help='framework to retry seeds that need Windows, or "none" (default {tfm}-windows10.0.19041.0)')
    ap.add_argument('--no-verify', action='store_true', help='skip the offline replay of every restore')
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    if args.jobs < 1 or args.top < 0:
        ap.error('--jobs must be at least 1 and --top at least 0')
    if not Path(args.extra).is_file():
        ap.error(f'--extra {args.extra} does not exist')

    started = time.monotonic()
    try:
        b = Build(args)
        b.work.mkdir(parents=True, exist_ok=True)
        b.prepare()
        api = NuGetApi()

        log('== publish packs (runtime packs, apphosts, ILLink, Crossgen2)')
        packs_ok, packs_skipped = b.restore_packs()
        log(f'runtime packs are version {b.runtime or "unknown"}')

        log('== SDK templates')
        templates_ok, templates_skipped = b.restore_templates()

        log('== seeds')
        seeds, left_out, n_top = b.collect_seeds(api)
        b.restore_seeds(seeds)
        ok = [s for s in seeds if s.status == 'ok']
        skipped = left_out + [s for s in seeds if s.status != 'ok']
        if seeds and len(ok) * 2 < len(seeds):
            raise Fatal(f'only {len(ok)} of {len(seeds)} seeds restored; see {b.logdir}')

        log('== per-RID packs for every build machine')
        mirrored = b.mirror_host_packs(api)

        log('== feed')
        kept, size = b.write_feed()
        if not kept:
            raise Fatal('the feed is empty')
        verified = 0 if args.no_verify else b.verify()

        facts = {
            'asset': 'nuget',
            'date': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'sdk': b.sdk,
            'runtime': b.runtime,
            'buildRid': b.host_rid,
            'tfm': b.tfm,
            'windowsTfm': b.windows_tfm,
            'rids': b.rids,
            'packages': len({p for p, _, _ in kept}),
            'versions': len(kept),
            'bytes': size,
            'seedCount': {'top': n_top, 'total': len(seeds) + len(left_out), 'ok': len(ok), 'skipped': len(skipped)},
            'seeds': {
                'ok': [f'{s.id} {s.version}' + (f' ({", ".join(s.notes)})' if s.notes else '') for s in ok],
                'skipped': [{'id': s.id, 'version': s.version, 'reason': s.reason} for s in skipped],
            },
            'templates': {'ok': templates_ok, 'skipped': templates_skipped},
            'publishPacks': {'ok': packs_ok, 'skipped': packs_skipped, 'otherBuildRids': mirrored},
            'verifiedRestores': verified,
        }
        b.facts_path.parent.mkdir(parents=True, exist_ok=True)
        b.facts_path.write_text(json.dumps(facts, indent=2) + '\n', encoding='utf-8')
        mins = (time.monotonic() - started) / 60
        log(f'done in {mins:.1f} min: {facts["packages"]} packages, {facts["versions"]} versions, {human(size)}; '
            f'seeds {len(ok)} ok, {len(skipped)} skipped; feed {b.out}; facts {b.facts_path}')
        return 0
    except Fatal as e:
        log(f'error: {e}')
        return 1
    except KeyboardInterrupt:
        log('interrupted')
        return 130


if __name__ == '__main__':
    sys.exit(main())
