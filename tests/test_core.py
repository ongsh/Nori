import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nori.store import Store, NoriError, digest, atomic_write
from nori.core import Controller, validate_spec
from nori.adapter import MockAdapter
from nori.demo import example_spec, run_demo


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = Store(self.base / 'control')
        self.c = Controller(self.store, self.base / 'work')
        self.c.initialize(example_spec())
        self.adapter = MockAdapter(self.store.home, self.c.root)
        self.seq = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, *args):
        self.seq += 1
        return getattr(self.c, method)(f'cmd-{self.seq}', self.c.state['revision'], *args)

    def approve(self):
        self.call('approve', digest(self.c.state['spec']))

    def attempt(self, task='A', mode='complete'):
        r = self.call('prepare', task)
        self.call('start', r['run_id'], self.adapter, mode)
        return r, self.call('verify', r['run_id'])

    def reopen(self):
        self.store.close()
        self.store = Store(self.base / 'control')
        self.c = Controller(self.store, self.base / 'work')
        self.adapter = MockAdapter(self.store.home, self.c.root)

    def test_end_to_end(self):
        result = run_demo(self.base / 'other')
        self.assertEqual(result['final_verdict'], 'PASS')
        self.assertEqual(result['steps'][0]['verdict'], 'FAIL')
        self.assertIn('dependency_blocked', result['steps'][1])

    def test_unapproved_and_wrong_digest(self):
        with self.assertRaises(NoriError):
            self.call('prepare', 'A')
        with self.assertRaises(NoriError):
            self.call('approve', 'invented')
        self.assertFalse((self.c.root / 'a.txt').exists())

    def test_approved_spec_tampering_even_with_new_checksum(self):
        self.approve()
        modified = self.c.state
        modified['spec']['requirements']['R-A'] = 'lower standard'
        self.store.path.write_bytes(Store.encode(modified))
        self.store.close()
        with self.assertRaises(NoriError):
            Store(self.base / 'control')

    def test_dedupe_revision_and_conflicting_replay(self):
        rev = self.c.state['revision']
        expected = digest(self.c.state['spec'])
        self.c.approve('same', rev, expected)
        self.c.approve('same', rev, expected)
        self.assertEqual(self.c.state['revision'], rev + 1)
        with self.assertRaises(NoriError):
            self.c.approve('same', rev, 'different')
        with self.assertRaises(NoriError):
            self.c.prepare('new', rev, 'A')

    def test_fake_done_does_not_release_dependency(self):
        self.approve()
        run = self.call('prepare', 'A')
        self.call('start', run['run_id'], self.adapter, 'incomplete')
        self.assertEqual(self.c.state['tasks']['A']['status'], 'IMPLEMENTED')
        with self.assertRaises(NoriError):
            self.call('prepare', 'B')
        evidence = self.call('verify', run['run_id'])
        self.assertEqual(evidence['verdict'], 'FAIL')
        with self.assertRaises(NoriError):
            self.call('prepare', 'B')

    def test_duplicate_start_has_one_adapter_invocation(self):
        self.approve()
        run = self.call('prepare', 'A')
        rev = self.c.state['revision']
        with patch.object(self.adapter, 'start', wraps=self.adapter.start) as start:
            self.c.start('start-once', rev, run['run_id'], self.adapter)
            self.c.start('start-once', rev, run['run_id'], self.adapter)
            self.assertEqual(start.call_count, 1)
        self.assertEqual(self.adapter.reconcile(run['run_id'])['start_count'], 1)

    def test_old_result_cannot_overwrite_new_attempt(self):
        self.approve()
        old, _ = self.attempt(mode='incomplete')
        new = self.call('prepare', 'A')
        with self.assertRaises(NoriError):
            self.call('reconcile', old['run_id'], self.adapter)
        self.assertEqual(self.c.state['lease'], new['run_id'])

    def test_prepared_crash_and_replayed_prepare_cannot_start(self):
        self.approve()
        rev = self.c.state['revision']
        run = self.c.prepare('prepare-once', rev, 'A')
        self.reopen()
        self.c.prepare('prepare-once', rev, 'A')
        with self.assertRaises(NoriError):
            self.call('start', run['run_id'], self.adapter)
        self.assertEqual(self.call('reconcile', run['run_id'], self.adapter), 'UNKNOWN')
        with self.assertRaises(NoriError):
            self.call('prepare', 'B')

    def test_crash_after_external_effect_reconciles_without_restart(self):
        self.approve()
        run = self.call('prepare', 'A')
        original = self.adapter.start
        def exit_after_start(*args):
            original(*args)
            raise RuntimeError('controller died after adapter output')
        with patch.object(self.adapter, 'start', side_effect=exit_after_start):
            with self.assertRaises(RuntimeError):
                self.call('start', run['run_id'], self.adapter)
        self.reopen()
        with patch.object(self.adapter, 'start') as start:
            self.assertEqual(self.call('reconcile', run['run_id'], self.adapter), 'EXITED')
            self.assertEqual(self.call('verify', run['run_id'])['verdict'], 'PASS')
            start.assert_not_called()

    def test_running_recovery_observes_without_relaunch(self):
        self.approve()
        run = self.call('prepare', 'A')
        self.call('start', run['run_id'], self.adapter, 'hold')
        self.reopen()
        self.assertEqual(self.call('reconcile', run['run_id'], self.adapter), 'RUNNING')
        self.assertEqual(self.c.state['lease'], run['run_id'])
        with self.assertRaises(NoriError):
            self.call('start', run['run_id'], self.adapter)

    def test_pause_keeps_permit_until_confirmed_exit(self):
        self.approve()
        run = self.call('prepare', 'A')
        self.call('start', run['run_id'], self.adapter, 'hold')
        self.call('pause', self.adapter)
        self.assertEqual(self.c.state['lease'], run['run_id'])
        with self.assertRaises(NoriError):
            self.call('resume')
        with self.assertRaises(NoriError):
            self.call('prepare', 'B')
        self.adapter.confirm_stopped(run['run_id'])
        self.call('reconcile', run['run_id'], self.adapter)
        self.assertEqual(self.call('verify', run['run_id'])['verdict'], 'INCONCLUSIVE')
        self.assertIsNone(self.c.state['lease'])
        self.call('authorize_rework', 'A', 'Confirmed simulator exited; inspected partial outputs')
        self.call('resume')
        self.assertEqual(self.attempt()[1]['verdict'], 'PASS')

    def test_wrong_execution_identity_blocks(self):
        self.approve()
        run = self.call('prepare', 'A')
        self.call('start', run['run_id'], self.adapter, 'hold')
        record = self.adapter.reconcile(run['run_id'])
        record['identity'] = 'another-session'
        self.adapter._path(run['run_id']).write_text(json.dumps(record))
        self.assertEqual(self.call('reconcile', run['run_id'], self.adapter), 'UNKNOWN')
        self.assertEqual(self.c.state['lease'], run['run_id'])

    def test_post_candidate_change_is_inconclusive(self):
        self.approve()
        run = self.call('prepare', 'A')
        self.call('start', run['run_id'], self.adapter)
        (self.c.root / 'a.txt').write_text('changed')
        self.assertEqual(self.call('verify', run['run_id'])['verdict'], 'INCONCLUSIVE')

    def test_accepted_files_changed_invalidate_downstream_and_final(self):
        self.approve()
        self.attempt('A')
        self.attempt('B', 'copy')
        self.call('close_project')
        (self.c.root / 'a.txt').write_text('changed')
        self.call('audit')
        self.assertEqual(self.c.state['tasks']['A']['status'], 'REWORK')
        self.assertEqual(self.c.state['tasks']['B']['status'], 'REWORK')
        self.assertIsNone(self.c.state['final_evidence'])
        self.assertNotEqual(self.c.state['project_status'], 'COMPLETED')

    def test_scope_violation_blocks_and_preserves_existing_files(self):
        (self.c.root / 'user.txt').write_text('user data')
        self.approve()
        run = self.call('prepare', 'A')
        self.call('start', run['run_id'], self.adapter, 'hold')
        (self.c.root / 'unexpected.txt').write_text('bad')
        self.adapter.confirm_stopped(run['run_id'])
        self.call('reconcile', run['run_id'], self.adapter)
        self.assertEqual(self.c.state['tasks']['A']['status'], 'BLOCKED')
        self.assertEqual((self.c.root / 'user.txt').read_text(), 'user data')
        with self.assertRaises(NoriError):
            self.call('verify', run['run_id'])

    def test_attempt_budget(self):
        self.approve()
        for _ in range(3):
            self.attempt(mode='incomplete')
        result = self.call('prepare', 'A')
        self.assertIn('blocked', result)
        self.assertEqual(self.c.state['tasks']['A']['attempts'], 3)
        self.assertEqual(self.c.state['tasks']['A']['status'], 'FAILED')

    def test_second_controller_and_different_home_same_workspace(self):
        with self.assertRaises(NoriError):
            Store(self.store.home)
        with Store(self.base / 'another-home') as other:
            with self.assertRaises(NoriError):
                Controller(other, self.c.root)

    def test_closed_controller_cannot_dispatch_or_mutate(self):
        self.approve()
        run = self.call('prepare', 'A')
        self.store.close()
        with self.assertRaises(NoriError):
            self.call('start', run['run_id'], self.adapter)
        with self.assertRaises(NoriError):
            self.store.save(self.c.state)
        self.assertIsNone(self.adapter.reconcile(run['run_id']))

    def test_workspace_binding_survives_controller_exit(self):
        root = self.c.root
        self.store.close()
        with Store(self.base / 'new-home') as other:
            with self.assertRaises(NoriError):
                Controller(other, root)

    def test_bad_state_blocks_explicit_backup_recovery_never_dispatches(self):
        self.approve()
        self.store.path.write_text('{half')
        self.store.close()
        with self.assertRaises(NoriError):
            Store(self.base / 'control')
        self.store = Store(self.base / 'control', recover=True)
        self.c = Controller(self.store, self.base / 'work')
        self.assertTrue(self.c.state['recovery_required'])
        with self.assertRaises(NoriError):
            self.call('prepare', 'A')

    def test_write_failure_never_starts_and_requires_reopen(self):
        self.approve()
        with patch('nori.store.atomic_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.call('prepare', 'A')
        self.assertIsNone(self.c.state['lease'])
        with self.assertRaises(NoriError):
            self.call('prepare', 'A')
        self.reopen()
        self.assertIsNone(self.c.state['lease'])

    def test_derived_view_failure_does_not_rollback(self):
        with patch.object(self.store, 'views', side_effect=OSError('view failure')):
            self.approve()
        self.reopen()
        self.assertEqual(self.c.state['project_status'], 'ACTIVE')

    def test_missing_invalid_utf8_is_not_pass(self):
        self.approve()
        run = self.call('prepare', 'A')
        self.call('start', run['run_id'], self.adapter, 'hold')
        (self.c.root / 'a.txt').write_bytes(b'\xff')
        receipt = self.adapter.reconcile(run['run_id'])
        receipt.update(phase='EXITED', exit_code=0)
        self.adapter._path(run['run_id']).write_text(json.dumps(receipt))
        self.call('reconcile', run['run_id'], self.adapter)
        self.assertEqual(self.call('verify', run['run_id'])['verdict'], 'INCONCLUSIVE')

    def test_plan_rejects_cycles_missing_coverage_and_path_escape(self):
        for mutation in ('cycle', 'coverage', 'path'):
            spec = example_spec()
            if mutation == 'cycle':
                spec['tasks']['A']['depends_on'] = ['B']
            elif mutation == 'coverage':
                del spec['tasks']['B']
            else:
                spec['tasks']['A']['write_scope'].append('../outside')
            with self.assertRaises(NoriError):
                validate_spec(spec)

    def test_symlink_is_rejected(self):
        (self.c.root / 'a.txt').symlink_to(self.base / 'outside')
        self.approve()
        with self.assertRaises(NoriError):
            self.call('prepare', 'A')

    def test_missing_primary_cannot_reset_existing_project(self):
        self.approve()
        run = self.call('prepare', 'A')
        self.call('start', run['run_id'], self.adapter, 'hold')
        self.store.close()
        (self.base / 'control' / 'state.json').unlink()
        with self.assertRaises(NoriError):
            Store(self.base / 'control')
        self.store = Store(self.base / 'control', recover=True)
        self.c = Controller(self.store, self.base / 'work')
        self.assertTrue(self.c.state['recovery_required'])
        with self.assertRaises(NoriError):
            self.c.initialize(example_spec())
        with self.assertRaises(NoriError):
            self.call('prepare', 'A')

    def test_concurrent_duplicate_start_invokes_adapter_once(self):
        import threading
        import time
        self.approve()
        run = self.call('prepare', 'A')
        revision = self.c.state['revision']
        barrier = threading.Barrier(2)
        results, errors = [], []
        original = self.adapter.start
        def slow_start(*args):
            time.sleep(0.03)
            return original(*args)
        def worker():
            barrier.wait()
            try:
                results.append(self.c.start('same-start', revision, run['run_id'], self.adapter))
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.adapter, 'start', side_effect=slow_start) as start:
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads: t.start()
            for t in threads: t.join(3)
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertFalse(errors)
            self.assertEqual(len(results), 2)
            self.assertEqual(start.call_count, 1)

    def test_real_process_kill_before_and_after_atomic_replace(self):
        self.approve()
        self.store.close()
        for boundary in ('before', 'after'):
            code = '''
import os, signal, sys
from pathlib import Path
from unittest.mock import patch
from nori.store import Store
home = Path(sys.argv[1]); boundary = sys.argv[2]
with Store(home) as st:
    state = st.state.copy(); state['revision'] += 1
    original = os.replace
    def replace(src, dst):
        if Path(dst).name == 'state.json':
            if boundary == 'before': os.kill(os.getpid(), signal.SIGKILL)
            original(src, dst)
            os.kill(os.getpid(), signal.SIGKILL)
        return original(src, dst)
    with patch('os.replace', side_effect=replace): st.save(state)
'''
            result = subprocess.run([sys.executable, '-c', code, str(self.base / 'control'), boundary], capture_output=True)
            self.assertEqual(result.returncode, -9, result.stderr)
            with Store(self.base / 'control') as recovered:
                self.assertEqual(recovered.state['project_status'], 'ACTIVE')
                self.assertIsNone(recovered.state['lease'])
                self.assertEqual(recovered.state['revision'], 1 if boundary == 'before' else 2)


if __name__ == '__main__':
    unittest.main()
