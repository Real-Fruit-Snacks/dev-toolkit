#!/usr/bin/env python3
"""Build a Cargo local registry of popular crates so `cargo build` works offline.

Seeds are the top N crates on crates.io by all-time downloads plus the names in
config/crates-extra.txt. Each seed contributes the newest release of each of its
newest K semver-compatible lines. From there the normal and build dependencies of
every chosen version are followed until nothing new turns up: for all targets
(Cargo puts every platform's dependencies in the lockfile), optional ones
included, dev ones skipped, taking for each requirement the highest non-yanked
version that satisfies it under Cargo's semver rules.

Two refinements keep the set resolvable the way Cargo resolves:
  * features: a requirement that asks for features takes the highest version that has
    them, and features a version can switch on in a dependency ("dep/feat") add the
    highest version that has those too;
  * pins: when an exact or capped requirement holds an older version back within a
    semver line, the crates that need a newer one in that line also get their newest
    version that accepts the older one, which is where Cargo falls back to.

The output directory is a Cargo local registry:

  <out>/<name>-<version>.crate   checked against the sha256 in the index
  <out>/index/<path>             sparse-index layout (1/, 2/, 3/a/, ab/cd/, lowercase),
                                 holding only the lines of the included versions, verbatim

Cargo reads nothing else from a local registry (no index/config.json). Point it there with:

  [source.crates-io]
  replace-with = "dev-toolkit"
  [source.dev-toolkit]
  local-registry = "<out>"

.crate files already in <out> with the right checksum are kept, so a re-run only
downloads what is new; files that are no longer wanted are removed.

Usage:
  py scripts/build-crates.py --out DIR [--top N] [--extra FILE] [--lines K] [--jobs J] [--facts FILE]
  py scripts/build-crates.py --self-test
"""
import argparse
import concurrent.futures as cf
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from functools import lru_cache

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
UA = 'dev-toolkit-build (https://github.com/Real-Fruit-Snacks/dev-toolkit)'
CRATES_API = 'https://crates.io/api/v1/crates'
INDEX_URL = 'https://index.crates.io/'
DL_URL = 'https://static.crates.io/crates/'
CRATES_IO_INDEX = 'https://github.com/rust-lang/crates.io-index'
INDEX_V_MAX = 2          # newest index entry format that stable Cargo reads
MAX_FAIL_RATIO = 0.01    # more index or download failures than this is a systemic error...
MAX_FAIL_FLOOR = 5       # ...as long as there are more than this many
MAX_BRIDGE_ROUNDS = 10   # bridging normally settles in three or four rounds
T0 = time.monotonic()
QUIET = False


def log(msg):
    if not QUIET:
        print(f'[{time.monotonic() - T0:7.1f}s] {msg}', flush=True)


# --- Semver as Cargo applies it: a port of the parsing and matching rules of the semver crate ---

_NUM = re.compile(r'[0-9]+')
_IDENT = re.compile(r'[0-9A-Za-z-]*(?:\.[0-9A-Za-z-]*)*')
_WILD = ('*', 'x', 'X')
_OPS = ('>=', '<=', '=', '>', '<', '~', '^')


def pre_key(pre):
    """Sort key for a pre-release; '' (a release) sorts above every pre-release."""
    if not pre:
        return (1,)
    return (0,) + tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split('.'))


class Version:
    __slots__ = ('major', 'minor', 'patch', 'pre', 'prekey', 'key')

    def __init__(self, major, minor, patch, pre=''):
        self.major, self.minor, self.patch, self.pre = major, minor, patch, pre
        self.prekey = pre_key(pre)
        self.key = (major, minor, patch, self.prekey)   # build metadata never affects order

    def __str__(self):
        return f'{self.major}.{self.minor}.{self.patch}' + (f'-{self.pre}' if self.pre else '')


def _numeric(text):
    m = _NUM.match(text)
    if not m:
        raise ValueError(f'expected a number at {text!r}')
    s = m.group()
    if len(s) > 1 and s[0] == '0':
        raise ValueError(f'leading zero in {s!r}')
    if len(s) > 20 or int(s) >= 1 << 64:
        raise ValueError(f'number too large: {s!r}')
    return int(s), text[m.end():]


def _identifier(text, pre):
    s = _IDENT.match(text).group()
    if not s:
        return '', text
    parts = s.split('.')
    if '' in parts:
        raise ValueError(f'empty identifier in {s!r}')
    if pre and any(len(p) > 1 and p[0] == '0' and p.isdigit() for p in parts):
        raise ValueError(f'leading zero in {s!r}')
    return s, text[len(s):]


def _pre_build(text):
    """Parse an optional -pre and +build; return (pre, rest)."""
    pre = ''
    if text.startswith('-'):
        pre, text = _identifier(text[1:], True)
        if not pre:
            raise ValueError('empty pre-release')
    if text.startswith('+'):
        build, text = _identifier(text[1:], False)
        if not build:
            raise ValueError('empty build metadata')
    return pre, text


def parse_version(text):
    major, rest = _numeric(text)
    nums = [major]
    for _ in range(2):
        if not rest.startswith('.'):
            raise ValueError(f'bad version {text!r}')
        n, rest = _numeric(rest[1:])
        nums.append(n)
    pre, rest = _pre_build(rest)
    if rest:
        raise ValueError(f'bad version {text!r}')
    return Version(*nums, pre)


def _comparator(text):
    """Parse one comparator; return ((op, major, minor, patch, pre, prekey), rest)."""
    op = next((o for o in _OPS if text.startswith(o)), None)
    default = op is None
    if default:
        op = '^'            # a bare version means a caret requirement
    else:
        text = text[len(op):]
    major, text = _numeric(text.lstrip(' '))
    minor = patch = None
    wild = False
    if text.startswith('.'):
        text = text[1:]
        if text[:1] in _WILD:
            wild, text = True, text[1:]
            if default:
                op = '*'
        else:
            minor, text = _numeric(text)
    if text.startswith('.'):
        text = text[1:]
        if text[:1] in _WILD:
            text = text[1:]
            if default:
                op = '*'
        elif wild:
            raise ValueError('a number after a wildcard')
        else:
            patch, text = _numeric(text)
    pre = ''
    if patch is not None:
        pre, text = _pre_build(text)
    return (op, major, minor, patch, pre, pre_key(pre)), text.lstrip(' ')


@lru_cache(maxsize=None)
def parse_req(text):
    """Parse a version requirement into a tuple of comparators; '*' is the empty tuple."""
    text = text.lstrip(' ')
    if text[:1] in _WILD:
        if text[1:].strip(' '):
            raise ValueError(f'a wildcard must be the only comparator: {text!r}')
        return ()
    comps = []
    while True:
        cmp, text = _comparator(text)
        comps.append(cmp)
        if not text:
            return tuple(comps)
        if text[0] != ',':
            raise ValueError(f'expected a comma at {text!r}')
        if len(comps) == 32:
            raise ValueError('more than 32 comparators')
        text = text[1:].lstrip(' ')


def _exact(c, v):
    return (v.major == c[1] and (c[2] is None or v.minor == c[2])
            and (c[3] is None or v.patch == c[3]) and v.pre == c[4])


def _greater(c, v):
    if v.major != c[1]:
        return v.major > c[1]
    if c[2] is None:
        return False
    if v.minor != c[2]:
        return v.minor > c[2]
    if c[3] is None:
        return False
    if v.patch != c[3]:
        return v.patch > c[3]
    return v.prekey > c[5]


def _less(c, v):
    if v.major != c[1]:
        return v.major < c[1]
    if c[2] is None:
        return False
    if v.minor != c[2]:
        return v.minor < c[2]
    if c[3] is None:
        return False
    if v.patch != c[3]:
        return v.patch < c[3]
    return v.prekey < c[5]


def _tilde(c, v):
    if v.major != c[1] or (c[2] is not None and v.minor != c[2]):
        return False
    if c[3] is not None and v.patch != c[3]:
        return v.patch > c[3]
    return v.prekey >= c[5]


def _caret(c, v):
    _, major, minor, patch, _, pk = c
    if v.major != major:
        return False
    if minor is None:
        return True
    if patch is None:
        return v.minor >= minor if major > 0 else v.minor == minor
    if major > 0:
        if v.minor != minor:
            return v.minor > minor
        if v.patch != patch:
            return v.patch > patch
    elif minor > 0:
        if v.minor != minor:
            return False
        if v.patch != patch:
            return v.patch > patch
    elif v.minor != minor or v.patch != patch:
        return False
    return v.prekey >= pk


_MATCH = {
    '=': _exact, '*': _exact, '>': _greater, '<': _less, '~': _tilde, '^': _caret,
    '>=': lambda c, v: _exact(c, v) or _greater(c, v),
    '<=': lambda c, v: _exact(c, v) or _less(c, v),
}


def matches(req, v):
    """True when Version v satisfies req (a parse_req result)."""
    if not all(_MATCH[c[0]](c, v) for c in req):
        return False
    # A pre-release only matches if some comparator names a pre-release of the same major.minor.patch.
    return not v.pre or any(c[4] and c[1] == v.major and c[2] == v.minor and c[3] == v.patch
                            for c in req)


def compat_key(v):
    """Versions sharing this key are semver-compatible: same major, or same minor for 0.x, or same 0.0.x."""
    if v.major:
        return (v.major,)
    if v.minor:
        return (0, v.minor)
    return (0, 0, v.patch)


def newest_lines(items, k, ver=lambda x: x):
    """From items sorted newest first, the newest one of each of the newest k compatible lines."""
    picks, seen = [], set()
    for item in items:
        key = compat_key(ver(item))
        if key not in seen:
            seen.add(key)
            picks.append(item)
            if len(picks) == k:
                break
    return picks


# --- HTTP: one keep-alive connection per host per thread, with retries ---

class FetchError(Exception):
    pass


_tls = threading.local()
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_NOT_FOUND = {404, 410, 451}


def _get_once(url, gzip_ok):
    conns = _tls.__dict__.setdefault('conns', {})
    for _ in range(5):                                      # follows up to 4 redirects
        u = urllib.parse.urlsplit(url)
        key = (u.scheme, u.netloc)
        path = (u.path or '/') + ('?' + u.query if u.query else '')
        headers = {'User-Agent': UA}
        if gzip_ok:
            headers['Accept-Encoding'] = 'gzip'
        for fresh in (False, True):
            conn = conns.get(key)
            reused = conn is not None
            if conn is None:
                cls = http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection
                conn = conns[key] = cls(u.netloc, timeout=120)
            try:
                conn.request('GET', path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                break
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                conn.close()
                conns.pop(key, None)
                if fresh or not reused:     # a reused connection may simply have gone idle; retry once
                    raise
            except Exception:
                conn.close()
                conns.pop(key, None)
                raise
        if resp.will_close:
            conn.close()
            conns.pop(key, None)
        location = resp.getheader('Location')
        if resp.status in (301, 302, 303, 307, 308) and location:
            url = urllib.parse.urljoin(url, location)
            continue
        if resp.getheader('Content-Encoding', '').lower() == 'gzip':
            body = gzip.decompress(body)
        return resp.status, resp.getheader('Retry-After'), body
    raise FetchError(f'too many redirects for {url}')


def fetch(url, gzip_ok=False, tries=6):
    """GET url and return the body, or None if it does not exist. Raises FetchError otherwise."""
    delay = 1.0
    for attempt in range(1, tries + 1):
        try:
            status, retry_after, body = _get_once(url, gzip_ok)
            if status == 200:
                return body
            if status in _NOT_FOUND:
                return None
            err = f'HTTP {status}'
            if status not in _RETRY_STATUS:
                break
            if retry_after and retry_after.isdigit():
                delay = max(delay, min(float(retry_after), 60.0))
        except Exception as e:                  # network, TLS, protocol and gzip errors
            err = f'{type(e).__name__}: {e}'
        if attempt < tries:
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise FetchError(f'{url}: {err}')


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# --- crates.io data ---

def top_crates(n):
    """Names of the n most downloaded crates, from the crates.io API at no more than 1 request/s."""
    names, page, last = [], 1, 0.0
    while len(names) < n:
        wait = 1.0 - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        body = fetch(f'{CRATES_API}?sort=downloads&per_page=100&page={page}', gzip_ok=True)
        if body is None:
            raise FetchError('crates.io returned 404 for the crate list')
        batch = json.loads(body).get('crates') or []
        if not batch:
            break
        names.extend(c['name'] for c in batch)
        page += 1
    return names[:n]


def index_path(name):
    """Where a crate's file lives in the sparse index, and in a local registry's index/."""
    n = name.lower()
    if len(n) <= 2:
        return f'{len(n)}/{n}'
    if len(n) == 3:
        return f'3/{n[0]}/{n}'
    return f'{n[:2]}/{n[2:4]}/{n}'


class Entry:
    """One line of an index file, that is one published version.

    feats: the features it has (its feature table plus implicit ones for optional dependencies).
    deps:  (crate, req, feats, more) for each normal or build dependency; feats are the features the
           dependency is always asked for, more the ones some feature of this version can switch on.
    """
    __slots__ = ('name', 'vers', 'ver', 'cksum', 'yanked', 'usable', 'feats', 'deps', 'raw')


class Crate:
    """A crate's index file: entries in file order, and the usable non-yanked ones newest first."""
    __slots__ = ('key', 'entries', 'candidates', 'foreign')


def _plain(names):
    """Feature names that must exist on a dependency ('default' always does; 'a/b' is not checked)."""
    return frozenset(n for n in names if isinstance(n, str) and n and n != 'default' and '/' not in n)


def parse_index(key, body):
    crate = Crate()
    crate.key, crate.entries, crate.foreign = key, [], 0
    for raw in body.split(b'\n'):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue                            # Cargo skips lines it cannot read
        e = Entry()
        e.raw, e.name, e.vers, e.cksum = raw, d.get('name'), d.get('vers'), d.get('cksum')
        e.yanked = bool(d.get('yanked'))
        e.usable = (isinstance(e.name, str) and isinstance(e.cksum, str)
                    and (d.get('v') or 1) <= INDEX_V_MAX)
        try:
            e.ver = parse_version(e.vers)
        except (TypeError, ValueError, AttributeError):
            e.ver, e.usable = None, False
        table = dict(d.get('features') or {})
        table.update(d.get('features2') or {})
        values = [v for vals in table.values() for v in vals or () if isinstance(v, str)]
        explicit = {v[4:] for v in values if v.startswith('dep:')}
        switched = {}                           # dependency name -> features "name/feat" can turn on
        for v in values:
            if '/' in v and not v.startswith('dep:'):
                name, feat = v.split('/', 1)
                switched.setdefault(name.rstrip('?'), []).append(feat)
        feats, deps = set(table), []
        for dep in d.get('deps') or ():
            req = dep.get('req')
            try:
                parse_req(req)
            except (TypeError, ValueError, AttributeError):
                e.usable = False                # Cargo drops a version with a requirement it cannot parse
                continue
            name = dep.get('name')
            if dep.get('optional') and name not in explicit:
                feats.add(name)                 # an optional dependency is also a feature, unless dep: hides it
            if dep.get('kind') == 'dev':        # null and anything else count as normal, like Cargo
                continue
            registry = dep.get('registry')
            if registry and str(registry).rstrip('/') != CRATES_IO_INDEX:
                crate.foreign += 1              # from another registry: cannot be served from here
                continue
            target = (dep.get('package') or name or '').lower()
            if target:
                always = _plain(dep.get('features') or ())
                deps.append((target, req, always, _plain(switched.get(name, ())) - always))
        e.feats = frozenset(f for f in feats if isinstance(f, str))
        e.deps = tuple(deps)
        crate.entries.append(e)
    seen, crate.candidates = set(), []
    for e in sorted((e for e in crate.entries if e.usable and not e.yanked),
                    key=lambda e: e.ver.key, reverse=True):
        if e.vers not in seen:
            seen.add(e.vers)
            crate.candidates.append(e)
    return crate


def fetch_crate(key):
    body = fetch(INDEX_URL + index_path(key), gzip_ok=True)
    return None if body is None else parse_index(key, body)


def download_crate(e, out):
    """Make sure <out>/<name>-<vers>.crate exists with the index checksum; return (bytes, downloaded)."""
    fname = f'{e.name}-{e.vers}.crate'
    dest = os.path.join(out, fname)
    if os.path.isfile(dest) and sha256_file(dest) == e.cksum:
        return os.path.getsize(dest), False
    url = DL_URL + urllib.parse.quote(e.name, safe='') + '/' + urllib.parse.quote(fname, safe='')
    for _ in range(2):                          # one more try if the bytes do not match
        body = fetch(url)
        if body is None:
            raise FetchError('not found on static.crates.io')
        if hashlib.sha256(body).hexdigest() == e.cksum:
            break
    else:
        raise FetchError('sha256 does not match the index cksum')
    tmp = dest + '.part'
    with open(tmp, 'wb') as f:
        f.write(body)
    os.replace(tmp, dest)
    return len(body), True


# --- Building the registry ---

class Builder:
    def __init__(self, pool):
        self.pool = pool
        self.crates = {}        # lowercase name -> Crate, or None when missing or unreadable
        self.index_failed = set()
        self.fetched = 0
        self.chosen = {}        # lowercase name -> {vers: Entry}
        self.best = {}          # memo of version lookups
        self.done = set()       # (name, req, feats) requirements already followed
        self.bridged = set()    # (name, vers) included only by bridge()
        self.missing = {}       # dependency not in the index -> one crate that wants it
        self.unsatisfied = {}   # (name, req) no non-yanked version satisfies -> one crate that wants it
        self.failures = []

    def fail(self, crate, version, stage, err):
        self.failures.append({'crate': crate, 'version': version, 'stage': stage, 'error': str(err)})
        log(f'  failed: {stage} {crate}{" " + version if version else ""}: {err}')

    def load(self, names):
        """Fetch the index files of the given lowercase names that are not loaded yet."""
        todo = sorted({n for n in names if n not in self.crates})
        futs = {self.pool.submit(fetch_crate, n): n for n in todo}
        for i, f in enumerate(cf.as_completed(futs), 1):
            n = futs[f]
            try:
                self.crates[n] = f.result()
            except Exception as e:
                self.crates[n] = None
                self.index_failed.add(n)
                self.fail(n, None, 'index', e)
            if i % 2000 == 0:
                log(f'  index files: {i}/{len(todo)}')
        self.fetched += len(todo)

    def seed_versions(self, name, k):
        key = name.lower()
        if self.crates.get(key) is None and key not in self.index_failed:
            for other in {key.replace('_', '-'), key.replace('-', '_')} - {key}:
                self.load([other])              # crates.io treats - and _ alike; the index does not
                if self.crates.get(other) is not None:
                    log(f'  {name} is published as {other}')
                    key = other
                    break
        crate = self.crates.get(key)
        if crate is None:
            if key not in self.index_failed:
                self.fail(name, None, 'seed', 'not in the crates.io index')
            return []
        picks = newest_lines([e for e in crate.candidates if not e.ver.pre], k, lambda e: e.ver)
        if not picks and crate.candidates:      # only pre-releases published: take the newest
            picks = crate.candidates[:1]
            log(f'  {name} has only pre-releases; taking {picks[0].vers}')
        if not picks:
            self.fail(name, None, 'seed', 'no usable, non-yanked version')
        return picks

    def add(self, e):
        """Include version e; True if it is new."""
        versions = self.chosen.setdefault(e.name.lower(), {})
        if e.vers in versions:
            return False
        versions[e.vers] = e
        return True

    def version_count(self):
        return sum(len(v) for v in self.chosen.values())

    def pick(self, name, req, feats):
        """Highest non-yanked version of name that satisfies req and has feats, as Cargo would take."""
        key = (name, req, feats)
        if key not in self.best:
            crate, r = self.crates.get(name), parse_req(req)
            self.best[key] = next((c for c in crate.candidates if matches(r, c.ver) and feats <= c.feats),
                                  None) if crate else None
        return self.best[key]

    def resolve(self, frontier):
        """Follow the dependencies of the newly included versions until nothing new turns up."""
        wave = 0
        while frontier:
            wave += 1
            wants = {}                          # (name, req, feats) -> (version that wants it, required)
            for e in frontier:
                parent = f'{e.name} {e.vers}'
                for name, req, always, more in e.deps:
                    wants[(name, req, always)] = (parent, True)
                    for feat in more:           # a feature of e can ask for more; Cargo then needs a version that has it
                        wants.setdefault((name, req, always | {feat}), (parent, False))
            wants = {k: v for k, v in wants.items() if k not in self.done}
            self.done.update(wants)
            self.load(name for name, _, _ in wants)
            frontier = []
            for (name, req, feats), (parent, required) in wants.items():
                pick = self.pick(name, req, feats)
                if pick is None and required:
                    if self.crates.get(name) is None:
                        if name not in self.index_failed:
                            self.missing.setdefault(name, parent)
                    else:
                        pick = self.pick(name, req, frozenset())
                        what = (name, req) if pick is None else (name, f'{req} with {",".join(sorted(feats))}')
                        self.unsatisfied.setdefault(what, parent)
                if pick is not None:
                    if self.add(pick):
                        frontier.append(pick)
                    else:
                        self.bridged.discard((name, pick.vers))   # a requirement wants it as well
            log(f'wave {wave}: {len(wants)} new requirements, {len(frontier)} new versions; '
                f'{self.version_count()} versions of {len(self.chosen)} crates so far')

    def bridge(self):
        """Versions that let Cargo work around an older version a requirement holds back.

        When an exact or capped requirement keeps an older version of B in the same semver line as a
        newer one (toml_datetime =0.6.3 next to 0.6.11), a crate A that needs the newer one can only
        share a lockfile with the older one through an older A whose requirement accepts it. For each
        such A this adds the newest version in the same line that does, which is the one Cargo would
        fall back to. Versions added this way do not start more bridging of their own: following
        them too roughly doubles what this adds without making more of the registry resolve.
        """
        chosen = {name: [e for e in versions.values() if (name, e.vers) not in self.bridged]
                  for name, versions in self.chosen.items()}
        held = {}                               # B -> versions below the newest of their line
        for name, versions in chosen.items():
            lines = {}
            for e in versions:
                lines.setdefault(compat_key(e.ver), []).append(e)
            for es in lines.values():
                if len(es) > 1:
                    es.sort(key=lambda e: e.ver.key)
                    held.setdefault(name, []).extend(es[:-1])
        added = []
        for versions in chosen.values():
            for a in versions:
                for name, req, _, _ in a.deps:
                    for b in held.get(name, ()):
                        r = parse_req(req)
                        if matches(r, b.ver):
                            continue
                        line = compat_key(b.ver)
                        if not any(compat_key(e.ver) == line and matches(r, e.ver) for e in chosen[name]):
                            continue            # a requirement on another line of B: no conflict
                        older = self.older_accepting(a, name, b)
                        if older is not None and self.add(older):
                            self.bridged.add((older.name.lower(), older.vers))
                            added.append(older)
        return added

    def older_accepting(self, a, name, b):
        """Newest version of a's crate below a, in a's line, with a requirement on name that accepts b."""
        key = (a.name.lower(), a.vers, name, b.vers)
        if key not in self.best:
            line, found = compat_key(a.ver), None
            for c in self.crates[a.name.lower()].candidates:
                if c.ver.key < a.ver.key and compat_key(c.ver) == line and any(
                        n == name and matches(parse_req(r), b.ver) for n, r, _, _ in c.deps):
                    found = c
                    break
            self.best[key] = found
        return self.best[key]

    def download(self, out):
        """Download every chosen version; return the (key, vers) in place, their bytes and the number wanted."""
        todo = [(key, e) for key in sorted(self.chosen) for e in self.chosen[key].values()]
        ok, size, new, last = set(), 0, 0, time.monotonic()
        futs = {self.pool.submit(download_crate, e, out): (key, e) for key, e in todo}
        for i, f in enumerate(cf.as_completed(futs), 1):
            key, e = futs[f]
            try:
                n, downloaded = f.result()
                ok.add((key, e.vers))
                size += n
                new += downloaded
            except Exception as err:
                self.fail(e.name, e.vers, 'download', err)
            if i == len(todo) or time.monotonic() - last >= 15:
                last = time.monotonic()
                log(f'  crates: {i}/{len(todo)}, {size / 2**30:.2f} GiB, {new} downloaded, '
                    f'{i - len(ok)} failed')
        return ok, size, len(todo)

    def write_index(self, out, ok):
        """Write index/ afresh with the lines of the versions in ok, verbatim and in index order."""
        root = os.path.join(out, 'index')
        if os.path.isdir(root):
            shutil.rmtree(root)
        files = lines = size = 0
        for key in sorted(self.chosen):
            keep = {v for v in self.chosen[key] if (key, v) in ok}
            if not keep:
                continue
            rows = [e.raw for e in self.crates[key].entries if e.vers in keep]
            data = b'\n'.join(rows) + b'\n'
            path = os.path.join(root, *index_path(key).split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(data)
            files, lines, size = files + 1, lines + len(rows), size + len(data)
        return files, lines, size


def read_extras(path):
    names = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.split('#', 1)[0].strip()
            if line:
                names.append(line)
    return names


def prune(out, wanted):
    """Remove .crate files that are not wanted any more, and leftovers of interrupted downloads."""
    removed = 0
    for name in os.listdir(out):
        path = os.path.join(out, name)
        stale = (name.endswith('.crate') and name not in wanted) or name.endswith('.crate.part')
        if stale and os.path.isfile(path):
            os.remove(path)
            removed += 1
    return removed


def too_many(failed, total):
    return failed > MAX_FAIL_FLOOR and failed > MAX_FAIL_RATIO * total


def build(args):
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    facts_path = os.path.abspath(args.facts or os.path.join(os.path.dirname(out), 'crates.facts.json'))

    log(f'top {args.top} crates by all-time downloads from crates.io')
    top = top_crates(args.top) if args.top > 0 else []
    if len(top) < args.top:
        log(f'  warning: crates.io listed only {len(top)} crates')
    extras = read_extras(args.extra) if args.extra else []
    seeds, seen = [], set()
    for name in top + extras:
        if name.lower() not in seen:
            seen.add(name.lower())
            seeds.append(name)
    log(f'{len(seeds)} seeds ({len(top)} top, {len(seeds) - len(top)} more from {args.extra or "no extras file"})')

    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        b = Builder(pool)
        b.load(s.lower() for s in seeds)
        roots, seed_count = [], 0
        for name in seeds:
            picks = b.seed_versions(name, args.lines)
            seed_count += bool(picks)
            roots.extend(picks)
        log(f'{seed_count} seeds resolved to {len(roots)} versions; following dependencies')
        b.resolve([e for e in roots if b.add(e)])
        for rounds in range(1, MAX_BRIDGE_ROUNDS + 1):
            extra = b.bridge()
            if not extra:
                break
            log(f'bridging {rounds}: {len(extra)} older versions that accept versions pins hold back')
            b.resolve(extra)
        else:
            log(f'  warning: still bridging after {MAX_BRIDGE_ROUNDS} rounds; stopping there')
        foreign = sum(c.foreign for c in b.crates.values() if c is not None)
        log(f'closure: {b.version_count()} versions of {len(b.chosen)} crates, '
            f'{b.fetched} index files read, {len(b.missing)} dependencies not in the index, '
            f'{len(b.unsatisfied)} requirements nothing satisfies, {foreign} dependencies on other registries skipped')
        for name, parent in sorted(b.missing.items())[:50]:
            log(f'  not in the index: {name} (wanted by {parent})')
        for (name, req), parent in sorted(b.unsatisfied.items())[:50]:
            log(f'  nothing satisfies: {name} {req} (wanted by {parent})')

        log(f'downloading into {out} with {args.jobs} jobs')
        ok, crate_bytes, wanted_count = b.download(out)

    files, lines, index_bytes = b.write_index(out, ok)
    wanted = {f'{b.chosen[key][vers].name}-{vers}.crate' for key, vers in ok}
    removed = prune(out, wanted)
    if removed:
        log(f'removed {removed} files left from an earlier run')
    crates = len({key for key, _ in ok})
    log(f'registry: {crates} crates, {lines} versions, {crate_bytes / 2**30:.2f} GiB of .crate files, '
        f'{index_bytes / 2**20:.1f} MiB of index in {files} files')

    failures = b.failures
    facts = {
        'asset': 'crates',
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'top': args.top,
        'lines': args.lines,
        'seeds': seed_count,
        'crates': crates,
        'versions': lines,
        'bytes': crate_bytes + index_bytes,
        'crateBytes': crate_bytes,
        'indexBytes': index_bytes,
        'unsatisfied': len(b.unsatisfied) + len(b.missing),
        'seconds': round(time.monotonic() - T0),
        'failureCount': len(failures),
        'failures': failures[:500],
    }
    os.makedirs(os.path.dirname(facts_path), exist_ok=True)
    with open(facts_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(facts, f, indent=2)
        f.write('\n')
    log(f'wrote {facts_path}')

    problems = []
    if not lines:
        problems.append('the registry is empty')
    if too_many(len(b.index_failed), b.fetched):
        problems.append(f'{len(b.index_failed)} of {b.fetched} index files could not be fetched')
    download_failed = wanted_count - len(ok)
    if too_many(download_failed, wanted_count):
        problems.append(f'{download_failed} of {wanted_count} crates could not be downloaded')
    if problems:
        sys.exit('error: ' + '; '.join(problems))
    log(f'done, {len(failures)} per-crate failures')


# --- Self checks ---

def self_test():
    V = parse_version
    bad = []

    yes_no = {
        '^1.2.3': ('1.2.3 1.2.4 1.8.1', '2.0.0 1.2.2 1.2.3-alpha 1.2.4-alpha 1.3.0-rc.1'),
        '1.2.3': ('1.2.3 1.9.0', '2.0.0 1.2.2'),
        '^1.2': ('1.2.0 1.9.9', '2.0.0 1.1.9'),
        '^1': ('1.0.0 1.99.0', '2.0.0 0.9.0'),
        '^0.2.3': ('0.2.3 0.2.9', '0.3.0 0.2.2 1.0.0'),
        '0.2': ('0.2.0 0.2.9', '0.3.0 0.1.9'),
        '^0.0.3': ('0.0.3', '0.0.4 0.0.2 0.1.0'),
        '^0.0': ('0.0.0 0.0.9', '0.1.0'),
        '^0': ('0.0.0 0.9.9', '1.0.0'),
        '~1.2.3': ('1.2.3 1.2.9', '1.3.0 1.2.2'),
        '~1.2': ('1.2.0 1.2.9', '1.3.0 1.1.0'),
        '~1': ('1.0.0 1.9.0', '2.0.0'),
        '*': ('0.0.1 1.0.0 99.0.0', '1.0.0-alpha'),
        '1.*': ('1.0.0 1.5.0', '2.0.0 0.9.0 1.1.0-beta'),
        '1.2.*': ('1.2.0 1.2.9', '1.3.0 1.1.9'),
        '1.x': ('1.0.0 1.9.0', '2.0.0'),
        '1.2.X': ('1.2.5', '1.3.0'),
        '1.*.*': ('1.4.2', '2.0.0'),
        '>=1.*': ('1.0.0 3.0.0', '0.9.0'),
        '>= 1.2.0, < 1.5': ('1.2.0 1.4.9', '1.5.0 1.1.9 1.3.0-alpha'),
        ' >= 1.0 , < 2 ': ('1.0.0 1.9.9', '2.0.0 0.9.0'),
        '>1.2': ('1.3.0 2.0.0', '1.2.9 1.2.0'),
        '>1.2.3': ('1.2.4 2.0.0', '1.2.3'),
        '>1': ('2.0.0', '1.9.9'),
        '>=1.2': ('1.2.0 1.3.0', '1.1.9'),
        '<=1.2': ('1.2.9 1.0.0', '1.3.0'),
        '<=1.2.3': ('1.2.3 1.2.2', '1.2.4'),
        '<1.2': ('1.1.9 0.1.0', '1.2.0'),
        '<1.2.3': ('1.2.2', '1.2.3 1.2.3-alpha'),
        '=1.2.3': ('1.2.3', '1.2.4 1.2.2'),
        '=1.2': ('1.2.0 1.2.9', '1.3.0'),
        '=1': ('1.0.0 1.9.9', '2.0.0'),
        '=1.2.3+meta': ('1.2.3', '1.2.4'),
        '=1.2.3-alpha.1': ('1.2.3-alpha.1', '1.2.3-alpha.2 1.2.3'),
        '^1.2.3-alpha.2': ('1.2.3-alpha.2 1.2.3-alpha.10 1.2.3-beta 1.2.3 1.2.4',
                           '1.2.4-alpha 1.2.3-alpha.1 2.0.0 1.2.3-alpha'),
        '^0.3.0-beta.1': ('0.3.0-beta.1 0.3.0 0.3.5', '0.3.1-beta 0.4.0 0.3.0-alpha'),
        '>=1.0.0-rc.1, <2.0.0': ('1.0.0-rc.1 1.0.0-rc.2 1.5.0', '1.1.0-alpha 2.0.0-alpha 1.0.0-beta'),
        '>=0.3.0-beta.1': ('0.3.0-beta.2 0.3.0 9.0.0', '0.3.1-beta'),
        '~1.2.3-beta': ('1.2.3-beta 1.2.3 1.2.4', '1.2.4-beta 1.3.0 1.2.3-alpha'),
        '<1.0.0-beta': ('1.0.0-alpha 0.9.0', '1.0.0-beta 1.0.0'),
        '^1.0.0+build.5': ('1.0.0 1.0.0+other 1.4.0+x', '2.0.0'),
    }
    for req, (yes, no) in yes_no.items():
        try:
            r = parse_req(req)
        except ValueError as e:
            bad.append(f'{req!r} did not parse: {e}')
            continue
        bad += [f'{req!r} should match {v}' for v in yes.split() if not matches(r, V(v))]
        bad += [f'{req!r} should not match {v}' for v in no.split() if matches(r, V(v))]

    for req in ['', ' ', '1.2.3-', '01.2', '>=', '1.*.3', '*, 1.0', '1.0, *', '>=1.0 <2.0', '1.2.3.4',
                'abc', '1.0.0-01', '>= 1.0,', '~>1.0', '1.2.3 - 1.4', '1.0 || 2.0', '^1.2.3-.a', 'v1']:
        try:
            parse_req(req)
            bad.append(f'{req!r} should not parse')
        except ValueError:
            pass
    for v in ['1.2', '1.2.3-', '01.2.3', '1.2.3-01', '1.2.3+', 'v1.2.3', '1.2.3 ', '1.2.3-a..b']:
        try:
            parse_version(v)
            bad.append(f'version {v!r} should not parse')
        except ValueError:
            pass

    order = ('1.0.0-alpha 1.0.0-alpha.1 1.0.0-alpha.beta 1.0.0-beta 1.0.0-beta.2 1.0.0-beta.11 '
             '1.0.0-rc.1 1.0.0 1.0.1 1.1.0 2.0.0-0 2.0.0 10.0.0').split()
    keys = [V(v).key for v in order]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        bad.append('version ordering')
    if V('1.0.0+a').key != V('1.0.0+b').key:
        bad.append('build metadata must not affect order')

    vs = sorted(map(V, '2.1.0 2.0.5 1.9.0 1.8.0 0.9.3 0.9.1 0.8.0 0.0.3 0.0.2'.split()),
                key=lambda v: v.key, reverse=True)
    for k, want in [(1, '2.1.0'), (2, '2.1.0 1.9.0'), (9, '2.1.0 1.9.0 0.9.3 0.8.0 0.0.3 0.0.2')]:
        got = ' '.join(str(v) for v in newest_lines(vs, k))
        if got != want:
            bad.append(f'newest_lines k={k}: {got}')

    for name, want in [('a', '1/a'), ('AB', '2/ab'), ('abc', '3/a/abc'), ('Serde', 'se/rd/serde'),
                       ('tokio-util', 'to/ki/tokio-util'), ('wasm_bindgen', 'wa/sm/wasm_bindgen')]:
        if index_path(name) != want:
            bad.append(f'index_path({name!r}) = {index_path(name)!r}')

    lines = [
        {'name': 'demo', 'vers': '1.0.0', 'cksum': 'a', 'deps': [
            {'name': 'serde', 'req': '^1.0', 'kind': 'normal'},
            {'name': 'cc', 'req': '^1', 'kind': 'build'},
            {'name': 'kind-null', 'req': '*', 'kind': None},
            {'name': 'proptest', 'req': '^1', 'kind': 'dev'},
            {'name': 'nix', 'req': '^0.29', 'target': 'cfg(unix)', 'optional': True},
            {'name': 'alias', 'package': 'Real-Name', 'req': '^2'},
            {'name': 'elsewhere', 'req': '^1', 'registry': 'https://example.com/index'},
            {'name': 'here', 'req': '^1', 'registry': CRATES_IO_INDEX}]},
        {'name': 'demo', 'vers': '1.1.0', 'cksum': 'b', 'yanked': True, 'deps': []},
        {'name': 'demo', 'vers': '1.2.0', 'cksum': 'c', 'deps': [{'name': 'x', 'req': '>=1 <2'}]},
        {'name': 'demo', 'vers': '1.3.0', 'cksum': 'd', 'v': 3, 'deps': []},
        {'name': 'demo', 'vers': '1.0.1-rc.1', 'cksum': 'e', 'deps': []},
    ]
    def index(name, rows):
        rows = [dict({'name': name, 'cksum': f'{name}-{r["vers"]}', 'deps': []}, **r) for r in rows]
        return parse_index(name, ('\n'.join(json.dumps(r) for r in rows) + '\n\nnot json\n').encode())

    crate = index('demo', lines)
    deps = sorted((n, r) for n, r, _, _ in crate.entries[0].deps)
    want = sorted([('serde', '^1.0'), ('cc', '^1'), ('kind-null', '*'), ('nix', '^0.29'),
                   ('real-name', '^2'), ('here', '^1')])
    if deps != want:
        bad.append(f'parse_index deps: {deps}')
    if [e.vers for e in crate.candidates] != ['1.0.1-rc.1', '1.0.0'] or crate.foreign != 1:
        bad.append(f'parse_index candidates: {[e.vers for e in crate.candidates]}, foreign {crate.foreign}')

    feat = index('feat', [{'vers': '1.0.0', 'features': {'default': ['std'], 'std': []},
                           'features2': {'derive': ['dep:serde_derive'], 'json': ['serde_json/std', 'toml?/display']},
                           'deps': [{'name': 'serde_derive', 'req': '^1', 'optional': True},
                                    {'name': 'toml', 'req': '^0.8', 'optional': True},
                                    {'name': 'serde_json', 'req': '^1', 'features': ['raw_value', 'default']}]}])
    e = feat.entries[0]
    if e.feats != {'default', 'std', 'derive', 'json', 'toml'}:
        bad.append(f'features provided: {sorted(e.feats)}')
    got = {n: (sorted(a), sorted(m)) for n, _, a, m in e.deps}
    if got != {'serde_derive': ([], []), 'toml': ([], ['display']), 'serde_json': (['raw_value'], ['std'])}:
        bad.append(f'features asked of dependencies: {got}')

    # Cargo's choices on a small made-up index: pre-releases, features, and an older version
    # a pin forces (b =0.6.3 from p, while the newest a wants b ^0.6.11).
    global QUIET
    QUIET = True
    b = Builder(None)
    for c in [crate,
              index('b', [{'vers': '0.6.3'}, {'vers': '0.6.11'}]),
              index('a', [{'vers': '0.8.2', 'deps': [{'name': 'b', 'req': '^0.6.3'}]},
                          {'vers': '0.8.23', 'deps': [{'name': 'b', 'req': '^0.6.11'}]}]),
              index('p', [{'vers': '2.0.2', 'deps': [{'name': 'b', 'req': '=0.6.3'}]}]),
              index('clap', [{'vers': '2.33.3', 'features': {'lints': []}}, {'vers': '2.34.0'}]),
              index('lib', [{'vers': '0.3.26', 'features': {'lints': ['clap/lints']},
                             'deps': [{'name': 'clap', 'req': '^2.33'}]}]),
              index('root', [{'vers': '1.0.0', 'deps': [
                  {'name': 'a', 'req': '^0.8'}, {'name': 'p', 'req': '^2'}, {'name': 'lib', 'req': '^0.3'},
                  {'name': 'b', 'req': '^0.6'}, {'name': 'clap', 'req': '^2', 'features': ['lints']}]}])]:
        b.crates[c.key] = c
    if b.pick('demo', '^1', frozenset()).vers != '1.0.0':
        bad.append('the highest match must skip a pre-release the requirement does not name')
    if b.pick('demo', '^1.0.1-rc.1', frozenset()).vers != '1.0.1-rc.1':
        bad.append('a requirement naming a pre-release must be able to pick it')
    root = b.crates['root'].candidates[0]
    b.add(root)
    b.resolve([root])
    while True:
        extra = b.bridge()
        if not extra:
            break
        b.resolve(extra)
    QUIET = False
    got = {n: sorted(v) for n, v in b.chosen.items()}
    want = {'root': ['1.0.0'], 'a': ['0.8.2', '0.8.23'], 'p': ['2.0.2'], 'b': ['0.6.11', '0.6.3'],
            'lib': ['0.3.26'], 'clap': ['2.33.3', '2.34.0']}
    if got != want:
        bad.append(f'closure: {got}')

    if bad:
        print('self-test FAILED:\n  ' + '\n  '.join(bad))
        sys.exit(1)
    print(f'self-test passed ({sum(len(y.split()) + len(n.split()) for y, n in yes_no.values())} matches checked)')


def main():
    ap = argparse.ArgumentParser(description='Build a Cargo local registry of popular crates for offline use.')
    ap.add_argument('--top', type=int, default=2000, help='seed with the N most downloaded crates (default 2000)')
    ap.add_argument('--extra', default=os.path.join(REPO, 'config', 'crates-extra.txt'),
                    help='file of more crate names to seed, one per line, # comments ("" for none)')
    ap.add_argument('--lines', type=int, default=2, help='newest semver-compatible lines per seed (default 2)')
    ap.add_argument('--out', help='registry root to write (required)')
    ap.add_argument('--jobs', type=int, default=24, help='parallel downloads (default 24)')
    ap.add_argument('--facts', help='facts file to write (default <out>/../crates.facts.json)')
    ap.add_argument('--self-test', action='store_true', help='check the semver matcher and exit')
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.out:
        ap.error('--out is required')
    if args.lines < 1 or args.jobs < 1 or args.top < 0:
        ap.error('--lines and --jobs must be at least 1, --top at least 0')
    try:
        build(args)
    except FetchError as e:
        sys.exit(f'error: {e}')


if __name__ == '__main__':
    main()
