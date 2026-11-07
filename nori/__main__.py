"""Trusted local operator CLI. Executor output is never parsed as control commands."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid
from . import __version__
from .demo import run_demo, example_spec
from .store import Store, NoriError, digest
from .core import manifest
from .workflow import WorkflowController
from .adapter import MockAdapter
from .process_adapter import ProcessAdapter
from .tool_adapters import CodexAdapter, ClaudeCodeAdapter


def adapter_for(name, home, root):
    return {'mock': MockAdapter, 'codex': CodexAdapter, 'claude': ClaudeCodeAdapter}[name](home, root)


def parser():
    p = argparse.ArgumentParser(prog='nori', description='Nori: local serial task control and independent acceptance')
    p.add_argument('--version', action='version', version=f'Nori {__version__}')
    sub = p.add_subparsers(dest='action', required=True)
    x = sub.add_parser('web', help='Open the local browser interface')
    x.add_argument('--data-dir', default=str(Path.home() / '.local/share/nori-web'))
    x.add_argument('--port', type=int, default=8765)
    x.add_argument('--no-browser', action='store_true')
    x = sub.add_parser('demo'); x.add_argument('directory')
    sub.add_parser('example', help='Print an editable synthetic spec')
    for action in ('init','status','approve','run','probe','pause','resume','reconcile','verify','audit','finalize',
                   'rework','retry-verification','review-candidate','cancel','partial','propose','accept-revision',
                   'accept-plan','manual-binding','manual','recover','recover-review','resolve-unstarted'):
        x = sub.add_parser(action)
        x.add_argument('home', help='Private controller directory, separate from work files')
        x.add_argument('--command-id', default=None)
        x.add_argument('--revision', type=int, help='Expected revision; omitted uses state read under exclusive lock')
        if action == 'init':
            x.add_argument('--workspace', required=True); x.add_argument('--spec', required=True)
        if action in ('run','probe','pause','reconcile','cancel','partial','resolve-unstarted'):
            x.add_argument('--adapter', choices=['mock','codex','claude'], default=None)
        if action in ('approve','accept-revision','review-candidate','recover-review'):
            x.add_argument('--digest', required=True)
        if action in ('verify','resolve-unstarted'):
            x.add_argument('--run-id', required=True)
        if action in ('rework','retry-verification','review-candidate'):
            x.add_argument('--task', required=True)
        if action in ('rework','review-candidate','cancel','partial','propose','manual','recover-review'):
            x.add_argument('--reason', required=True)
        if action in ('propose','accept-revision','accept-plan'):
            x.add_argument('--proposal', required=True)
        if action == 'propose':
            x.add_argument('--spec', required=True)
        if action in ('manual','manual-binding'):
            x.add_argument('--requirement', required=True)
            x.add_argument('--task', help='Omit to use whole-project final check')
        if action == 'manual':
            x.add_argument('--binding', required=True); x.add_argument('--verdict', choices=['PASS','FAIL'], required=True)
        if action == 'run':
            x.add_argument('--max-steps', type=int, default=30)
            x.add_argument('--poll-seconds', type=float, default=1.0)
    return p


def run_session(args):
    """Release the control lock between steps so another CLI can pause/query/cancel."""
    if not 1 <= args.max_steps <= 1000 or not 0.05 <= args.poll_seconds <= 60:
        raise NoriError('Bounded run requires 1..1000 steps and 0.05..60 second polling')
    cid = args.command_id or str(uuid.uuid4())
    results = []
    for i in range(args.max_steps):
        with Store(args.home, lock_timeout=2) as store:
            if store.state is None:
                raise NoriError('Project not initialized')
            c = WorkflowController(store, store.state['workspace'])
            if i == 0 and args.revision is not None and args.revision != c.state['revision']:
                raise NoriError('Stale expected_state_revision')
            active = c.state['runs'].get(c.state['lease'], {})
            if active and args.adapter and active.get('adapter') not in ('unselected', args.adapter):
                raise NoriError('Cannot switch an active adapter')
            name = args.adapter or active.get('adapter') or c.state['spec'].get('executor')
            if not name:
                raise NoriError('Select --adapter or declare spec.executor; no silent mock fallback')
            if name == 'unselected': name = 'mock'
            adapter = adapter_for(name, store.home, c.root)
            if not active:
                probe = adapter.probe()
                if probe['status'] != 'available':
                    return {'status': 'blocked', 'probe': probe}
            result = c.advance(adapter, cid + ':' + str(i))
            results.append(result)
            status, revision = c.state['project_status'], c.state['revision']
        if result['status'] in ('blocked','COMPLETED','AWAITING_DECISION','CANCELLED','CLOSED_PARTIAL'):
            break
        if result['status'] == 'running':
            time.sleep(args.poll_seconds)
    return {'steps': results, 'project_status': status, 'revision': revision}


def execute(args):
    if args.action == 'demo': return run_demo(args.directory)
    if args.action == 'example': return example_spec()
    if args.action == 'run': return run_session(args)
    with Store(args.home, recover=args.action == 'recover', lock_timeout=2) as store:
        if args.action == 'recover': return store.state
        if args.action == 'init':
            spec = json.loads(Path(args.spec).read_text())
            c = WorkflowController(store, args.workspace)
            c.initialize(spec)
            return {'revision': c.state['revision'], 'approval_digest': digest(spec), 'status': 'AWAITING_APPROVAL'}
        if store.state is None: raise NoriError('Project not initialized')
        c = WorkflowController(store, store.state['workspace'])
        cid = args.command_id or str(uuid.uuid4())
        rev = c.state['revision'] if args.revision is None else args.revision
        if args.action == 'status':
            return {'revision': rev, 'project_status': c.state['project_status'], 'ready': c.ready(),
                    'lease': c.state['lease'], 'approval_digest': digest(c.state['spec']),
                    'workspace_digest': digest(manifest(c.root)), 'findings': c.state.get('findings', []),
                    'final_evidence': c.state['final_evidence'], 'recovery_required': c.state['recovery_required']}
        if args.action == 'approve': return c.approve(cid, rev, args.digest)
        if args.action == 'resume': return c.resume(cid, rev)
        if args.action == 'audit': return c.audit(cid, rev)
        if args.action == 'finalize': return c.close_project(cid, rev)
        if args.action == 'verify': return c.verify(cid, rev, args.run_id)
        if args.action == 'rework': return c.authorize_rework(cid, rev, args.task, args.reason)
        if args.action == 'retry-verification': return c.retry_verification(cid, rev, args.task)
        if args.action == 'review-candidate': return c.review_candidate(cid, rev, args.task, args.digest, args.reason)
        if args.action == 'propose': return c.propose_revision(cid, rev, args.proposal, json.loads(Path(args.spec).read_text()), args.reason)
        if args.action == 'accept-revision': return c.accept_revision(cid, rev, args.proposal, args.digest)
        if args.action == 'accept-plan': return c.accept_plan(cid, rev, args.proposal)
        if args.action in ('manual','manual-binding'):
            from .quality_gate import QualityGate
            mapping = c.state['spec']['tasks'][args.task]['checks'] if args.task else c.state['spec']['final_checks']
            descriptor = mapping[args.requirement]
            if not isinstance(descriptor, dict) or descriptor.get('kind') != 'manual':
                raise NoriError('Selected check is not a manual requirement')
            if args.action == 'manual-binding':
                return {'binding': QualityGate(c.root, c.state['spec']).binding(args.requirement, descriptor), 'check': descriptor}
            return c.record_manual(cid, rev, args.requirement, descriptor, args.binding, args.verdict, args.reason)
        if args.action == 'recover-review':
            adapters = [MockAdapter(store.home, c.root), ProcessAdapter(store.home, c.root)]
            return c.recover_review(cid, rev, adapters, args.digest, args.reason)
        selected = getattr(args, 'adapter', None)
        active = c.state['runs'].get(c.state['lease'], {})
        if active and selected and active.get('adapter') not in ('unselected', selected):
            raise NoriError('Cannot switch adapter until existing run has stopped and is reconciled')
        name = selected or active.get('adapter', 'mock')
        if name == 'unselected': name = 'mock'
        adapter = adapter_for(name, store.home, c.root)
        if args.action == 'probe': return adapter.probe()
        if args.action == 'pause': return c.pause(cid, rev, adapter)
        if args.action == 'reconcile':
            if not c.state['lease']: return {'status': 'idle'}
            return c.reconcile(cid, rev, c.state['lease'], adapter)
        if args.action == 'resolve-unstarted': return c.resolve_unstarted(cid, rev, args.run_id, adapter)
        if args.action in ('cancel','partial'): return c.cancel(cid, rev, adapter, args.reason, partial=args.action == 'partial')
        raise NoriError('Unsupported action')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        argv = ['web']
    args = parser().parse_args(argv)
    if args.action == 'web':
        from .web import serve
        try:
            serve(args.data_dir, port=args.port, open_browser=not args.no_browser)
            return 0
        except (OSError, ValueError, NoriError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    try:
        print(json.dumps(execute(args), ensure_ascii=False, indent=2))
        return 0
    except (NoriError, OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
