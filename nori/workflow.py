"""V1 operator workflows: revisions, bounded scheduling, supervision and recovery.

These methods belong to the trusted local operator process, not to model output.
"""
import copy
import time
from .core import Controller, serialized, validate_spec, manifest, in_scope
from .store import NoriError, digest


class WorkflowController(Controller):
    @serialized
    def initialize(self, spec):
        super().initialize(spec)
        s = self.state
        s.update(budget_limit=spec.get('max_total_attempts', sum(t['max_attempts'] for t in spec['tasks'].values())), proposals={}, versions=[{'requirement_version': 1, 'plan_version': 1,
                                         'spec': copy.deepcopy(spec), 'approval': None}],
                 findings=[], manual_decisions={}, terminal_reason=None)
        self.store.save(s)

    def _defaults(self, s):
        s.setdefault('proposals', {})
        s.setdefault('versions', [])
        s.setdefault('findings', [])
        s.setdefault('manual_decisions', {})

    @serialized
    def propose_revision(self, cid, rev, proposal_id, spec, reason):
        validate_spec(spec)
        def apply(s):
            self._defaults(s)
            if s['lease'] is not None or not reason.strip():
                raise NoriError('Stop/reconcile active work first; revision reason required')
            if proposal_id in s['proposals']:
                raise NoriError('Proposal ID already exists')
            proposal = {'spec': copy.deepcopy(spec), 'digest': digest(spec), 'reason': reason,
                        'from_plan_version': s['plan_version'], 'status': 'PROPOSED'}
            s['proposals'][proposal_id] = proposal
            return proposal
        return self.command(cid, rev, 'propose_revision', {'id': proposal_id, 'spec': spec, 'reason': reason}, apply)

    @serialized
    def accept_revision(self, cid, rev, proposal_id, expected_digest):
        def apply(s):
            self._defaults(s)
            if s['lease'] is not None:
                raise NoriError('Active inputs cannot be revised')
            p = s['proposals'][proposal_id]
            if p['status'] != 'PROPOSED' or p['digest'] != expected_digest or digest(p['spec']) != expected_digest:
                raise NoriError('Proposal approval does not match content')
            if p['from_plan_version'] != s['plan_version']:
                raise NoriError('Proposal based on stale plan')
            old = copy.deepcopy(s['spec'])
            s['versions'].append({'requirement_version': s['requirement_version'],
                                  'plan_version': s['plan_version'], 'spec': old,
                                  'approval': s['approval'], 'tasks': copy.deepcopy(s['tasks'])})
            s['requirement_version'] += int(old['requirements'] != p['spec']['requirements'] or old['goal'] != p['spec']['goal'])
            s['plan_version'] += 1
            s['spec'] = copy.deepcopy(p['spec'])
            s['approval'] = expected_digest
            # Conservative invalidation, never silently reuse changed acceptance evidence.
            prior_tasks = s['tasks']
            s['tasks'] = {tid: {'status': 'PENDING', 'attempts': prior_tasks.get(tid, {}).get('attempts', 0), 'run_id': None, 'evidence': None}
                          for tid in s['spec']['tasks']}
            s['budget_limit'] = s['spec'].get('max_total_attempts', sum(t['max_attempts'] for t in s['spec']['tasks'].values()))
            s['manual_decisions'] = {}
            s['final_evidence'] = None
            s['project_status'] = 'ACTIVE'
            p['status'] = 'APPROVED'
            p['approved_by'] = 'local_operator'
            return {'requirement_version': s['requirement_version'], 'plan_version': s['plan_version']}
        return self.command(cid, rev, 'accept_revision', {'id': proposal_id, 'digest': expected_digest}, apply)

    @serialized
    def accept_plan(self, cid, rev, proposal_id):
        """Accept only a contained plan adjustment; broader changes need explicit version approval."""
        s = self.state
        proposal = s.get('proposals', {}).get(proposal_id)
        if not proposal:
            raise NoriError('Unknown plan proposal')
        old, new = s['spec'], proposal['spec']
        protected = ('goal', 'requirements', 'final_checks', 'operations', 'forbidden_actions', 'out_of_scope', 'max_total_attempts', 'executor')
        if any(old.get(k) != new.get(k) for k in protected):
            raise NoriError('Requirement, acceptance or operation change needs explicit approval')
        old_checks = {tid: t['checks'] for tid, t in old['tasks'].items()}
        new_checks = {tid: t['checks'] for tid, t in new['tasks'].items()}
        if old_checks != new_checks:
            raise NoriError('Task acceptance changes require explicit approval')
        old_writes = {p for t in old['tasks'].values() for p in t['write_scope']}
        new_writes = {p for t in new['tasks'].values() for p in t['write_scope']}
        if not all(in_scope(p, old_writes) and (not p.endswith('/') or any(q.endswith('/') and in_scope(p, [q]) for q in old_writes)) for p in new_writes) or sum(t['max_attempts'] for t in new['tasks'].values()) > sum(t['max_attempts'] for t in old['tasks'].values()):
            raise NoriError('Scope or budget expansion requires explicit approval')
        return self.accept_revision(cid, rev, proposal_id, proposal['digest'])

    @serialized
    def prepare(self, cid, rev, tid, run_id=None):
        if cid not in self.state['commands'] and len(self.state['runs']) >= self.state.get('budget_limit', self.state['spec'].get('max_total_attempts', sum(t['max_attempts'] for t in self.state['spec']['tasks'].values()))):
            raise NoriError('Project attempt budget exhausted; explicit revised budget approval required')
        return super().prepare(cid, rev, tid, run_id)

    def ready(self):
        """Read-only readiness explanations; callers audit actual files before dispatch."""
        s = self.state
        rows = []
        for tid, task in s['tasks'].items():
            why = []
            if s['project_status'] != 'ACTIVE':
                why.append('project ' + s['project_status'])
            if len(s['runs']) >= s.get('budget_limit', s['spec'].get('max_total_attempts', sum(t['max_attempts'] for t in s['spec']['tasks'].values()))):
                why.append('project attempt budget exhausted')
            if s['lease']:
                why.append('workspace occupied by ' + s['lease'])
            if task['status'] not in ('PENDING', 'REWORK'):
                why.append('task ' + task['status'])
            if task['attempts'] >= s['spec']['tasks'][tid]['max_attempts']:
                why.append('attempt budget exhausted')
            why += ['waiting for ' + d for d in s['spec']['tasks'][tid]['depends_on'] if s['tasks'][d]['status'] != 'DONE']
            rows.append({'task_id': tid, 'status': task['status'], 'ready': not why, 'reasons': why})
        return rows

    @serialized
    def supervise(self, cid, rev, adapter):
        s0 = self.state
        rid = s0['lease']
        if rid is None:
            return {'findings': [], 'action': 'idle'}
        now = time.time()
        current = manifest(self.root)
        def apply(s):
            self._defaults(s)
            run, task = self._current(s, rid)
            findings = []
            changed = {p for p in current.keys() | run['baseline'].keys() if current.get(p) != run['baseline'].get(p)}
            outside = {p for p in changed if not in_scope(p, run['task']['write_scope'])}
            if outside:
                findings.append({'rule': 'scope_violation', 'paths': sorted(outside), 'blocking': True})
            started = run.get('started_at', run.get('prepared_at', now))
            if run['phase'] in ('RUNNING', 'UNKNOWN', 'STOP_REQUESTED') and now - started > run['task'].get('max_seconds', 600):
                findings.append({'rule': 'time_budget_exhausted', 'seconds': now - started, 'blocking': True})
            signature = digest(current)
            old = run.get('checkpoint')
            checkpoints = (old.get('unchanged', 0) + 1) if old and old['signature'] == signature else 0
            # No file changes alone do not prove a stalled compiler or reasonable investigation.
            if checkpoints == 2:
                findings.append({'rule': 'progress_unconfirmed', 'blocking': False,
                                 'recommendation': 'Inspect adapter output/tool state; do not infer failure from file types'})
            run['checkpoint'] = {'at': now, 'signature': signature, 'unchanged': checkpoints}
            for finding in findings:
                finding.update(run_id=rid, observed_at=now)
            s['findings'].extend(findings)
            if any(f['blocking'] for f in findings):
                s['project_status'] = 'PAUSED'
                task['status'] = 'BLOCKED'
                if run['phase'] != 'EXITED':
                    run['phase'] = 'STOP_REQUESTED'
            return {'findings': findings, 'action': 'pause' if any(f['blocking'] for f in findings) else 'observe'}
        result = self.command(cid, rev, 'supervise', {'run_id': rid}, apply)
        if result['action'] == 'pause':
            adapter.request_stop(rid)
        return result

    @serialized
    def record_manual(self, cid, rev, requirement_id, descriptor, binding, verdict, reason):
        from .quality_gate import QualityGate
        def apply(s):
            self._defaults(s)
            if s['lease'] is not None and s['runs'][s['lease']]['phase'] != 'EXITED':
                raise NoriError('Cannot inspect manual acceptance while executor may be writing')
            if verdict not in ('PASS', 'FAIL') or not reason.strip():
                raise NoriError('Manual decision requires PASS/FAIL and an actual inspection reason')
            expected = QualityGate(self.root, s['spec']).binding(requirement_id, descriptor)
            if binding != expected:
                raise NoriError('Manual decision refers to stale files or requirements')
            s['manual_decisions'][requirement_id] = {'binding': binding, 'verdict': verdict, 'reason': reason,
                                                    'source': 'local_operator'}
            return s['manual_decisions'][requirement_id]
        return self.command(cid, rev, 'record_manual', {'requirement_id': requirement_id, 'descriptor': descriptor,
                           'binding': binding, 'verdict': verdict, 'reason': reason}, apply)

    def _checks(self, s, mapping):
        from .quality_gate import QualityGate
        return QualityGate(self.root, s['spec'], s.get('manual_decisions')).evaluate(mapping)

    @serialized
    def authorize_rework(self, cid, rev, tid, reason):
        def apply(s):
            task = s['tasks'][tid]
            run = s['runs'].get(task['run_id'])
            if not reason.strip() or s['lease'] is not None:
                raise NoriError('Stopped execution and explicit rework decision required')
            eligible = task['status'] in ('BLOCKED', 'REWORK') or (task['status'] == 'DONE' and s['project_status'] == 'AWAITING_DECISION')
            if not eligible or not run or run['phase'] != 'EXITED' or run.get('scope_violation'):
                raise NoriError('Task is not a stopped, scoped rework candidate')
            if task['attempts'] >= s['spec']['tasks'][tid]['max_attempts']:
                raise NoriError('Attempt budget exhausted')
            task['status'] = 'REWORK'
            task['rework_decision'] = reason
            if task.get('evidence'):
                task['evidence']['stale'] = True
            s['final_evidence'] = None
            if s['project_status'] == 'AWAITING_DECISION':
                s['project_status'] = 'ACTIVE'
        return self.command(cid, rev, 'authorize_rework', {'task_id': tid, 'reason': reason}, apply)

    @serialized
    def retry_verification(self, cid, rev, tid):
        """Re-inspect stopped inconclusive outputs without another implementation attempt."""
        def apply(s):
            task = s['tasks'][tid]
            run = s['runs'].get(task['run_id'])
            if s['lease'] is not None or task['status'] != 'BLOCKED' or not run or run['phase'] != 'EXITED' or run.get('scope_violation'):
                raise NoriError('Task is not a stopped, scoped candidate')
            if run['candidate']['files'] != manifest(self.root):
                raise NoriError('Candidate changed; inspect and authorize rework instead')
            task['status'] = 'IMPLEMENTED'
            s['lease'] = run['run_id']
            return run['run_id']
        return self.command(cid, rev, 'retry_verification', {'task_id': tid}, apply)

    @serialized
    def cancel(self, cid, rev, adapter, reason, partial=False):
        def apply(s):
            if not reason.strip():
                raise NoriError('Cancellation reason required')
            rid = s['lease']
            if rid and s['runs'][rid]['phase'] != 'EXITED':
                s['project_status'] = 'PAUSED'
                s['runs'][rid]['phase'] = 'STOP_REQUESTED'
                s['tasks'][s['runs'][rid]['task_id']]['status'] = 'BLOCKED'
                s['terminal_reason'] = reason
                return {'pending_stop': rid}
            for task in s['tasks'].values():
                if task['status'] != 'DONE':
                    task['status'] = 'CANCELLED'
            s['lease'] = None
            s['project_status'] = 'CLOSED_PARTIAL' if partial else 'CANCELLED'
            s['terminal_reason'] = reason
            s['final_evidence'] = None
            return {'status': s['project_status']}
        result = self.command(cid, rev, 'close_partial' if partial else 'cancel', {'reason': reason}, apply)
        if result.get('pending_stop'):
            adapter.request_stop(result['pending_stop'])
        return result

    @serialized
    def resolve_unstarted(self, cid, rev, rid, adapter):
        """PREPARED is durable proof the controller has not reserved an adapter call yet."""
        def apply(s):
            run, task = self._current(s, rid)
            if run['phase'] != 'PREPARED' or adapter.reconcile(rid) is not None:
                raise NoriError('Not a provably uninvoked prepared run')
            if any(h.get('event') == 'start_intent' and h.get('payload', {}).get('run_id') == rid for h in s['history']):
                raise NoriError('Invocation intent exists; must reconcile effects')
            run['phase'] = 'NOT_STARTED'
            run['resolution'] = 'durable PREPARED with no adapter invocation intent or receipt'
            task['status'] = 'REWORK'
            s['lease'] = None
            return run['resolution']
        return self.command(cid, rev, 'resolve_unstarted', {'run_id': rid}, apply)

    @serialized
    def recover_review(self, cid, rev, adapters, expected_workspace_digest, reason):
        """Explicit operator recovery; requires all inventoried executions proven exited.

Old backup state can miss dispatches. Merge original run specs from adapter journals,
then invalidate acceptance rather than trusting the rolled-back progress.
"""
        s = self.state
        fingerprint = digest(['recovery_review', expected_workspace_digest, reason])
        if cid in s['commands']:
            if s['commands'][cid]['fingerprint'] != fingerprint:
                raise NoriError('command_id reused with different recovery review')
            return s['commands'][cid]['result']
        if not s.get('recovery_required') or rev != s['revision'] or not reason.strip():
            raise NoriError('No matching explicit backup recovery review')
        if expected_workspace_digest != digest(manifest(self.root)):
            raise NoriError('Workspace changed during recovery review')
        records = []
        for adapter in adapters:
            if not hasattr(adapter, 'inventory'):
                raise NoriError('Adapter does not expose complete recovery inventory')
            for record in adapter.inventory():
                observed = adapter.reconcile(record['run_id'])
                if not observed or observed.get('phase') not in ('EXITED', 'NOT_STARTED'):
                    raise NoriError('An execution is still active or unknown; keep recovery blocked')
                records.append((record, observed))
        inventoried = {record['run_id'] for record, _ in records}
        expected = {p.stem for p in (self.store.home / 'mock-receipts').glob('*.json')}
        process_dir = self.store.home / 'process-receipts'
        if process_dir.exists():
            expected.update(p.name for p in process_dir.iterdir() if p.is_dir())
        if not expected <= inventoried:
            raise NoriError('Recovery adapter inventory is incomplete')
        for run in s['runs'].values():
            if run['phase'] not in ('EXITED', 'NOT_STARTED', 'PREPARED') and not any(r['run_id'] == run['run_id'] for r, _ in records):
                raise NoriError('Missing proof for a known active execution')
        for record, observed in records:
            rid = record['run_id']
            if rid not in s['runs']:
                original = record.get('run')
                if not original or original.get('task_id') not in s['tasks'] or original.get('approval') != s['approval']:
                    raise NoriError('Orphan execution cannot be matched to approved plan')
                s['runs'][rid] = copy.deepcopy(original)
            if s['runs'][rid]['identity'] != observed.get('identity'):
                raise NoriError('Execution identity mismatch')
            s['runs'][rid]['phase'] = observed['phase']
        for run in s['runs'].values():
            if run['phase'] == 'PREPARED':
                # With no start-intent reservation in backup and no adapter record,
                # do not claim the work ran; reviewed workspace remains the baseline.
                run['phase'] = 'NOT_STARTED'
        for tid, task in s['tasks'].items():
            attempts = [r for r in s['runs'].values() if r['task_id'] == tid]
            task['attempts'] = max(task['attempts'], len(attempts))
            task['status'] = 'REWORK' if task['attempts'] else 'PENDING'
            if task.get('evidence'):
                task['evidence']['stale'] = True
        s['lease'] = None
        s['manual_decisions'] = {}
        s['final_evidence'] = None
        s['recovery_required'] = False
        s['project_status'] = 'PAUSED' if s['approval'] else 'AWAITING_APPROVAL'
        s['revision'] += 1
        s['history'].append({'revision': s['revision'], 'command_id': cid, 'event': 'recovery_review',
                             'reason': reason, 'workspace_digest': expected_workspace_digest,
                             'observed_runs': [r['run_id'] for r, _ in records]})
        s['commands'][cid] = {'fingerprint': fingerprint, 'result': {'status': s['project_status'], 'recovered_runs': len(records)}}
        self.store.save(s)
        return {'status': s['project_status'], 'recovered_runs': len(records)}

    @serialized
    def close_project(self, cid, rev):
        """Revalidate every accepted task on the integrated workspace, then the final checks."""
        def apply(s):
            if s['lease'] is not None or s['project_status'] != 'ACTIVE':
                raise NoriError('Project cannot be inspected while writing or paused')
            if any(not t.get('evidence') or t['evidence']['verdict'] != 'PASS' or t['status'] not in ('DONE', 'REWORK') for t in s['tasks'].values()):
                return {'verdict': 'INCONCLUSIVE', 'reason': 'An implementation has not passed independent acceptance'}
            before = manifest(self.root)
            task_results = {}
            for tid, task in s['tasks'].items():
                verdict, checks = self._checks(s, s['spec']['tasks'][tid]['checks'])
                task_results[tid] = {'verdict': verdict, 'checks': checks}
                if verdict != 'PASS':
                    task['status'] = 'REWORK' if verdict == 'FAIL' else 'BLOCKED'
            final_verdict, final_checks = self._checks(s, s['spec']['final_checks'])
            if any(r['verdict'] == 'FAIL' for r in task_results.values()):
                final_verdict = 'FAIL'
            elif any(r['verdict'] == 'INCONCLUSIVE' for r in task_results.values()) and final_verdict == 'PASS':
                final_verdict = 'INCONCLUSIVE'
            if manifest(self.root) != before:
                final_verdict = 'INCONCLUSIVE'
            e = {'verdict': final_verdict, 'checks': final_checks, 'task_checks': task_results,
                 'files': before, 'requirement_version': s['requirement_version'],
                 'plan_version': s['plan_version'], 'method': 'integrated original task and project checks'}
            s['final_evidence'] = e
            if final_verdict == 'PASS':
                for tid, task in s['tasks'].items():
                    task['status'] = 'DONE'
                    task['integrated_evidence'] = task_results[tid]
                    task['evidence'] = {**task['evidence'], 'checks': task_results[tid]['checks'],
                                        'files': {p: before.get(p) for p in task['evidence']['files']},
                                        'verdict': 'PASS', 'stale': False,
                                        'integrated_after_runs': sorted(s['runs'])}
                s['project_status'] = 'COMPLETED'
            else:
                s['project_status'] = 'AWAITING_DECISION'
            return e
        return self.command(cid, rev, 'close_project', {}, apply)

    @serialized
    def review_candidate(self, cid, rev, tid, expected_digest, reason):
        """Inspect allowed generated artifacts after an inconclusive check, then recheck."""
        def apply(s):
            task = s['tasks'][tid]
            run = s['runs'].get(task['run_id'])
            current = manifest(self.root)
            if not reason.strip() or digest(current) != expected_digest:
                raise NoriError('Current inspected workspace and review reason required')
            if s['lease'] is not None or task['status'] != 'BLOCKED' or not run or run['phase'] != 'EXITED':
                raise NoriError('Candidate is not stopped and awaiting review')
            allowed = set(run['task']['write_scope'])
            for check in run['task']['checks'].values():
                if isinstance(check, dict) and check.get('kind') == 'command':
                    allowed.update(s['spec']['operations'][check['operation']].get('generated_paths', []))
            changed = {p for p in current.keys() | run['candidate']['files'].keys() if current.get(p) != run['candidate']['files'].get(p)}
            if not all(in_scope(p, allowed) for p in changed):
                raise NoriError('Observed changes exceed approved outputs')
            run['candidate']['files'] = current
            run['candidate_review'] = {'reason': reason, 'files': current}
            task['status'] = 'IMPLEMENTED'
            s['lease'] = run['run_id']
            return run['run_id']
        return self.command(cid, rev, 'review_candidate', {'task_id': tid, 'digest': expected_digest, 'reason': reason}, apply)

    @serialized
    def advance(self, adapter, cid_prefix):
        """One bounded scheduler step, with explicit outcomes instead of an unbounded loop."""
        s = self.state
        if s['recovery_required']:
            return {'status': 'blocked', 'reason': 'backup recovery needs review'}
        if s['lease']:
            rid = s['lease']
            if s['runs'][rid]['phase'] == 'PREPARED':
                self.resolve_unstarted(cid_prefix + ':unstarted', s['revision'], rid, adapter)
                return {'status': 'reconciled_unstarted'}
            self.reconcile(cid_prefix + ':observe', s['revision'], rid, adapter)
            s = self.state
            if s['tasks'][s['runs'][rid]['task_id']]['status'] == 'IMPLEMENTED':
                result = self.verify(cid_prefix + ':verify', s['revision'], rid)
                return {'status': 'verified', 'verdict': result['verdict'], 'run_id': rid}
            result = self.supervise(cid_prefix + ':supervise', s['revision'], adapter)
            return {'status': 'running' if result['action'] == 'observe' else 'blocked', 'details': result}
        if s['project_status'] != 'ACTIVE':
            return {'status': 'blocked', 'reason': s['project_status']}
        self.audit(cid_prefix + ':audit', s['revision'])
        current_tasks = self.state['tasks'].values()
        if all(t.get('evidence') and t['evidence']['verdict'] == 'PASS' and t['status'] in ('DONE', 'REWORK') for t in current_tasks):
            result = self.close_project(cid_prefix + ':final', self.state['revision'])
            return {'status': self.state['project_status'], 'verdict': result['verdict']}
        ready = [r for r in self.ready() if r['ready']]
        if ready:
            tid = ready[0]['task_id']
            run = self.prepare(cid_prefix + ':prepare', self.state['revision'], tid)
            self.start(cid_prefix + ':start', self.state['revision'], run['run_id'], adapter,
                       run['task'].get('mock_mode', 'complete') if getattr(adapter, 'name', '') == 'mock' else 'complete')
            return {'status': 'started', 'task_id': tid, 'run_id': run['run_id']}
        if all(t['status'] == 'DONE' for t in self.state['tasks'].values()):
            result = self.close_project(cid_prefix + ':final', self.state['revision'])
            return {'status': self.state['project_status'], 'verdict': result['verdict']}
        return {'status': 'blocked', 'tasks': self.ready()}
