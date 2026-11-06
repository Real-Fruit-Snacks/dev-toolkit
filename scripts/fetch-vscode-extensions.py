#!/usr/bin/env python3
"""Download the VS Code extensions each group needs from Open VSX.

For every extension id in config/groups.json, group by group in file order:
  * pick the newest stable release built for --target (win32-x64) or for every
    platform (universal), whichever is newer. Open VSX's "latest" can be a
    nightly, so pre-releases are only used with --pre-release, or when an
    extension has never had a stable release.
  * download the .vsix and check it against the sha256 Open VSX publishes
  * put its "dependencies" first. VS Code will not activate an extension whose
    dependency is missing, so a dependency that isn't on Open VSX is an error.
  * put the members of an extension pack ("bundledExtensions") after the pack,
    best effort: a member that isn't on Open VSX is logged and skipped, and so
    is one named in the group's optional "skipExtensions" list (for members
    that can't work offline)
  * skip dependencies in the "vscode" namespace: they ship inside VS Code

Writes:
  OUT/<group>/<publisher>.<name>-<version>.vsix
  OUT/extensions.json  {"<group>": ["<file>.vsix", ...]}, in install order.
                       An extension needed by two groups is only in the first
                       group (config order) that needs it.
  FACTS                versions, platforms, licences, sizes and why each file
                       is there, for the release manifest and VERSIONS.md

Usage:
  python3 scripts/fetch-vscode-extensions.py --out stage/vscode-extensions
      [--config config/groups.json] [--target win32-x64] [--facts FILE]
      [--vscode-version 1.105.1] [--pre-release]

Standard library only, so it runs on a bare GitHub Actions runner.
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone

API = 'https://open-vsx.org/api'
UNIVERSAL = 'universal'
BUILT_IN_NAMESPACE = 'vscode'  # Open VSX mirrors VS Code's own built-in extensions under this name
PAGE = 200  # versions per query page; Open VSX allows up to 1000 but each entry is ~10 KB
TRIES = 5
RETRY_STATUS = (408, 429, 500, 502, 503, 504)
HEADERS = {'User-Agent': 'dev-toolkit-build', 'Accept': 'application/json'}
IN_ACTIONS = os.environ.get('GITHUB_ACTIONS') == 'true'

# SPDX ids counted as open source. Anything else gets flagged for a person to read.
OPEN_LICENCES = {
    '0BSD', 'Apache-2.0', 'Artistic-2.0', 'BSD-2-Clause', 'BSD-3-Clause', 'BSL-1.0', 'CC0-1.0',
    'EPL-1.0', 'EPL-2.0', 'ISC', 'MIT', 'MIT-0', 'MPL-2.0', 'PSF-2.0', 'Python-2.0', 'Unlicense', 'Zlib',
}
GNU_LICENCE = re.compile(r'^(A|L)?GPL-(2\.0|2\.1|3\.0)(-only|-or-later|\+)?$')

# How to recognise a licence from the start of its text, when package.json only says
# "SEE LICENSE IN <file>" or nothing. Checked in order; the first match wins.
LICENCE_TEXTS = [
    (r'microsoft software license terms|microsoft pre-release software license', 'Microsoft licence terms', False),
    (r'gnu affero general public license', 'AGPL-3.0', True),
    (r'gnu lesser general public license', 'LGPL', True),
    (r'gnu general public license version 3', 'GPL-3.0', True),
    (r'gnu general public license version 2', 'GPL-2.0', True),
    (r'apache license,? version 2\.0', 'Apache-2.0', True),
    (r'mozilla public license,? (version )?2\.0', 'MPL-2.0', True),
    (r'eclipse public license', 'EPL', True),
    (r'permission is hereby granted, free of charge', 'MIT', True),
    (r'redistribution and use in source and binary forms', 'BSD', True),
    (r'permission to use, copy, modify, and/or distribute this software for any purpose', 'ISC', True),
    (r'free and unencumbered software released into the public domain', 'Unlicense', True),
]


def log(msg=''):
    print(msg, flush=True)


def warn(msg):
    print(f'::warning::{msg}' if IN_ACTIONS else f'warning: {msg}', flush=True)


def fail(msg):
    print(f'::error::{msg}' if IN_ACTIONS else f'error: {msg}', file=sys.stderr, flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# HTTP


class NotFound(Exception):
    pass


def retrying(what, fn):
    """Run fn(), retrying rate limits, server errors and dropped connections."""
    for attempt in range(1, TRIES + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(what) from None
            if e.code not in RETRY_STATUS or attempt == TRIES:
                raise
            after = e.headers.get('Retry-After', '')
            wait, reason = (int(after) if after.isdigit() else 2 ** attempt), f'HTTP {e.code}'
        except (urllib.error.URLError, ConnectionError, TimeoutError, ssl.SSLError, http.client.HTTPException) as e:
            if attempt == TRIES:
                raise
            wait, reason = 2 ** attempt, str(e)
        log(f'    {what}: {reason}, retrying in {wait}s')
        time.sleep(wait)


def get_json(url):
    def fetch():
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=120) as r:
            return json.load(r)
    return retrying(url, fetch)


def get_text(url):
    def fetch():
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=120) as r:
            return r.read().decode('utf-8', 'replace')
    return retrying(url, fetch)


# Extensions whose Open VSX build is a small bootstrap that downloads its platform part on first use.
# Their makers publish the self-contained platform builds on GitHub; take those instead (same version).
GITHUB_BUILDS = {
    'vadimcn.vscode-lldb': ('vadimcn/codelldb', 'v{version}', 'codelldb-{target}.vsix'),
}


def github_build(ext_id, version, target):
    """The GitHub release asset for an extension in GITHUB_BUILDS: its URL and sha256, or None."""
    spec = GITHUB_BUILDS.get(ext_id.lower())
    if not spec:
        return None
    repo, tag, asset = spec
    tag, asset = tag.format(version=version), asset.format(target=target)
    headers = dict(HEADERS, Accept='application/vnd.github+json')
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'

    def fetch():
        url = f'https://api.github.com/repos/{repo}/releases/tags/{tag}'
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
            return json.load(r)
    rel = retrying(f'{repo} {tag}', fetch)
    for a in rel.get('assets', []):
        if a['name'] == asset:
            digest = (a.get('digest') or '').replace('sha256:', '') or None
            return {'url': a['browser_download_url'], 'sha256': digest}
    fail(f'{ext_id} {version}: {repo} release {tag} has no {asset}')


def download_to(url, path):
    """Stream url into path and return its sha256."""
    def fetch():
        h = hashlib.sha256()
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=300) as r, \
                open(path, 'wb') as f:
            for chunk in iter(lambda: r.read(1 << 20), b''):
                f.write(chunk)
                h.update(chunk)
        return h.hexdigest()
    return retrying(url, fetch)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Picking a version


def version_key(version):
    """Sort key: 1.10.0 > 1.9.0, and 1.2.3 > 1.2.3-beta."""
    core, _, suffix = version.partition('-')
    numbers = tuple(int(n) if n.isdigit() else 0 for n in core.split('.'))
    return numbers, suffix == ''


def engine_ok(engine, vscode_version):
    """True when VS Code vscode_version satisfies an engines.vscode range such as ^1.90.0 or >=1.80.0."""
    if not vscode_version or not engine or engine.strip() == '*':
        return True
    m = re.match(r'\s*(\^|>=|~)?\s*(\d+)\.(\d+|x)\.(\d+|x)', engine)
    if not m:
        return True  # a form we don't parse; VS Code checks again at install time
    want = tuple(0 if n == 'x' else int(n) for n in m.group(2, 3, 4))
    have = tuple(int(n) for n in re.findall(r'\d+', vscode_version)[:3])
    return have >= want and (m.group(1) != '^' or have[0] == want[0])


def releases(ext_id, platform):
    """Every version Open VSX has of ext_id for one platform, newest first, with full metadata."""
    offset = 0
    while True:
        query = urllib.parse.urlencode({'extensionId': ext_id, 'targetPlatform': platform,
                                        'includeAllVersions': 'true', 'size': PAGE, 'offset': offset})
        page = get_json(f'{API}/-/query?{query}')
        items = page.get('extensions') or []
        yield from items
        offset += len(items)
        if not items or offset >= (page.get('totalSize') or 0):
            return


def newest(ext_id, platform, args):
    """Newest usable stable release for one platform, else the newest pre-release, else None."""
    first_pre = None
    for item in releases(ext_id, platform):
        if item.get('targetPlatform') != platform or item.get('downloadable') is False:
            continue
        if not engine_ok((item.get('engines') or {}).get('vscode'), args.vscode_version):
            continue
        if not item.get('preRelease') or args.pre_release:
            return item
        first_pre = first_pre or item
    return first_pre


def choose(ext_id, args):
    """The release to ship: newest of the target-specific and universal builds; stable beats pre-release."""
    platforms = [args.target] if args.target == UNIVERSAL else [args.target, UNIVERSAL]
    found = [c for c in (newest(ext_id, p, args) for p in platforms) if c]
    stable = [c for c in found if not c.get('preRelease')]
    pool = found if args.pre_release else (stable or found)
    if not pool:
        return None
    # a tie goes to the platform-specific build, which carries native binaries for the target
    return max(pool, key=lambda c: (version_key(c['version']), c['targetPlatform'] != UNIVERSAL))


def ref_ids(refs):
    """Extension ids from a dependencies / bundledExtensions list (objects, or plain ids in older replies)."""
    ids = []
    for ref in refs or []:
        if isinstance(ref, str):
            ids.append(ref)
        elif ref.get('namespace') and ref.get('extension'):
            ids.append(f"{ref['namespace']}.{ref['extension']}")
    return ids


def is_built_in(ext_id):
    return ext_id.split('.', 1)[0].lower() == BUILT_IN_NAMESPACE


def full_id(meta):
    return f"{meta['namespace']}.{meta['name']}"


# ---------------------------------------------------------------------------
# Working out what goes where


class Plan:
    def __init__(self, args, groups):
        self.args = args
        self.skip = {g: {i.lower() for i in spec.get('skipExtensions') or []} for g, spec in groups.items()}
        self.meta = {}       # id (lower case) -> chosen release, or None when Open VSX doesn't have it
        self.usable_ = {}    # id -> True when it and all its dependencies can be downloaded
        self.where = {}      # id -> group it was put in
        self.order = {g: [] for g in groups}
        self.why = {}        # id -> {'listedIn': [...], 'dependencyOf': [...], 'packMemberOf': [...]}
        self.skipped = {}    # pack members left out: id -> {'packMemberOf': [...], 'reason': ...}
        self.built_in = {}   # built-in dependencies: id -> [extensions that need it]
        self.needs_groups = {}  # group -> earlier groups holding dependencies it needs
        self.errors = []

    def lookup(self, ext_id):
        key = ext_id.lower()
        if key not in self.meta:
            self.meta[key] = choose(ext_id, self.args)
            m = self.meta[key]
            if m and m.get('deprecated'):
                repl = m.get('replacement') or {}
                warn(f"{ext_id} is marked deprecated on Open VSX"
                     + (f"; suggested replacement: {repl.get('url') or repl}" if repl else ''))
        return self.meta[key]

    def usable(self, ext_id, visiting=()):
        key = ext_id.lower()
        if is_built_in(ext_id):
            return True
        if key in self.usable_:
            return self.usable_[key]
        if key in visiting:
            return True  # a dependency cycle; the outer call settles it
        meta = self.lookup(ext_id)
        ok = meta is not None and all(self.usable(d, visiting + (key,)) for d in ref_ids(meta.get('dependencies')))
        self.usable_[key] = ok
        return ok

    def missing(self, ext_id, seen=()):
        """Why ext_id can't be used, for messages."""
        key = ext_id.lower()
        meta = self.lookup(ext_id)
        if meta is None:
            accepts = f' in a release that VS Code {self.args.vscode_version} accepts' if self.args.vscode_version else ''
            return f'not on Open VSX for {self.args.target} or {UNIVERSAL}{accepts}'
        if key in seen:
            return 'in a dependency cycle'
        bad = [d for d in ref_ids(meta.get('dependencies')) if not self.usable(d)]
        return 'needs ' + ', '.join(f'{d} ({self.missing(d, seen + (key,))})' for d in bad)

    def place(self, ext_id, group, parent=None, role='listedIn', stack=()):
        key = ext_id.lower()
        if is_built_in(ext_id):
            self.built_in.setdefault(key, []).append(parent or group)
            return
        if key not in self.where:
            if key in stack:
                return  # dependency cycle: the call further up the stack places it
            if key in self.skip[group]:
                if role == 'packMemberOf':
                    log(f'  {group}: skipping {ext_id}, a member of the {parent} pack: in skipExtensions')
                    entry = self.skipped.setdefault(key, {'id': ext_id, 'packMemberOf': [], 'reason': 'in skipExtensions'})
                    entry['packMemberOf'].append(parent)
                else:
                    what = 'listed' if role == 'listedIn' else f'a dependency of {parent}'
                    self.errors.append(f'{group}: {ext_id} ({what}) is in skipExtensions; only pack members can be skipped')
                return
            if not self.usable(ext_id):
                if role == 'packMemberOf':
                    log(f'  {group}: skipping {ext_id}, a member of the {parent} pack: {self.missing(ext_id)}')
                    entry = self.skipped.setdefault(key, {'id': ext_id, 'packMemberOf': [], 'reason': self.missing(ext_id)})
                    entry['packMemberOf'].append(parent)
                else:
                    what = 'listed' if role == 'listedIn' else f'a dependency of {parent}'
                    self.errors.append(f'{group}: {ext_id} ({what}) is {self.missing(ext_id)}')
                return
            meta = self.meta[key]
            this = full_id(meta)
            for dep in ref_ids(meta.get('dependencies')):
                self.place(dep, group, this, 'dependencyOf', stack + (key,))
            self.where[key] = group
            self.order[group].append(key)
            for member in ref_ids(meta.get('bundledExtensions')):
                self.place(member, group, this, 'packMemberOf', stack + (key,))
        elif self.where[key] != group:
            # already packed with an earlier group; this group relies on that one being installed
            self.needs_groups.setdefault(group, set()).add(self.where[key])
        why = self.why.setdefault(key, {'listedIn': [], 'dependencyOf': [], 'packMemberOf': []})
        value = group if role == 'listedIn' else parent
        if value not in why[role]:
            why[role].append(value)


# ---------------------------------------------------------------------------
# Licences


def spdx_open(expr):
    """True/False when expr is an SPDX expression of known licences (every id must be open), else None."""
    if not re.fullmatch(r'[A-Za-z0-9.+\-() ]+', expr):
        return None
    ids, after_with = [], False
    for token in re.findall(r'[A-Za-z0-9.+\-]+', expr):
        if token in ('AND', 'OR'):
            continue
        if token == 'WITH':
            after_with = True
            continue
        if not after_with:
            ids.append(token)
        after_with = False
    if not ids:
        return None
    known = [i in OPEN_LICENCES or bool(GNU_LICENCE.match(i)) for i in ids]
    return True if all(known) else None


def licence_text(vsix, hint):
    """(path, text) of the licence file inside a .vsix: the "SEE LICENSE IN" file, the manifest's
    <License> entry, or a LICENSE file in the usual place."""
    with zipfile.ZipFile(vsix) as z:
        names = {n.lower(): n for n in z.namelist()}
        candidates = []
        if hint:
            candidates.append('extension/' + (hint[2:] if hint.startswith('./') else hint))
        if 'extension.vsixmanifest' in names:
            try:
                root = ET.fromstring(z.read(names['extension.vsixmanifest']))
                candidates += [(el.text or '').strip() for el in root.iter() if el.tag.split('}')[-1] == 'License']
            except ET.ParseError:
                pass
        candidates += ['extension/LICENSE', 'extension/LICENSE.txt', 'extension/LICENSE.md']
        for c in candidates:
            name = names.get(c.replace('\\', '/').lower())
            if name:
                return name, z.read(name).decode('utf-8', 'replace')
    return None, ''


def licence_facts(api_licence, vsix):
    lic = (api_licence or '').strip()
    facts = {'licence': api_licence}
    if lic and not lic.upper().startswith('SEE LICENSE IN') and lic.upper() != 'UNLICENSED':
        ok = spdx_open(lic)
        if ok is not None:
            facts['openSource'] = ok
            return facts
    hint = lic[len('SEE LICENSE IN'):].strip() if lic.upper().startswith('SEE LICENSE IN') else None
    path, text = licence_text(vsix, hint)
    flat = ' '.join(text.lower().split())
    facts['licenceFile'] = path
    facts['licenceDetected'] = None
    facts['openSource'] = False if lic.upper() == 'UNLICENSED' else None
    # The licence named first is the extension's own; files that bundle third-party notices name more.
    found = sorted((m.start(), name, is_open) for pattern, name, is_open in LICENCE_TEXTS
                   for m in [re.search(pattern, flat)] if m)
    if found:
        facts['licenceDetected'], facts['openSource'] = found[0][1], found[0][2]
        others = sorted({name for _, name, _ in found[1:]} - {found[0][1]})
        if others:
            facts['licenceNote'] = 'the file also contains: ' + ', '.join(others)
        if any(not is_open for _, _, is_open in found):
            facts['openSource'] = False  # closed terms anywhere in the file need a person to read them
    return facts


# ---------------------------------------------------------------------------


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description='Download VS Code extensions for each group from Open VSX.')
    ap.add_argument('--config', default=os.path.join(here, '..', 'config', 'groups.json'))
    ap.add_argument('--out', required=True, help='folder that becomes vscode-extensions.zip')
    ap.add_argument('--target', default='win32-x64', help='Open VSX target platform (default win32-x64)')
    ap.add_argument('--facts', help='facts file (default <out>/../vscode-extensions.facts.json)')
    ap.add_argument('--vscode-version', help='only pick releases whose engines.vscode accepts this VS Code version')
    ap.add_argument('--pre-release', action='store_true', help="allow pre-releases (Open VSX's plain 'latest')")
    args = ap.parse_args()
    # A Windows console may not be UTF-8; never let a licence string crash the build.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')

    out = os.path.abspath(args.out)
    facts_path = os.path.abspath(args.facts or os.path.join(out, '..', 'vscode-extensions.facts.json'))
    with open(args.config, encoding='utf-8') as f:
        groups = json.load(f)['groups']

    log(f'Resolving extensions on Open VSX for {args.target} (or {UNIVERSAL})')
    plan = Plan(args, groups)
    for group, spec in groups.items():
        for ext_id in spec.get('extensions') or []:
            if is_built_in(ext_id):
                warn(f'{group}: {ext_id} is built into VS Code; leave it out of groups.json')
                continue
            plan.place(ext_id, group)
    for key, needed_by in plan.built_in.items():
        log(f'  {key} is built into VS Code (needed by {", ".join(needed_by)}); not downloaded')
    if plan.errors:
        for e in plan.errors:
            print(f'::error::{e}' if IN_ACTIONS else f'error: {e}', file=sys.stderr)
        fail(f'{len(plan.errors)} extension(s) cannot be fetched; fix the lists in {args.config}')
    for group, earlier in plan.needs_groups.items():
        optional = [g for g in sorted(earlier) if not groups[g].get('required')]
        if optional:
            warn(f'{group} needs extensions that are packed in group(s) {", ".join(optional)}; '
                 'the installer has to install those first')

    os.makedirs(out, exist_ok=True)
    order, records, flags = {}, [], []
    for group, keys in plan.order.items():
        order[group] = []
        if not keys:
            continue
        log(f'{group}:')
        gdir = os.path.join(out, group)
        os.makedirs(gdir, exist_ok=True)
        for key in keys:
            meta = plan.meta[key]
            ext_id, version, platform = full_id(meta), meta['version'], meta['targetPlatform']
            name = f'{ext_id}-{version}.vsix'
            dest = os.path.join(gdir, name)
            files = meta.get('files') or {}
            published = None
            gh = github_build(ext_id, version, args.target)
            if gh:
                # the self-contained platform build its maker publishes on GitHub, same version
                files = {'download': gh['url']}
                published = gh['sha256']
                platform = args.target
            elif files.get('sha256'):
                try:
                    m = re.search(r'\b[0-9a-fA-F]{64}\b', get_text(files['sha256']))
                    published = m.group(0).lower() if m else None
                except NotFound:
                    pass
            if published and os.path.exists(dest) and sha256_file(dest) == published:
                digest, how = published, 'already here'
            else:
                digest = download_to(files['download'], dest + '.part')
                if published and digest != published:
                    os.remove(dest + '.part')
                    fail(f'{name}: sha256 {digest} does not match the published {published}')
                os.replace(dest + '.part', dest)
                how = 'downloaded'
            if not published:
                warn(f'{ext_id} {version}: Open VSX publishes no sha256, so the download is unchecked')
            size = os.path.getsize(dest)
            why = plan.why[key]
            role = 'listed' if why['listedIn'] else 'dependency' if why['dependencyOf'] else 'pack member'
            rec = {
                'id': ext_id, 'version': version, 'targetPlatform': platform, 'group': group,
                'file': f'{group}/{name}', 'role': role,
                'dependencyOf': why['dependencyOf'], 'packMemberOf': why['packMemberOf'],
                'size': size, 'sha256': digest, 'sha256Verified': bool(published),
                'preRelease': bool(meta.get('preRelease')),
                'engine': (meta.get('engines') or {}).get('vscode'),
                'published': meta.get('timestamp'), 'publisherVerified': meta.get('verified'),
                'source': files['download'],
            }
            rec.update(licence_facts(meta.get('license'), dest))
            if rec['openSource'] is not True:
                reason = 'not an open-source licence' if rec['openSource'] is False else 'licence not recognised'
                flags.append({'id': ext_id, 'licence': rec['licence'],
                              'licenceDetected': rec.get('licenceDetected'), 'reason': reason})
            if rec['preRelease']:
                warn(f'{ext_id} {version} is a pre-release')
            records.append(rec)
            order[group].append(name)
            lic = rec.get('licenceDetected') or rec['licence']
            log(f'  {name}  [{platform}, {role}, {lic}, {size / 1048576:.1f} MiB, '
                f'{"sha256 ok" if published else "unchecked"}, {how}]')

    # Remove files a previous run left behind (older versions, interrupted downloads).
    for group in groups:
        gdir = os.path.join(out, group)
        if not os.path.isdir(gdir):
            continue
        for leftover in sorted(set(os.listdir(gdir)) - set(order[group])):
            if leftover.endswith(('.vsix', '.vsix.part')):
                log(f'  removing stale {group}/{leftover}')
                os.remove(os.path.join(gdir, leftover))
        if not os.listdir(gdir):
            os.rmdir(gdir)

    with open(os.path.join(out, 'extensions.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(order, f, indent=2)
        f.write('\n')

    total = sum(r['size'] for r in records)
    facts = {
        'source': 'https://open-vsx.org',
        'target': args.target,
        'preReleaseAllowed': args.pre_release,
        'vscodeVersion': args.vscode_version,
        'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'count': len(records),
        'bytes': total,
        'groups': {g: {'count': len(order[g]), 'bytes': sum(r['size'] for r in records if r['group'] == g)}
                   for g in groups},
        'needsGroups': {g: sorted(e) for g, e in plan.needs_groups.items()},
        'extensions': records,
        'skipped': list(plan.skipped.values()),
        'builtIn': [{'id': k, 'neededBy': v} for k, v in plan.built_in.items()],
        'licenceFlags': flags,
    }
    os.makedirs(os.path.dirname(facts_path), exist_ok=True)
    with open(facts_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(facts, f, indent=2)
        f.write('\n')

    log(f'\n{len(records)} extensions, {total / 1048576:.1f} MiB, in {out}')
    for s in plan.skipped.values():
        log(f'  skipped pack member {s["id"]} (from {", ".join(s["packMemberOf"])}): {s["reason"]}')
    for fl in flags:
        warn(f"{fl['id']}: {fl['reason']} ({fl['licence']}; detected {fl['licenceDetected']})")
    log(f'facts: {facts_path}')


if __name__ == '__main__':
    main()
