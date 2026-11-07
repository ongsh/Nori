"""P1 control core. Only trusted local code gets Controller methods; no actor field API."""
import copy
import hashlib
from pathlib import Path
import uuid
import time
from functools import wraps
from .store import NoriError, canonical, digest, atomic_write


def file_path(root, name):
    p = Path(name)
    if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0] == '.nori.lock':
        raise NoriError('Unsafe relative path')
    target = root / p
    if any(x.is_symlink() for x in [target, *target.parents] if x != root.parent):
        raise NoriError('Symlinks are not supported in P1')
    if not target.resolve().is_relative_to(root):
        raise NoriError('Path escapes workspace')
    return target


def in_scope(name, scope):
    """Exact files or explicit trailing-slash directories, compared by path components."""
    candidate = Path(name).parts
    for rule in scope:
        parts = Path(rule).parts
        if candidate == parts or (rule.endswith('/') and candidate[:len(parts)] == parts):
            return True
    return False


def scopes_overlap(left, right):
    return any(in_scope(a.rstrip('/'), right) or in_scope(b.rstrip('/'), left)
               for a in left for b in right)


def manifest(root):
    result = {}
    for p in sorted(root.rglob('*')):
        if p == root / '.nori.lock':
            continue
        if p.is_symlink():
            raise NoriError('Symlink in workspace')
        if p.is_file():
            result[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def validate_spec(spec):
    if not isinstance(spec.get('goal'), str) or not spec['goal'].strip():
        raise NoriError('Goal required')
    reqs = spec.get('requirements', {})
    tasks = spec.get('tasks', {})
    if not reqs or not tasks or not spec.get('final_checks'):
        raise NoriError('Requirements, tasks and final checks required')
    from .quality_gate import validate_check, check_paths
    total = spec.get('max_total_attempts', sum(t.get('max_attempts', 0) for t in tasks.values()))
    if type(total) is not int or not 1 <= total <= 10000:
        raise NoriError('Invalid project attempt budget')
    covered = set()
    outputs = set()
    for tid, t in tasks.items():
        if not t.get('why') or not t.get('requirements') or not t.get('checks'):
            raise NoriError('Task needs purpose, requirements and checks')
        if not set(t['requirements']) <= reqs.keys():
            raise NoriError('Unknown requirement')
        if not isinstance(t.get('max_attempts'), int) or not 1 <= t['max_attempts'] <= 10:
            raise NoriError('Invalid attempt budget')
        for rid in t['requirements']:
            if rid not in t['checks']:
                raise NoriError('Uncovered task requirement')
            covered.add(rid)
        for rid, name in t['checks'].items():
            if rid not in t['requirements'] or not isinstance(reqs[rid], str):
                raise NoriError('Requirement needs a stable ID and text')
            validate_check(name, spec, rid)
            if not all(in_scope(p, t['write_scope'] + t.get('read_scope', [])) for p in check_paths(name, spec)):
                raise NoriError('Check path outside declared task scope')
        outputs.update(t['write_scope'])
        for name in [*t['write_scope'], *t.get('read_scope', [])]:
            file_path(Path('/tmp/nori-validation'), name)
        if not set(t['depends_on']) <= tasks.keys():
            raise NoriError('Unknown dependency')
    if covered != reqs.keys() or set(spec['final_checks']) != reqs.keys():
        raise NoriError('Plan/final checks omit requirements')
    for rid, check in spec['final_checks'].items():
        validate_check(check, spec, rid)
    for tid, t in tasks.items():
        seconds = t.get('max_seconds', 600)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 86400:
            raise NoriError('Task max_seconds must be 0 < n <= 86400')
    def visit(tid, stack, done):
        if tid in stack:
            raise NoriError('Cyclic dependency')
        if tid in done:
            return
        for dep in tasks[tid]['depends_on']:
            visit(dep, stack | {tid}, done)
        done.add(tid)
    done = set()
    for tid in tasks:
        visit(tid, set(), done)
    def ancestors(tid):
        result = set(tasks[tid]['depends_on'])
        for dep in tasks[tid]['depends_on']:
            result.update(ancestors(dep))
        return result
    ids = list(tasks)
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            if scopes_overlap(tasks[left]['write_scope'], tasks[right]['write_scope']):
                if left not in ancestors(right) and right not in ancestors(left):
                    raise NoriError('Overlapping writers need an explicit dependency')



def serialized(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self._mutex:
            if self.store.lock.closed:
                raise NoriError('Controller is closed; reopen under project and workspace locks')
            return fn(self, *args, **kwargs)
    return wrapped


class Controller:
    def __init__(self, store, workspace):
        self._mutex = store.mutex
        self.store = store
        self.root = Path(workspace).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_relative_to(store.home) or store.home.is_relative_to(self.root):
            raise NoriError('State home and workspace must be separate')
        if store.state and store.state['workspace'] != str(self.root):
            raise NoriError('Wrong workspace')
        store.lock_workspace(self.root)
        self.fresh = set()

    @property
    def state(self):
        return copy.deepcopy(self.store.state)

    @serialized
    def initialize(self, spec):
        if self.store.state is not None:
            raise NoriError('Project already exists')
        validate_spec(spec)
        self.store.save({'schema_version': 1, 'revision': 0, 'workspace': str(self.root),
            'spec': copy.deepcopy(spec), 'requirement_version': 1, 'plan_version': 1,
            'approval': None, 'project_status': 'AWAITING_APPROVAL', 'recovery_required': False,
            'tasks': {tid: {'status': 'PENDING', 'attempts': 0, 'run_id': None, 'evidence': None}
                      for tid in spec['tasks']}, 'runs': {}, 'lease': None, 'commands': {},
            'history': [], 'final_evidence': None})

    @serialized
    def command(self, command_id, revision, operation, payload, fn):
        if not command_id or not isinstance(command_id, str):
            raise NoriError('command_id required')
        s = self.state
        if s is None:
            raise NoriError('Project not initialized')
        fingerprint = digest([operation, payload])
        if command_id in s['commands']:
            previous = s['commands'][command_id]
            if previous['fingerprint'] != fingerprint:
                raise NoriError('command_id reused with different payload')
            return copy.deepcopy(previous['result'])
        if revision != s['revision']:
            raise NoriError('Stale expected_state_revision')
        if s['recovery_required']:
            raise NoriError('Recovered backup requires manual reconciliation; dispatch disabled')
        result = fn(s)
        s['revision'] += 1
        s['commands'][command_id] = {'fingerprint': fingerprint, 'result': copy.deepcopy(result)}
        s['history'].append({'revision': s['revision'], 'command_id': command_id,
                             'event': operation, 'payload': payload})
        self.store.save(s)
        return result

    @serialized
    def approve(self, cid, rev, expected_digest):
        def apply(s):
            if s['project_status'] != 'AWAITING_APPROVAL' or expected_digest != digest(s['spec']):
                raise NoriError('Approval does not match current requirement and plan')
            s['approval'] = expected_digest
            s['project_status'] = 'ACTIVE'
            return expected_digest
        return self.command(cid, rev, 'approve', expected_digest, apply)

    def _validate_run_spec(self, s, run):
        """Receipts and mutable run copies are evidence, not acceptance policy."""
        tid = run.get('task_id')
        constraints = {k: s['spec'].get(k, [] if k != 'operations' else {})
                       for k in ('forbidden_actions', 'out_of_scope', 'read_scope', 'write_scope', 'operations')}
        if (tid not in s['spec']['tasks']
                or run.get('approval') != s['approval']
                or s['approval'] != digest(s['spec'])
                or run.get('plan_version') != s['plan_version']
                or run.get('requirement_version') != s['requirement_version']
                or run.get('task') != s['spec']['tasks'][tid]
                or run.get('goal') != s['spec']['goal']
                or run.get('requirements') != s['spec']['requirements']
                or run.get('constraints') != constraints):
            raise NoriError('Run does not match the current approved task')

    def _current(self, s, rid):
        if s['lease'] != rid or rid not in s['runs']:
            raise NoriError('Stale or inactive run')
        run = s['runs'][rid]
        self._validate_run_spec(s, run)
        if s['tasks'][run['task_id']]['run_id'] != rid:
            raise NoriError('Stale task run')
        return run, s['tasks'][run['task_id']]

    def _stale(self, s):
        current = manifest(self.root)
        for tid, t in s['tasks'].items():
            e = t.get('evidence')
            if t['status'] == 'DONE' and any(current.get(p) != h for p, h in e['files'].items()):
                t['status'] = 'REWORK'
                t['evidence']['stale'] = True
        # Invalidate downstream acceptance as well, even when its output is unchanged.
        changed = True
        while changed:
            changed = False
            for tid, t in s['tasks'].items():
                if t['status'] == 'DONE' and any(s['tasks'][d]['status'] != 'DONE' for d in s['spec']['tasks'][tid]['depends_on']):
                    t['status'] = 'REWORK'
                    t['evidence']['stale'] = True
                    changed = True
        if any(t['status'] != 'DONE' for t in s['tasks'].values()):
            s['final_evidence'] = None
            if s['project_status'] == 'COMPLETED':
                s['project_status'] = 'ACTIVE'

    @serialized
    def audit(self, cid, rev):
        def apply(s):
            self._stale(s)
            return {k: v['status'] for k, v in s['tasks'].items()}
        return self.command(cid, rev, 'audit', {}, apply)

    @serialized
    def prepare(self, cid, rev, tid, run_id=None):
        # command retry must not generate a different random identifier.
        rid = run_id or 'run-' + digest([cid, tid])[:24]
        def apply(s):
            if s['approval'] != digest(s['spec']) or s['project_status'] != 'ACTIVE':
                raise NoriError('Project not approved/active')
            if s['lease'] is not None or rid in s['runs']:
                raise NoriError('Run permit occupied or run_id already used')
            self._stale(s)
            t = s['tasks'][tid]
            spec = s['spec']['tasks'][tid]
            if t['status'] not in ('PENDING', 'REWORK'):
                raise NoriError('Task not dispatchable')
            if any(s['tasks'][d]['status'] != 'DONE' for d in spec['depends_on']):
                raise NoriError('Dependency not independently accepted')
            if t['attempts'] >= spec['max_attempts']:
                t['status'] = 'FAILED'
                s['project_status'] = 'AWAITING_DECISION'
                return {'blocked': 'attempt budget exhausted'}
            t['attempts'] += 1
            t['run_id'] = rid
            t['status'] = 'READY'
            s['lease'] = rid
            run = {'run_id': rid, 'task_id': tid, 'phase': 'PREPARED',
                'identity': str(uuid.uuid4()), 'requirement_version': s['requirement_version'],
                'plan_version': s['plan_version'], 'task_revision': 1,
                'approval': s['approval'], 'task': copy.deepcopy(spec), 'attempt': t['attempts'],
                'baseline': manifest(self.root), 'candidate': None,
                'stop_when': ['scope violation', 'unknown execution state', 'budget exhausted'],
                'adapter': 'unselected', 'goal': s['spec']['goal'],
                'requirements': copy.deepcopy(s['spec']['requirements']),
                'constraints': {k: copy.deepcopy(s['spec'].get(k, [] if k != 'operations' else {}))
                                for k in ('forbidden_actions', 'out_of_scope', 'read_scope', 'write_scope', 'operations')},
                'prepared_at': time.time(), 'usage': {'tokens': None}}
            s['runs'][rid] = run
            return copy.deepcopy(run)
        existed = cid in self.state['commands']
        result = self.command(cid, rev, 'prepare', {'task_id': tid, 'run_id': rid}, apply)
        # Only a newly PREPARED run in this controller lifetime may start.
        if result.get('run_id') and self.state['runs'][rid]['phase'] == 'PREPARED':
            # A replayed command must not turn a recovered run into a fresh intent.
            if not existed:
                self.fresh.add(rid)
        return result

    @serialized
    def start(self, cid, rev, rid, adapter, mode='complete'):
        # Persist an invocation reservation before external effects. Any failure requires reconcile.
        def apply(s):
            run, task = self._current(s, rid)
            if run['phase'] != 'PREPARED' or rid not in self.fresh or s['project_status'] != 'ACTIVE':
                raise NoriError('Run must be reconciled, not restarted')
            run['phase'] = 'UNKNOWN'
            run['adapter'] = getattr(adapter, 'name', 'mock')
            run['started_at'] = time.time()
            task['status'] = 'BLOCKED'
            return copy.deepcopy(run)
        already = cid in self.state['commands']
        run = self.command(cid, rev, 'start_intent', {'run_id': rid, 'mode': mode}, apply)
        if already:
            return self.state['runs'][rid]  # No repeated adapter invocation.
        self.fresh.discard(rid)
        adapter.start(run, mode)
        return self.reconcile(cid + ':observe', self.state['revision'], rid, adapter)

    @serialized
    def reconcile(self, cid, rev, rid, adapter):
        observation = adapter.reconcile(rid)
        def apply(s):
            run, task = self._current(s, rid)
            if run['phase'] == 'EXITED' and run['candidate'] is not None:
                return run['phase']
            if not observation or observation.get('identity') != run['identity']:
                run['phase'] = 'UNKNOWN'
                task['status'] = 'BLOCKED'
                return 'UNKNOWN'
            phase = observation['phase']
            if phase not in ('RUNNING', 'STOP_REQUESTED', 'EXITED', 'UNKNOWN'):
                raise NoriError('Invalid adapter phase')
            run['phase'] = phase
            if phase == 'EXITED':
                current = manifest(self.root)
                changed = {p for p in current.keys() | run['baseline'].keys()
                           if current.get(p) != run['baseline'].get(p)}
                run['candidate'] = {'files': current, 'claimed_result': observation.get('claimed_result'),
                                    'exit_code': observation.get('exit_code'), 'budget_reason': observation.get('budget_reason'), 'changes': sorted(changed)}
                if not all(in_scope(p, run['task']['write_scope']) for p in changed):
                    task['status'] = 'BLOCKED'
                    run['scope_violation'] = sorted(p for p in changed if not in_scope(p, run['task']['write_scope']))
                else:
                    task['status'] = 'IMPLEMENTED'
            else:
                task['status'] = 'BLOCKED' if phase in ('STOP_REQUESTED', 'UNKNOWN') else 'IN_PROGRESS'
            return phase
        return self.command(cid, rev, 'reconcile', {'run_id': rid}, apply)

    @serialized
    def pause(self, cid, rev, adapter):
        def apply(s):
            s['project_status'] = 'PAUSED'
            if s['lease']:
                run, task = self._current(s, s['lease'])
                if run['phase'] != 'EXITED':
                    run['phase'] = 'STOP_REQUESTED'
                    task['status'] = 'BLOCKED'
            return s['lease']
        rid = self.command(cid, rev, 'pause', {}, apply)
        if rid:
            adapter.request_stop(rid)  # Request does not release the permit.
        return rid

    @serialized
    def resume(self, cid, rev):
        def apply(s):
            if s['project_status'] != 'PAUSED' or s['lease'] is not None:
                raise NoriError('Cannot resume until stop and verification are resolved')
            s['project_status'] = 'ACTIVE'
        return self.command(cid, rev, 'resume', {}, apply)

    @serialized
    def authorize_rework(self, cid, rev, tid, reason):
        """Trusted local user decision after inspecting a stopped inconclusive attempt."""
        def apply(s):
            task = s['tasks'][tid]
            if not isinstance(reason, str) or not reason.strip():
                raise NoriError('An explicit review reason is required')
            if s['lease'] is not None or task['status'] != 'BLOCKED':
                raise NoriError('Task is not a stopped blocked attempt')
            run = s['runs'].get(task['run_id'])
            if not run or run['phase'] != 'EXITED' or run.get('scope_violation'):
                raise NoriError('Execution and side effects are not reconciled')
            if task['attempts'] >= s['spec']['tasks'][tid]['max_attempts']:
                raise NoriError('Approved attempt budget exhausted')
            task['status'] = 'REWORK'
            task['rework_decision'] = reason
        return self.command(cid, rev, 'authorize_rework', {'task_id': tid, 'reason': reason}, apply)

    def _checks(self, s, mapping):
        checks = []
        for rid, name in mapping.items():
            p = file_path(self.root, name)
            try:
                actual = p.read_text(encoding='utf-8')
                verdict = 'PASS' if actual == s['spec']['requirements'][rid] else 'FAIL'
                output = {'expected': s['spec']['requirements'][rid], 'actual': actual}
            except FileNotFoundError:
                verdict, output = 'FAIL', {'missing': name}
            except (OSError, UnicodeError) as exc:
                verdict, output = 'INCONCLUSIVE', {'error': str(exc)}
            checks.append({'requirement_id': rid, 'path': name, 'verdict': verdict, 'raw_output': output})
        verdict = 'FAIL' if any(x['verdict'] == 'FAIL' for x in checks) else (
            'INCONCLUSIVE' if any(x['verdict'] == 'INCONCLUSIVE' for x in checks) else 'PASS')
        return verdict, checks

    @serialized
    def verify(self, cid, rev, rid):
        def apply(s):
            run, task = self._current(s, rid)
            if run['phase'] != 'EXITED' or task['status'] != 'IMPLEMENTED':
                raise NoriError('No stopped, scoped candidate to verify')
            task['status'] = 'VERIFYING'
            before = manifest(self.root)
            verdict, checks = self._checks(s, run['task']['checks'])
            scopes = run['task']['write_scope'] + run['task'].get('read_scope', [])
            paths = {p for p in before if in_scope(p, scopes)}
            paths.update(p for p in scopes if not p.endswith('/'))
            for check in checks:
                paths.update(check.get('input_snapshot', {}))
                paths.update(check.get('output_snapshot', {}))
            for dep in run['task']['depends_on']:
                paths.update(s['tasks'][dep]['evidence']['files'])
            if before != run['candidate']['files'] or manifest(self.root) != before:
                verdict = 'INCONCLUSIVE'
            if run['candidate']['exit_code'] != 0 or run['candidate'].get('budget_reason'):
                verdict = 'INCONCLUSIVE'
            e = {'run_id': rid, 'requirement_version': run['requirement_version'],
                 'plan_version': run['plan_version'], 'task_revision': 1,
                 'method': 'independent declared checks against approved requirements; see check methods',
                 'files': {p: before.get(p) for p in sorted(paths)}, 'checks': checks,
                 'verdict': verdict, 'stale': False,
                 'limitations': [run['adapter'] + ' executor', 'checks limited to declared methods', 'no OS account isolation']}
            task['evidence'] = e
            run['verification'] = copy.deepcopy(e)
            task['status'] = {'PASS': 'DONE', 'FAIL': 'REWORK', 'INCONCLUSIVE': 'BLOCKED'}[verdict]
            s['lease'] = None  # EXITED established before this path, including inconclusive.
            return e
        return self.command(cid, rev, 'verify', {'run_id': rid}, apply)

    @serialized
    def close_project(self, cid, rev):
        def apply(s):
            if s['lease'] is not None or s['project_status'] != 'ACTIVE':
                raise NoriError('Project is not ready for final verification')
            self._stale(s)
            if any(t['status'] != 'DONE' for t in s['tasks'].values()):
                return {'verdict': 'INCONCLUSIVE', 'reason': 'Tasks not all currently accepted'}
            before = manifest(self.root)
            verdict, checks = self._checks(s, s['spec']['final_checks'])
            if manifest(self.root) != before:
                verdict = 'INCONCLUSIVE'
            e = {'verdict': verdict, 'checks': checks, 'files': before,
                 'requirement_version': s['requirement_version'], 'plan_version': s['plan_version'],
                 'method': 'whole-project exact text checks and accepted dependency graph'}
            s['final_evidence'] = e
            s['project_status'] = 'COMPLETED' if verdict == 'PASS' else 'AWAITING_DECISION'
            return e
        return self.command(cid, rev, 'close_project', {}, apply)
