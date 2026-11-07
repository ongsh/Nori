"""Pinned, synthetic validation boundary over the existing workflow and Store.

This is fail-closed integrity checking, NOT a sandbox or same-UID security
boundary. No candidate code is executed. Trusted callers supply both pins on
creation and reopening; never derive replacement pins from an untrusted run.
"""
import copy
import hashlib
import json
from pathlib import Path

from .adapter import MockAdapter
from .core import serialized, validate_spec
from .quality_gate import validate_check
from .store import NoriError, atomic_write, canonical, digest
from .workflow import WorkflowController


def _controller_location():
    archive = getattr(globals().get('__loader__'), 'archive', None)
    return Path(archive).resolve() if archive else Path(__file__).resolve().parent


def controller_digest():
    """Fingerprint the trusted installed package, not the candidate package."""
    root = _controller_location()
    if root.is_file():  # zipapp: __file__ is a virtual path inside this archive.
        return hashlib.sha256(root.read_bytes()).hexdigest()
    files = {}
    for path in sorted(root.rglob('*.py')):
        if path.is_symlink():
            raise NoriError('Controller source symlink refused')
        files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest(files)


class SyntheticBoundaryController(WorkflowController):
    """Thin opt-in fixture harness; all progress remains in the ordinary Store."""
    def __init__(self, store, workspace, *, trusted_policy_digest, trusted_controller_digest):
        root = Path(workspace).resolve()
        source = _controller_location()
        if root.is_relative_to(source) or source.is_relative_to(root):
            raise NoriError('Candidate and controller source must be separate')
        self.policy_digest = trusted_policy_digest
        self.controller_digest = trusted_controller_digest
        self.policy_path = store.home / 'approved-policy.json'
        self._check_controller()
        super().__init__(store, workspace)
        if self.state is not None:
            self._guard(self.state)

    def _check_controller(self):
        if controller_digest() != self.controller_digest:
            raise NoriError('Trusted controller integrity mismatch')

    def _validate_policy(self, spec):
        validate_spec(spec)
        if digest(spec) != self.policy_digest:
            raise NoriError('Trusted policy digest mismatch')
        if spec.get('executor', 'mock') != 'mock' or spec.get('operations'):
            raise NoriError('Synthetic boundary forbids real executors and command operations')
        mappings = [spec['final_checks'], *(t['checks'] for t in spec['tasks'].values())]
        for mapping in mappings:
            for rid, descriptor in mapping.items():
                if validate_check(descriptor, spec, rid)['kind'] not in ('exact_text', 'json_value'):
                    raise NoriError('Synthetic boundary permits only built-in exact text and JSON checks')

    def _guard(self, state):
        self._check_controller()
        try:
            if self.policy_path.is_symlink():
                raise NoriError('Policy symlink refused')
            spec = json.loads(self.policy_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise NoriError('Pinned approved policy missing or unreadable; cannot rebaseline') from exc
        self._validate_policy(spec)
        if state['spec'] != spec:
            raise NoriError('State differs from pinned approved policy')
        for run in state['runs'].values():
            self._validate_run_spec(state, run)
        return spec

    @serialized
    def initialize(self, spec):
        self._check_controller()
        self._validate_policy(spec)
        if self.policy_path.exists() or self.policy_path.is_symlink():
            raise NoriError('Existing policy cannot be replaced')
        super().initialize(spec)
        atomic_write(self.policy_path, canonical(copy.deepcopy(spec)))
        self._guard(self.state)

    @serialized
    def command(self, command_id, revision, operation, payload, fn):
        self._guard(self.state)
        if operation in ('propose_revision', 'accept_revision', 'record_manual'):
            raise NoriError('Synthetic policy is fixed; revisions require a new explicitly pinned fixture')
        def checked(state):
            result = fn(state)
            self._guard(state)  # Check before the ordinary Store commits any result.
            return result
        return super().command(command_id, revision, operation, payload, checked)

    def _checks(self, state, mapping):
        spec = self._guard(state)
        allowed = [spec['final_checks'], *(t['checks'] for t in spec['tasks'].values())]
        if mapping not in allowed:
            raise NoriError('Checks do not match pinned policy')
        verdict, checks = super()._checks(state, mapping)
        self._guard(state)
        for check in checks:
            check['policy_digest'] = self.policy_digest
            check['controller_digest'] = self.controller_digest
        return verdict, checks

    def _adapter(self, adapter):
        self._guard(self.state)
        if (type(adapter) is not MockAdapter or adapter.root != self.root
                or adapter.home != self.store.home / 'mock-receipts'):
            raise NoriError('Synthetic boundary requires exact MockAdapter bound to this workspace')

    @serialized
    def start(self, cid, rev, rid, adapter, mode='complete'):
        self._adapter(adapter)
        return super().start(cid, rev, rid, adapter, mode)

    @serialized
    def reconcile(self, cid, rev, rid, adapter):
        self._adapter(adapter)
        return super().reconcile(cid, rev, rid, adapter)

    @serialized
    def supervise(self, cid, rev, adapter):
        self._adapter(adapter)
        return super().supervise(cid, rev, adapter)

    @serialized
    def pause(self, cid, rev, adapter):
        self._adapter(adapter)
        return super().pause(cid, rev, adapter)

    @serialized
    def cancel(self, cid, rev, adapter, reason, partial=False):
        self._adapter(adapter)
        return super().cancel(cid, rev, adapter, reason, partial)

    @serialized
    def resolve_unstarted(self, cid, rev, rid, adapter):
        self._adapter(adapter)
        return super().resolve_unstarted(cid, rev, rid, adapter)

    @serialized
    def recover_review(self, cid, rev, adapters, expected_workspace_digest, reason):
        adapters = list(adapters)
        self._guard(self.state)
        for adapter in adapters:
            self._adapter(adapter)
            # Validate receipt inputs before recovery's direct Store save.
            for record in adapter.inventory():
                run = record.get('run')
                if run is not None:
                    self._validate_run_spec(self.state, run)
        result = super().recover_review(cid, rev, adapters, expected_workspace_digest, reason)
        self._guard(self.state)
        return result

    @serialized
    def advance(self, adapter, cid_prefix):
        self._adapter(adapter)
        return super().advance(adapter, cid_prefix)


def run_synthetic_demo(directory):
    """Run a new disposable fixture only. Never bootstrap a business project."""
    from .demo import example_spec
    from .store import Store

    directory = Path(directory)
    if directory.is_symlink() or (directory.exists() and (not directory.is_dir() or any(directory.iterdir()))):
        raise NoriError('Synthetic demo requires a new or empty directory')
    directory.mkdir(parents=True, exist_ok=True)
    spec = example_spec()
    # This fresh fixture is the only point pins are established. Reopen reuses
    # these exact values, not values obtained from the persisted candidate.
    pins = {'trusted_policy_digest': digest(spec), 'trusted_controller_digest': controller_digest()}
    with Store(directory / 'control') as store:
        controller = SyntheticBoundaryController(store, directory / 'candidate', **pins)
        controller.initialize(spec)
        controller.approve('approve', controller.state['revision'], pins['trusted_policy_digest'])
        adapter = MockAdapter(store.home, controller.root)
        (controller.root / 'approved-policy.json').write_text('{"verdict":"PASS"}')
        run = controller.prepare('incomplete', controller.state['revision'], 'A')
        controller.start('start-incomplete', controller.state['revision'], run['run_id'], adapter, 'incomplete')
        failed = controller.verify('verify-incomplete', controller.state['revision'], run['run_id'])
        for i in range(5):
            controller.advance(adapter, 'repair-' + str(i))
        final = controller.state['final_evidence']
        original = controller.policy_path.read_bytes()
        atomic_write(controller.policy_path, b'{}')
        try:
            controller.audit('tampered', controller.state['revision'])
        except NoriError:
            tamper_blocked = True
        else:
            tamper_blocked = False
        finally:
            atomic_write(controller.policy_path, original)
    with Store(directory / 'control') as store:
        reopened = SyntheticBoundaryController(store, directory / 'candidate', **pins)
        status = reopened.state['project_status']
    if failed['verdict'] != 'FAIL' or final['verdict'] != 'PASS' or not tamper_blocked or status != 'COMPLETED':
        raise NoriError('Synthetic boundary demonstration did not meet its assertions')
    return {'mode': 'synthetic-only', 'missing_content': failed['verdict'],
            'repaired_candidate': final['verdict'], 'policy_tamper_blocked': tamper_blocked,
            'reopened_status': status, 'policy_digest': pins['trusted_policy_digest'],
            'controller_digest': pins['trusted_controller_digest'],
            'limitations': ['No model or candidate code executed', 'Not an OS sandbox or same-UID security boundary']}
