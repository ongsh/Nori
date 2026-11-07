import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nori.prototype import LIMITS, PrototypeError, PrototypeStore, RevisionConflict


def concurrent_edit(path, project_id, root_id, ready, go, queue):
    store = PrototypeStore(path)
    ready.put(True)
    go.wait(5)
    try:
        store.mutate(project_id, 'rename', expected_revision=0, node_id=root_id, title='process')
        queue.put('ok')
    except RevisionConflict:
        queue.put('conflict')


class PrototypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = PrototypeStore(self.root)
        self.default = self.store.get_project(self.store.list_projects()[0]['id'])
        self.p = self.store.create_project('Empty test project')

    def mutate(self, action, node_id=None, **fields):
        self.p = self.store.mutate(self.p['id'], action, expected_revision=self.p['revision'],
                                  node_id=node_id or self.p['root_id'], **fields)
        return self.p

    def test_default_persistence_and_isolation(self):
        self.p = self.default
        self.assertEqual(self.p['title'], 'Nori 自我迭代')
        self.assertEqual(len(self.p['nodes']), 13)
        packages = [n for n in self.p['nodes'] if n['parent_id'] == self.p['root_id']]
        self.assertEqual([n['title'] for n in packages], ['界面体验', '任务拆解', '执行监督', '独立验收'])
        for package in packages:
            self.assertEqual(sum(n['parent_id'] == package['id'] for n in self.p['nodes']), 2)
        self.assertTrue(all(n['status'] == '原型未执行' and n['progress'] == 0 for n in self.p['nodes']))
        marker = self.root / 'state.json'
        marker.write_text('existing kernel state')
        self.mutate('rename', title='new title')
        self.assertEqual(PrototypeStore(self.root).get_project(self.p['id']), self.p)
        self.assertEqual(marker.read_text(), 'existing kernel state')
        self.assertTrue(self.p['illustrative_only'])
        self.assertEqual(self.p['limits'], LIMITS)
        created = self.store.create_project('Second')
        self.assertNotEqual(created['id'], self.p['id'])
        self.assertEqual(len(created['nodes']), 1)
        self.assertEqual(len(self.store.list_projects()), 3)
        self.assertEqual(self.store.get_project(self.p['id']), self.p)

    def test_recursive_tree_siblings_and_delete(self):
        parent = self.p['root_id']
        for depth in range(15):
            self.mutate('add_child', parent, title=f'Level {depth}')
            parent = self.p['nodes'][-1]['id']
        self.mutate('add_sibling', parent, title='Sibling')
        self.assertEqual(self.p['nodes'][-1]['parent_id'], self.p['nodes'][-2]['parent_id'])
        first = self.p['nodes'][1]['id']
        with self.assertRaises(PrototypeError):
            self.mutate('delete', first)
        self.mutate('delete', first, confirmed=True)
        self.assertEqual(len(self.p['nodes']), 1)
        with self.assertRaises(PrototypeError):
            self.mutate('delete', confirmed=True)
        with self.assertRaises(PrototypeError):
            self.mutate('add_sibling', title='Invalid root sibling')

    def test_independent_acceptance_and_plain_strings(self):
        self.mutate('update', acceptance='独立验收：待复核', description='<script>alert(1)</script>', agent='demo')
        self.mutate('update', status='自报完成', agent_report='Passed all checks', progress=100, collapsed=True)
        node = self.p['nodes'][0]
        self.assertEqual(node['acceptance'], '独立验收：待复核')
        self.assertEqual(node['description'], '<script>alert(1)</script>')
        for invalid in (-1, 101, True, 2.2, '10'):
            with self.assertRaises(PrototypeError):
                self.mutate('update', progress=invalid)
        with self.assertRaises(PrototypeError):
            self.mutate('update', shell='rm -rf /')

    def test_proposal_requires_review_and_never_overwrites(self):
        self.mutate('propose_decomposition')
        self.assertEqual(len(self.p['nodes']), 1)
        proposal = self.p['proposal']
        self.assertEqual(proposal['revision'], self.p['revision'])
        with self.assertRaises(RevisionConflict):
            self.mutate('confirm_decomposition', proposal_id='wrong')
        self.mutate('confirm_decomposition', proposal_id=proposal['id'])
        self.assertEqual(len(self.p['nodes']), 4)
        self.assertIsNone(self.p['proposal'])
        with self.assertRaises(PrototypeError):
            self.mutate('propose_decomposition')
        with self.assertRaises(RevisionConflict):
            self.mutate('confirm_decomposition', proposal_id=proposal['id'])

    def test_edit_invalidates_proposal_and_revision(self):
        self.mutate('propose_decomposition')
        proposal = self.p['proposal']
        old_revision = self.p['revision']
        self.mutate('rename', title='Changed')
        with self.assertRaises(RevisionConflict):
            self.store.mutate(self.p['id'], 'confirm_decomposition', expected_revision=old_revision,
                              node_id=self.p['root_id'], proposal_id=proposal['id'])
        with self.assertRaises(RevisionConflict):
            self.mutate('confirm_decomposition', proposal_id=proposal['id'])
        self.assertEqual(len(self.store.get_project(self.p['id'])['nodes']), 1)

    def test_path_validation_and_symlinks(self):
        for invalid in ('../state', '/tmp/other', '', 'a/b', 'a\\b'):
            with self.assertRaises(PrototypeError):
                self.store.get_project(invalid)
        target = self.root / 'valuable.json'
        target.write_text('{"do":"not overwrite"}')
        (self.store.path / 'linked.json').symlink_to(target)
        with self.assertRaises(PrototypeError):
            self.store.get_project('linked')
        self.assertEqual(target.read_text(), '{"do":"not overwrite"}')
        other = self.root / 'other'
        other.mkdir()
        (other / '.prototypes').symlink_to(self.store.path, target_is_directory=True)
        with self.assertRaises(PrototypeError):
            PrototypeStore(other)

    def test_atomic_failure_keeps_prior_state(self):
        before = self.store.get_project(self.p['id'])
        with patch('nori.prototype.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(PrototypeError):
                self.mutate('rename', title='Not committed')
        self.assertEqual(self.store.get_project(self.p['id']), before)
        self.assertFalse(list(self.store.path.glob('*.tmp')))

    def test_limits_and_corruption_preserve_original(self):
        with self.assertRaises(PrototypeError):
            self.mutate('rename', title='x' * 201)
        with patch.dict(LIMITS, max_nodes=1):
            with self.assertRaises(PrototypeError):
                self.mutate('add_child', title='Overflow')
        path = self.store.path / (self.p['id'] + '.json')
        path.write_text('{broken')
        with self.assertRaises(PrototypeError):
            self.store.get_project(self.p['id'])
        self.assertEqual(path.read_text(), '{broken')

    def test_cross_process_revision_lock(self):
        ctx = multiprocessing.get_context('fork')
        ready, result, go = ctx.Queue(), ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=concurrent_edit,
                      args=(str(self.root), self.p['id'], self.p['root_id'], ready, go, result))
                     for _ in range(2)]
        for p in processes:
            p.start()
        for _ in processes:
            self.assertTrue(ready.get(timeout=5))
        go.set()
        self.assertEqual(sorted(result.get(timeout=5) for _ in processes), ['conflict', 'ok'])
        for p in processes:
            p.join(5)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(self.store.get_project(self.p['id'])['revision'], 1)


if __name__ == '__main__':
    unittest.main()
