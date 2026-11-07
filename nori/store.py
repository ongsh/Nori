"""Single-writer durable JSON. POSIX-tested; not a hostile-user security boundary."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading


class NoriError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name == 'posix':
            d = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Store:
    def __init__(self, home, recover=False):
        self.mutex = threading.RLock()
        if os.name != 'posix':
            raise NoriError('P1 currently requires POSIX locks; Windows is unverified')
        import fcntl
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.lock = open(self.home / 'controller.lock', 'a+b')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            raise NoriError('Another controller holds this project lock') from exc
        self.path = self.home / 'state.json'
        self.state = None
        self.poisoned = False
        self.workspace_lock = None
        try:
            prior_artifacts = any(p.name != 'controller.lock' for p in self.home.iterdir())
            if self.path.exists() or prior_artifacts:
                try:
                    self.state = self.decode(self.path.read_bytes())
                except (OSError, ValueError, KeyError, TypeError, NoriError) as exc:
                    if not recover:
                        raise NoriError('Invalid state; inspect backup and use explicit recovery') from exc
                    self.state = self.decode((self.home / 'state.previous.json').read_bytes())
                    self.state['project_status'] = 'AWAITING_DECISION'
                    self.state['recovery_required'] = True
                    self.state['revision'] += 1
                    self.state['history'].append({'event': 'backup_restored_dispatch_blocked'})
                    atomic_write(self.path, self.encode(self.state))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def encode(state):
        return canonical({'sha256': digest(state), 'state': state})

    @staticmethod
    def decode(raw):
        obj = json.loads(raw)
        state = obj['state']
        if obj['sha256'] != digest(state) or state['schema_version'] != 1:
            raise NoriError('State checksum/schema mismatch')
        if not isinstance(state['revision'], int) or state['revision'] < 0:
            raise NoriError('Invalid revision')
        if state['approval'] is not None and state['approval'] != digest(state['spec']):
            raise NoriError('Approved requirement/plan was modified')
        lease = state['lease']
        if lease is not None and lease not in state['runs']:
            raise NoriError('Invalid lease')
        for tid, task in state['tasks'].items():
            if task['run_id'] is not None and task['run_id'] not in state['runs']:
                raise NoriError('Invalid task run')
            if task['status'] == 'DONE' and not task.get('evidence'):
                raise NoriError('DONE without evidence')
        return state

    def save(self, state):
        if self.lock.closed:
            raise NoriError('Store is closed')
        if self.poisoned:
            raise NoriError('Prior persistence failed; reopen and reconcile before continuing')
        self.decode(self.encode(state))
        try:
            if self.path.exists():
                previous = self.path.read_bytes()
                self.decode(previous)
                atomic_write(self.home / 'state.previous.json', previous)
            atomic_write(self.path, self.encode(state))
        except BaseException:
            self.poisoned = True
            raise
        self.state = copy.deepcopy(state)
        try:
            self.views()
        except OSError:
            pass  # Derived artifacts never roll back a committed state.

    def views(self):
        s = self.state
        text = '# Nori progress\n\n' + s['project_status'] + '\n\n'
        text += '\n'.join(f"- {k}: {v['status']} (attempts: {v['attempts']})" for k, v in s['tasks'].items())
        atomic_write(self.home / 'progress.md', text.encode())
        atomic_write(self.home / 'events.jsonl', b'\n'.join(canonical(x) for x in s['history']) + b'\n')

    def lock_workspace(self, root):
        import fcntl
        if self.workspace_lock is not None:
            raise NoriError('Store already has a workspace controller')
        path = root / '.nori.lock'
        if path.is_symlink():
            raise NoriError('Workspace lock cannot be a symlink')
        lock = open(path, 'a+b')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise NoriError('Another controller holds this workspace') from exc
        try:
            lock.seek(0)
            bound = lock.read()
            if bound and bound.decode() != str(self.home):
                raise NoriError('Workspace is bound to a different control home')
            if not bound:
                lock.write(str(self.home).encode())
                lock.flush()
                os.fsync(lock.fileno())
        except BaseException:
            lock.close()
            raise
        self.workspace_lock = lock

    def close(self):
        with self.mutex:
            if self.workspace_lock is not None:
                self.workspace_lock.close()
            if not self.lock.closed:
                self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
