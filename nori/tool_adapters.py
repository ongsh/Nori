"""Official CLI adapters. No installation, authentication or permission bypass."""
import json
import shutil
import subprocess

from .process_adapter import ProcessAdapter
from .store import NoriError


class ToolAdapter(ProcessAdapter):
    executable_name = None
    auth_args = ()

    def __init__(self, home, workspace, *, executable=None, stop_grace=2.0):
        super().__init__(home, workspace, stop_grace=stop_grace)
        self.executable = executable or self.executable_name

    def probe(self):
        result = super().probe()
        if result['status'] != 'available':
            return result
        binary = shutil.which(self.executable)
        if not binary:
            return dict(result, status='not_installed', executable=self.executable)
        try:
            version = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=10)
            auth = subprocess.run([binary, *self.auth_args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return dict(result, status='probe_failed', reason=str(exc))
        # Do not expose auth output or credentials in status/receipts.
        return dict(result, status='available' if version.returncode == 0 and auth.returncode == 0 else 'authentication_or_cli_unavailable',
                    executable=binary, version=version.stdout.strip()[:200],
                    authentication='reported_authenticated' if auth.returncode == 0 else 'not_confirmed',
                    model_access='not_tested')

    def start(self, run, mode='complete'):
        if mode != 'complete':
            raise NoriError('Real tool adapters do not support mock modes')
        prompt = ('Execute only this approved task. Stay within write_scope. '
                  'Report blockers honestly. Completion is independently verified.\n' +
                  json.dumps({'task': run['task'], 'requirements': run.get('requirements', {}),
                              'goal': run.get('goal', ''), 'constraints': run.get('constraints', {}),
                              'stop_when': run.get('stop_when', []), 'baseline': run.get('baseline', {}),
                              'run_id': run['run_id'], 'task_revision': run.get('task_revision'),
                              'requirement_version': run.get('requirement_version'),
                              'plan_version': run.get('plan_version')}, ensure_ascii=False))
        return self.start_argv(run, self.build_argv(), prompt)


class CodexAdapter(ToolAdapter):
    name = 'codex'
    executable_name = 'codex'
    auth_args = ('login', 'status')

    def build_argv(self):
        return [self.executable, 'exec', '--json', '--sandbox', 'workspace-write',
                '--skip-git-repo-check', '-']


class ClaudeCodeAdapter(ToolAdapter):
    name = 'claude'
    executable_name = 'claude'
    auth_args = ('auth', 'status')

    def build_argv(self):
        return [self.executable, '-p', '--output-format', 'stream-json', '--verbose',
                '--permission-mode', 'default']


ClaudeAdapter = ClaudeCodeAdapter
