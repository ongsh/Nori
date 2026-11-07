"""A deliberately incomplete candidate, rejection, rework and dependency release."""
from pathlib import Path
from .store import Store, NoriError, digest
from .core import Controller
from .adapter import MockAdapter


def example_spec():
    return {'executor': 'mock', 'goal': 'Create two dependent text artifacts and independently verify both',
        'requirements': {'R-A': 'alpha\nbeta\n', 'R-B': 'summary:\nalpha\nbeta\n'},
        'tasks': {
            'A': {'why': 'Produce complete source text', 'requirements': ['R-A'],
                  'depends_on': [], 'read_scope': [], 'write_scope': ['a.txt'],
                  'checks': {'R-A': 'a.txt'}, 'max_attempts': 3,
                  'mock_outputs': {'a.txt': 'alpha\nbeta\n'}},
            'B': {'why': 'Consume the accepted source text', 'requirements': ['R-B'],
                  'depends_on': ['A'], 'read_scope': ['a.txt'], 'write_scope': ['b.txt'],
                  'checks': {'R-B': 'b.txt'}, 'max_attempts': 3,
                  'mock_outputs': {'b.txt': 'summary:\nalpha\nbeta\n'}}},
        'final_checks': {'R-A': 'a.txt', 'R-B': 'b.txt'}}


def run_demo(directory):
    directory = Path(directory)
    with Store(directory / 'control') as store:
        c = Controller(store, directory / 'work')
        if c.state is not None:
            raise NoriError('Demo destination already initialized; choose a fresh directory')
        c.initialize(example_spec())
        a = MockAdapter(store.home, c.root)
        c.approve('approve', c.state['revision'], digest(c.state['spec']))
        results = []
        def attempt(tid, mode, label):
            run = c.prepare(label + ':prepare', c.state['revision'], tid)
            rid = run['run_id']
            c.start(label + ':start', c.state['revision'], rid, a, mode)
            evidence = c.verify(label + ':verify', c.state['revision'], rid)
            results.append({'task': tid, 'run_id': rid, 'verdict': evidence['verdict']})
        attempt('A', 'incomplete', 'a1')
        try:
            c.prepare('premature-b', c.state['revision'], 'B')
        except NoriError as exc:
            results.append({'dependency_blocked': str(exc)})
        else:
            raise AssertionError('Incomplete A incorrectly released B')
        attempt('A', 'complete', 'a2')
        attempt('B', 'copy', 'b1')
        final = c.close_project('final', c.state['revision'])
        return {'steps': results, 'final_verdict': final['verdict'],
                'project_status': c.state['project_status'], 'revision': c.state['revision']}
