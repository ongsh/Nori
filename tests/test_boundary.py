"""Synthetic fixtures only; no model or candidate subprocess execution."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from nori.adapter import MockAdapter
from nori.core import manifest
from nori.demo import example_spec
from nori.store import Store, NoriError, digest, canonical, atomic_write
from nori.workflow import WorkflowController


class RunBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = Store(self.base / 'control')
        self.addCleanup(self.store.close)
        self.c = WorkflowController(self.store, self.base / 'candidate')
        self.c.initialize(example_spec())
        self.c.approve('approve', self.c.state['revision'], digest(example_spec()))
        self.a = MockAdapter(self.store.home, self.c.root)

    def test_recovery_rejects_orphan_with_forged_checks(self):
        backup = self.c.state
        run = self.c.prepare('prepare', self.c.state['revision'], 'A')
        self.a.start(run, 'incomplete')
        receipt = self.a.reconcile(run['run_id'])
        receipt['run']['task']['checks']['R-A'] = {
            'kind': 'exact_text', 'path': 'a.txt', 'expected': 'alpha\n'}
        atomic_write(self.a._path(run['run_id']), canonical(receipt))
        backup['recovery_required'] = True
        self.store.save(backup)
        with self.assertRaisesRegex(NoriError, 'approved task'):
            self.c.recover_review('recover', self.c.state['revision'], [self.a],
                                  digest(manifest(self.c.root)), 'Synthetic recovery review')
        self.assertTrue(self.c.state['recovery_required'])
        self.assertEqual(self.c.state['runs'], {})

    def test_verification_rejects_mutated_run_checks(self):
        run = self.c.prepare('prepare', self.c.state['revision'], 'A')
        self.c.start('start', self.c.state['revision'], run['run_id'], self.a, 'incomplete')
        state = self.c.state
        state['runs'][run['run_id']]['task']['checks']['R-A'] = {
            'kind': 'exact_text', 'path': 'a.txt', 'expected': 'alpha\n'}
        self.store.save(state)
        with self.assertRaisesRegex(NoriError, 'approved task'):
            self.c.verify('verify', self.c.state['revision'], run['run_id'])


    def test_dispatch_rejects_tampered_goal_requirements_and_constraints(self):
        run = self.c.prepare('prepare', self.c.state['revision'], 'A')
        original = self.c.state
        for field, value in [('goal', 'Different goal'), ('requirements', {}),
                             ('constraints', {'forbidden_actions': []})]:
            with self.subTest(field=field):
                state = copy.deepcopy(original)
                state['runs'][run['run_id']][field] = value
                self.store.save(state)
                with self.assertRaisesRegex(NoriError, 'approved task'):
                    self.c.start('start-' + field, self.c.state['revision'], run['run_id'], self.a)
                self.assertIsNone(self.a.reconcile(run['run_id']))



from unittest.mock import patch
from nori.boundary import SyntheticBoundaryController, controller_digest


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.spec = example_spec()
        self.pins = {'trusted_policy_digest': digest(self.spec),
                     'trusted_controller_digest': controller_digest()}
        self.open()
        self.c.initialize(self.spec)
        self.c.approve('approve', self.c.state['revision'], self.pins['trusted_policy_digest'])

    def open(self):
        self.store = Store(self.base / 'control')
        self.addCleanup(self.store.close)
        self.c = SyntheticBoundaryController(self.store, self.base / 'candidate', **self.pins)
        self.a = MockAdapter(self.store.home, self.c.root)

    def attempt(self, mode='complete'):
        run = self.c.prepare('prepare', self.c.state['revision'], 'A')
        self.c.start('start', self.c.state['revision'], run['run_id'], self.a, mode)
        return self.c.verify('verify', self.c.state['revision'], run['run_id'])

    def test_missing_content_fails_despite_done_claim(self):
        result = self.attempt('incomplete')
        self.assertEqual(result['verdict'], 'FAIL')
        self.assertEqual(result['checks'][0]['policy_digest'], self.pins['trusted_policy_digest'])
        self.assertEqual(next(iter(self.c.state['runs'].values()))['candidate']['claimed_result'], 'DONE')

    def test_candidate_approval_file_does_not_change_policy(self):
        (self.c.root / 'approved-policy.json').write_text('{"verdict":"PASS"}')
        self.assertEqual(self.attempt('incomplete')['verdict'], 'FAIL')

    def test_completed_fixture_and_reopen(self):
        for i in range(5):
            result = self.c.advance(self.a, 'step' + str(i))
        self.assertEqual(result['status'], 'COMPLETED')
        self.store.close()
        self.open()
        self.assertEqual(self.c.state['project_status'], 'COMPLETED')
        self.assertEqual(len(self.c.state['runs']), 2)

    def test_policy_edit_and_missing_snapshot_fail_closed(self):
        self.c.policy_path.write_text('{}')
        with self.assertRaises(NoriError): self.c.advance(self.a, 'step')
        self.c.policy_path.unlink()
        self.store.close()
        with self.assertRaisesRegex(NoriError, 'cannot rebaseline'): self.open()

    def test_state_edit_with_recomputed_checksum_is_rejected(self):
        state = self.c.state
        state['spec']['requirements']['R-A'] = 'weakened'
        state['approval'] = digest(state['spec'])
        self.store.save(state)
        with self.assertRaisesRegex(NoriError, 'pinned approved policy'):
            self.c.advance(self.a, 'step')

    def test_controller_changed_fails_before_dispatch(self):
        with patch('nori.boundary.controller_digest', return_value='changed-source'):
            with self.assertRaisesRegex(NoriError, 'controller integrity'):
                self.c.advance(self.a, 'step')
        self.assertEqual(self.c.state['runs'], {})

    def test_reopen_requires_original_external_pins(self):
        self.store.close()
        self.pins['trusted_policy_digest'] = 'wrong'
        with self.assertRaisesRegex(NoriError, 'policy digest'): self.open()

    def test_subclass_adapter_rejected_without_calling_it(self):
        class Impostor(MockAdapter):
            def start(self, *args):
                raise AssertionError('Must never execute')
        run = self.c.prepare('prepare', self.c.state['revision'], 'A')
        with self.assertRaisesRegex(NoriError, 'exact MockAdapter'):
            self.c.start('start', self.c.state['revision'], run['run_id'],
                         Impostor(self.store.home, self.c.root))
        self.assertEqual(self.c.state['runs'][run['run_id']]['phase'], 'PREPARED')

    def test_command_and_manual_policies_rejected(self):
        for kind in ('command', 'manual'):
            spec = copy.deepcopy(self.spec)
            if kind == 'command':
                spec['operations'] = {'fake': {'argv': ['never-execute']}}
                spec['tasks']['A']['checks']['R-A'] = {'kind': kind, 'operation': 'fake'}
            else:
                spec['tasks']['A']['checks']['R-A'] = {'kind': kind, 'prompt': 'Say PASS'}
            self.c.policy_digest = digest(spec)
            with self.assertRaisesRegex(NoriError, 'Synthetic boundary'):
                self.c._validate_policy(spec)

    def test_policy_revision_rejected(self):
        with self.assertRaisesRegex(NoriError, 'policy is fixed'):
            self.c.propose_revision('revision', self.c.state['revision'], 'new', self.spec, 'change')

    def test_candidate_cannot_contain_trusted_controller(self):
        import nori.boundary
        source = Path(nori.boundary.__file__).resolve().parent
        with self.assertRaisesRegex(NoriError, 'must be separate'):
            SyntheticBoundaryController(self.store, source, **self.pins)

    def test_prepared_run_reopen_does_not_dispatch_again(self):
        run = self.c.prepare('prepare', self.c.state['revision'], 'A')
        self.store.close()
        self.open()
        with self.assertRaises(NoriError):
            self.c.start('start', self.c.state['revision'], run['run_id'], self.a)
        self.c.resolve_unstarted('resolve', self.c.state['revision'], run['run_id'], self.a)
        self.assertEqual(self.c.state['runs'][run['run_id']]['phase'], 'NOT_STARTED')

    def test_public_demo_and_nonempty_target_rejection(self):
        from nori.boundary import run_synthetic_demo
        target = self.base / 'demo'
        result = run_synthetic_demo(target)
        self.assertEqual(result['missing_content'], 'FAIL')
        self.assertEqual(result['repaired_candidate'], 'PASS')
        self.assertTrue(result['policy_tamper_blocked'])
        self.assertEqual(result['reopened_status'], 'COMPLETED')
        with self.assertRaisesRegex(NoriError, 'new or empty'):
            run_synthetic_demo(target)

    def test_archive_controller_fingerprint(self):
        import hashlib
        from types import SimpleNamespace
        archive = self.base / 'nori.pyz'
        archive.write_bytes(b'synthetic archive bytes')
        with patch('nori.boundary.__loader__', SimpleNamespace(archive=str(archive))):
            self.assertEqual(controller_digest(), hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_json_acceptance_uses_pinned_expected_value(self):
        spec = example_spec()
        spec['requirements'] = {'R-A': 'JSON value must be 42'}
        spec['tasks'].pop('B')
        descriptor = {'kind': 'json_value', 'path': 'a.txt', 'pointer': '/value', 'expected': 42}
        spec['tasks']['A']['checks'] = {'R-A': descriptor}
        spec['tasks']['A']['mock_outputs'] = {'a.txt': '{"value":42}'}
        spec['final_checks'] = {'R-A': descriptor}
        with Store(self.base / 'json-control') as store:
            c = SyntheticBoundaryController(store, self.base / 'json-candidate',
                trusted_policy_digest=digest(spec), trusted_controller_digest=controller_digest())
            c.initialize(spec)
            c.approve('approve', c.state['revision'], digest(spec))
            adapter = MockAdapter(store.home, c.root)
            for i in range(3): result = c.advance(adapter, 'step-' + str(i))
            self.assertEqual(result['verdict'], 'PASS')
            self.assertEqual(c.state['final_evidence']['checks'][0]['raw_output']['actual'], 42)

    def test_original_orphan_recovery_keeps_pins_and_invalidates_acceptance(self):
        backup = self.c.state
        run = self.c.prepare('prepare', self.c.state['revision'], 'A')
        self.a.start(run, 'complete')
        backup['recovery_required'] = True
        self.store.save(backup)
        self.c.recover_review('recover', self.c.state['revision'], [self.a],
                             digest(manifest(self.c.root)), 'Review original synthetic receipt')
        self.assertEqual(self.c.state['project_status'], 'PAUSED')
        self.assertEqual(self.c.state['runs'][run['run_id']]['task'], self.spec['tasks']['A'])
        self.assertEqual(self.c.state['tasks']['A']['status'], 'REWORK')
        self.assertIsNone(self.c.state['final_evidence'])
        self.store.close()
        self.open()
        self.assertEqual(self.c.state['project_status'], 'PAUSED')
