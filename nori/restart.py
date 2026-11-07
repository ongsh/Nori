"""Cooperative, local-only web restart protocol. Never signals executor processes.

The launcher remains the parent of each web process. A private socketpair carries
control messages, never a public endpoint or a detached shell. A child has its own
run.lock description, retained by its detached executor supervisors; closing the
web process's descriptor cannot hide a surviving executor.
"""
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

from . import __version__
from . import updater
from .store import Store

PROTOCOL = 1
START_TIMEOUT = 12
STOP_TIMEOUT = 8


def send(sock, value):
    raw = json.dumps(value).encode()
    if len(raw) > 16384:
        raise RuntimeError('Restart control message too large')
    sock.sendall(raw)


def receive(sock, timeout=None):
    sock.settimeout(timeout)
    raw = sock.recv(16385)
    if not raw:
        raise RuntimeError('Restart supervisor connection closed')
    if len(raw) > 16384:
        raise RuntimeError('Invalid restart control message')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError('Invalid restart control message shape')
    return value


def open_lease(shared=False):
    fd = os.open(updater.safe(updater.root() / 'run.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    if shared:
        fcntl.flock(fd, fcntl.LOCK_SH)
    return fd


def release_path(entry, *, protocol=False):
    path = updater.safe(updater.root() / 'releases' / (entry['sha256'] + '.pyz'))
    if path.stat().st_size > updater.MAX_BUNDLE or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        raise RuntimeError('Release integrity check failed; current installation retained.')
    if protocol:
        with zipfile.ZipFile(path) as archive:
            if 'nori/restart.py' not in archive.namelist():
                raise RuntimeError('This release needs a manual restart because it predates managed web restart.')
            if archive.read('NORI_VERSION').decode().strip() != entry['version']:
                raise RuntimeError('Release version metadata does not match the confirmed target.')
    return path


def hold_projects(stack, data_dir):
    """Read-only evidence check under project locks, retained through activation."""
    root = updater.safe(Path(data_dir))
    for project in sorted(root.iterdir()):
        if project.name.startswith('.'):
            continue
        updater.safe(project)
        control = project / 'control'
        if not control.exists():
            continue
        for child in (control, control / 'state.json', control / 'controller.lock'):
            updater.safe(child)
        store = stack.enter_context(Store(control))
        state = store.state
        if state is not None and state.get('workspace') != str(project / 'work'):
            raise RuntimeError('Restart blocked: project workspace is outside the app data directory.')
        if state is None or state.get('recovery_required') or state.get('lease') is not None:
            raise RuntimeError('Restart blocked: project ' + project.name + ' has an active or unreconciled run.')
        runs = state.get('runs')
        if not isinstance(runs, dict) or any(not isinstance(run, dict) for run in runs.values()):
            raise RuntimeError('Restart blocked: project run records are invalid.')
        if any(run.get('phase') not in ('EXITED', 'NOT_STARTED') for run in runs.values()):
            raise RuntimeError('Restart blocked: project ' + project.name + ' has unknown execution state.')
        # Do not instantiate adapters here: even observation must not create files.
        for receipts, pattern in ((control / 'mock-receipts', '*.json'), (control / 'process-receipts', '*/receipt.json')):
            if not receipts.exists():
                continue
            updater.safe(receipts)
            for path in receipts.glob(pattern):
                updater.safe(path)
                record = json.loads(path.read_text())
                if not isinstance(record, dict):
                    raise RuntimeError('Restart blocked: invalid executor receipt in ' + project.name)
                if record.get('phase') not in ('EXITED', 'NOT_STARTED'):
                    raise RuntimeError('Restart blocked: executor receipt is active or unknown in ' + project.name)
            if receipts.name == 'process-receipts':
                for item in receipts.iterdir():
                    updater.safe(item)
                    if item.is_dir() and not (item / 'receipt.json').is_file():
                        raise RuntimeError('Restart blocked: unreadable executor receipt in ' + project.name)


class Client:
    """Child-side control channel; not constructible from HTTP input."""
    def __init__(self, app):
        self.app = app
        self.socket = socket.socket(fileno=int(os.environ.pop('NORI_WEB_CONTROL_FD')))
        config = receive(self.socket, START_TIMEOUT)
        if config.get('protocol') != PROTOCOL:
            raise RuntimeError('Unsupported restart protocol')
        self.identity = config
        self.boot_lock = int(os.environ.pop('NORI_WEB_BOOT_LOCK_FD', '-1'))
        self.replies = queue.Queue()
        self.write_lock = threading.Lock()
        self.state = config.get('restart_state', 'idle')
        self.error = config.get('restart_error', '')
        self.available = True
        app.token = config['token']
        app.maintenance = bool(config['probation'])

    def write(self, value):
        with self.write_lock:
            send(self.socket, value)

    def start(self, server):
        self.server = server
        self.write({'event': 'ready', 'protocol': PROTOCOL, 'port': server.server_port,
                    'version': __version__, 'instance_id': self.app.instance_id,
                    'sha256': self.identity['sha256']})
        threading.Thread(target=self.listen, daemon=True).start()

    def listen(self):
        try:
            while True:
                message = receive(self.socket)
                command = message.get('command')
                if command == 'release':
                    fd = updater.inherited_run_fd()
                    if fd is None:
                        raise RuntimeError('Missing web process lifecycle lock')
                    os.close(fd)
                    os.environ.pop('NORI_RUN_LOCK_FD', None)
                    self.write({'event': 'released'})
                elif command in ('resume', 'refused'):
                    # Parent released exclusive lifecycle lock before this message.
                    if command == 'resume':
                        fd = open_lease(shared=True)
                        os.environ['NORI_RUN_LOCK_FD'] = str(fd)
                    self.app.maintenance = False
                    self.state, self.error = 'blocked', message['error']
                    self.write({'event': 'resumed'})
                    self.replies.put(message)
                elif command == 'accepted':
                    self.state, self.error = 'restarting', ''
                    self.replies.put(message)
                elif command == 'activate':
                    fd = updater.inherited_run_fd()
                    if fd is None:
                        raise RuntimeError('Missing candidate lifecycle descriptor')
                    fcntl.flock(fd, fcntl.LOCK_SH)
                    if self.boot_lock >= 0:
                        os.close(self.boot_lock)
                        self.boot_lock = -1
                    # Remain read-only until parent receives this lease ACK.
                    self.write({'event': 'activated'})
                elif command == 'commit':
                    # Validate/finish startup while mutations remain fenced.
                    self.write({'event': 'committed'})
                elif command == 'serve':
                    self.state = message.get('state', 'completed')
                    self.error = message.get('error', '')
                    self.app.maintenance = False
                elif command == 'stop':
                    self.app.maintenance = True
                    self.server.shutdown()
                    return
                else:
                    raise RuntimeError('Invalid restart supervisor command')
        except (OSError, ValueError, RuntimeError) as exc:
            self.available = False
            self.state = 'uncertain'
            self.error = str(exc)
            # No parent: no more work may begin with an uncertain lifecycle lease.
            self.app.maintenance = True
            self.replies.put({'command': 'failed', 'error': self.error})

    def request(self, token):
        if not self.available:
            raise RuntimeError('Restart supervisor is unavailable; close and reopen Nori.')
        self.app.maintenance = True
        self.state = 'restarting'
        self.write({'event': 'restart', 'token': token})
        try:
            result = self.replies.get(timeout=START_TIMEOUT + STOP_TIMEOUT)
        except queue.Empty as exc:
            self.state = 'uncertain'
            raise RuntimeError('Restart admission is uncertain; no new work can start. Reopen the update panel.') from exc
        if result.get('command') != 'accepted':
            raise RuntimeError(result.get('error', 'Restart refused'))
        return {'restarting': True, 'restart_state': 'restarting',
                'target_version': result['version'], 'target_sha256': result['sha256']}


def attach(app):
    if os.environ.get('NORI_WEB_CONTROL_FD'):
        app.restart_client = Client(app)


def status(app):
    client = app.restart_client
    supported = bool(client and client.available)
    result = dict(restart_supported=supported, instance_id=app.instance_id,
                  running_sha256=client.identity['sha256'] if client else None,
                  restart_state=client.state if client else 'idle',
                  restart_error=client.error if client else '',
                  restart_block_reason='' if supported else '此会话使用旧启动器或源码模式。已安装版本暂存更新后，请仅手动关闭并重新打开一次 Nori，以启用此后的网页安全重启。')
    if client and not client.available:
        result['restart_block_reason'] = '安全重启管理进程已断开，工作台暂时不能修改数据或开始任务。请关闭此会话后重新打开 Nori。'
    if any(job.get('running') for job in app.jobs.values()):
        result['restart_block_reason'] = '仍有任务运行中，不能重启。请等待任务退出并确认执行状态。'
    elif supported and not app.maintenance:
        try:
            with ExitStack() as stack:
                hold_projects(stack, app.root)
        except (OSError, ValueError, RuntimeError) as exc:
            result['restart_block_reason'] = str(exc)
    return result


class Supervisor:
    def __init__(self, args):
        self.args = args
        self.args.data_dir = str(Path(args.data_dir).resolve())
        self.token = secrets.token_urlsafe(32)
        self.port = args.port
        self.child = None
        self.channel = None
        self.run_fd = open_lease()
        self.stop = False

    def spawn(self, entry, *, probation=False, state='idle', error=''):
        path = release_path(entry, protocol=True)
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        # Separate from the supervisor's lock description: a surviving executor
        # keeps this shared lease and correctly prevents an exclusive restart.
        lease = open_lease()
        # During transition the supervisor owns EX. The child receives its own
        # unlocked descriptor and adopts a SH lease only after health succeeds.
        if not probation:
            fcntl.flock(lease, fcntl.LOCK_SH)
        env = dict(os.environ, NORI_WEB_CONTROL_FD=str(child.fileno()), NORI_RUN_LOCK_FD=str(lease))
        argv = [sys.executable, str(path), 'web', '--port', str(self.port),
                '--data-dir', self.args.data_dir, '--no-browser']
        descriptors = (child.fileno(), lease)
        if probation:
            # A crashed parent must not leave a probationary child unleased.
            # The inherited EX description survives until activation or exit.
            env['NORI_WEB_BOOT_LOCK_FD'] = str(self.run_fd)
            descriptors += (self.run_fd,)
        try:
            self.child = subprocess.Popen(argv, env=env, pass_fds=descriptors)
        finally:
            child.close()
            os.close(lease)
        self.channel = parent
        send(parent, dict(protocol=PROTOCOL, token=self.token, sha256=entry['sha256'],
                          probation=probation, restart_state=state, restart_error=error))
        ready = receive(parent, START_TIMEOUT)
        if (ready.get('event') != 'ready' or ready.get('protocol') != PROTOCOL
                or ready.get('version') != entry['version'] or ready.get('sha256') != entry['sha256']
                or type(ready.get('port')) is not int or not 1 <= ready['port'] <= 65535
                or not isinstance(ready.get('instance_id'), str) or len(ready['instance_id']) != 32
                or any(c not in '0123456789abcdef' for c in ready['instance_id'])):
            raise RuntimeError('Candidate startup identity check failed')
        if self.port and ready['port'] != self.port:
            raise RuntimeError('Candidate changed the listening port')
        self.port = ready['port']
        self.identity = entry, ready
        self.check_health()

    def check_health(self):
        entry, ready = self.identity
        url = 'http://127.0.0.1:' + str(self.port) + '/api/health'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), updater.NoRedirect())
        deadline = time.monotonic() + START_TIMEOUT
        while True:
            if self.child.poll() is not None:
                raise RuntimeError('Candidate exited before startup health succeeded')
            try:
                request = urllib.request.Request(url, headers={'X-Nori-Token': self.token})
                with opener.open(request, timeout=.5) as response:
                    health = json.loads(response.read(4096))
                if health == {'instance_id': ready['instance_id'], 'version': entry['version'], 'sha256': entry['sha256']}:
                    return
            except (OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError('Candidate startup health timed out')
            time.sleep(.05)

    def stop_child(self):
        if self.child is None or self.child.poll() is not None:
            return True
        try:
            send(self.channel, {'command': 'stop'})
        except OSError:
            pass
        try:
            self.child.wait(timeout=STOP_TIMEOUT)
            return True
        except subprocess.TimeoutExpired:
            return False  # Never kill an unresponsive process or compete for its port.

    def activate(self, state, error='', data=None):
        # Parent still owns EX until no more rollback state needs writing.
        fcntl.flock(self.run_fd, fcntl.LOCK_SH)
        send(self.channel, {'command': 'activate', 'state': state, 'error': error})
        message = receive(self.channel, START_TIMEOUT)
        if message.get('event') != 'activated':
            raise RuntimeError('Candidate activation handshake failed')
        send(self.channel, {'command': 'commit'})
        if receive(self.channel, START_TIMEOUT).get('event') != 'committed':
            raise RuntimeError('Candidate final startup acknowledgment failed')
        self.check_health()
        if data is not None:
            updater.save(data)
        fcntl.flock(self.run_fd, fcntl.LOCK_UN)
        send(self.channel, {'command': 'serve', 'state': state, 'error': error})

    def restart(self, token):
        released = False
        accepted = False
        old = None
        with ExitStack() as stack:
            try:
                stack.enter_context(updater.locked('launch.lock'))
                hold_projects(stack, self.args.data_dir)
                send(self.channel, {'command': 'release'})
                if receive(self.channel, START_TIMEOUT).get('event') != 'released':
                    raise RuntimeError('Web process did not release its lifecycle lease')
                released = True
                try:
                    fcntl.flock(self.run_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError('其他 Nori 进程或执行器仍在运行；请等待它们正常退出后再重启。') from exc
                stack.enter_context(updater.locked())
                data = updater.state()
                if not token or token != updater.state_token(data):
                    raise RuntimeError('安装状态已改变，请重新确认待生效版本。')
                if not data.get('pending'):
                    raise RuntimeError('No staged release is available to restart into.')
                old = dict(data)
                target = data['pending']
                release_path(target, protocol=True)
                release_path(data['current'], protocol=True)
                send(self.channel, {'command': 'accepted', **target})
                accepted = True
                # The old child remains alive until its HTTP response is flushed;
                # it then tells the parent it is safe to stop the server.
                if receive(self.channel, START_TIMEOUT).get('event') != 'response_sent':
                    raise RuntimeError('Restart HTTP response was not acknowledged')
                if not self.stop_child():
                    raise RuntimeError('Old web process did not exit; restart stopped without killing it.')
                self.channel.close()
                try:
                    self.spawn(target, probation=True, state='restarting')
                except (OSError, ValueError, RuntimeError) as failure:
                    if not self.stop_child():
                        raise RuntimeError('Candidate is unresponsive; no process was killed and automatic rollback is blocked.') from failure
                    self.channel.close()
                    # The authoritative current/previous entries were never changed.
                    data['pending'] = None
                    updater.save(data)
                    self.spawn(data['current'], probation=True, state='rolled_back', error=str(failure))
                    self.activate('rolled_back', str(failure))
                    return
                data['previous'], data['current'], data['pending'] = data['current'], target, None
                self.activate('completed', data=data)
            except (OSError, ValueError, RuntimeError) as exc:
                if not accepted:
                    # Retain a shared lease across handoff, including admission
                    # failure, so no launcher/uninstall can enter a lease gap.
                    fcntl.flock(self.run_fd, fcntl.LOCK_SH)
                    send(self.channel, {'command': 'resume' if released else 'refused', 'error': str(exc)})
                    if receive(self.channel, START_TIMEOUT).get('event') != 'resumed':
                        raise RuntimeError('Web process did not resume its lifecycle lease')
                    fcntl.flock(self.run_fd, fcntl.LOCK_UN)
                    return
                print('Nori restart blocked: ' + str(exc), file=sys.stderr, flush=True)
                # Accepted failures after the old child exits are not called success.
                # Retain the child and wait; never kill it or launch a competing server.
                # Keep all barriers until an unknown child exits on its own.
                if self.child and self.child.poll() is None and not self.stop_child():
                    self.child.wait()
                try:
                    # A failed activation may have downgraded EX to SH.
                    fcntl.flock(self.run_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.channel.close()
                    old['pending'] = None
                    updater.save(old)
                    self.spawn(old['current'], probation=True, state='rolled_back', error=str(exc))
                    self.activate('rolled_back', str(exc))
                except (OSError, ValueError, RuntimeError) as rollback_error:
                    print('Nori automatic rollback could not start: ' + str(rollback_error), file=sys.stderr, flush=True)
                    self.stop = True
                    if self.child and self.child.poll() is None:
                        self.child.wait()

    def run(self):
        original = updater.inherited_run_fd()
        with updater.locked():
            entry = updater.state()['current']
        try:
            self.spawn(entry)
            if original is not None:
                os.close(original)
                os.environ.pop('NORI_RUN_LOCK_FD', None)
            if not self.args.no_browser:
                import webbrowser
                webbrowser.open(f'http://127.0.0.1:{self.port}/#token={self.token}')
            while not self.stop and self.child.poll() is None:
                try:
                    event = receive(self.channel, .25)
                except socket.timeout:
                    continue
                if event.get('event') == 'restart':
                    self.restart(event.get('token'))
                else:
                    raise RuntimeError('Unexpected web supervisor message')
        finally:
            # Normal Ctrl-C/terminal closure asks only our idle web child to close.
            # It never kills or waits for detached business executors.
            if self.child and self.child.poll() is None:
                self.stop_child()
            if self.channel:
                self.channel.close()
            os.close(self.run_fd)
        return self.child.returncode if self.child and self.child.returncode is not None else 2


def supervise(argv):
    from .__main__ import parser
    args = parser().parse_args(argv or ['web'])
    supervisor = Supervisor(args)
    def stopping(signum, frame):
        supervisor.stop = True
    signal.signal(signal.SIGTERM, stopping)
    try:
        return supervisor.run()
    except KeyboardInterrupt:
        return 0
