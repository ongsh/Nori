import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from nori.store import Store, NoriError, digest
from nori.core import manifest, validate_spec
from nori.workflow import WorkflowController
from nori.adapter import MockAdapter
from nori.demo import example_spec
from nori.quality_gate import QualityGate


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = Store(self.base / 'control')
        self.c = WorkflowController(self.store, self.base / 'work')
        self.c.initialize(example_spec())
        self.a = MockAdapter(self.store.home, self.c.root)
        self.n = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def call(self, name, *args):
        self.n += 1
        return getattr(self.c, name)(str(self.n), self.c.state['revision'], *args)

    def approve(self): self.call('approve', digest(self.c.state['spec']))

    def attempt(self, tid='A', mode='complete'):
        run = self.call('prepare', tid)
        self.call('start', run['run_id'], self.a, mode)
        return run, self.call('verify', run['run_id'])

    def test_bounded_scheduler_completes(self):
        self.approve()
        outcomes = [self.c.advance(self.a, 'step' + str(i)) for i in range(5)]
        self.assertEqual(outcomes[-1]['status'], 'COMPLETED')
        self.assertEqual(len(self.c.state['runs']), 2)

    def test_requirements_revision_requires_exact_new_approval(self):
        self.approve(); self.attempt(mode='incomplete')
        spec = copy.deepcopy(self.c.state['spec'])
        spec['goal'] = 'Explicit changed goal'
        p = self.call('propose_revision', 'p1', spec, 'User changed goal')
        self.assertNotEqual(self.c.state['spec']['goal'], spec['goal'])
        with self.assertRaises(NoriError): self.call('accept_plan', 'p1')
        with self.assertRaises(NoriError): self.call('accept_revision', 'p1', 'stale')
        self.call('accept_revision', 'p1', p['digest'])
        self.assertEqual(self.c.state['requirement_version'], 2)
        self.assertEqual(self.c.state['tasks']['A']['attempts'], 1)
        self.assertEqual(len(self.c.state['runs']), 1)
        self.assertEqual(next(iter(self.c.state['runs'].values()))['verification']['verdict'], 'FAIL')

    def test_active_inputs_cannot_be_revised(self):
        self.approve(); self.call('prepare', 'A')
        with self.assertRaises(NoriError): self.call('propose_revision', 'p', example_spec(), 'adjust')

    def test_contained_plan_revision_and_budget_preserved(self):
        self.approve(); self.attempt(mode='incomplete')
        spec = self.c.state['spec']; spec['tasks']['A']['why'] = 'Clarify source text purpose'
        self.call('propose_revision', 'p', spec, 'clarification')
        self.call('accept_plan', 'p')
        self.assertEqual(self.c.state['plan_version'], 2)
        self.assertEqual(self.c.state['tasks']['A']['attempts'], 1)

    def test_cancel_waits_for_stop_then_partial_is_not_completed(self):
        self.approve()
        run = self.call('prepare', 'A'); self.call('start', run['run_id'], self.a, 'hold')
        r = self.call('cancel', self.a, 'stop', True)
        self.assertIn('pending_stop', r)
        self.assertEqual(self.c.state['lease'], run['run_id'])
        self.a.confirm_stopped(run['run_id']); self.call('reconcile', run['run_id'], self.a)
        r = self.call('cancel', self.a, 'Reviewed stopped run', True)
        self.assertEqual(r['status'], 'CLOSED_PARTIAL')
        self.assertIsNone(self.c.state['final_evidence'])

    def test_prepared_restart_can_safely_resolve_not_started(self):
        self.approve(); run = self.call('prepare', 'A')
        self.store.close()
        self.store = Store(self.base / 'control'); self.c = WorkflowController(self.store, self.base / 'work')
        self.call('resolve_unstarted', run['run_id'], self.a)
        self.assertIsNone(self.c.state['lease'])
        self.assertEqual(self.c.state['runs'][run['run_id']]['phase'], 'NOT_STARTED')
        self.assertEqual(self.attempt()[1]['verdict'], 'PASS')

    def test_running_supervisor_scope_and_timeout(self):
        self.approve(); run = self.call('prepare', 'A')
        self.call('start', run['run_id'], self.a, 'hold')
        (self.c.root / 'outside.txt').write_text('unauthorized')
        r = self.call('supervise', self.a)
        self.assertEqual(r['action'], 'pause')
        self.assertEqual(r['findings'][0]['rule'], 'scope_violation')
        self.assertIsNotNone(self.c.state['lease'])

    def test_no_progress_alone_does_not_punish_tests(self):
        self.approve(); run = self.call('prepare', 'A')
        self.call('start', run['run_id'], self.a, 'hold')
        for _ in range(3): result = self.call('supervise', self.a)
        self.assertEqual(result['findings'][0]['rule'], 'progress_unconfirmed')
        self.assertEqual(result['action'], 'observe')
        self.assertEqual(self.c.state['project_status'], 'ACTIVE')

    def test_manual_gate_requires_bound_explicit_inspection(self):
        spec = self.c.state['spec']
        spec['tasks']['A']['checks']['R-A'] = {'kind': 'manual', 'prompt': 'Inspect both lines'}
        self.call('propose_revision', 'manual', spec, 'Require inspection')
        self.call('accept_revision', 'manual', digest(spec))
        run, e = self.attempt()
        self.assertEqual(e['verdict'], 'INCONCLUSIVE')
        descriptor = spec['tasks']['A']['checks']['R-A']
        binding = QualityGate(self.c.root, spec).binding('R-A', descriptor)
        self.call('record_manual', 'R-A', descriptor, binding, 'PASS', 'Inspected both synthetic lines')
        self.call('retry_verification', 'A')
        self.assertEqual(self.call('verify', run['run_id'])['verdict'], 'PASS')

    def test_backup_recovery_review_restores_safe_dispatch(self):
        self.approve(); self.attempt()
        self.store.path.write_text('{bad')
        self.store.close()
        self.store = Store(self.base / 'control', recover=True)
        self.c = WorkflowController(self.store, self.base / 'work')
        self.call('recover_review', [self.a], digest(manifest(self.c.root)), 'Inspected all simulator exits and files')
        self.assertFalse(self.c.state['recovery_required'])
        self.assertEqual(self.c.state['project_status'], 'PAUSED')
        self.call('resume')
        self.assertEqual(self.attempt()[1]['verdict'], 'PASS')

    def test_backup_review_cannot_release_live_execution(self):
        self.approve(); run = self.call('prepare', 'A'); self.call('start', run['run_id'], self.a, 'hold')
        self.store.path.write_text('{bad'); self.store.close()
        self.store = Store(self.base / 'control', recover=True)
        self.c = WorkflowController(self.store, self.base / 'work')
        with self.assertRaises(NoriError):
            self.call('recover_review', [self.a], digest(manifest(self.c.root)), 'cannot guess')
        self.assertTrue(self.c.state['recovery_required'])

    def test_partial_pass_does_not_replace_integrated_check(self):
        spec = self.c.state['spec']
        spec['final_checks']['R-B'] = {'kind': 'exact_text', 'path': 'b.txt', 'expected': 'different integration contract'}
        self.call('propose_revision', 'integration', spec, 'Stricter final contract')
        self.call('accept_revision', 'integration', digest(spec))
        self.attempt('A'); self.attempt('B', 'copy')
        result = self.call('close_project')
        self.assertEqual(result['verdict'], 'FAIL')
        self.assertNotEqual(self.c.state['project_status'], 'COMPLETED')

    def test_plan_only_change_cannot_weaken_task_acceptance(self):
        self.approve()
        spec = self.c.state['spec']
        spec['tasks']['A']['checks']['R-A'] = {'kind': 'exact_text', 'path': 'a.txt', 'expected': 'alpha\n'}
        self.call('propose_revision', 'weak', spec, 'attempt to weaken')
        with self.assertRaises(NoriError): self.call('accept_plan', 'weak')

    def test_duplicate_requirement_cannot_hide_weakened_task_gate(self):
        self.approve()
        spec = self.c.state['spec']
        spec['tasks']['A']['checks']['R-A'] = {'kind': 'exact_text', 'path': 'a.txt', 'expected': 'alpha\n'}
        spec['tasks']['B']['requirements'].append('R-A')
        spec['tasks']['B']['checks']['R-A'] = 'a.txt'
        self.call('propose_revision', 'hidden-weak', spec, 'weakening hidden by duplicate requirement')
        with self.assertRaises(NoriError): self.call('accept_plan', 'hidden-weak')

    def test_initialization_crash_does_not_lose_total_budget(self):
        from nori.core import Controller
        spec = example_spec(); spec['max_total_attempts'] = 1
        with Store(self.base / 'interrupted-init') as st:
            c = Controller(st, self.base / 'interrupted-work'); c.initialize(spec)
        with Store(self.base / 'interrupted-init') as st:
            c = WorkflowController(st, self.base / 'interrupted-work'); c.approve('a', 0, digest(spec))
            a = MockAdapter(st.home, c.root)
            run = c.prepare('p', c.state['revision'], 'A'); c.start('s', c.state['revision'], run['run_id'], a, 'incomplete')
            c.verify('v', c.state['revision'], run['run_id'])
            with self.assertRaises(NoriError): c.prepare('p2', c.state['revision'], 'A')
            self.assertFalse(any(row['ready'] for row in c.ready()))

    def test_directory_scope_uses_path_components(self):
        from nori.core import in_scope
        self.assertTrue(in_scope('allowed/sub/a.txt', ['allowed/']))
        self.assertFalse(in_scope('allowed-other/a.txt', ['allowed/']))
        self.assertFalse(in_scope('other/allowed/a.txt', ['allowed/']))

    def test_total_budget_is_respected_at_initialization(self):
        spec = example_spec(); spec['max_total_attempts'] = 1
        with Store(self.base / 'small-control') as st:
            c = WorkflowController(st, self.base / 'small-work'); c.initialize(spec)
            c.approve('a', 0, digest(spec))
            self.assertEqual(c.state['budget_limit'], 1)
            run = c.prepare('p', c.state['revision'], 'A')
            a = MockAdapter(st.home, c.root); c.start('s', c.state['revision'], run['run_id'], a, 'incomplete')
            c.verify('v', c.state['revision'], run['run_id'])
            with self.assertRaises(NoriError): c.prepare('again', c.state['revision'], 'A')

    def test_cli_run_releases_lock_for_pause_and_status(self):
        import time
        spec = self.c.state['spec']; spec['tasks']['A']['mock_mode'] = 'hold'
        self.call('propose_revision', 'hold', spec, 'Synthetic ongoing task')
        self.call('accept_revision', 'hold', digest(spec))
        self.store.close()
        runner = subprocess.Popen([sys.executable, '-m', 'nori', 'run', str(self.base/'control'),
                                   '--adapter','mock','--max-steps','30','--poll-seconds','0.1'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # Allow initial dispatch; operators should get a lock between observation steps.
            time.sleep(0.2)
            for _ in range(20):
                pause = subprocess.run([sys.executable, '-m', 'nori', 'pause', str(self.base/'control')], capture_output=True, text=True)
                if pause.returncode == 0: break
                time.sleep(.05)
            self.assertEqual(pause.returncode, 0, pause.stderr)
            status = subprocess.run([sys.executable, '-m', 'nori', 'status', str(self.base/'control')], capture_output=True, text=True)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)['project_status'], 'PAUSED')
            runner.communicate(timeout=8)
        finally:
            if runner.poll() is None: runner.terminate(); runner.communicate(timeout=3)

    def test_two_cli_adapters_share_one_control_state_on_synthetic_tasks(self):
        import time
        from nori.tool_adapters import CodexAdapter, ClaudeCodeAdapter
        fixture = self.base / 'synthetic-cli'
        fixture.write_text('#!' + sys.executable + '\n' + """import sys, json
from pathlib import Path
if '--version' in sys.argv:
    print('synthetic-cli 1'); raise SystemExit(0)
if 'status' in sys.argv:
    raise SystemExit(0)
raw = sys.stdin.read()
spec = json.loads(raw[raw.index('{'):])
for name, content in spec['task']['mock_outputs'].items():
    path=Path(name); path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
print(json.dumps({'type':'result','is_error':False}))
""")
        fixture.chmod(0o700)
        self.approve()
        for tid, cls in [('A', CodexAdapter), ('B', ClaudeCodeAdapter)]:
            adapter = cls(self.store.home, self.c.root, executable=str(fixture))
            run = self.call('prepare', tid)
            self.call('start', run['run_id'], adapter)
            for _ in range(100):
                if self.call('reconcile', run['run_id'], adapter) == 'EXITED': break
                time.sleep(.03)
            self.assertEqual(self.call('verify', run['run_id'])['verdict'], 'PASS')
        self.assertEqual({r['adapter'] for r in self.c.state['runs'].values()}, {'codex','claude'})
        self.assertEqual(self.call('close_project')['verdict'], 'PASS')
        self.assertEqual(self.c.state['requirement_version'], 1)

    def test_cli_import_approve_run_query(self):
        base = self.base / 'cli'; base.mkdir()
        spec = base / 'spec.json'; spec.write_text(json.dumps(example_spec()))
        def cli(*args):
            r = subprocess.run([sys.executable, '-m', 'nori', *map(str,args)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)
        result = cli('init', base / 'control', '--workspace', base / 'work', '--spec', spec)
        cli('approve', base / 'control', '--digest', result['approval_digest'])
        result = cli('run', base / 'control', '--adapter', 'mock')
        self.assertEqual(result['project_status'], 'COMPLETED')
        self.assertEqual(cli('status', base / 'control')['project_status'], 'COMPLETED')


if __name__ == '__main__': unittest.main()
