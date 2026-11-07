import copy
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from nori.intake import IntakeError, LIMITS, STANDARD_TEMPLATE, preview_intake
from nori.prototype import PrototypeStore


def document():
    doc = copy.deepcopy(STANDARD_TEMPLATE)
    doc['original_request'] = ' 原文\n不要改变任何字词。 '
    doc['constraints'] = ['不可联网']
    doc['out_of_scope'] = ['部署']
    doc['open_questions'] = ['谁验收？']
    doc['nodes'].append(dict(id='child', parent_id='root', title='子需求',
                             description='完整说明', acceptance_criteria=['人工检查一', '人工检查二']))
    return doc


def raw(doc=None):
    return json.dumps(document() if doc is None else doc, ensure_ascii=False, indent=2)


def import_one(path, text, command='test-command'):
    return PrototypeStore(path).import_intake(text, confirmed=True,
        digest=preview_intake(text)['digest'], command_id=command)


def concurrent_import(args):
    return import_one(*args)['id']


class IntakeTests(unittest.TestCase):
    def test_preview_is_pure_and_preserves_every_field(self):
        text = raw()
        with mock.patch('builtins.open', side_effect=AssertionError('write/read')), \
             mock.patch('os.mkdir', side_effect=AssertionError('mkdir')):
            preview = preview_intake(text)
        self.assertEqual(preview['document'], document())
        self.assertEqual(preview['node_count'], 2)
        self.assertEqual(preview['approval_status'], 'UNAPPROVED')
        self.assertEqual(preview['execution_status'], 'UNEXECUTED')

    def test_schema_rejects_unknown_execution_and_wrong_types(self):
        for key in ('status', 'progress', 'approval', 'evidence', 'run_id', 'commands', 'operations', 'argv'):
            for location in ('top', 'node'):
                doc = document()
                (doc if location == 'top' else doc['nodes'][0])[key] = []
                with self.subTest(key=key, location=location), self.assertRaises(IntakeError):
                    preview_intake(raw(doc))
        for key, value in [('schema_version', True), ('schema_version', 2), ('title', ''),
                           ('goal', []), ('constraints', 'text'), ('nodes', []),
                           ('original_request', None)]:
            doc = document(); doc[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(IntakeError):
                preview_intake(raw(doc))

    def test_json_encoding_duplicates_and_bounds(self):
        for text in ('{}', '[]', 'null', '{"a":1,"a":2}', raw().replace('"schema_version": 1', '"schema_version": NaN'),
                     '[' * 2000 + ']' * 2000, '\ud800', ' ' * (LIMITS['max_bytes'] + 1),
                     raw().replace('需求标题', '\\ud800')):
            with self.subTest(text=text[:40]), self.assertRaises(IntakeError):
                preview_intake(text)
        for key, value in [('goal', 'x' * 10001), ('title', 'x' * 201), ('constraints', ['x'] * 201)]:
            doc = document(); doc[key] = value
            with self.assertRaises(IntakeError): preview_intake(raw(doc))

    def test_invalid_graphs(self):
        variants = []
        d = document(); d['nodes'][1]['id'] = 'root'; variants.append(d)
        d = document(); d['nodes'][1]['parent_id'] = None; variants.append(d)
        d = document(); d['nodes'][1]['parent_id'] = 'missing'; variants.append(d)
        d = document(); d['nodes'][1]['parent_id'] = 'child'; variants.append(d)
        d = document(); d['nodes'][0]['parent_id'] = 'child'; variants.append(d)
        d = document(); d['nodes'][1]['id'] = '../bad'; variants.append(d)
        d = document(); d['nodes'][1]['acceptance_criteria'] = [True]; variants.append(d)
        for d in variants:
            with self.assertRaises(IntakeError): preview_intake(raw(d))

    def test_confirm_cancel_and_stale_preview_no_save(self):
        with tempfile.TemporaryDirectory() as path:
            store = PrototypeStore(path)
            for args in ({}, {'confirmed': 'true'}, {'confirmed': True, 'digest': 'stale'}):
                with self.assertRaises(IntakeError): store.import_intake(raw(), command_id='x', **args)
            self.assertEqual(list(store.path.glob('*.json')), [])

    def test_import_atomic_provenance_no_execution_restart_and_edit(self):
        with tempfile.TemporaryDirectory() as path:
            store = PrototypeStore(path)
            prior = store.create_project('existing draft')
            before = (store.path / (prior['id'] + '.json')).read_bytes()
            with mock.patch('subprocess.Popen', side_effect=AssertionError('execution')), \
                 mock.patch('os.system', side_effect=AssertionError('execution')):
                p = import_one(path, raw())
            self.assertEqual(p['intake']['raw_text'], raw())
            self.assertEqual(p['intake']['document'], document())
            self.assertEqual(p['nodes'][1]['acceptance_criteria'], ['人工检查一', '人工检查二'])
            self.assertTrue(all(n['progress'] == 0 and n['acceptance'] == '未验收' for n in p['nodes']))
            self.assertEqual(before, (store.path / (prior['id'] + '.json')).read_bytes())
            self.assertEqual(set(os.listdir(path)), {'.prototypes'})
            again = import_one(path, raw(), 'retry')
            self.assertEqual(again['id'], p['id'])
            reordered = json.dumps(document(), ensure_ascii=False, sort_keys=True)
            self.assertEqual(import_one(path, reordered, 'reformatted')['id'], p['id'])
            edited = store.mutate(p['id'], 'update', expected_revision=0,
                                  node_id=p['nodes'][1]['id'], description='edited', acceptance_criteria=['new criterion'])
            self.assertEqual(edited['intake'], p['intake'])
            self.assertEqual(import_one(path, raw())['revision'], 1)
            changed = document(); changed['title'] = 'different'
            with self.assertRaises(IntakeError): import_one(path, raw(changed))
            self.assertEqual(len(list(store.path.glob('*.json'))), 2)

    def test_project_list_has_only_small_intake_flag(self):
        with tempfile.TemporaryDirectory() as path:
            store = PrototypeStore(path)
            ordinary = store.create_project('ordinary')
            imported = import_one(path, raw())
            summaries = {p['id']: p for p in store.list_projects()}
            self.assertIs(summaries[ordinary['id']]['intake'], False)
            self.assertIs(summaries[imported['id']]['intake'], True)
            self.assertNotIn('document', summaries[imported['id']])
            self.assertNotIn('raw_text', summaries[imported['id']])

    def test_write_failure_preserves_old_projects_and_leaves_no_orphan(self):
        with tempfile.TemporaryDirectory() as path:
            store = PrototypeStore(path)
            prior = store.create_project('prior')
            before = (store.path / (prior['id'] + '.json')).read_bytes()
            with mock.patch('os.replace', side_effect=OSError('synthetic failure')):
                with self.assertRaises(Exception): import_one(path, raw())
            self.assertEqual(before, (store.path / (prior['id'] + '.json')).read_bytes())
            self.assertEqual(len(list(store.path.glob('*.json'))), 1)
            self.assertFalse(list(store.path.glob('*.tmp')))

    @unittest.skipUnless(os.name == 'posix', 'cross-process file locking is POSIX')
    def test_cross_process_retry_has_single_record(self):
        with tempfile.TemporaryDirectory() as path:
            with multiprocessing.get_context('fork').Pool(4) as pool:
                ids = pool.map(concurrent_import, [(path, raw(), 'parallel')] * 8)
            self.assertEqual(len(set(ids)), 1)
            self.assertEqual(len(list((Path(path) / '.prototypes').glob('*.json'))), 1)
            self.assertEqual(PrototypeStore(path).get_project(ids[0])['intake']['document'], document())


if __name__ == '__main__':
    unittest.main()
