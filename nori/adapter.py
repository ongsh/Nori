"""Deterministic, disk-backed simulator; no model, shell, network or PID guessing."""
import json
from .core import file_path
from .store import atomic_write, canonical, NoriError


class MockAdapter:
    def __init__(self, home, workspace):
        self.home = home / 'mock-receipts'
        self.root = workspace
        self.home.mkdir(parents=True, exist_ok=True)

    def _path(self, rid):
        if not rid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in rid):
            raise NoriError('Invalid run_id')
        return self.home / (rid + '.json')

    def reconcile(self, rid):
        path = self._path(rid)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            return None

    def start(self, run, mode):
        rid = run['run_id']
        previous = self.reconcile(rid)
        if self._path(rid).exists():
            if not previous or previous['identity'] != run['identity']:
                raise NoriError('Unreadable or mismatched receipt; do not restart')
            return previous
        if mode not in ('complete', 'incomplete', 'hold', 'copy'):
            raise NoriError('Unsupported mock scenario')
        receipt = {'run_id': rid, 'identity': run['identity'], 'phase': 'RUNNING',
                   'start_count': 1, 'claimed_result': 'DONE', 'exit_code': None}
        atomic_write(self._path(rid), canonical(receipt))  # Before any output side effect.
        if mode == 'hold':
            return receipt
        for name, text in run['task']['mock_outputs'].items():
            if name not in run['task']['write_scope']:
                raise NoriError('Mock output outside approved write scope')
            if mode == 'incomplete':
                text = text.splitlines(keepends=True)[0]
            if mode == 'copy':
                source = run['task']['read_scope'][0]
                text = 'summary:\n' + file_path(self.root, source).read_text()
            atomic_write(file_path(self.root, name), text.encode())
        receipt.update(phase='EXITED', exit_code=0)
        atomic_write(self._path(rid), canonical(receipt))
        return receipt

    def request_stop(self, rid):
        receipt = self.reconcile(rid)
        if receipt and receipt['phase'] != 'EXITED':
            receipt['phase'] = 'STOP_REQUESTED'
            atomic_write(self._path(rid), canonical(receipt))

    def confirm_stopped(self, rid):
        """Simulator-only: emulate a separately observed process exit, not a real kill."""
        receipt = self.reconcile(rid)
        if not receipt:
            raise NoriError('Cannot confirm an unknown execution')
        receipt.update(phase='EXITED', exit_code=130)
        atomic_write(self._path(rid), canonical(receipt))
