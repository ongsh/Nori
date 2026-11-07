"""Linux supervisor. Receipts are evidence, never permission to relaunch.

Trusted same-account processes only: process groups do not contain a malicious
child that deliberately calls setsid(). Use an OS container for hostile code.
"""
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .store import NoriError, atomic_write, canonical


def _positive_limit(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NoriError(f'{name} must be a finite positive number')
    try:
        valid = math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise NoriError(f'{name} must be a finite positive number')
    return value


def process_identity(pid):
    try:
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': int(pid), 'start_ticks': fields[19],
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'pgid': int(fields[2]), 'session': int(fields[3])}
    except (OSError, ValueError, IndexError):
        return None


def _group_members(pgid):
    """Fail closed if proc enumeration is unavailable."""
    members = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            ident = process_identity(int(entry.name))
            if ident and ident['pgid'] == pgid:
                members.append(ident)
    return members


class ProcessAdapter:
    name = 'process'

    def __init__(self, home, workspace, *, stop_grace=2.0):
        self.home = Path(home).resolve() / 'process-receipts'
        self.root = Path(workspace).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.stop_grace = _positive_limit(stop_grace, 'stop_grace')
        self._supervisors = {}

    def _directory(self, rid):
        if not isinstance(rid, str) or not rid or any(c not in
                'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in rid):
            raise NoriError('Invalid run_id')
        p = self.home / rid
        if p.is_symlink():
            raise NoriError('Receipt symlink refused')
        return p

    def probe(self):
        supported = sys.platform.startswith('linux') and process_identity(os.getpid()) is not None
        return {'adapter': self.name, 'status': 'available' if supported else 'unsupported_platform',
                'process_identity': 'linux boot_id + pid + start_ticks',
                'containment': 'process-group, not hostile-code isolation'}

    def start_argv(self, run, argv, prompt=''):
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or '\0' in x for x in argv):
            raise NoriError('Executable argv list required')
        directory = self._directory(run['run_id'])
        if directory.exists():
            old = self.reconcile(run['run_id'])
            if old and old.get('identity') == run['identity']:
                return old
            raise NoriError('Existing/unknown receipt must not be relaunched')
        task = run.get('task', {})
        limits = {'max_seconds': _positive_limit(task.get('max_seconds', 600), 'max_seconds'),
                  'max_output_bytes': _positive_limit(task.get('max_output_bytes', 8 * 1024 * 1024), 'max_output_bytes')}
        if self.probe()['status'] != 'available':
            raise NoriError('Adapter is not available; inspect probe')
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            old = self.reconcile(run['run_id'])
            if old and old.get('identity') == run['identity']:
                return old
            raise NoriError('Existing/unknown receipt must not be relaunched')
        fd = os.open(self.home, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        receipt = {'run_id': run['run_id'], 'identity': run['identity'], 'adapter': self.name,
                   'phase': 'UNKNOWN', 'start_count': 1, 'argv': argv, 'cwd': str(self.root),
                   'stop_grace': self.stop_grace, 'exit_code': None, 'claimed_result': None,
                   'task_id': run.get('task_id'), 'run': run, 'limits': limits}
        atomic_write(directory / 'receipt.json', canonical(receipt))
        atomic_write(directory / 'stdin.txt', prompt.encode())
        # Absolute trusted package path, independent of the business workspace.
        package_root = str(Path(__file__).resolve().parent.parent)
        launch = [sys.executable, '-c',
                  'import sys; sys.path.insert(0, sys.argv[1]); '
                  'from nori.process_adapter import supervise; supervise(sys.argv[2])',
                  package_root, str(directory)]
        with open(directory / 'supervisor.log', 'ab') as log:
            child = subprocess.Popen(launch, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, close_fds=True)
        self._supervisors[run['run_id']] = child
        return self.reconcile(run['run_id'])

    def reconcile(self, rid):
        child = self._supervisors.get(rid)
        if child is not None:
            child.poll()  # Reap local supervisor; receipt remains source of evidence.
        p = self._directory(rid) / 'receipt.json'
        try:
            receipt = json.loads(p.read_text())
        except (OSError, ValueError):
            return None
        if (not isinstance(receipt, dict) or receipt.get('run_id') != rid
                or not isinstance(receipt.get('identity'), str)
                or receipt.get('phase') not in ('UNKNOWN', 'RUNNING', 'STOP_REQUESTED', 'EXITED')):
            return None
        if receipt.get('phase') == 'EXITED' and child is not None:
            try:
                child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        if receipt.get('phase') != 'EXITED':
            ident = receipt.get('supervisor')
            if not isinstance(ident, dict) or 'pid' not in ident or process_identity(ident['pid']) != ident:
                receipt['phase'] = 'UNKNOWN'
                receipt['reason'] = 'Supervisor identity absent or no longer matches; no automatic relaunch'
        return receipt

    def inventory(self):
        result = []
        for entry in sorted(self.home.iterdir()):
            if entry.is_dir():
                observation = self.reconcile(entry.name)
                result.append(observation or {'run_id': entry.name, 'phase': 'UNKNOWN',
                                               'identity': None, 'reason': 'Unreadable receipt'})
        return result

    observe = reconcile

    def request_stop(self, rid):
        receipt = self.reconcile(rid)
        if not receipt:
            raise NoriError('Cannot stop unknown receipt')
        if receipt['phase'] != 'EXITED':
            atomic_write(self._directory(rid) / 'stop.json', canonical({'identity': receipt['identity']}))
        return self.reconcile(rid)  # Request is NOT exit confirmation.

    def collect(self, rid):
        receipt = self.reconcile(rid)
        if not receipt or receipt['phase'] != 'EXITED':
            raise NoriError('Run exit has not been confirmed')
        directory = self._directory(rid)
        events, invalid = [], 0
        with open(directory / 'stdout.jsonl', errors='replace') as stream:
            for line in stream:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    invalid += 1
        return {'receipt': receipt, 'events': events, 'invalid_json_lines': invalid,
                'stdout_path': str(directory / 'stdout.jsonl'),
                'stderr_path': str(directory / 'stderr.log')}


def supervise(directory):
    """Only entry point allowed to write live/exit receipts and signal children."""
    directory = Path(directory)
    receipt = json.loads((directory / 'receipt.json').read_text())
    limits = receipt.get('limits', {'max_seconds': 600, 'max_output_bytes': 8 * 1024 * 1024})
    for name in ('max_seconds', 'max_output_bytes'):
        _positive_limit(limits[name], name)
    _positive_limit(receipt['stop_grace'], 'stop_grace')
    receipt['supervisor'] = process_identity(os.getpid())
    atomic_write(directory / 'receipt.json', canonical(receipt))
    with open(directory / 'stdin.txt', 'rb') as stdin, open(directory / 'stdout.jsonl', 'wb') as out, open(directory / 'stderr.log', 'wb') as err:
        started = time.monotonic()
        try:
            process = subprocess.Popen(receipt['argv'], cwd=receipt['cwd'], stdin=stdin,
                                       stdout=out, stderr=err, start_new_session=True, close_fds=True)
        except OSError as exc:
            receipt.update(phase='EXITED', exit_code=127, launch_error=str(exc), exit_confirmed=True)
            atomic_write(directory / 'receipt.json', canonical(receipt))
            return
        receipt.update(phase='RUNNING', process=process_identity(process.pid), pgid=process.pid)
        atomic_write(directory / 'receipt.json', canonical(receipt))
        stop_at = None
        while True:
            code = process.poll()
            members = _group_members(process.pid)
            elapsed = time.monotonic() - started
            output_bytes = os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size
            reason = ('max_seconds' if elapsed >= limits['max_seconds'] else
                      'max_output_bytes' if output_bytes >= limits['max_output_bytes'] else None)
            if reason and 'budget_reason' not in receipt:
                receipt.update(budget_reason=reason, budget_elapsed_seconds=elapsed,
                               budget_output_bytes=output_bytes)
            requested = reason is not None or stop_at is not None
            if (directory / 'stop.json').exists():
                request = json.loads((directory / 'stop.json').read_text())
                requested = requested or request.get('identity') == receipt['identity']
            if requested and stop_at is None:
                stop_at = time.monotonic()
                receipt['phase'] = 'STOP_REQUESTED'
                atomic_write(directory / 'receipt.json', canonical(receipt))
            if code is not None and not members:
                break
            # Members must still belong to original child session. Never signal a reused group.
            if stop_at is not None and members and all(m['session'] == process.pid for m in members):
                sig = signal.SIGTERM if time.monotonic() - stop_at < receipt['stop_grace'] else signal.SIGKILL
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
            time.sleep(0.05)
        out.flush(); err.flush()
        os.fsync(out.fileno()); os.fsync(err.fileno())
    receipt.update(phase='EXITED', exit_code=code, exit_confirmed=True,
                   stop_requested=stop_at is not None, group_empty=True,
                   elapsed_seconds=time.monotonic() - started,
                   output_bytes=(directory / 'stdout.jsonl').stat().st_size + (directory / 'stderr.log').stat().st_size)
    atomic_write(directory / 'receipt.json', canonical(receipt))
