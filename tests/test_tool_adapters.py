import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from nori.process_adapter import ProcessAdapter, process_identity
from nori.tool_adapters import CodexAdapter, ClaudeCodeAdapter
from nori.store import NoriError, atomic_write, canonical


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process identity required')
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.work = self.base / 'work'
        self.work.mkdir()
        self.adapter = ProcessAdapter(self.base / 'control', self.work, stop_grace=.15)
        self.run = {'run_id': 'run-1', 'identity': 'test-identity', 'task': {'why': 'simulation'}}

    def tearDown(self):
        receipt = self.adapter.reconcile('run-1')
        if receipt and receipt['phase'] != 'EXITED':
            self.adapter.request_stop('run-1')
            try:
                self.wait('EXITED')
            except AssertionError:
                pass
        self.temp.cleanup()

    def wait(self, phase):
        for _ in range(120):
            result = self.adapter.observe('run-1')
            if result and result['phase'] == phase:
                return result
            time.sleep(.025)
        self.fail(f'Expected {phase}, got {result}')

    def test_complete_logs_exit_and_reopen(self):
        self.adapter.start_argv(self.run, [sys.executable, '-c',
            'import json,sys; print(json.dumps({"type":"result","value":sys.stdin.read()})); print("warning", file=sys.stderr)'], '$(touch NEVER)')
        result = self.wait('EXITED')
        self.assertEqual(result['exit_code'], 0)
        reopened = ProcessAdapter(self.base / 'control', self.work)
        self.assertEqual(reopened.collect('run-1')['events'][0]['value'], '$(touch NEVER)')
        self.assertFalse((self.work / 'NEVER').exists())
        self.assertTrue(result['group_empty'])
        self.assertEqual(reopened.start_argv(self.run, [sys.executable, '-c', 'raise Exception()'])['start_count'], 1)

    def test_intent_durable_before_launch_and_unknown_not_restarted(self):
        with patch('nori.process_adapter.subprocess.Popen', side_effect=OSError('synthetic crash')):
            with self.assertRaises(OSError):
                self.adapter.start_argv(self.run, [sys.executable, '-c', 'pass'])
        self.assertEqual(self.adapter.reconcile('run-1')['phase'], 'UNKNOWN')
        with patch('nori.process_adapter.subprocess.Popen') as popen:
            result = self.adapter.start_argv(self.run, [sys.executable, '-c', 'pass'])
            self.assertEqual(result['phase'], 'UNKNOWN')
            popen.assert_not_called()
        # This fixture has conclusive local knowledge no subprocess was started.
        atomic_write(self.adapter._directory('run-1') / 'receipt.json', canonical(dict(result, phase='EXITED')))

    def test_stop_confirms_group_exit_including_grandchild(self):
        code = ('import subprocess,sys,time,signal; '
                'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                'subprocess.Popen([sys.executable,"-c","import time,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"]); '
                'time.sleep(30)')
        self.adapter.start_argv(self.run, [sys.executable, '-c', code])
        self.wait('RUNNING')
        time.sleep(.1)
        self.adapter.request_stop('run-1')
        done = self.wait('EXITED')
        self.assertTrue(done['exit_confirmed'])
        self.assertTrue(done['stop_requested'])
        self.assertEqual(done['exit_code'], -signal.SIGKILL)

    def test_supervisor_timeout_without_controller_polling(self):
        self.run['task']['max_seconds'] = .2
        self.adapter.start_argv(self.run, [sys.executable, '-c',
            'import time,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'])
        # Wait on the supervisor OS handle only: no observe/reconcile/stop calls.
        self.adapter._supervisors['run-1'].wait(timeout=5)
        receipt = json.loads((self.adapter._directory('run-1') / 'receipt.json').read_text())
        self.assertEqual(receipt['phase'], 'EXITED')
        self.assertEqual(receipt['budget_reason'], 'max_seconds')
        self.assertEqual(receipt['exit_code'], -signal.SIGKILL)
        self.assertTrue(receipt['group_empty'])
        self.assertTrue(receipt['stop_requested'])

    def test_supervisor_output_budget_without_controller_polling(self):
        self.run['task']['max_output_bytes'] = 1024
        self.adapter.start_argv(self.run, [sys.executable, '-c',
            'import os,time; os.write(2,b"x" * 2048); time.sleep(30)'])
        self.adapter._supervisors['run-1'].wait(timeout=5)
        receipt = json.loads((self.adapter._directory('run-1') / 'receipt.json').read_text())
        self.assertEqual(receipt['phase'], 'EXITED')
        self.assertEqual(receipt['budget_reason'], 'max_output_bytes')
        self.assertGreaterEqual(receipt['output_bytes'], 2048)
        self.assertNotEqual(receipt['exit_code'], 0)

    def test_budget_limits_reject_invalid_values_before_reserving_run(self):
        for key in ('max_seconds', 'max_output_bytes'):
            for value in (0, -1, float('nan'), float('inf'), '5', None, True):
                with self.subTest(key=key, value=value):
                    self.run['task'] = {key: value}
                    with self.assertRaises(NoriError):
                        self.adapter.start_argv(self.run, [sys.executable, '-c', 'pass'])
                    self.assertFalse(self.adapter._directory('run-1').exists())

    def test_nonzero_and_invalid_log_not_success(self):
        self.adapter.start_argv(self.run, [sys.executable, '-c', 'print("not-json"); raise SystemExit(7)'])
        self.assertEqual(self.wait('EXITED')['exit_code'], 7)
        self.assertEqual(self.adapter.collect('run-1')['invalid_json_lines'], 1)

    def test_missing_executable_records_failed_exit(self):
        self.adapter.start_argv(self.run, ['/does/not/exist'])
        self.assertEqual(self.wait('EXITED')['exit_code'], 127)

    def test_identity_not_just_pid(self):
        ident = process_identity(os.getpid())
        self.assertIn('boot_id', ident)
        self.assertIn('start_ticks', ident)
        directory = self.adapter._directory('run-1')
        directory.mkdir()
        ident['start_ticks'] = 'wrong'
        atomic_write(directory / 'receipt.json', canonical(dict(self.run, phase='RUNNING', supervisor=ident)))
        self.assertEqual(self.adapter.reconcile('run-1')['phase'], 'UNKNOWN')
        atomic_write(directory / 'receipt.json', canonical(dict(self.run, phase='EXITED')))

    def test_inventory_preserves_original_task(self):
        self.run['task_id'] = 'simulated-task'
        self.adapter.start_argv(self.run, [sys.executable, '-c', 'pass'])
        self.wait('EXITED')
        item = self.adapter.inventory()[0]
        self.assertEqual(item['task_id'], 'simulated-task')
        self.assertEqual(item['run'], self.run)

    def test_corrupt_receipt_never_relaunches(self):
        directory = self.adapter._directory('run-1')
        directory.mkdir()
        (directory / 'receipt.json').write_text('[]')
        self.assertIsNone(self.adapter.reconcile('run-1'))
        self.assertEqual(self.adapter.inventory()[0]['phase'], 'UNKNOWN')
        with self.assertRaises(NoriError):
            self.adapter.start_argv(self.run, [sys.executable, '-c', 'pass'])

    def test_missing_receipt_and_bad_id(self):
        self.assertIsNone(self.adapter.reconcile('missing'))
        with self.assertRaises(NoriError):
            self.adapter.reconcile('../bad')
        with self.assertRaises(NoriError):
            self.adapter.collect('missing')

    def test_official_cli_argv_and_simulated_probe(self):
        fixture = self.base / 'simulated-cli'
        fixture.write_text('#!' + sys.executable + '\nimport sys,json\n'
            'if "--version" in sys.argv: print("simulated 1.0")\n'
            'elif "status" in sys.argv: pass\n'
            'else: print(json.dumps({"type":"result","result":sys.stdin.read()}))\n')
        fixture.chmod(0o700)
        codex = CodexAdapter(self.base / 'control', self.work, executable=str(fixture))
        self.assertEqual(codex.probe()['status'], 'available')
        self.assertEqual(codex.build_argv()[1:3], ['exec', '--json'])
        claude = ClaudeCodeAdapter(self.base / 'other', self.work, executable=str(fixture))
        self.assertNotIn('--dangerously-skip-permissions', claude.build_argv())
        codex.start(self.run)
        self.wait('EXITED')
        self.assertIn('simulation', codex.collect('run-1')['events'][0]['result'])
        missing = CodexAdapter(self.base / 'absent', self.work, executable='/not-installed')
        self.assertEqual(missing.probe()['status'], 'not_installed')
        with self.assertRaises(NoriError):
            missing.start(self.run)


if __name__ == '__main__':
    unittest.main()
