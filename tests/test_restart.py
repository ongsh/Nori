"""Disposable HOME, local loopback servers and synthetic immutable releases only."""
import fcntl
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

from nori import __version__, updater
from nori.distribution import install, MARKER
from nori.store import Store
from scripts.build_distribution import build


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux lifecycle locks required')
class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, HOME=str(self.home))
        self.env.start(); self.addCleanup(self.env.stop)
        self.archive = build(self.home / 'download/nori.pyz')
        install(self.archive)
        self.launcher = self.home / '.local/bin/nori'
        self.data = self.home / 'synthetic-data'
        self.process = None
        self.log = open(self.home / 'stderr.log', 'w+')
        self.addCleanup(self.log.close)
        self.addCleanup(self.close)
        self.start()

    def start(self):
        self.process = subprocess.Popen([str(self.launcher), 'web', '--port', '0', '--no-browser', '--data-dir', str(self.data)],
                                        stdout=subprocess.PIPE, stderr=self.log, text=True)
        ready, _, _ = select.select([self.process.stdout], [], [], 15)
        self.assertTrue(ready, 'No supervised server URL')
        line = self.process.stdout.readline()
        match = re.search(r'(http://127\.0\.0\.1:\d+)/#token=(\S+)', line)
        if not match:
            self.log.seek(0)
            self.fail('No URL: ' + line + self.log.read())
        self.origin, self.token = match.groups()
        self.wait_status(lambda result: result['restart_supported'])

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()  # Our test-owned parent: graceful handler, not executors.
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.fail('Synthetic supervised server failed to exit cooperatively')
        if self.process and self.process.stdout:
            self.process.stdout.close()

    def request(self, body=None, path='/api/update', headers=None):
        fields = {'X-Nori-Token': self.token, 'Origin': self.origin, 'Content-Type': 'application/json'}
        fields.update(headers or {})
        request = urllib.request.Request(self.origin + path, data=json.dumps(body).encode() if body is not None else None, headers=fields)
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def wait_status(self, predicate):
        deadline = time.monotonic() + 20
        last = None
        while time.monotonic() < deadline:
            try:
                code, last = self.request()
                if code == 200 and predicate(last):
                    return last
            except (OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(.05)
        self.log.flush(); self.log.seek(0)
        self.fail('No expected status: ' + str(last) + '\n' + self.log.read())

    def stage(self, *, broken=False, missing_protocol=False, wrong_health=False, malformed_ready=False, commit_crash=False):
        output = io.BytesIO()
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(output, 'w') as target:
            for name in source.namelist():
                content = source.read(name)
                if name == 'nori/version.py': content = b"VERSION = '9.0.0-test'\n"
                if name == 'NORI_VERSION': content = b'9.0.0-test\n'
                if name == 'nori/web.py' and broken: content = b"raise RuntimeError('synthetic startup failure')\n"
                if name == 'nori/web.py' and wrong_health:
                    content = content.replace(b"'version': __version__,", b"'version': 'invalid-health',")
                if name == 'nori/restart.py' and malformed_ready:
                    content = content.replace(b"'port': server.server_port,", b"'missing_port': server.server_port,")
                if name == 'nori/restart.py' and commit_crash:
                    content = content.replace(b"elif command == 'commit':", b"elif command == 'commit':\n                    os._exit(2)")
                if name == 'nori/restart.py' and missing_protocol: continue
                target.writestr(name, content)
        raw = output.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        with updater.locked():
            updater.atomic(updater.root() / 'releases' / (digest + '.pyz'), raw)
            state = updater.state()
            state['pending'] = {'version': '9.0.0-test', 'sha256': digest}
            updater.save(state)
        return self.request()[1]

    def restart(self, status):
        return self.request({'action': 'restart', 'confirmed': True, 'restart_token': status['restart_token']})

    def test_same_port_auth_identity_data_and_second_restart(self):
        code, created = self.request({'action': 'create', 'title': 'Synthetic preserved map'}, '/api/map')
        self.assertEqual(code, 200, created)
        files = {str(path.relative_to(self.data)): path.read_bytes() for path in self.data.rglob('*') if path.is_file()}
        before = self.stage()
        self.assertEqual(self.restart(before)[0], 200)
        after = self.wait_status(lambda result: result['restart_state'] == 'completed')
        self.assertEqual(after['current_version'], '9.0.0-test')
        self.assertEqual(after['running_sha256'], before['pending_sha256'])
        self.assertNotEqual(after['instance_id'], before['instance_id'])
        self.assertEqual(self.request(headers={'X-Nori-Token': 'wrong'})[0], 403)
        for name, content in files.items(): self.assertEqual((self.data / name).read_bytes(), content)
        # The replacement can itself request a later restart through the same parent.
        code, pending = self.request({'action': 'rollback', 'confirmed': True, 'rollback_token': after['rollback_token']})
        self.assertEqual(code, 200, pending)
        self.assertEqual(self.restart(pending)[0], 200)
        rolled = self.wait_status(lambda result: result['current_version'] == __version__ and result['restart_state'] == 'completed')
        self.assertNotEqual(rolled['instance_id'], after['instance_id'])

    def test_failed_candidate_rolls_back_without_changing_project_bytes(self):
        self.data.joinpath('keep.txt').write_text('preserve byte for byte')
        before = self.stage(broken=True)
        old = updater.state()['current']
        self.assertEqual(self.restart(before)[0], 200)
        after = self.wait_status(lambda result: result['restart_state'] == 'rolled_back')
        self.assertEqual(after['current_version'], __version__)
        self.assertEqual(after['running_sha256'], old['sha256'])
        self.assertNotEqual(after['instance_id'], before['instance_id'])
        self.assertFalse(after['pending'])
        self.assertTrue(after['restart_error'])
        self.assertEqual(self.data.joinpath('keep.txt').read_text(), 'preserve byte for byte')

    def assert_startup_protocol_failure_rolls_back(self, **fault):
        sentinel = self.data / 'preserved.txt'
        sentinel.write_bytes(b'Preserve project bytes across rejected startup.')
        before = self.stage(**fault)
        original = updater.state()
        self.assertEqual(self.restart(before)[0], 200)
        after = self.wait_status(lambda result: result['restart_state'] == 'rolled_back')
        self.assertEqual(after['current_version'], __version__)
        self.assertEqual(after['running_sha256'], original['current']['sha256'])
        self.assertNotEqual(after['instance_id'], before['instance_id'])
        actual = updater.state()
        self.assertEqual(actual['current'], original['current'])
        self.assertEqual(actual['previous'], original['previous'])
        self.assertIsNone(actual['pending'])
        self.assertEqual(sentinel.read_bytes(), b'Preserve project bytes across rejected startup.')
        self.assertTrue(after['restart_error'])

    def test_malformed_candidate_ready_rolls_back(self):
        self.assert_startup_protocol_failure_rolls_back(malformed_ready=True)

    def test_candidate_commit_crash_rolls_back_before_current_is_changed(self):
        self.assert_startup_protocol_failure_rolls_back(commit_crash=True)

    def test_control_protocol_rejects_non_object_frames(self):
        import socket
        from nori.restart import receive, send
        first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            for value in (None, [], 'invalid', 1):
                send(first, value)
                with self.assertRaisesRegex(RuntimeError, 'shape'):
                    receive(second, 1)
        finally:
            first.close(); second.close()

    def test_failed_http_health_stays_read_only_then_rolls_back(self):
        original_maps = self.request(path='/api/maps')[1]['maps']
        before = self.stage(wrong_health=True)
        self.assertEqual(self.restart(before)[0], 200)
        # A candidate can listen before it passes authenticated startup health;
        # none of its mutation routes may be usable during that probation.
        deadline = time.monotonic() + 8
        blocked = False
        while time.monotonic() < deadline:
            try:
                code, _ = self.request({'action':'create', 'title':'Must not save'}, '/api/map')
                if code == 409:
                    blocked = True
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.05)
        self.assertTrue(blocked)
        after = self.wait_status(lambda result: result['restart_state'] == 'rolled_back')
        self.assertEqual(after['current_version'], __version__)
        self.assertIn('health', after['restart_error'])
        self.assertEqual(self.request(path='/api/maps')[1]['maps'], original_maps)

    def test_another_lifecycle_holder_blocks_without_stopping_server(self):
        status = self.stage()
        fd = os.open(updater.root() / 'run.lock', os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            code, error = self.restart(status)
            self.assertEqual(code, 400, error)
            self.assertIn('其他 Nori', error['error'])
            after = self.request()[1]
            self.assertEqual(after['instance_id'], status['instance_id'])
            self.assertEqual(after['current_version'], __version__)
            self.assertTrue(after['pending'])
            self.assertEqual(self.request({'action': 'create', 'title': 'Still usable'}, '/api/map')[0], 200)
        finally:
            os.close(fd)
        self.assertEqual(self.restart(self.request()[1])[0], 200)
        self.wait_status(lambda result: result['restart_state'] == 'completed')

    def test_stale_token_auth_and_missing_confirmation_cannot_restart(self):
        status = self.stage()
        for confirmed in (None, False, 1, 'true'):
            self.assertEqual(self.request({'action': 'restart', 'confirmed': confirmed, 'restart_token': status['restart_token']})[0], 400)
        self.assertEqual(self.request({'action': 'restart', 'confirmed': True, 'restart_token': 'stale'})[0], 400)
        self.assertEqual(self.request({'action': 'restart', 'confirmed': True, 'restart_token': status['restart_token']}, headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.request()[1]['instance_id'], status['instance_id'])
        self.assertEqual(self.request({'action': 'create', 'title': 'Not frozen'}, '/api/map')[0], 200)

    def test_active_unknown_project_and_receipt_block_without_data_changes(self):
        self.assertEqual(self.request({'project': 'synthetic', 'action': 'create'}, '/api/command')[0], 200)
        status = self.stage()
        control = self.data / 'synthetic/control'
        with Store(control) as store:
            state = store.state
            state['recovery_required'] = True
            store.save(state)
        before = (control / 'state.json').read_bytes()
        self.assertEqual(self.restart(status)[0], 400)
        self.assertEqual((control / 'state.json').read_bytes(), before)
        self.assertEqual(self.request()[1]['instance_id'], status['instance_id'])
        with Store(control) as store:
            state = store.state
            state['recovery_required'] = False
            store.save(state)
        receipts = control / 'process-receipts/unknown'
        receipts.mkdir(parents=True)
        (receipts / 'receipt.json').write_text('{"phase":"UNKNOWN"}')
        self.assertEqual(self.restart(self.request()[1])[0], 400)
        self.assertEqual(self.request({'action': 'create', 'title': 'Not frozen'}, '/api/map')[0], 200)

    def test_unknown_target_protocol_refused_before_old_server_stops(self):
        status = self.stage(missing_protocol=True)
        code, error = self.restart(status)
        self.assertEqual(code, 400)
        self.assertIn('manual restart', error['error'])
        self.assertEqual(self.request()[1]['instance_id'], status['instance_id'])

    def test_source_checkout_has_no_restart_capability(self):
        from nori.web import WebApp
        app = WebApp(self.home / 'source-data')
        self.assertFalse(app.update_status()['restart_supported'])
        with self.assertRaises(RuntimeError):
            app.update_action({'action': 'restart', 'confirmed': True})

    def test_detached_synthetic_executor_blocks_until_its_natural_exit(self):
        ready, stop, done = (self.home / name for name in ('executor-ready', 'executor-stop', 'executor-done'))
        script = r"""
import fcntl, os, pathlib, subprocess, sys
root = pathlib.Path.home()
fd = os.open(root / '.local/lib/nori/run.lock', os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_SH)
code = "import pathlib,time; p=pathlib.Path.home(); (p/'executor-ready').touch(); exec(\"while not (p/'executor-stop').exists(): time.sleep(.02)\"); (p/'executor-done').touch()"
subprocess.Popen([sys.executable, '-c', code], pass_fds=(fd,), start_new_session=True,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
"""
        # The launcher process exits; its detached synthetic child retains SH.
        parent = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=5)
        self.assertEqual(parent.returncode, 0, parent.stderr)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline: time.sleep(.02)
            self.assertTrue(ready.exists())
            status = self.stage()
            self.assertEqual(self.restart(status)[0], 400)
            self.assertFalse(done.exists(), 'Restart must not stop or kill the synthetic executor')
            self.assertEqual(self.request()[1]['instance_id'], status['instance_id'])
        finally:
            stop.touch()
            deadline = time.monotonic() + 5
            while not done.exists() and time.monotonic() < deadline: time.sleep(.02)
            self.assertTrue(done.exists(), 'Synthetic executor did not exit naturally')
        # Child may be between its last write and interpreter exit.
        time.sleep(.1)
        self.assertEqual(self.restart(self.request()[1])[0], 200)
        self.wait_status(lambda value: value['restart_state'] == 'completed')

    def test_corrupt_target_and_project_lock_refuse_without_freezing_ui(self):
        status = self.stage()
        target = updater.root() / 'releases' / (status['pending_sha256'] + '.pyz')
        target.write_bytes(target.read_bytes() + b'corrupt')
        code, error = self.restart(status)
        self.assertEqual(code, 400)
        self.assertIn('integrity', error['error'])
        self.assertEqual(self.request()[1]['instance_id'], status['instance_id'])
        self.assertEqual(self.request({'project': 'locked', 'action': 'create'}, '/api/command')[0], 200)
        with Store(self.data / 'locked/control'):
            self.assertEqual(self.restart(self.request()[1])[0], 400)
        self.assertEqual(self.request({'action': 'create', 'title': 'Not frozen'}, '/api/map')[0], 200)

    def test_http_maintenance_barrier_rejects_every_mutation(self):
        from nori.web import create_server
        import threading
        server = create_server(self.home / 'barrier-data')
        server.app.maintenance = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin, token = self.origin, self.token
        try:
            self.origin = 'http://127.0.0.1:' + str(server.server_port)
            self.token = server.app.token
            for path, body in [('/api/command', {'project':'blocked','action':'create'}),
                               ('/api/map', {'action':'create','title':'Blocked'}),
                               ('/api/draft', {'text':'Blocked'}),
                               ('/api/requirements/import', {'raw_text':'Blocked'}),
                               ('/api/update', {'action':'restart','confirmed':True})]:
                self.assertEqual(self.request(body, path)[0], 409)
            self.assertFalse((server.app.root / 'blocked').exists())
        finally:
            self.origin, self.token = origin, token
            server.shutdown(); server.server_close(); thread.join(3)

    def test_legacy_launcher_migrates_on_one_ordinary_start(self):
        self.close()
        text = self.launcher.read_text()
        text = text.replace('# Nori managed web supervisor v2\n', '')
        text = text.replace("    gate = lock('launch.lock')\n    fcntl.flock(gate, fcntl.LOCK_EX)\n", '')
        text = text.replace('    os.close(gate)\n', '')
        self.launcher.write_text(text)
        self.start()
        self.assertIn('# Nori managed web supervisor v2', self.launcher.read_text())
        self.assertIn(MARKER, self.launcher.read_text())
        self.assertTrue(self.request()[1]['restart_supported'])


if __name__ == '__main__': unittest.main()
