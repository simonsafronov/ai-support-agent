"""Offline contract tests. No model, server, release or customer database required."""
import csv
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CLI = Path(__file__).resolve().parents[1] / 'eval/retail_matrix.py'
spec = importlib.util.spec_from_file_location('retail_matrix', CLI)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class RetailMatrix(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.work = Path(t.name)

    def cli(self, *args, ok=True):
        r = subprocess.run([sys.executable, '-IB', str(CLI), *map(str, args)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode == 0, ok, r.stderr)
        return r.stdout if ok else r.stderr

    def test_run_requires_consent(self):
        self.assertIn('--idle-window', self.cli('run', ok=False))

    def test_only_pinned_suite(self):
        path = self.work / 'copied.json'
        path.write_bytes(m.DEFAULT_SUITE.read_bytes())
        self.assertEqual(len(m.definitions(path)), 33)
        path.write_text('[{"prompt":"arbitrary"}]')
        with self.assertRaisesRegex(ValueError, 'suite'):
            m.definitions(path)

    def test_slot_change_and_busy_fail_closed(self):
        for value in [[], [{}], [{'id': 0, 'is_processing': True, 'id_task': 9}]]:
            with self.assertRaises(ValueError):
                m.slot_marker(value)
        with patch.object(m.time, 'sleep') as sleep, patch.object(m, 'marker', side_effect=[(0, 8), (0, 9)]):
            with self.assertRaisesRegex(ValueError, 'changed'):
                m.quiet((0, 8))
            sleep.assert_called_once_with(12)

    def test_offline_runner_isolation(self):
        # A child preserves the actual audit hook, affinity and app import path.
        # Only runtime metadata and the clock are stubbed; no socket is opened.
        import sqlite3
        release = self.work/'release'
        app = release/'app'
        (app/'agent').mkdir(parents=True)
        (app/'__init__.py').touch()
        (app/'agent/__init__.py').touch()
        (app/'agent/loop.py').write_text('''
import os, sqlite3
from pathlib import Path

def run_turn(sid, prompt, *, client_id):
    assert sid is None and client_id == 'eval-synthetic-price' and prompt.startswith('Во сколько')
    assert len(os.sched_getaffinity(0)) == 1 and os.nice(0) >= 10
    assert 'MATRIX_TEST_SECRET' not in os.environ
    assert Path(__file__).is_relative_to(Path.cwd())
    catalog = Path(os.environ['CATALOG_DB'])
    assert catalog.parent == Path.cwd() and not catalog.stat().st_mode & 0o222
    with sqlite3.connect(catalog.as_uri()+'?mode=ro', uri=True) as db:
        assert db.execute('select count(*) from offers').fetchone() == (0,)
    with sqlite3.connect(os.environ['APP_DB']) as db:
        db.execute('create table fictional(x)')
    return dict(reply='offline response', usage=dict(llm_calls=0, tool_calls=0, prompt_tokens=0, completion_tokens=0), trace=[], events=[])
''')
        refs = []
        for name, tables in [('catalog', ['categories','products','offers']), ('kb', ['docs','chunks'])]:
            p = self.work/(name+'.sqlite')
            with sqlite3.connect(p) as db:
                for t in tables:
                    db.execute(f'create table {t}(x)')
            db.close()
            refs.append(p)
        out = self.work/'run'
        program = '''
import importlib.util, sys
s=importlib.util.spec_from_file_location('matrix',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.get_local=lambda path: {'/props': {'total_slots': 1, 'default_generation_settings': {},'build_info':'test'}, '/v1/models': {'data':[{'id':m.MODEL}]}, '/slots':[{'id':0,'id_task':42,'is_processing':False}]}[path]
m.time.sleep=lambda sec: None
m.run(m.parser().parse_args(sys.argv[2:]))
'''
        args = ['run', '--app', str(release), '--catalog', str(refs[0]), '--kb', str(refs[1]),
                '--only', 'price,waiting_operator', '--limit', '2', '--idle-window', '--synthetic-inputs', '--out', str(out)]
        clean = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}
        r = subprocess.run([sys.executable, '-IB', '-c', program, str(CLI), *args],
                           env=clean, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        manifest, rows = m.load_bundle(out)
        self.assertEqual(rows['price']['reply'], 'offline response')
        self.assertEqual(rows['waiting_operator']['status'], 'SKIPPED')
        self.assertEqual(manifest['conditions']['catalog'], m.file_hash(refs[0]))
        self.assertFalse((release/'__pycache__').exists())
        self.assertFalse((release/'app.sqlite').exists())
        # Real command entry and clean re-exec; rejection must precede all probes.
        (release/'.env').touch()
        args[-1] += '-second'
        self.assertIn('release contains .env', self.cli(*args, ok=False))

    def test_guard_and_interturn_stop(self):
        g = m.Boundary(self.work)
        for event, args in [('socket.getaddrinfo', ('unapproved.invalid', 443)),
                            ('socket.connect', (type('Socket', (), {'family': m.socket.AF_INET})(), ('127.0.0.1', 80))),
                            ('sqlite3.connect', ('file:/other.sqlite?mode=ro',)),
                            ('sqlite3.connect', (str(self.work/'catalog.sqlite'),))]:
            with self.assertRaises(PermissionError):
                g.audit(event, args)
        out = self.work/'out'
        rows = m.definitions(m.DEFAULT_SUITE)
        manifest = m.start_bundle(out, rows, {'selected':['price','tiers','vat']}, 'a'*64)
        observed = dict(reply='demo', usage=dict(llm_calls=1, prompt_tokens=5, completion_tokens=1, tool_calls=0), trace=[], events=[])
        from unittest.mock import Mock
        turn = Mock(return_value=observed)
        with patch.object(m.time, 'sleep'), patch.object(m, 'marker', side_effect=[(0,1),(0,1),(0,2),(0,3)]):
            with self.assertRaisesRegex(ValueError, 'changed between turns'):
                m.observe(out, manifest, rows, turn, m.Boundary(self.work), (0,1))
        self.assertEqual(turn.call_count, 1)
        _, result = m.load_bundle(out)
        self.assertEqual([result[k]['status'] for k in ['price','tiers','vat']], ['OBSERVED','ERROR','UNTESTED'])

    def bundle(self, name, release, catalog='c'*64):
        out = self.work / name
        rows = m.definitions(m.DEFAULT_SUITE)
        selected = ['price', 'waiting_operator']
        manifest = m.start_bundle(out, rows, {'selected': selected, 'catalog': catalog}, release)
        m.record(out, manifest, 'price', 'OBSERVED', 'awaiting expert review',
                 {'reply': '  =synthetic', 'usage': {'llm_calls': 1}})
        m.record(out, manifest, 'waiting_operator', 'SKIPPED', m.UNMET['waiting_operator'])
        return out

    def test_review_export_compare(self):
        left = self.bundle('left', 'a'*64)
        right = self.bundle('right', 'b'*64)
        csvfile = self.work / 'result.csv'
        self.cli('export', left, '--out', csvfile)
        self.assertTrue(csvfile.read_bytes().startswith(b'\xef\xbb\xbf'))
        rows = {r['family']: r for r in csv.DictReader(io.StringIO(csvfile.read_text('utf-8-sig')))}
        self.assertEqual(len(rows), 33)
        self.assertEqual(rows['price']['status'], 'OBSERVED')
        self.assertTrue(rows['price']['reply'].startswith("'"))
        self.assertEqual(rows['waiting_operator']['status'], 'SKIPPED')
        self.assertEqual(rows['tiers']['status'], 'UNTESTED')
        def verdict(family, h, ok):
            return self.cli('verdict', left, family, 'PASS', '--evidence-sha256', h,
                            '--expert', 'Reviewer', '--reason', 'Checked against rubric', ok=ok)
        self.assertIn('hash', verdict('price', '0'*64, False))
        verdict('waiting_operator', rows['waiting_operator']['evidence_sha256'], False)
        verdict('price', rows['price']['evidence_sha256'], True)
        verdict('price', rows['price']['evidence_sha256'], False)
        result = json.loads(self.cli('compare', left, right))
        self.assertEqual(result['rows']['price']['left'], 'PASS')
        self.assertEqual(result['rows']['price']['right'], 'OBSERVED')
        other = self.bundle('other', 'a'*64, 'd'*64)
        self.assertIn('conditions', self.cli('compare', left, other, ok=False))
        snap = left/'evidence/price.json'
        snap.write_text(snap.read_text().replace('=synthetic', '=different'))
        self.assertIn('hash', self.cli('compare', left, right, ok=False))


if __name__ == '__main__':
    unittest.main()
