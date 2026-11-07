import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from scripts.build_distribution import build


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux/WSL2 required')
class DistributionTests(unittest.TestCase):
    def test_offline_install_run_process_and_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home with spaces'
            home.mkdir()
            archive = build(root / 'download/nori.pyz')
            first = archive.read_bytes()
            build(archive)
            self.assertEqual(first, archive.read_bytes())
            env = dict(os.environ, HOME=str(home))
            def call(*args):
                return subprocess.run(args, cwd=root, env=env, text=True, capture_output=True, timeout=30)
            result = call(sys.executable, str(archive), '--install')
            self.assertEqual(result.returncode, 0, result.stderr)
            launcher = home / '.local/bin/nori'
            archive.unlink()  # Installed program must not depend on download or checkout.
            self.assertIn('Nori 1.0.3-preview', call(str(launcher), '--version').stdout)
            self.assertEqual(call(str(launcher), '--help').returncode, 0)
            server = subprocess.Popen([str(launcher), 'web', '--port', '0', '--no-browser', '--data-dir', str(root/'web-data')], cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                url = server.stdout.readline().strip().split('http://', 1)[1]
                with urllib.request.urlopen('http://' + url.split('#')[0], timeout=5) as response:
                    self.assertIn('Nori 工作台', response.read().decode())
            finally:
                server.terminate()
                server.communicate(timeout=5)

            result = call(str(launcher), 'demo', str(root / 'synthetic'))
            self.assertEqual(result.returncode, 0, result.stderr)
            result = call(str(launcher), 'boundary-demo', str(root / 'boundary-synthetic'))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('COMPLETED', result.stdout)
            installed = home / '.local/lib/nori/nori.pyz'
            script = '''import sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from nori.process_adapter import ProcessAdapter
root=Path(sys.argv[2]); root.mkdir()
a=ProcessAdapter(root/'control',root)
r={'run_id':'package-test','identity':'test','task':{}}
a.start_argv(r,[sys.executable,'-c','print("installed supervisor works")'],'')
for _ in range(200):
    result=a.observe(r['run_id'])
    if result and result['phase']=='EXITED': break
    time.sleep(.025)
assert result['phase']=='EXITED' and result['exit_code']==0, result
'''
            result = call(sys.executable, '-c', script, str(installed), str(root / 'process'))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(call(str(launcher), '--uninstall').returncode, 0)
            self.assertFalse(installed.exists())
            self.assertFalse(launcher.exists())
            self.assertTrue((root / 'synthetic').exists())

    def test_refuses_unrelated_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = build(root / 'download/nori.pyz')
            launcher = root / '.local/bin/nori'
            launcher.parent.mkdir(parents=True)
            launcher.write_text('existing user command')
            result = subprocess.run([sys.executable, str(archive), '--install'], env=dict(os.environ, HOME=str(root)), capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(launcher.read_text(), 'existing user command')

    def test_refuses_symlinked_installation_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'; home.mkdir()
            elsewhere = root / 'elsewhere'; elsewhere.mkdir()
            (home / '.local').symlink_to(elsewhere, target_is_directory=True)
            archive = build(root / 'download/nori.pyz')
            result = subprocess.run([sys.executable, str(archive), '--install'], env=dict(os.environ, HOME=str(home)), capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(list(elsewhere.iterdir()), [])
