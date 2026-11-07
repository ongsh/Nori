"""Self-update tests use temporary HOME, synthetic releases and mocked HTTPS."""
import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from nori import updater
from nori.distribution import install
from scripts.build_distribution import build


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux required')
class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.running_managed = updater.running_managed
        managed = patch.object(updater, 'running_managed', return_value=True)
        managed.start(); self.addCleanup(managed.stop)
        self.env = patch.dict(os.environ, HOME=str(self.home))
        self.env.start(); self.addCleanup(self.env.stop)
        archive = build(self.home / 'download/nori.pyz')
        install(archive)
        self.launcher = self.home / '.local/bin/nori'
        buffer = io.BytesIO()
        with zipfile.ZipFile(archive) as old, zipfile.ZipFile(buffer, 'w') as new:
            for name in old.namelist():
                content = old.read(name)
                if name == 'nori/version.py':
                    content = b"VERSION = '1.0.2-test'\n"
                if name == 'NORI_VERSION':
                    content = b'1.0.2-test\n'
                new.writestr(name, content)
        self.payload = buffer.getvalue()
        self.item = dict(schema=1, version='1.0.2-test', commit='a' * 40,
                         bundle_sha256=hashlib.sha256(self.payload).hexdigest(),
                         bundle_size=len(self.payload), manifest_token='b'*64)

    def stage(self, payload=None):
        with patch.object(updater, 'manifest', return_value=self.item), patch.object(updater, 'fetch', return_value=base64.b64encode(self.payload if payload is None else payload)):
            return updater.stage_update(self.item['commit'], self.item['manifest_token'])

    def launch(self):
        return subprocess.run([str(self.launcher), '--version'], capture_output=True, text=True, timeout=10)

    def test_check_requires_explicit_exact_release(self):
        with patch.object(updater, 'manifest', return_value=self.item):
            result = updater.check()
            self.assertTrue(result['update_available'])
            self.assertEqual(result['manifest_digest'], self.item['manifest_token'])
            for commit, token in [(None, None), ('c'*40, self.item['manifest_token']), (self.item['commit'], 'd'*64)]:
                with self.assertRaises(RuntimeError):
                    updater.stage_update(commit, token)
        self.assertFalse(updater.status()['pending'])

    def test_integrity_failure_leaves_current_unchanged(self):
        before = updater.state()
        with self.assertRaises(RuntimeError):
            self.stage(b'not the released bundle')
        self.assertEqual(updater.state(), before)
        self.assertIn('1.0.1-preview', self.launch().stdout)

    def test_stage_activate_and_rollback_preserves_bytes_and_data(self):
        before = updater.state()['current']
        data = self.home / 'project-data'; data.write_text('retain me')
        result = self.stage()
        self.assertTrue(result['restart_required'])
        self.assertEqual(updater.state()['current'], before)
        self.assertIn('1.0.2-test', self.launch().stdout)
        self.assertEqual(updater.state()['previous'], before)
        updater.stage_rollback(updater.status()['rollback_token'])
        self.assertIn('1.0.1-preview', self.launch().stdout)
        self.assertEqual(data.read_text(), 'retain me')
        self.assertTrue((updater.root()/'releases'/(self.item['bundle_sha256']+'.pyz')).exists())

    def test_live_process_lock_defers_activation(self):
        lock = os.open(updater.root() / 'run.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_SH)
            self.stage()
            self.assertIn('1.0.1-preview', self.launch().stdout)
            self.assertTrue(updater.state()['pending'])
        finally:
            os.close(lock)
        self.assertIn('1.0.2-test', self.launch().stdout)

    def test_pending_corruption_runs_unchanged_current(self):
        self.stage()
        path = updater.root()/'releases'/(self.item['bundle_sha256']+'.pyz')
        path.write_bytes(b'corrupted')
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1.0.1-preview', result.stdout)
        self.assertIn('not activated', result.stderr)

    def test_cancel_pending_and_refuse_symlink(self):
        self.stage()
        updater.stage_rollback(updater.status()['rollback_token'])
        self.assertFalse(updater.status()['pending'])
        state = updater.root()/'update-state.json'
        state.rename(state.with_suffix('.saved'))
        state.symlink_to(state.with_suffix('.saved'))
        with self.assertRaises(RuntimeError):
            updater.status()

    def test_source_checkout_status_does_not_create_install(self):
        other = self.home / 'empty'; other.mkdir()
        with patch.dict(os.environ, HOME=str(other)):
            self.assertFalse(updater.status()['installed'])
            with self.assertRaises(RuntimeError):
                updater.stage_update('a'*40, 'b'*64)
            self.assertEqual(list(other.iterdir()), [])

    def test_concurrent_pending_release_is_not_overwritten(self):
        self.stage()
        before = updater.state()
        self.item = dict(self.item, commit='c'*40)
        with self.assertRaises(RuntimeError):
            self.stage()
        self.assertEqual(updater.state(), before)

    def test_stale_rollback_and_emergency_launcher_rollback(self):
        token = updater.status()['rollback_token']
        self.stage()
        with self.assertRaises(RuntimeError):
            updater.stage_rollback(token)
        self.launch()
        result = subprocess.run([str(self.launcher), '--rollback'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1.0.1-preview', self.launch().stdout)

    def test_live_installed_process_retains_shared_lock(self):
        server = subprocess.Popen([str(self.launcher), 'web', '--port', '0', '--no-browser', '--data-dir', str(self.home/'web')], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn('http://', server.stdout.readline())
            lock = os.open(updater.root()/'run.lock', os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(lock)
            blocked = subprocess.run([str(self.launcher), '--uninstall'], capture_output=True, text=True, timeout=10)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn('Close all other Nori', blocked.stderr)
            self.assertTrue(self.launcher.exists())
            self.stage()
            self.assertIn('1.0.1-preview', self.launch().stdout)
        finally:
            server.terminate(); server.communicate(timeout=10)
        self.assertIn('1.0.2-test', self.launch().stdout)

    def test_reinstall_repairs_missing_launcher_after_partial_install(self):
        self.launcher.unlink()
        install(self.home/'download/nori.pyz')
        self.assertIn('1.0.1-preview', self.launch().stdout)

    def test_cancel_during_download_wins_over_older_update(self):
        self.stage()
        def download(*args):
            updater.stage_rollback(updater.status()['rollback_token'])
            return base64.b64encode(self.payload)
        with patch.object(updater, 'manifest', return_value=self.item), patch.object(updater, 'fetch', side_effect=download):
            with self.assertRaisesRegex(RuntimeError, 'changed during download'):
                updater.stage_update(self.item['commit'], self.item['manifest_token'])
        self.assertFalse(updater.status()['pending'])

    def test_claimed_version_must_match_embedded_metadata(self):
        self.item = dict(self.item, version='9.9.9-false')
        with self.assertRaisesRegex(RuntimeError, 'version does not match'):
            self.stage()
        self.assertFalse(updater.status()['pending'])

    def test_detached_supervisor_retains_installation_lease(self):
        workspace = self.home/'detached-workspace'; workspace.mkdir()
        ready, stop = workspace/'ready', workspace/'stop'
        archive = updater.root()/'releases'/(updater.state()['current']['sha256']+'.pyz')
        parent_script = r"""
import fcntl, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from nori.process_adapter import ProcessAdapter
root = pathlib.Path(sys.argv[2])
lock = os.open(pathlib.Path.home()/'.local/lib/nori/run.lock', os.O_CREAT|os.O_RDWR, 0o600)
fcntl.flock(lock, fcntl.LOCK_SH)
os.environ['NORI_RUN_LOCK_FD'] = str(lock)
child_code = "import pathlib,time; p=pathlib.Path('.'); (p/'ready').write_text('ready'); exec(\"while not (p/'stop').exists(): time.sleep(.025)\")"
a = ProcessAdapter(root/'control', root)
a.start_argv({'run_id':'lease-test','identity':'synthetic','task':{'max_seconds':20}}, [sys.executable, '-c', child_code])
"""
        try:
            parent = subprocess.run([sys.executable, '-c', parent_script, str(archive), str(workspace)], capture_output=True, text=True, timeout=10)
            self.assertEqual(parent.returncode, 0, parent.stderr)
            deadline = time.monotonic()+5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.025)
            self.assertTrue(ready.exists(), 'Detached synthetic child did not start')
            self.stage()
            self.assertIn('1.0.1-preview', self.launch().stdout)
            self.assertTrue(updater.state()['pending'])
            blocked = subprocess.run([str(self.launcher), '--uninstall'], capture_output=True, text=True, timeout=10)
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            self.assertIn('Close all other Nori', blocked.stderr)
            self.assertTrue(archive.exists())
        finally:
            stop.touch()
            lock = os.open(updater.root()/'run.lock', os.O_CREAT|os.O_RDWR, 0o600)
            try:
                deadline = time.monotonic()+8
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail('Detached supervisor did not release installation lease')
                        time.sleep(.025)
            finally:
                os.close(lock)
        self.assertIn('1.0.2-test', self.launch().stdout)

    def test_installation_lock_helper_rejects_missing_and_unrelated_fds(self):
        from nori.process_adapter import _installation_lock_fds
        lock = os.open(updater.root()/'run.lock', os.O_CREAT|os.O_RDWR, 0o600)
        unrelated = os.open(self.home/'unrelated', os.O_CREAT|os.O_RDWR, 0o600)
        try:
            for value in ('', 'not-a-number', '-1', str(unrelated), '999999'):
                with patch.dict(os.environ, NORI_RUN_LOCK_FD=value):
                    self.assertEqual(_installation_lock_fds(), ())
            with patch.dict(os.environ, NORI_RUN_LOCK_FD=str(lock)):
                self.assertEqual(_installation_lock_fds(), (lock,))
            os.close(lock); lock = None
            with patch.dict(os.environ, NORI_RUN_LOCK_FD=str(unrelated)):
                (updater.root()/'run.lock').unlink()
                (updater.root()/'run.lock').symlink_to(self.home/'unrelated')
                self.assertEqual(_installation_lock_fds(), ())
        finally:
            if lock is not None:
                os.close(lock)
            os.close(unrelated)

    def test_managed_status_requires_launcher_lifecycle_descriptor(self):
        archive = updater.root()/'releases'/(updater.state()['current']['sha256']+'.pyz')
        lock = os.open(updater.root()/'run.lock', os.O_CREAT|os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_SH)
            with patch.object(updater, '__file__', str(archive/'nori/updater.py')):
                with patch.dict(os.environ, NORI_RUN_LOCK_FD=''):
                    self.assertFalse(self.running_managed())
                with patch.dict(os.environ, NORI_RUN_LOCK_FD=str(lock)):
                    self.assertTrue(self.running_managed())
        finally:
            os.close(lock)

    def test_reinstall_commit_serializes_with_uninstall(self):
        from nori import distribution
        at_launcher, resume, uninstall_started = threading.Event(), threading.Event(), threading.Event()
        errors = []
        chmod = os.chmod
        def pause_launcher(path, mode):
            if mode == 0o700:
                at_launcher.set()
                if not resume.wait(5):
                    raise RuntimeError('Test did not release install transaction')
            return chmod(path, mode)
        def reinstall():
            try:
                install(self.home/'download/nori.pyz')
            except Exception as exc:
                errors.append(exc)
        def remove():
            uninstall_started.set()
            try:
                distribution.uninstall()
            except Exception as exc:
                errors.append(exc)
        with patch.object(distribution.os, 'chmod', side_effect=pause_launcher):
            first = threading.Thread(target=reinstall)
            second = threading.Thread(target=remove)
            first.start()
            try:
                self.assertTrue(at_launcher.wait(5))
                second.start()
                self.assertTrue(uninstall_started.wait(5))
                second.join(.1)
                self.assertTrue(second.is_alive(), 'Uninstall bypassed the install commit lock')
            finally:
                resume.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.launcher.exists())
        self.assertFalse((updater.root()/'update-state.json').exists())

    def test_reinstall_refuses_corrupt_existing_immutable_release(self):
        before = updater.state()
        candidate = self.home/'candidate.pyz'
        candidate.write_bytes(self.payload)
        (updater.root()/'releases'/(self.item['bundle_sha256']+'.pyz')).write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'immutable release failed integrity'):
            install(candidate)
        self.assertEqual(updater.state(), before)
        self.assertIn('1.0.1-preview', self.launch().stdout)

    def test_redirect_is_rejected_before_follow(self):
        with self.assertRaises(RuntimeError):
            updater.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid')

    def test_manifest_rejects_untrusted_revision_and_unbounded_size(self):
        for commit, size in [('main', 4), ('a'*40, updater.MAX_BUNDLE+1)]:
            item = dict(self.item, commit=commit, bundle_size=size)
            with patch.object(updater, 'fetch', side_effect=[json.dumps({'sha':'c'*40}).encode(), json.dumps(item).encode()]):
                with self.assertRaises(RuntimeError):
                    updater.manifest()
