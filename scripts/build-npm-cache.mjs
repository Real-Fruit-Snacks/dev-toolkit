#!/usr/bin/env node
// Seeds an npm cache folder so that, on a machine with no network, `npm install <pkg> --offline`
// works for popular packages, with the native optional packages for every target platform.
//
// How it works:
//   1. Seeds: the most depended-on packages from npm-high-impact, plus config/npm-extra.txt.
//   2. Install: real `npm install <pkg>@latest ...` runs in throwaway projects, in batches, once per
//      target platform (npm's --os/--cpu/--libc), so each platform's optional native packages land
//      in the cache. A failing batch is split until the bad packages are isolated and skipped.
//   3. Verify: every seed is resolved on its own with --offline, the way a user installs it, and
//      every tarball that resolution needs on every target platform is looked up in the cache.
//      Batch installs dedupe differently from single installs, and they skip peer dependencies
//      (--legacy-peer-deps), so this step finds what is missing and adds it.
//   4. Report: install scripts in the verified trees are classified, so the release can say which
//      packages still need network when they are installed (browser and engine downloads).
//
// Writes the cache folder (it holds only _cacache/) and <cache>/../npm-cache.facts.json.
// Never run `npm cache verify` on the result: it keeps one index entry per URL and so drops the
// abbreviated packuments that `npm install <pkg>@latest` reads.
//
// Node built-ins and the npm CLI only. `node scripts/build-npm-cache.mjs --help` for options.

import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import zlib from 'node:zlib';

const REGISTRY = 'https://registry.npmjs.org/';
const CORGI_ACCEPT = 'application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8, */*';
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// If more than this share of the seeds fail, something is wrong with the build, not with packages.
const MAX_FAILED_SHARE = 0.1;
// Network errors (after retries) in this many different batches in a row: the network is down.
const MAX_NETWORK_FAILED_BATCHES = 3;

const TIMEOUT = { install: 30 * 60e3, verify: 15 * 60e3, cacheAdd: 30 * 60e3, version: 60e3 };

// npm error codes that point at the network or the registry rather than at a package.
const NETWORK = new Set([
  'ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT', 'ESOCKETTIMEDOUT', 'EAI_AGAIN', 'ENOTFOUND', 'EPIPE',
  'ERR_SOCKET_TIMEOUT', 'ERR_STREAM_PREMATURE_CLOSE', 'UND_ERR_SOCKET', 'E429', 'E500', 'E502',
  'E503', 'E504', 'TIMEOUT',
]);
// Worth retrying: the above, a download that arrived damaged, and Windows file locks.
const TRANSIENT = new Set([...NETWORK, 'EINTEGRITY', 'EPERM', 'EBUSY', 'EACCES', 'ENOTEMPTY', 'EMFILE']);

const USAGE = `Usage: node scripts/build-npm-cache.mjs [options]

  --top N          seeds taken from npm-high-impact (default 2000)
  --extra FILE     more seeds, one package per line (default config/npm-extra.txt)
  --cache DIR      cache folder to create (default stage/npm-cache); must be empty or missing
  --platforms LIST targets as os-cpu[-libc] (default win32-x64,linux-x64; linux means glibc)
  --batch B        packages per install (default 40)
  --jobs J         installs run in parallel (default 4)
  --facts FILE     facts output (default <cache>/../npm-cache.facts.json)
  --work DIR       throwaway projects (default <cache>/../npm-cache.work)
  --clean          delete the cache folder first if it is not empty
  --keep-work      keep the work folder (state.json, npm logs) for a later --only run
  --only verify    on an existing cache and kept work folder, redo the verify step and report
  --only report    on an existing cache and kept work folder, redo the install-script report
  -h, --help       show this help
`;

// ---------------------------------------------------------------------------------------------
// Options

function parseOptions() {
  const { values } = parseArgs({
    options: {
      top: { type: 'string', default: '2000' },
      extra: { type: 'string', default: path.join(REPO, 'config', 'npm-extra.txt') },
      cache: { type: 'string', default: path.join(REPO, 'stage', 'npm-cache') },
      platforms: { type: 'string', default: 'win32-x64,linux-x64' },
      batch: { type: 'string', default: '40' },
      jobs: { type: 'string', default: '4' },
      facts: { type: 'string' },
      work: { type: 'string' },
      clean: { type: 'boolean', default: false },
      'keep-work': { type: 'boolean', default: false },
      only: { type: 'string' },
      help: { type: 'boolean', short: 'h', default: false },
    },
    strict: true,
    allowPositionals: false,
  });
  if (values.help) {
    process.stdout.write(USAGE);
    process.exit(0);
  }
  const int = (name, min) => {
    const n = Number(values[name]);
    if (!Number.isInteger(n) || n < min) throw new Fatal(`--${name} must be a whole number >= ${min}`);
    return n;
  };
  const cache = path.resolve(values.cache);
  if (values.only && !['verify', 'report'].includes(values.only)) throw new Fatal('--only accepts "verify" or "report"');
  return {
    top: int('top', 0),
    extra: values.extra ? path.resolve(values.extra) : null,
    cache,
    platforms: parsePlatforms(values.platforms),
    batch: int('batch', 1),
    jobs: int('jobs', 1),
    facts: path.resolve(values.facts ?? path.join(cache, '..', 'npm-cache.facts.json')),
    work: path.resolve(values.work ?? path.join(cache, '..', 'npm-cache.work')),
    clean: values.clean,
    keepWork: values['keep-work'] || !!values.only,
    only: values.only ?? null,
  };
}

// "win32-x64" -> npm flags for that target. Linux defaults to glibc (Ubuntu under WSL).
function parsePlatforms(list) {
  const out = [];
  for (const id of list.split(',').map(s => s.trim()).filter(Boolean)) {
    const [os, cpu, libcName] = id.split('-');
    if (!os || !cpu) throw new Fatal(`bad platform "${id}", expected os-cpu like win32-x64`);
    const libc = os === 'linux' ? (libcName === 'musl' ? 'musl' : 'glibc') : undefined;
    const flags = [`--os=${os}`, `--cpu=${cpu}`];
    // Without --libc, npm on a Windows host cannot tell glibc from musl and skips both variants.
    if (libc) flags.push(`--libc=${libc}`);
    out.push({ id, os, cpu, libc, flags });
  }
  if (!out.length) throw new Fatal('--platforms is empty');
  return out;
}

// ---------------------------------------------------------------------------------------------
// Logging and small helpers

class Fatal extends Error {}

const started = Date.now();
const clock = () => {
  const s = Math.floor((Date.now() - started) / 1000);
  return `${String(Math.floor(s / 60)).padStart(3, ' ')}:${String(s % 60).padStart(2, '0')}`;
};
const log = msg => console.log(`[${clock()}] ${msg}`);
const warn = msg => {
  console.log(`[${clock()}] WARN ${msg}`);
  if (process.env.GITHUB_ACTIONS) console.log(`::warning::${msg}`);
};
const secs = ms => Math.round(ms / 100) / 10;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const chunk = (arr, n) => Array.from({ length: Math.ceil(arr.length / n) }, (_, i) => arr.slice(i * n, i * n + n));
const pad = (n, w = 3) => String(n).padStart(w, '0');

// Runs fn over items with at most n in flight. Stops handing out work once abort.error is set.
async function pool(items, n, fn, abort) {
  let next = 0;
  const worker = async () => {
    while (next < items.length && !abort.error) {
      const i = next++;
      try {
        await fn(items[i], i);
      } catch (err) {
        abort.error ??= err;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, worker));
  if (abort.error) throw abort.error;
}

async function rmrf(p) {
  await fsp.rm(p, { recursive: true, force: true, maxRetries: 10, retryDelay: 300 });
}

async function makeProject(dir) {
  await rmrf(dir);
  await fsp.mkdir(dir, { recursive: true });
  await fsp.writeFile(path.join(dir, 'package.json'), '{ "name": "seed", "version": "0.0.0", "private": true }\n');
}

// ---------------------------------------------------------------------------------------------
// npm CLI

let NPM = null; // { cli, version, common, env }

function findNpmCli() {
  const bin = path.dirname(process.execPath);
  const candidates = [
    process.env.npm_execpath,
    path.join(bin, 'node_modules', 'npm', 'bin', 'npm-cli.js'), // Windows layout
    path.join(bin, '..', 'lib', 'node_modules', 'npm', 'bin', 'npm-cli.js'), // Unix layout
  ];
  for (const c of candidates) {
    if (c && /npm-cli\.js$/.test(c) && fs.existsSync(c)) return path.resolve(c);
  }
  throw new Fatal(`cannot find npm-cli.js next to ${process.execPath}`);
}

// Children get the caller's environment minus npm_config_* settings (except proxies), so a
// developer's or runner's npm settings cannot leak into the build. Nothing outside is changed.
function childEnv() {
  const env = { ...process.env };
  for (const k of Object.keys(env)) {
    if (/^npm_config_/i.test(k) && !/proxy$/i.test(k)) delete env[k];
  }
  env.NO_UPDATE_NOTIFIER = '1';
  return env;
}

function setupNpm(work, cache) {
  const cli = findNpmCli();
  const version = JSON.parse(fs.readFileSync(path.join(cli, '..', '..', 'package.json'), 'utf8')).version;
  const userrc = path.join(work, 'npmrc-user');
  const globalrc = path.join(work, 'npmrc-global');
  fs.mkdirSync(work, { recursive: true });
  fs.writeFileSync(userrc, '');
  fs.writeFileSync(globalrc, '');
  NPM = {
    cli,
    version,
    env: childEnv(),
    common: [
      '--cache', cache,
      '--userconfig', userrc,
      '--globalconfig', globalrc,
      '--registry', REGISTRY,
      '--logs-dir', path.join(work, 'logs'),
      '--update-notifier=false', '--audit=false', '--fund=false',
      '--color=false', '--progress=false', '--loglevel=warn',
    ],
  };
}

function errorCode(stderr) {
  const m = stderr.match(/npm (?:error|ERR!) code (\S+)/);
  if (m) return m[1];
  if (/ENOSPC|no space left/i.test(stderr)) return 'ENOSPC';
  return 'EUNKNOWN';
}

function errorDetail(stderr) {
  const lines = stderr.split(/\r?\n/)
    .filter(l => /^npm (?:error|ERR!)/.test(l) && !/complete log of this run|^npm (?:error|ERR!)\s*(?:code \S+|errno \S+|\d+)?\s*$/.test(l))
    .map(l => l.replace(/^npm (?:error|ERR!)\s*/, '').trim());
  return lines.slice(0, 2).join(' | ').slice(0, 300);
}

function npmRun(args, cwd, timeoutMs) {
  return new Promise(resolve => {
    const t0 = Date.now();
    const child = spawn(process.execPath, [NPM.cli, ...args, ...NPM.common], {
      cwd, env: NPM.env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    child.stdout.on('data', d => { stdout = (stdout + d).slice(-64e3); });
    child.stderr.on('data', d => { stderr = (stderr + d).slice(-64e3); });
    const timer = setTimeout(() => { timedOut = true; child.kill(); }, timeoutMs);
    child.on('error', err => { stderr += `\nspawn failed: ${err.message}`; });
    child.on('close', code => {
      clearTimeout(timer);
      const ok = code === 0 && !timedOut;
      resolve({
        ok, code, stdout, stderr, ms: Date.now() - t0,
        err: ok ? null : timedOut ? 'TIMEOUT' : errorCode(stderr),
        detail: ok ? '' : timedOut ? `no result after ${secs(timeoutMs)} s` : errorDetail(stderr),
      });
    });
  });
}

// ---------------------------------------------------------------------------------------------
// Tarballs (gzip + tar) with built-ins only

function checkIntegrity(buf, integrity) {
  for (const part of String(integrity).split(/\s+/)) {
    const [algo, digest] = part.split(/-(.*)/s);
    if (!algo || !digest) continue;
    if (crypto.createHash(algo).update(buf).digest('base64') === digest.replace(/\?.*$/, '')) return;
  }
  throw new Fatal(`integrity mismatch (${String(integrity).slice(0, 30)}...)`);
}

function parsePax(buf) {
  const out = {};
  let off = 0;
  while (off < buf.length) {
    const sp = buf.indexOf(0x20, off);
    if (sp < 0) break;
    const len = parseInt(buf.subarray(off, sp).toString(), 10);
    if (!len) break;
    const rec = buf.subarray(sp + 1, off + len - 1).toString('utf8');
    const eq = rec.indexOf('=');
    if (eq > 0) out[rec.slice(0, eq)] = rec.slice(eq + 1);
    off += len;
  }
  return out;
}

// Yields { path, type, data } with the first path component removed, as npm does.
function* tarEntries(tar) {
  let off = 0;
  let longName = null;
  let pax = null;
  while (off + 512 <= tar.length) {
    const h = tar.subarray(off, off + 512);
    if (h[0] === 0) break;
    const field = (a, b) => {
      const s = h.subarray(a, b);
      const z = s.indexOf(0);
      return s.subarray(0, z < 0 ? s.length : z).toString('utf8');
    };
    let size;
    if (h[124] & 0x80) {
      size = 0;
      for (let i = 125; i < 136; i++) size = size * 256 + h[i];
    } else {
      size = parseInt(field(124, 136).trim() || '0', 8);
    }
    const type = h[156] ? String.fromCharCode(h[156]) : '0';
    let name = field(0, 100);
    if (field(257, 263).startsWith('ustar')) {
      const prefix = field(345, 500);
      if (prefix) name = `${prefix}/${name}`;
    }
    const data = tar.subarray(off + 512, off + 512 + size);
    off += 512 + Math.ceil(size / 512) * 512;
    if (type === 'L') { longName = data.toString('utf8').replace(/\0+$/, ''); continue; }
    if (type === 'x') { pax = parsePax(data); continue; }
    if (type === 'g') continue;
    if (pax?.path) name = pax.path;
    else if (longName) name = longName;
    pax = null;
    longName = null;
    const rel = name.replace(/^(\.\/)+/, '').replace(/^[^/]*\/?/, '');
    yield { path: rel, type, data };
  }
}

function gunzip(buf) {
  return zlib.gunzipSync(buf, { maxOutputLength: 512 * 1024 * 1024 });
}

async function fetchRetry(url, init = {}, tries = 4) {
  for (let i = 1; ; i++) {
    try {
      const res = await fetch(url, { ...init, signal: AbortSignal.timeout(120e3) });
      if (res.ok) return res;
      if (res.status < 500 && res.status !== 429) throw new Fatal(`GET ${url}: HTTP ${res.status}`);
      throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      if (err instanceof Fatal || i >= tries) throw new Fatal(`GET ${url} failed: ${err.message}`);
      await sleep(2000 * i);
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Seeds

// npm-high-impact's lib files are plain arrays of quoted names. They are read as text, never run.
function parseNameList(text, file) {
  const names = [...text.matchAll(/^\s*(['"])((?:(?!\1).)+)\1,?\s*$/gm)].map(m => m[2]);
  if (names.length < 500) throw new Fatal(`npm-high-impact ${file}: only ${names.length} names; has the format changed?`);
  return names;
}

const NAME_RE = /^(?:@[a-z0-9~][a-z0-9._~-]*\/)?[A-Za-z0-9~][A-Za-z0-9._~-]*$/;

async function loadHighImpact() {
  const doc = await (await fetchRetry(`${REGISTRY}npm-high-impact`, { headers: { accept: CORGI_ACCEPT } })).json();
  const version = doc['dist-tags']?.latest;
  const dist = doc.versions?.[version]?.dist;
  if (!dist?.tarball || !dist.integrity) throw new Fatal('npm-high-impact: no tarball for the latest version');
  const tgz = Buffer.from(await (await fetchRetry(dist.tarball)).arrayBuffer());
  checkIntegrity(tgz, dist.integrity);
  const files = new Map();
  for (const e of tarEntries(gunzip(tgz))) files.set(e.path, e.data);
  const list = file => {
    const buf = files.get(file);
    if (!buf) throw new Fatal(`npm-high-impact ${version} has no ${file}`);
    return parseNameList(buf.toString('utf8'), file);
  };
  return { version, dependents: list('lib/top-dependent.js'), downloads: list('lib/top-download.js') };
}

function readExtra(file) {
  if (!file) return [];
  if (!fs.existsSync(file)) throw new Fatal(`extra seed file not found: ${file}`);
  const out = [];
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.replace(/#.*/, '').trim();
    if (!line) continue;
    // "name" or "name@range"; a scoped name starts with @, so look for an @ after the first char.
    const at = line.indexOf('@', 1);
    const name = at > 0 ? line.slice(0, at) : line;
    const range = at > 0 ? line.slice(at + 1) : null;
    if (!NAME_RE.test(name) || (range !== null && !/^[\w.^~<>=| *-]+$/.test(range))) {
      warn(`${path.basename(file)}: skipping "${line}"`);
      continue;
    }
    out.push({ name, range });
  }
  return out;
}

// The seed list: packages from npm-high-impact's top-dependent list (counts devDependencies too,
// so it reflects what developers put in their own package.json) that also clear its download
// threshold (1M a week, i.e. also in top-download), kept in dependents order. The filter drops
// spam names and long-dead tooling that only old packages still list. The download list on its
// own is mostly transitive packages (semver, ms, ansi-styles) that nobody installs directly.
async function loadSeeds(opt) {
  const seeds = [];
  const seen = new Map();
  const add = (name, range, source, rank) => {
    const key = `${name}@${range ?? 'latest'}`;
    if (seen.has(key)) {
      const s = seen.get(key);
      if (!s.source.includes(source)) s.source += `+${source}`;
      return;
    }
    // Installs use @latest (or the given range); verification uses the plain name, the way most
    // people type it. Both forms work offline afterwards.
    const s = { name, spec: key, verifySpec: range ? key : name, source, rank };
    seen.set(key, s);
    seeds.push(s);
  };
  let hi = null;
  if (opt.top > 0) {
    hi = await loadHighImpact();
    const popular = new Set(hi.downloads);
    const ranked = hi.dependents.filter(n => popular.has(n) && NAME_RE.test(n));
    ranked.slice(0, opt.top).forEach((n, i) => add(n, null, 'top', i + 1));
    log(`npm-high-impact ${hi.version}: ${hi.dependents.length} by dependents, ${hi.downloads.length} by downloads, ${ranked.length} in both; taking ${Math.min(opt.top, ranked.length)}`);
  }
  const extra = readExtra(opt.extra);
  for (const e of extra) add(e.name, e.range, 'extra', null);
  log(`seeds: ${seeds.length} (${extra.length} lines in ${opt.extra ? path.basename(opt.extra) : 'no extra file'})`);
  seeds.forEach((s, i) => { s.index = i; });
  return {
    seeds,
    source: {
      package: hi ? `npm-high-impact@${hi.version}` : null,
      list: 'top-dependent, keeping names also in top-download, in dependents order',
      top: opt.top,
      extraFile: opt.extra ? path.relative(REPO, opt.extra).split(path.sep).join('/') : null,
      extraLines: extra.length,
    },
  };
}

// ---------------------------------------------------------------------------------------------
// Install phase

// The seed a failed install's error points at, if npm names one: a missing package (E404/E403),
// a non-optional package for another OS (EBADPLATFORM) or a dist-tag with no version (ETARGET).
// The caller confirms it by installing that seed alone.
function blamedSeed(r, list) {
  const text = r.stderr;
  const names = new Set();
  const specs = new Set();
  for (const m of text.matchAll(/\b40[34] [^\n]*?GET https:\/\/registry\.npmjs\.org\/(\S+)/g)) {
    names.add(decodeURIComponent(m[1].split('/-/')[0]));
  }
  for (const m of text.matchAll(/requested resource '(.+?)' could not be found/g)) specs.add(m[1]);
  for (const m of text.matchAll(/Unsupported platform for ((?:@[^/\s]+\/)?[^@\s]+)@/g)) names.add(m[1]);
  for (const m of text.matchAll(/No matching version found for (\S+?)\.?(?:\s|$)/g)) specs.add(m[1]);
  return list.find(s => names.has(s.name) || specs.has(s.spec)) ?? null;
}

async function installPhase(opt, seeds, abort) {
  const t0 = Date.now();
  const stats = { installs: 0, netFailBatches: new Set(), ok: 0, failed: 0 };
  const failures = new Map(); // seed index -> [{ platform, code, detail }]
  const batches = chunk(seeds, opt.batch).map((list, i) => ({ id: `b${pad(i + 1)}`, list }));
  let done = 0;

  const once = async (list, plat, label) => {
    const dir = path.join(opt.work, 'p', label);
    let r;
    for (let attempt = 1; attempt <= 3; attempt++) {
      await makeProject(dir);
      r = await npmRun([
        'install', ...list.map(s => s.spec),
        '--ignore-scripts', '--legacy-peer-deps', '--prefer-offline',
        '--no-save', '--package-lock=false', '--bin-links=false',
        ...plat.flags,
      ], dir, TIMEOUT.install);
      stats.installs++;
      await rmrf(dir);
      if (r.ok || r.err === 'ENOSPC' || !TRANSIENT.has(r.err)) break;
      warn(`${label} ${plat.id}: ${r.err} (${r.detail}), retry ${attempt}`);
      await sleep(5000 * attempt);
    }
    if (r.err === 'ENOSPC') throw new Fatal('disk full while installing');
    // One bad package fails many times in a row while its batch is split, so count batches:
    // network failures in several different batches with no success in between are systemic.
    if (r.ok) stats.netFailBatches.clear();
    else if (NETWORK.has(r.err)) {
      stats.netFailBatches.add(label.match(/^b\d+/)[0]);
      if (stats.netFailBatches.size >= MAX_NETWORK_FAILED_BATCHES) {
        throw new Fatal(`network errors in ${stats.netFailBatches.size} batches in a row; last: ${r.err} ${r.detail}`);
      }
    }
    return r;
  };

  const fail = (s, plat, r) => {
    stats.failed++;
    warn(`${s.spec} failed on ${plat.id}: ${r.err} ${r.detail}`);
    if (!failures.has(s.index)) failures.set(s.index, []);
    failures.get(s.index).push({ platform: plat.id, code: r.err, detail: r.detail });
    if (stats.failed >= 20 && stats.ok === 0) {
      throw new Fatal(`the first ${stats.failed} packages all failed (last: ${r.err} ${r.detail}); not a package problem`);
    }
  };

  // Install a list. On failure, drop the seed npm names in its error if it fails alone too, else
  // split the list in two and recurse until single packages fail alone.
  const run = async (list, plat, label) => {
    const r = await once(list, plat, label);
    if (r.ok) {
      stats.ok += list.length;
      return r;
    }
    if (list.length === 1) {
      fail(list[0], plat, r);
      return r;
    }
    const named = blamedSeed(r, list);
    if (named) {
      const alone = await once([named], plat, `${label}x`);
      if (!alone.ok) {
        fail(named, plat, alone);
        log(`${label} ${plat.id}: dropped ${named.spec}, retrying the other ${list.length - 1}`);
        await run(list.filter(s => s !== named), plat, `${label}r`);
        return r;
      }
    }
    log(`${label} ${plat.id}: ${list.length} packages failed together (${r.err}); splitting`);
    const mid = Math.ceil(list.length / 2);
    await run(list.slice(0, mid), plat, `${label}a`);
    await run(list.slice(mid), plat, `${label}b`);
    return r;
  };

  log(`install: ${seeds.length} seeds in ${batches.length} batches of up to ${opt.batch}, ${opt.jobs} at a time, for ${opt.platforms.map(p => p.id).join(' and ')}`);
  await pool(batches, opt.jobs, async b => {
    // Platforms one after the other for the same batch, so the second reuses what the first fetched.
    for (const plat of opt.platforms) {
      const t = Date.now();
      await run(b.list, plat, b.id);
      done++;
      const lost = b.list.filter(s => failures.get(s.index)?.some(f => f.platform === plat.id)).length;
      log(`install ${done}/${batches.length * opt.platforms.length} ${b.id} ${plat.id}: ${lost ? `${lost} of ${b.list.length} failed` : 'ok'} in ${secs(Date.now() - t)} s`);
    }
  }, abort);

  const failed = [...failures].map(([i, f]) => ({ spec: seeds[i].spec, stage: 'install', failures: f }));
  log(`install done in ${secs(Date.now() - t0)} s: ${stats.installs} npm installs, ${failed.length} seeds failed somewhere`);
  return { ms: Date.now() - t0, failed, failedIndex: [...failures.keys()], npmInstalls: stats.installs };
}

// ---------------------------------------------------------------------------------------------
// Verify phase

function contentPath(cache, integrity) {
  const [algo, b64] = integrity.split(/-(.*)/s);
  const hex = Buffer.from(b64.replace(/\?.*$/, ''), 'base64').toString('hex');
  return path.join(cache, '_cacache', 'content-v2', algo, hex.slice(0, 2), hex.slice(2, 4), hex.slice(4));
}

const contentSeen = new Map();
function hasContent(cache, integrity) {
  if (contentSeen.get(integrity)) return true;
  const ok = integrity.split(/\s+/).filter(Boolean).some(part => fs.existsSync(contentPath(cache, part)));
  if (ok) contentSeen.set(integrity, true);
  return ok;
}

// Same rule as npm-install-checks: no negated match, and a match if any plain values are listed.
function listOk(value, list) {
  if (!list) return true;
  list = [].concat(list);
  if (list.length === 1 && list[0] === 'any') return true;
  let negated = 0;
  let match = false;
  for (const entry of list) {
    if (entry.startsWith('!')) {
      negated++;
      if (entry.slice(1) === value) return false;
    } else if (entry === value) {
      match = true;
    }
  }
  return match || negated === list.length;
}
const platformOk = (e, p) => listOk(p.os, e.os) && listOk(p.cpu, e.cpu) && (!e.libc || (!!p.libc && listOk(p.libc, e.libc)));

// Node's lookup order: the package's own node_modules, then each parent's, then the root's.
function lockHas(packages, fromKey, dep) {
  let base = fromKey;
  for (;;) {
    if (packages[base ? `${base}/node_modules/${dep}` : `node_modules/${dep}`]) return true;
    if (!base) return false;
    const i = base.lastIndexOf('/node_modules/');
    base = i < 0 ? '' : base.slice(0, i);
  }
}

const lockName = (key, e) => e.name || key.slice(key.lastIndexOf('node_modules/') + 'node_modules/'.length);

// What a single install of this tree needs from the cache on each target platform.
function inspectLock(lock, opt) {
  const packages = lock.packages || {};
  const out = { size: 0, gaps: [], scripts: [], firstUse: [], nonRegistry: [], missingOptional: [] };
  for (const [key, e] of Object.entries(packages)) {
    if (!key || e.link || e.inBundle) continue;
    const name = lockName(key, e);
    for (const dep of Object.keys(e.optionalDependencies || {})) {
      if (!lockHas(packages, key, dep)) out.missingOptional.push(dep);
    }
    if (!opt.platforms.some(p => platformOk(e, p))) continue;
    out.size++;
    const id = `${name}@${e.version}`;
    if (FIRST_USE[name]) out.firstUse.push(id);
    if (!e.resolved?.startsWith(REGISTRY)) {
      out.nonRegistry.push({ id, resolved: e.resolved || null });
      continue;
    }
    if (!e.integrity) {
      out.nonRegistry.push({ id, resolved: e.resolved });
      continue;
    }
    if (e.hasInstallScript) out.scripts.push({ id, name, version: e.version, integrity: e.integrity, optional: !!e.optional });
    if (!hasContent(opt.cache, e.integrity)) out.gaps.push({ id, name, version: e.version });
  }
  return out;
}

async function resolveAlone(opt, seed, plat, strict) {
  const dir = path.join(opt.work, 'v', pad(seed.index, 4));
  let spec = seed.verifySpec;
  let offline = true;
  let legacyPeers = false;
  let r;
  for (let attempt = 0; attempt < 6; attempt++) {
    await makeProject(dir);
    // No --no-save here: with it npm writes no package-lock.json, and the lockfile is the point.
    r = await npmRun([
      'install', spec, '--package-lock-only', '--ignore-scripts', ...plat.flags,
      offline ? '--offline' : '--prefer-offline', ...(legacyPeers ? ['--legacy-peer-deps'] : []),
    ], dir, TIMEOUT.verify);
    if (r.ok) {
      const lock = JSON.parse(await fsp.readFile(path.join(dir, 'package-lock.json'), 'utf8'));
      const info = inspectLock(lock, opt);
      // An optional dependency whose packument is not cached is dropped silently offline; going
      // online once fetches the packument so its native package is checked too.
      if (info.missingOptional.length && offline && !strict) {
        offline = false;
        continue;
      }
      await rmrf(dir);
      return { ok: true, spec, offline, legacyPeers, info };
    }
    if (r.err === 'ENOTCACHED' && offline && !strict) { offline = false; continue; }
    if (r.err === 'ERESOLVE' && !legacyPeers) { legacyPeers = true; continue; }
    // A package with only prerelease versions cannot be installed by bare name, online or not.
    if (r.err === 'ETARGET' && spec === seed.name) { spec = `${seed.name}@latest`; continue; }
    if (TRANSIENT.has(r.err) && !offline && attempt < 4) { await sleep(5000); continue; }
    break;
  }
  await rmrf(dir);
  return { ok: false, spec, offline, legacyPeers, err: r.err, detail: r.detail };
}

async function cacheAdd(opt, ids, abort, jobs = opt.jobs) {
  const failed = [];
  const dir = path.join(opt.work, 'add');
  await fsp.mkdir(dir, { recursive: true });
  const add = async list => {
    const r = await npmRun(['cache', 'add', ...list, '--prefer-offline'], dir, TIMEOUT.cacheAdd);
    if (r.ok) return;
    if (r.err === 'ENOSPC') throw new Fatal('disk full while adding to the cache');
    if (list.length === 1) {
      failed.push({ id: list[0], code: r.err, detail: r.detail });
      return;
    }
    const mid = Math.ceil(list.length / 2);
    await add(list.slice(0, mid));
    await add(list.slice(mid));
  };
  await pool(chunk(ids, 50), jobs, add, abort);
  return failed;
}

async function verifyPhase(opt, seeds, skip, abort) {
  const t0 = Date.now();
  const todo = seeds.filter(s => !skip.has(s.index));
  const results = new Map();
  const firstPlatform = opt.platforms[0];

  log(`verify: resolving ${todo.length} seeds one at a time against the cache, ${opt.jobs} at a time`);
  const pass = async (list, strict, label, jobs) => {
    let done = 0;
    await pool(list, jobs, async s => {
      const r = await resolveAlone(opt, s, firstPlatform, strict);
      results.set(s.index, r);
      done++;
      if (done % 50 === 0 || done === list.length) log(`${label} ${done}/${list.length}`);
    }, abort);
  };
  await pass(todo, false, 'verify', opt.jobs);
  // The non-strict result per seed; its missing optional packages are the ones that are missing
  // online too (never published), as opposed to ones only missing from the cache.
  const online = new Map(results);
  const neededOnline = [...online.values()].filter(r => r.ok && !r.offline).length;

  // Everything single installs need that the batches did not bring in.
  const addGaps = async (list, jobs) => {
    const ids = new Set();
    for (const s of list) {
      const r = results.get(s.index);
      if (r.ok) for (const g of r.info.gaps) ids.add(g.id);
    }
    if (!ids.size) return 0;
    log(`verify: ${ids.size} tarballs missing for single installs; adding them`);
    const failedAdds = await cacheAdd(opt, [...ids], abort, jobs);
    for (const f of failedAdds) warn(`cache add ${f.id} failed: ${f.code} ${f.detail}`);
    addFailed.push(...failedAdds);
    return ids.size - failedAdds.length;
  };
  const addFailed = [];
  let added = await addGaps(todo, opt.jobs);

  const problem = s => {
    const r = results.get(s.index);
    if (!r.ok) return { code: r.err, detail: r.detail };
    if (r.info.gaps.length) return { code: 'ENOTCACHED', detail: `missing ${r.info.gaps.slice(0, 5).map(g => g.id).join(', ')}` };
    // Optional dependencies that resolve online but not offline would be skipped without a word.
    const known = new Set(online.get(s.index).info?.missingOptional ?? []);
    const lost = [...new Set(r.info.missingOptional)].filter(d => !known.has(d));
    if (lost.length) return { code: 'ENOTCACHED', detail: `optional packages dropped offline: ${lost.slice(0, 5).join(', ')}` };
    return null;
  };

  // Second pass, strictly offline, for every seed that needed anything.
  const again = todo.filter(s => {
    const r = online.get(s.index);
    return r.ok && (!r.offline || r.info.gaps.length);
  });
  if (again.length) {
    log(`verify: checking ${again.length} seeds again, offline`);
    await pass(again, true, 'recheck', opt.jobs);
  }

  // cacache rewrites an index bucket on every read and drops lines it catches half-written, so
  // npm processes writing the same keys at once can lose a packument entry. Repair what is still
  // missing one seed at a time, with nothing else running, then check again.
  for (let round = 1; round <= 2; round++) {
    const list = todo.filter(s => problem(s)?.code === 'ENOTCACHED');
    if (!list.length) break;
    log(`verify: repairing ${list.length} seeds one at a time (round ${round})`);
    await pass(list, false, 'repair', 1);
    for (const s of list) online.set(s.index, results.get(s.index));
    added += await addGaps(list, 1);
    await pass(list, true, 'recheck', 1);
  }

  const failed = [];
  const legacyPeers = [];
  const tagOnly = [];
  for (const s of todo) {
    const p = problem(s);
    const r = results.get(s.index);
    if (p) failed.push({ spec: r.spec, stage: 'verify', ...p });
    if (r.ok && r.legacyPeers) legacyPeers.push(r.spec);
    if (r.ok && r.spec !== s.verifySpec) tagOnly.push(r.spec);
  }
  for (const f of failed) warn(`${f.spec} does not install offline on its own: ${f.code} ${f.detail}`);
  log(`verify done in ${secs(Date.now() - t0)} s: ${todo.length - failed.length}/${todo.length} seeds install offline, ${added} tarballs added`);

  return {
    ms: Date.now() - t0,
    checked: todo.length,
    failed,
    tarballsAdded: added,
    addFailed,
    neededOnlinePackuments: neededOnline,
    legacyPeers,
    tagOnly,
    // Compact per-seed data for the report phase.
    trees: todo.filter(s => results.get(s.index).ok).map(s => {
      const r = results.get(s.index);
      return { seed: r.spec, size: r.info.size, scripts: r.info.scripts, firstUse: r.info.firstUse, nonRegistry: r.info.nonRegistry };
    }),
  };
}

// ---------------------------------------------------------------------------------------------
// Install scripts: which packages still need network when installed

// Checked by hand against the package sources (versions in the cache as of November 2026).
// Keyed by package name. at: 'install' means `npm install` fails or leaves the package broken
// offline; 'first use' means the install works and the package downloads something later.
const KNOWN_NETWORK = {
  puppeteer: { at: 'install', why: 'postinstall downloads Chrome for Testing', workaround: 'set PUPPETEER_SKIP_DOWNLOAD=true and pass executablePath for an installed browser, or use puppeteer-core' },
  cypress: { at: 'install', why: 'postinstall downloads the Cypress app', workaround: 'set CYPRESS_INSTALL_BINARY=0; Cypress cannot run without the app' },
  '@prisma/engines': { at: 'install', why: 'postinstall downloads the Prisma engine binaries' },
  'playwright-chromium': { at: 'install', why: 'postinstall downloads Chromium', workaround: 'set PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1' },
  'playwright-firefox': { at: 'install', why: 'postinstall downloads Firefox', workaround: 'set PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1' },
  'playwright-webkit': { at: 'install', why: 'postinstall downloads WebKit', workaround: 'set PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1' },
  chromedriver: { at: 'install', why: 'install script downloads ChromeDriver', workaround: 'set CHROMEDRIVER_SKIP_DOWNLOAD=true' },
  'ffmpeg-static': { at: 'install', why: 'install script downloads an ffmpeg binary' },
  'node-sass': { at: 'install', why: 'install script downloads the LibSass binding, else compiles it', workaround: 'use sass (Dart Sass) instead' },
  'phantomjs-prebuilt': { at: 'install', why: 'install script downloads PhantomJS' },
  'pngquant-bin': { at: 'install', why: 'postinstall downloads pngquant from GitHub, else builds it with make' },
  snyk: { at: 'install', why: 'postinstall downloads the Snyk CLI binary' },
  node: { at: 'install', why: 'preinstall installs the node-<os>-<arch> package, which is not in the cache' },
  edgedriver: { at: 'first use', why: 'postinstall tries to download msedgedriver and carries on without it' },
  geckodriver: { at: 'first use', why: 'postinstall tries to download geckodriver and carries on without it' },
};
// Packages without install scripts that download something the first time they run.
const FIRST_USE = {
  playwright: { at: 'first use', why: 'browsers come from `npx playwright install`', workaround: "use the installed Edge with channel: 'msedge' on Windows" },
  'playwright-core': { at: 'first use', why: 'browsers come from `npx playwright install`', workaround: "use the installed Edge with channel: 'msedge' on Windows" },
  electron: { at: 'first use', why: 'downloads the Electron binary the first time it runs' },
};

// Install scripts read and found to work offline. Keyed by package name, value says why.
const KNOWN_OK = {
  esbuild: 'checks the optional platform package is present',
  workerd: 'checks the optional platform package is present',
  '@swc/core': 'checks the native binding from the optional platform package; fetches @swc/wasm only if that fails',
  '@parcel/watcher': 'compiles only when npm_config_build_from_source=true; otherwise uses the optional platform package',
  pnpm: 'links the binary from its optional platform package',
  'core-js': 'prints a message',
  'core-js-pure': 'prints a message',
  'es5-ext': 'prints a message',
  'aws-sdk': 'prints a notice',
  'svelte-preprocess': 'prints a message',
  protobufjs: 'checks the version ranges its dependents use',
  '@apollo/protobufjs': 'checks the version ranges its dependents use',
  '@firebase/util': 'fetches web config only when FIREBASE_WEBAPP_CONFIG is set, and always exits 0',
  '@scarf/scarf': 'sends install analytics when it can, and always exits 0',
  nx: 'updates the Nx Cloud client only in workspaces that use it, and always exits 0',
  ssh2: 'tries to compile an optional crypto binding and carries on without it',
  'bigint-buffer': 'tries to compile a binding and falls back to plain JavaScript',
  yorkie: 'installs git hooks',
  'pre-commit': 'installs a git hook',
  'simple-git-hooks': 'installs git hooks',
  'electron-winstaller': 'copies its bundled 7-Zip',
  'uglifyjs-webpack-plugin': 'builds only when dist is missing, and the package ships dist',
  'vue-demi': 'switches between its Vue 2 and Vue 3 builds',
  gatsby: 'refreshes its API list from unpkg when it can, else keeps the bundled copy',
  'gatsby-cli': 'prints a welcome message for global installs',
  yarn: 'tidies up an old global install and ignores errors',
};

function readCachedPackage(cache, integrity) {
  const file = integrity.split(/\s+/).map(p => contentPath(cache, p)).find(p => fs.existsSync(p));
  if (!file) return null;
  const files = new Set();
  let pkg = null;
  try {
    for (const e of tarEntries(gunzip(fs.readFileSync(file)))) {
      files.add(e.path);
      if (e.path === 'package.json' && !pkg) pkg = JSON.parse(e.data.toString('utf8'));
    }
  } catch {
    return null;
  }
  return pkg ? { pkg, files } : null;
}

const platformTag = p => `${p.os === 'linux' && p.libc === 'musl' ? 'linuxmusl' : p.os}-${p.cpu}`;
const hasPrebuild = (files, p) => [...files].some(f => f.startsWith('prebuilds/') && f.includes(platformTag(p)));
const hasOptionalFor = (pkg, p) => Object.keys(pkg.optionalDependencies || {}).some(d => d.includes(`${p.os}-${p.cpu}`));

function classify(name, info, platforms) {
  const s = info?.pkg?.scripts || {};
  const lines = ['preinstall', 'install', 'postinstall'].filter(k => s[k]).map(k => `${k}: ${s[k]}`);
  if (info?.files.has('binding.gyp') && !s.install && !s.preinstall && info.pkg.gypfile !== false) {
    lines.push('install: node-gyp rebuild (npm adds this for binding.gyp)');
  }
  const script = lines.join('\n');
  if (KNOWN_NETWORK[name]) return { kind: 'network', script, ...KNOWN_NETWORK[name] };
  if (KNOWN_OK[name]) return { kind: 'ok', script, why: KNOWN_OK[name] };
  if (!info) return { kind: 'unreviewed', script: '(tarball not readable)' };
  const compile = 'compiling needs Python, a C/C++ compiler and Node headers, which node-gyp downloads';
  const network = (why) => ({ kind: 'network', at: 'install', script, why });
  if (name === 'deasync') {
    // Ships bin/<os>-<cpu>-node-<major>/ and compiles when the running Node has none.
    const major = process.versions.node.split('.')[0];
    const missing = platforms.filter(p => ![...info.files].some(f => f.startsWith(`bin/${p.os}-${p.cpu}-node-${major}/`)));
    if (!missing.length) return { kind: 'ok', script, why: `prebuilt binaries for Node ${major} on every target are in the package` };
    return network(`no prebuilt binary for Node ${major} on ${missing.map(p => p.id).join(', ')}, so it compiles; ${compile}`);
  }
  if (/prebuild-install|node-pre-gyp|install-from-cache|cmake-js/.test(script)) {
    return network(`downloads a prebuilt binary, else compiles it; ${compile}`);
  }
  if (/node-gyp-build-optional-packages/.test(script)) {
    const missing = platforms.filter(p => !hasOptionalFor(info.pkg, p)).map(p => p.id);
    if (!missing.length) return { kind: 'ok', script, why: 'loads the binary from its optional platform package' };
    return network(`no optional platform package for ${missing.join(', ')}, so it compiles; ${compile}`);
  }
  if (/node-gyp-build/.test(script)) {
    const missing = platforms.filter(p => !hasPrebuild(info.files, p)).map(p => p.id);
    if (!missing.length) return { kind: 'ok', script, why: 'prebuilt binaries for every target are in the package' };
    return network(`no prebuilt binary for ${missing.join(', ')}, so it compiles; ${compile}`);
  }
  if (/node-gyp\s+(rebuild|configure|build)/.test(script)) return network(`compiles a native addon; ${compile}`);
  if (!script) return { kind: 'ok', script, why: 'no install script in the published package' };
  return { kind: 'unreviewed', script };
}

function reportScripts(opt, verify) {
  const byId = new Map(); // name@version -> { name, version, integrity, required, seeds }
  const firstUse = new Map(); // name -> { versions, seeds }
  const nonRegistry = new Map();
  for (const t of verify.trees) {
    for (const sc of t.scripts) {
      if (!byId.has(sc.id)) byId.set(sc.id, { ...sc, required: false, seeds: new Set() });
      const item = byId.get(sc.id);
      item.required ||= !sc.optional;
      item.seeds.add(t.seed);
    }
    for (const id of t.firstUse) {
      const name = id.slice(0, id.lastIndexOf('@'));
      if (!firstUse.has(name)) firstUse.set(name, { versions: new Set(), seeds: new Set() });
      firstUse.get(name).versions.add(id.slice(name.length + 1));
      firstUse.get(name).seeds.add(t.seed);
    }
    for (const n of t.nonRegistry) {
      if (!nonRegistry.has(n.id)) nonRegistry.set(n.id, { ...n, seeds: new Set() });
      nonRegistry.get(n.id).seeds.add(t.seed);
    }
  }

  // Group by package name and verdict; versions of one package nearly always share a verdict.
  const groups = new Map();
  for (const item of byId.values()) {
    const c = classify(item.name, readCachedPackage(opt.cache, item.integrity), opt.platforms);
    const key = `${item.name}\n${c.kind}\n${c.why ?? ''}`;
    if (!groups.has(key)) groups.set(key, { name: item.name, c, required: false, versions: new Set(), seeds: new Set(), scripts: new Set() });
    const g = groups.get(key);
    g.required ||= item.required;
    g.versions.add(item.version);
    for (const s of item.seeds) g.seeds.add(s);
    if (c.script) g.scripts.add(c.script);
  }
  const neededBy = seeds => {
    const list = [...seeds].sort();
    return { neededBy: list.slice(0, 25), neededByCount: list.length };
  };
  const entry = g => ({
    package: g.name,
    versions: [...g.versions].sort(),
    at: g.c.at,
    why: g.c.why,
    ...(g.c.workaround ? { workaround: g.c.workaround } : {}),
    ...neededBy(g.seeds),
  });
  const all = [...groups.values()];
  const byCount = (a, b) => b.neededByCount - a.neededByCount || a.package.localeCompare(b.package);
  const network = all.filter(g => g.c.kind === 'network');

  const networkAtInstall = network.filter(g => g.required && g.c.at === 'install').map(entry);
  for (const n of nonRegistry.values()) {
    networkAtInstall.push({
      package: n.id, versions: [], at: 'install',
      why: `fetched from ${n.resolved ?? 'outside the registry'}, which npm cannot take from the cache`,
      ...neededBy(n.seeds),
    });
  }
  const networkAtFirstUse = [
    ...network.filter(g => g.required && g.c.at === 'first use').map(entry),
    ...[...firstUse].map(([name, f]) => ({ package: name, versions: [...f.versions].sort(), ...FIRST_USE[name], ...neededBy(f.seeds) })),
  ];
  return {
    networkAtInstall: networkAtInstall.sort(byCount),
    networkAtFirstUse: networkAtFirstUse.sort(byCount),
    installScripts: {
      packages: byId.size,
      // Needs network or a compiler, but only ever an optional dependency here, so npm carries
      // on without it and the install still succeeds.
      optionalSkippedOffline: network.filter(g => !g.required).map(entry).sort(byCount),
      offlineOk: all.filter(g => g.c.kind === 'ok').map(g => ({ package: g.name, why: g.c.why })).sort((a, b) => a.package.localeCompare(b.package)),
      unreviewed: all.filter(g => g.c.kind === 'unreviewed').map(g => ({ ...entry(g), scripts: [...g.scripts] })).sort(byCount),
    },
  };
}

// ---------------------------------------------------------------------------------------------
// Cache statistics and clean-up

function* walkFiles(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) yield* walkFiles(p);
    else yield p;
  }
}

async function finishCache(opt) {
  // npm leaves logs, npx folders and temp files next to _cacache; only _cacache ships.
  for (const name of fs.readdirSync(opt.cache)) {
    if (name !== '_cacache') await rmrf(path.join(opt.cache, name));
  }
  await rmrf(path.join(opt.cache, '_cacache', 'tmp'));

  let bytes = 0;
  let files = 0;
  for (const f of walkFiles(opt.cache)) {
    files++;
    bytes += fs.statSync(f).size;
  }

  // Read the index: one line per stored response, "<sha1>\t<json>".
  const stats = { packuments: 0, abbreviatedPackuments: 0, packumentBytes: 0, tarballs: 0, tarballBytes: 0, packages: 0, missingContent: 0 };
  const names = new Set();
  const counted = new Set();
  const indexDir = path.join(opt.cache, '_cacache', 'index-v5');
  for (const f of walkFiles(indexDir)) {
    for (const line of fs.readFileSync(f, 'utf8').split('\n')) {
      const tab = line.indexOf('\t');
      if (tab < 0) continue;
      let e;
      try { e = JSON.parse(line.slice(tab + 1)); } catch { continue; }
      if (!e.integrity) continue;
      if (counted.has(e.integrity)) continue;
      counted.add(e.integrity);
      if (!hasContent(opt.cache, e.integrity)) { stats.missingContent++; continue; }
      const url = e.key.replace(/^make-fetch-happen:request-cache:/, '');
      if (/\/-\/[^/]+\.tgz$/.test(url)) {
        stats.tarballs++;
        stats.tarballBytes += e.size || 0;
      } else {
        stats.packuments++;
        stats.packumentBytes += e.size || 0;
        if (/vnd\.npm\.install-v1/.test(e.metadata?.reqHeaders?.accept || '')) stats.abbreviatedPackuments++;
        names.add(url);
      }
    }
  }
  stats.packages = names.size;
  if (stats.missingContent) throw new Fatal(`${stats.missingContent} cache index entries point at missing content`);
  return { bytes, files, ...stats };
}

// ---------------------------------------------------------------------------------------------
// Main

async function main() {
  const opt = parseOptions();
  const abort = { error: null };
  const statePath = path.join(opt.work, 'state.json');
  let state;

  if (opt.only) {
    if (!fs.existsSync(statePath)) throw new Fatal(`no ${statePath}; run a full build with --keep-work first`);
    state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    setupNpm(opt.work, opt.cache);
    if (opt.only === 'verify') {
      const seeds = state.seeds.map((s, index) => ({ ...s, index, name: s.spec.slice(0, s.spec.lastIndexOf('@')) }));
      state.verify = await verifyPhase(opt, seeds, new Set(state.install.failedIndex), abort);
      await fsp.writeFile(statePath, JSON.stringify(state));
    }
  } else {
    if (fs.existsSync(opt.cache) && fs.readdirSync(opt.cache).length) {
      if (!opt.clean) throw new Fatal(`cache folder is not empty: ${opt.cache} (pass --clean to delete it first)`);
      log(`deleting ${opt.cache}`);
      await rmrf(opt.cache);
    }
    await rmrf(opt.work);
    await fsp.mkdir(opt.cache, { recursive: true });
    setupNpm(opt.work, opt.cache);
    log(`npm ${NPM.version} at ${NPM.cli}, node ${process.version}`);
    log(`cache ${opt.cache}`);
    const v = await npmRun(['--version'], opt.work, TIMEOUT.version);
    if (!v.ok || v.stdout.trim() !== NPM.version) throw new Fatal(`npm does not run: ${v.err} ${v.detail}`);

    const { seeds, source } = await loadSeeds(opt);
    if (!seeds.length) throw new Fatal('no seeds');
    const install = await installPhase(opt, seeds, abort);
    // Seeds that failed to install on any platform are already recorded as failed.
    const verify = await verifyPhase(opt, seeds, new Set(install.failedIndex), abort);
    state = { seeds: seeds.map(s => ({ spec: s.spec, verifySpec: s.verifySpec, source: s.source, rank: s.rank })), source, install, verify, platforms: opt.platforms.map(p => p.id) };
    await fsp.writeFile(statePath, JSON.stringify(state));
  }

  log('report: reading install scripts from the cached tarballs');
  const scripts = reportScripts(opt, state.verify);
  const cache = await finishCache(opt);

  const failedSpecs = new Set([...state.install.failed.map(f => f.spec), ...state.verify.failed.map(f => f.spec)]);
  const seedsFailed = state.seeds.filter(s => failedSpecs.has(s.spec) || failedSpecs.has(s.verifySpec));
  const facts = {
    asset: 'npm-cache',
    date: new Date().toISOString(),
    npm: NPM.version,
    node: process.version,
    registry: REGISTRY,
    platforms: state.platforms,
    seedSource: state.source,
    seeds: {
      requested: state.seeds.length,
      ok: state.seeds.length - seedsFailed.length,
      failed: [...state.install.failed, ...state.verify.failed],
      // These only have prerelease versions, so a bare `npm install <name>` fails (online too);
      // they install with the spec given here.
      installOnlyWithTag: state.verify.tagOnly ?? [],
      // A single install of these hits a peer dependency conflict (ERESOLVE) and needs
      // --legacy-peer-deps, online or not.
      needLegacyPeerDeps: state.verify.legacyPeers,
    },
    verify: {
      checked: state.verify.checked,
      tarballsAdded: state.verify.tarballsAdded,
      seedsThatFetchedPackuments: state.verify.neededOnlinePackuments,
      cacheAddFailed: state.verify.addFailed,
    },
    cache: {
      bytes: cache.bytes,
      mib: Math.round(cache.bytes / 1048576),
      files: cache.files,
      packages: cache.packages,
      tarballs: cache.tarballs,
      tarballBytes: cache.tarballBytes,
      packuments: cache.packuments,
      abbreviatedPackuments: cache.abbreviatedPackuments,
      packumentBytes: cache.packumentBytes,
    },
    networkAtInstall: scripts.networkAtInstall,
    networkAtFirstUse: scripts.networkAtFirstUse,
    installScripts: scripts.installScripts,
    seconds: {
      install: secs(state.install.ms),
      verify: secs(state.verify.ms),
      total: opt.only ? null : secs(Date.now() - started),
    },
  };
  await fsp.mkdir(path.dirname(opt.facts), { recursive: true });
  await fsp.writeFile(opt.facts, `${JSON.stringify(facts, null, 2)}\n`);
  if (!opt.keepWork) await rmrf(opt.work);

  log(`cache: ${facts.cache.mib} MiB in ${cache.files} files; ${cache.packages} packages, ${cache.tarballs} tarballs`);
  log(`seeds: ${facts.seeds.ok}/${facts.seeds.requested} install offline; ${facts.seeds.failed.length} failures recorded`);
  log(`network at install: ${scripts.networkAtInstall.map(n => n.package).join(', ') || 'none'}`);
  log(`network at first use: ${scripts.networkAtFirstUse.map(n => n.package).join(', ') || 'none'}`);
  log(`optional, skipped offline: ${scripts.installScripts.optionalSkippedOffline.map(n => n.package).join(', ') || 'none'}`);
  if (scripts.installScripts.unreviewed.length) {
    warn(`install scripts not yet classified: ${scripts.installScripts.unreviewed.map(u => u.package).join(', ')}`);
  }
  log(`facts ${opt.facts}`);

  if (seedsFailed.length > Math.max(5, state.seeds.length * MAX_FAILED_SHARE)) {
    throw new Fatal(`${seedsFailed.length} of ${state.seeds.length} seeds failed; that is more than package trouble`);
  }
}

main().catch(err => {
  const msg = err instanceof Fatal ? err.message : err.stack || String(err);
  console.error(`[${clock()}] FAILED: ${msg}`);
  if (process.env.GITHUB_ACTIONS) console.log(`::error::${msg.split('\n')[0]}`);
  process.exit(1);
});
