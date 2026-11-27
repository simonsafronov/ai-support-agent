#!/usr/bin/env python3
"""Offline review tools and an opt-in Linux runner for the pinned synthetic retail suite.

Run with the release's trusted Python interpreter (-IB). Only `run` contacts the
existing single-slot local model. Supply *closed, synthetic-only* SQLite fixtures
and a trusted release, not a deployment or an arbitrary Python plugin. Idle probes
are cooperative admission checks, not a reservation: coordinate the idle window
with the runtime owner. JSON hashes detect stale/mixed evidence, not malicious
rewriting of an entire bundle. Keep the result directory under reviewer control.
"""
import argparse
from contextlib import closing
import csv
import hashlib
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import sys
import tempfile
import time
import unicodedata
from urllib.parse import unquote, urlsplit

SCRIPT = Path(__file__).resolve()
DEFAULT_SUITE = SCRIPT.with_name('retail-synthetic-v1.json')
SUITE_HASH = '99ebb5f4046502951c61a5133f27b1b4928c6dd6c825e83e4137dcbc54db15fb'
DEFINITIONS_HASH = 'f8e6d3e447465795dd769057363cc9e873f087a0eaecc63e37e5ef04acf0f625'
VERSION = 'retail-matrix/1'
ENDPOINT = ('127.0.0.1', 18347)
MODEL = 'qwen3-4b-local'
UNMET = {
    'waiting_operator': 'No queued synthetic handoff fixture',
    'already_connected_operator': 'No connected operator fixture',
    'correction_repeat': 'No prior eight-seat quote fixture',
    'operator_learning': 'No reviewed cross-buyer teaching fixture',
    'actual_commercial_invoice': 'No prior demo invoice / authorized issuance fixture',
}
SETTINGS = dict(COMPACT_LOCAL='1', LLM_PROVIDER='openai', LLM_MODEL=MODEL,
                LLM_BASE_URL='http://127.0.0.1:18347/v1', LLM_API_KEY='',
                MAX_PARALLEL_TURNS='1', TURN_DEADLINE='90', PRODUCT_CARDS='0',
                FEED_URL='', WEBIM_API_URL='', WEBIM_TOKEN='', TG_TOKEN='',
                PRICE_IN_PER_1K='0', PRICE_OUT_PER_1K='0', OMP_NUM_THREADS='1',
                OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', TZ='UTC')


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def encoded(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def regular(path):
    need(not path.is_symlink() and path.is_file(), f'expected a regular file: {path.name}')
    return path


def definitions(path):
    data = regular(path).read_bytes()
    need(sha(data) == SUITE_HASH, 'unapproved suite hash; no raw case replay')
    return json.loads(data)


def put(path, value):
    # Publish complete, write-once records even when two reviewers race.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(encoded(value) + b'\n')
            f.flush()
            os.link(tmp, path)
        finally:
            tmp.unlink()


def read(path):
    return json.loads(regular(path).read_bytes())


def start_bundle(out, rows, conditions, release):
    out.mkdir(mode=0o700)  # never resume or overwrite a result
    (out/'evidence').mkdir()
    (out/'reviews').mkdir()
    put(out/'definitions.json', rows)
    manifest = dict(version=VERSION, suite_sha256=SUITE_HASH, release_sha256=release,
                    conditions=conditions, definitions_sha256=sha(encoded(rows)))
    put(out/'manifest.json', manifest)
    return manifest


def record(out, manifest, family, status, reason, snapshot=None):
    body = dict(manifest_sha256=sha(encoded(manifest)), family=family, status=status,
                reason=reason, snapshot=snapshot)
    put(out/'evidence'/f'{family}.json', dict(body=body, sha256=sha(encoded(body))))


def load_bundle(out):
    for directory in (out, out/'evidence', out/'reviews'):
        need(directory.is_dir() and directory.absolute() == directory.resolve(), 'expected local unlinked result directory')
    manifest, rows = read(out/'manifest.json'), read(out/'definitions.json')
    need(manifest['version'] == VERSION and manifest['suite_sha256'] == SUITE_HASH and
         sha(encoded(rows)) == manifest['definitions_sha256'] == DEFINITIONS_HASH,
         'unsupported or mismatched suite/manifest')
    selected = manifest['conditions']['selected']
    result = {}
    for row in rows:
        family = row['family']
        status, reason, digest, snapshot, expert = 'UNTESTED', 'not selected', '', {}, ''
        path = out/'evidence'/f'{family}.json'
        review = out/'reviews'/f'{family}.json'
        if family in selected:
            reason = 'not reached (stopped or incomplete run)'
        if path.exists():
            envelope = read(path)
            b, digest = envelope['body'], envelope['sha256']
            need(sha(encoded(b)) == digest and b['manifest_sha256'] == sha(encoded(manifest))
                 and b['family'] == family and family in selected, 'evidence hash/context mismatch')
            status, reason, snapshot = b['status'], b['reason'], b['snapshot'] or {}
            need(status in ('OBSERVED', 'SKIPPED', 'ERROR') and
                 (family not in UNMET or status == 'SKIPPED'), 'invalid observation status')
        if review.exists():
            v = read(review)
            need(status == 'OBSERVED' and v['evidence_sha256'] == digest and
                 v['status'] in ('PASS', 'FAIL', 'INCONCLUSIVE') and v['expert'].strip()
                 and v['reason'].strip(), 'invalid review or stale evidence hash')
            status, reason, expert = v['status'], v['reason'], v['expert']
        result[family] = dict(**row, status=status, reason=reason, expert=expert,
                              reply=snapshot.get('reply', ''), usage=snapshot.get('usage'),
                              evidence_sha256=digest)
    return manifest, result


def verdict(a):
    _, rows = load_bundle(a.result)
    r = rows[a.family]
    need(r['status'] == 'OBSERVED', 'only a previously unreviewed observation may be reviewed')
    need(r['evidence_sha256'] == a.evidence_sha256, 'reviewed evidence hash mismatch')
    need(a.expert.strip() and a.reason.strip(), 'expert and reason must be nonempty')
    put(a.result/'reviews'/f'{a.family}.json', dict(status=a.status, expert=a.expert.strip(),
        reason=a.reason.strip(), evidence_sha256=a.evidence_sha256))


def cell(value):
    text = value if isinstance(value, str) else encoded(value).decode() if value is not None else ''
    # Check normalized text, but preserve the original response for review.
    probe = unicodedata.normalize('NFKC', text)
    for char in probe:
        if not (char.isspace() or unicodedata.category(char) in ('Cc', 'Cf')):
            break
    else:
        char = ''
    return "'" + text if text.startswith(('\t', '\r', '\n')) or char in ('=', '+', '-', '@') else text


def export(a):
    manifest, rows = load_bundle(a.result)
    with a.out.open('x', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        fields = list(next(iter(rows.values())))
        writer.writerow(['release_sha256', *fields])
        for row in rows.values():
            writer.writerow([cell(manifest['release_sha256']), *(cell(row[k]) for k in fields)])


def compare(a):
    left, lrows = load_bundle(a.left)
    right, rrows = load_bundle(a.right)
    different = sorted(k for k in left.keys() | right.keys() if k != 'release_sha256' and left.get(k) != right.get(k))
    need(not different, 'unmatched conditions: ' + ', '.join(different))
    print(json.dumps(dict(left_release=left['release_sha256'], right_release=right['release_sha256'], rows={
        k: dict(left=v['status'], right=rrows[k]['status'], left_reason=v['reason'], right_reason=rrows[k]['reason'])
        for k, v in lrows.items()}), ensure_ascii=False, indent=2))


def get_local(path):
    # No proxies, redirects, auth, hostname lookup or endpoint arguments.
    with closing(http.client.HTTPConnection(*ENDPOINT, timeout=5)) as c:
        c.request('GET', path)
        r = c.getresponse()
        need(r.status == 200, f'local runtime {path}: HTTP {r.status}')
        data = r.read(4_000_001)
        need(len(data) <= 4_000_000, 'oversize runtime metadata')
        return json.loads(data)


def slot_marker(slots):
    need(isinstance(slots, list) and len(slots) == 1 and isinstance(slots[0], dict), 'expected one runtime slot')
    slot = slots[0]
    need(slot.get('is_processing') is False, 'slot busy or idle state unknown')
    need(all(type(slot.get(k)) is int for k in ('id', 'id_task')), 'missing slot marker')
    return slot['id'], slot['id_task']


def marker():
    return slot_marker(get_local('/slots'))


def quiet(expected):
    need(marker() == expected, 'idle slot marker changed between turns')
    time.sleep(12)
    need(marker() == expected, 'idle slot marker changed during delay')


def backend():
    models = get_local('/v1/models')['data']
    need(len(models) == 1 and models[0]['id'] == MODEL, 'unexpected local model')
    props = get_local('/props')
    need(props.get('total_slots') == 1, 'expected one configured slot')
    # Drop volatile fields (creation time) and never retain slot prompts.
    return dict(model=MODEL, model_meta=models[0].get('meta'), build=props.get('build_info'),
                settings_sha256=sha(encoded(props['default_generation_settings'])),
                model_path_sha256=sha(encoded(props.get('model_path'))))


class Boundary:
    """Guard cooperating release code and its HTTP/SQLite libraries; not a hostile-code VM."""
    def __init__(self, work):
        self.work = work
        self.denied = False

    def audit(self, event, args):
        if event.startswith('socket.'):
            if event == 'socket.__new__' and args[1:3] == (socket.AF_INET, socket.SOCK_STREAM):
                return
            if event == 'socket.connect' and args[0].family == socket.AF_INET and args[1] == ENDPOINT:
                return
            if event == 'socket.getaddrinfo' and args[:2] in (ENDPOINT, (ENDPOINT[0], str(ENDPOINT[1]))):
                return
        elif event == 'sqlite3.connect':
            name = os.fsdecode(args[0])
            if name in [str(self.work/n) for n in ('app.sqlite', 'cards.sqlite')]:
                return
            u = urlsplit(name)
            if u.scheme == 'file' and not u.netloc and u.query == 'mode=ro' and Path(unquote(u.path)) in (
                    self.work/'catalog.sqlite', self.work/'kb.sqlite'):
                return
        elif event not in ('subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.fork'):
            return
        self.denied = True
        raise PermissionError('retail matrix isolation boundary')


def stage_release(root, work):
    need(root == root.resolve() and (root/'app').is_dir() and not (root/'app').is_symlink(), 'expected isolated release containing app/')
    need(not os.path.lexists(root/'.env'), 'release contains .env')
    files = {}
    for base, dirs, names in os.walk(root/'app'):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for n in dirs + names:
            p = Path(base)/n
            need(not p.is_symlink() and n != '.env', 'linked source or .env in app')
        for n in names:
            p = Path(base)/n
            rel = p.relative_to(root)
            if p.suffix == '.py' or (rel.parts[:3] == ('app', 'quote', 'assets') and p.suffix in ('.ttf', '.png', '.txt')):
                files[rel] = regular(p)
    need(Path('app/__init__.py') in files and Path('app/agent/loop.py') in files, 'incomplete release')
    # Do not copy release data/, examples, logs, added cases, or database files.
    digests = {}
    for rel, p in sorted(files.items()):
        dest = work/rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
        digests[str(rel)] = file_hash(dest)
    return sha(encoded(digests))


TABLES = {'catalog': ({'categories', 'products', 'offers'}, {'meta'},
                      {'products_fts', 'products_tri', 'offers_fts', 'categories_fts'}),
          'kb': ({'docs', 'chunks'}, set(), {'chunks_fts'})}


def check_schema(path, kind):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)) as con:
        objects = con.execute("SELECT type, name FROM sqlite_schema WHERE type != 'index'").fetchall()
    need(all(t == 'table' for t, _ in objects), f'unexpected {kind} schema object')
    names = {n for _, n in objects}
    required, optional, fts = TABLES[kind]
    allowed = required | optional | fts | {'sqlite_stat1', 'sqlite_stat4', 'sqlite_sequence'}
    allowed.update(t+s for t in fts for s in ('_data','_idx','_content','_docsize','_config'))
    need(required <= names <= allowed, f'unexpected {kind} schema; never supply application/session databases')


def fixture(source, work, kind):
    regular(source)
    need(source == source.resolve(), 'linked fixture path')
    def sealed():
        need(not any(os.path.lexists(str(source)+s) for s in ('-wal', '-shm', '-journal')), 'fixture must be a closed SQLite snapshot')
    sealed()
    check_schema(source, kind)  # reject wrong-role databases before copying their pages
    dest = work/(kind+'.sqlite')
    shutil.copyfile(source, dest)
    digest = file_hash(dest)
    need(digest == file_hash(source), 'fixture changed during copy')
    sealed()
    dest.chmod(0o400)
    check_schema(dest, kind)
    return digest


def selected_families(rows, only, limit):
    names = [r['family'] for r in rows]
    if only:
        wanted = {v for item in only for v in item.split(',')}
        need(wanted <= set(names), 'unknown --only family')
        names = [n for n in names if n in wanted]
    need(limit is None or limit > 0, '--limit must be positive')
    return names[:limit]


def observe(out, manifest, rows, turn, guard, baseline):
    selected = manifest['conditions']['selected']
    for name in selected:
        if name in UNMET:
            record(out, manifest, name, 'SKIPPED', UNMET[name])
    for row in rows:
        family = row['family']
        if family not in selected or family in UNMET:
            continue
        snapshot = None
        try:
            quiet(baseline)
            reply = turn(None, row['prompt'], client_id='eval-synthetic-'+family)
            usage = reply['usage']
            need(isinstance(reply['reply'], str) and isinstance(usage, dict) and all(
                type(usage.get(k)) is int and usage[k] >= 0 for k in ('llm_calls','tool_calls','prompt_tokens','completion_tokens')),
                'invalid response/usage snapshot')
            snapshot = dict(reply=reply['reply'], usage=usage, trace=reply['trace'], events=reply['events'],
                            cards=reply.get('cards', []), choices=reply.get('choices', []))
            need(not guard.denied, 'release attempted disallowed operation')
            after = marker()
            need(after[0] == baseline[0] and (usage['llm_calls'] or after == baseline), 'unexpected slot marker change')
            baseline = after  # our own inference may advance id_task; pin its post-turn marker for the next gate
            need(not any(t.get('kind') == 'error' for t in snapshot['trace']), 'application returned an error trace')
            need(reply['reply'].strip(), 'empty reply')
        except Exception as exc:
            reason = str(exc) if type(exc) is ValueError else type(exc).__name__
            record(out, manifest, family, 'ERROR', reason, snapshot)
            raise ValueError(f'stopped at {family}: {reason}') from exc
        record(out, manifest, family, 'OBSERVED', 'awaiting expert review', snapshot)


def run(a):
    # Re-exec before loading any application or third-party module. No inherited
    # proxy, provider, loader, Python, or app configuration in the evaluator.
    clean = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}
    if dict(os.environ) != clean or not (sys.flags.isolated and sys.dont_write_bytecode):
        os.execve(sys.executable, [sys.executable, '-IB', str(SCRIPT), *sys.argv[1:]], clean)
    os.umask(0o077)
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    os.nice(max(0, 10-os.nice(0)))
    rows = definitions(a.suite)
    selected = selected_families(rows, a.only, a.limit)
    root, catalog, kb, out = (p.absolute() for p in (a.app, a.catalog, a.kb, a.out))
    need(not os.path.lexists(out) and out.parent == out.parent.resolve() and not out.is_relative_to(root),
         'output must be a new directory outside the release')
    with tempfile.TemporaryDirectory(prefix='retail-matrix-', dir='/tmp') as td:
        work = Path(td)
        release = stage_release(root, work)
        conditions = {kind: fixture(p, work, kind) for kind, p in [('catalog', catalog), ('kb', kb)]}
        for n in ('bridge','quotes'):
            (work/n).mkdir()
        os.chdir(work)
        os.environ.update(SETTINGS, HOME=td, TMPDIR=td, CATALOG_DB=str(work/'catalog.sqlite'),
                          KB_DB=str(work/'kb.sqlite'), APP_DB=str(work/'app.sqlite'), CARDS_DB=str(work/'cards.sqlite'),
                          QUOTES_DIR=str(work/'quotes'), BRIDGE_DIR=str(work/'bridge'))
        guard = Boundary(work)
        sys.addaudithook(guard.audit)
        baseline = marker()
        conditions.update(backend=backend(), settings=SETTINGS, selected=selected, runner_sha256=file_hash(SCRIPT),
                          delay_seconds=12, python=sys.version, sqlite=sqlite3.sqlite_version,
                          cpu=sorted(os.sched_getaffinity(0)), nice=os.nice(0), packages=sorted(
                              (d.metadata['Name'], d.version) for d in importlib.metadata.distributions()))
        manifest = start_bundle(out, rows, conditions, release)
        sys.path.insert(0, td)
        from app.agent import loop
        need(Path(loop.__file__).resolve() == work/'app/agent/loop.py', 'wrong release import')
        observe(out, manifest, rows, loop.run_turn, guard, baseline)
    print(out)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', action='version', version=VERSION)
    sub = p.add_subparsers(required=True)
    r = sub.add_parser('run', help='capture synthetic observations; never assigns PASS')
    r.set_defaults(fn=run)
    for arg, desc in [('app', 'trusted isolated release root containing app/ (no .env)'),
                      ('catalog', 'closed synthetic catalog SQLite fixture'), ('kb', 'closed synthetic KB SQLite fixture'),
                      ('out', 'new result directory; parent must exist')]:
        r.add_argument('--'+arg, type=Path, required=True, help=desc)
    r.add_argument('--idle-window', action='store_true', required=True, help='operator confirms a reserved idle window')
    r.add_argument('--synthetic-inputs', action='store_true', required=True, help='attest both SQLite fixtures contain synthetic data only')
    r.add_argument('--suite', type=Path, default=DEFAULT_SUITE, help='byte-identical copy of the pinned definitions')
    r.add_argument('--only', action='append', help='select families by exact name (repeat or comma-separate)')
    r.add_argument('--limit', type=int, help='take first N selected families in suite order, including skips')
    v = sub.add_parser('verdict', help='record a write-once expert decision on one observed case')
    v.set_defaults(fn=verdict)
    v.add_argument('result', type=Path)
    v.add_argument('family')
    v.add_argument('status', choices=['PASS','FAIL','INCONCLUSIVE'])
    for field in ('expert','reason','evidence-sha256'):
        v.add_argument('--'+field, required=True)
    e = sub.add_parser('export', help='write a new local CSV including all 33 cases')
    e.set_defaults(fn=export)
    e.add_argument('result', type=Path)
    e.add_argument('--out', type=Path, required=True)
    c = sub.add_parser('compare', help='compare matched conditions, varying only release')
    c.set_defaults(fn=compare)
    c.add_argument('left', type=Path)
    c.add_argument('right', type=Path)
    return p


if __name__ == '__main__':
    args = parser().parse_args()
    try:
        args.fn(args)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ImportError, http.client.HTTPException) as err:
        sys.exit('retail_matrix: ' + str(err))
