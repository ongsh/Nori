import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from nori.web import create_server
from nori.demo import example_spec


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(self.temp.name, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        for job in self.server.app.jobs.values():
            job['thread'].join(timeout=3)
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        h = {'X-Nori-Token': self.server.app.token, 'Origin': self.origin}
        if body is not None: h['Content-Type'] = 'application/json'
        h.update(headers or {})
        conn.request('POST' if body is not None else 'GET', path,
                     body=json.dumps(body) if body is not None else None, headers=h)
        response = conn.getresponse()
        raw = response.read().decode()
        result = raw if response.getheader('Content-Type').startswith('text/html') else json.loads(raw)
        status = response.status
        conn.close()
        return status, result

    def command(self, action, **values):
        body = {'project': 'simulation', 'action': action}
        if action != 'create': body['revision'] = self.snapshot()['state']['revision']
        body.update(values)
        status, result = self.request('/api/command', body)
        self.assertEqual(status, 200, result)
        return result

    def snapshot(self):
        status, result = self.request('/api/project/simulation')
        self.assertEqual(status, 200, result)
        return result

    def test_update_auth_confirmation_and_no_implicit_network(self):
        from unittest.mock import Mock
        updater = Mock()
        updater.status.return_value = {'installed': True, 'current_version': '1'}
        updater.check.return_value = {'installed': True, 'current_version': '1',
                                     'latest_version': '2', 'commit': 'a'*40,
                                     'manifest_digest': 'b'*64, 'update_available': True}
        updater.stage_update.return_value = {'pending': True, 'current_version': '1'}
        updater.stage_rollback.return_value = {'pending': True, 'current_version': '1'}
        self.server.app.updater = updater
        self.assertEqual(self.request('/api/update')[0], 200)
        updater.check.assert_not_called()
        updater.stage_update.assert_not_called()
        for action in ('check', 'stage', 'rollback'):
            for headers in ({'X-Nori-Token': ''}, {'Host': 'evil.example'},
                            {'Origin': 'https://evil.example'}):
                self.assertEqual(self.request('/api/update', {'action': action, 'confirmed': True}, headers)[0], 403)
        updater.check.assert_not_called()
        for action in ('stage', 'rollback'):
            for confirmed in (None, False, 'true', 1):
                self.assertEqual(self.request('/api/update', {'action': action, 'confirmed': confirmed})[0], 400)
        updater.stage_update.assert_not_called()
        updater.stage_rollback.assert_not_called()
        self.assertEqual(self.request('/api/update', {'action': 'check'})[0], 200)
        updater.check.assert_called_once_with()
        self.assertEqual(self.request('/api/update', {'action': 'stage', 'confirmed': True,
            'commit': 'a'*40, 'manifest_digest': 'b'*64})[0], 200)
        updater.stage_update.assert_called_once_with(expected_commit='a'*40, expected_manifest='b'*64)
        self.assertEqual(self.request('/api/update', {'action': 'rollback', 'confirmed': True, 'rollback_token': 'state-1'})[0], 200)
        updater.stage_rollback.assert_called_once_with(expected_state='state-1')
        updater.stage_rollback.side_effect = RuntimeError('更新状态已改变，请重新打开更新面板')
        code, result = self.request('/api/update', {'action': 'rollback', 'confirmed': True, 'rollback_token': 'stale'})
        self.assertEqual(code, 400)
        self.assertIn('状态已改变', result['error'])
        updater.check.side_effect = RuntimeError('校验失败')
        code, result = self.request('/api/update', {'action': 'check'})
        self.assertEqual(code, 400)
        self.assertIn('校验失败', result['error'])
        self.assertEqual(self.request('/api/update')[1]['current_version'], '1')
        self.assertEqual(self.request('/api/update', {'action': 'arbitrary'})[0], 400)
        self.assertEqual(self.request('/api/update', headers={'X-Nori-Token': ''})[0], 403)

    def test_invalid_import_does_not_reserve_project_name(self):
        status, _ = self.request('/api/command', {'project':'simulation','action':'create','spec':{'goal':''}})
        self.assertEqual(status, 400)
        self.assertFalse((self.server.app.root/'simulation').exists())
        self.command('create')
        self.assertEqual(self.snapshot()['state']['project_status'], 'AWAITING_APPROVAL')

    def test_http_complete_synthetic_workflow(self):
        status, html = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn('Nori 工作台', html)
        self.assertNotIn(self.server.app.token, html)
        self.assertIn('location.hash', html)
        self.assertIn('创建漏项失败示例', html)
        self.command('create')
        self.command('approve', digest=self.snapshot()['approval_digest'])
        self.command('run', max_steps=30)
        for _ in range(100):
            snap = self.snapshot()
            if not snap['job']['running']: break
            time.sleep(.03)
        self.assertIsNone(snap['job'].get('error'))
        self.assertEqual(snap['state']['project_status'], 'COMPLETED')
        self.assertEqual(snap['state']['final_evidence']['verdict'], 'PASS')
        self.assertEqual(snap['state']['tasks']['B']['status'], 'DONE')
        self.assertEqual(self.request('/api/projects')[1]['projects'], ['simulation'])

    def test_ui_distinguishes_saved_plan_draft_and_creation_failure(self):
        _, html = self.request('/')
        self.assertIn('当前保存的计划（只读，批准对象）', html)
        self.assertIn('草稿修改未提交不生效', html)
        self.assertIn("pretty(snapshot.state.spec)", html)
        self.assertIn("await refresh(action==='accept-revision')", html)
        self.assertIn("const created=await api('/api/command',{project:name,action:'create',spec});reset();current=name;snapshot=created", html)
        self.command('create')
        before = self.snapshot()['state']
        status, _ = self.request('/api/command', {'project':'simulation', 'action':'create'})
        self.assertEqual(status, 400)
        self.assertEqual(self.snapshot()['state'], before)

    def test_requirement_drafts_persist_original_text_and_reopen(self):
        text = '做一个本地任务管理页。<script>alert(1)</script>'
        status, first = self.request('/api/draft', {'text': text})
        self.assertEqual(status, 200, first)
        self.assertEqual(first['status'], 'DRAFT')
        self.assertEqual(first['original_requirement'], text)
        self.assertTrue(first['created_at'])
        self.assertEqual(self.request('/api/projects')[1]['projects'], [])
        status, second = self.request('/api/draft', {'id': first['id'], 'text': '补充：无需联网，先做界面。'})
        self.assertEqual(status, 200, second)
        self.assertEqual(second['original_requirement'], text)
        self.assertEqual(len(second['messages']), 2)
        self.assertEqual(self.request('/api/draft/' + first['id'])[1], second)
        from nori.web import WebApp
        reopened = WebApp(self.temp.name)
        self.assertEqual(reopened.read_draft(first['id']), second)
        self.assertEqual(reopened.list_drafts()[0]['id'], first['id'])
        _, html = self.request('/')
        self.assertIn('尚未连接自动规划', html)
        self.assertIn('el.textContent=text', html)
        self.assertNotIn(text, html)
        self.assertEqual(self.request('/api/draft', {'text': ' '})[0], 400)
        self.assertEqual(self.request('/api/draft', {'text': 'test', 'id': '../escape'})[0], 400)
        self.assertEqual(self.request('/api/drafts', headers={'X-Nori-Token': ''})[0], 403)

    def test_one_click_example_repair_requires_separate_approval(self):
        spec = example_spec()
        spec['tasks']['A']['mock_mode'] = 'incomplete'
        self.command('create', spec=spec)
        self.command('approve', digest=self.snapshot()['approval_digest'])
        self.command('step'); self.command('step')
        before = self.snapshot()['state']
        result = self.command('repair-example')['result']
        after = self.snapshot()['state']
        self.assertEqual(after['spec'], before['spec'])
        self.assertEqual(after['tasks']['B']['attempts'], 0)
        self.assertEqual(after['tasks']['A']['evidence']['verdict'], 'FAIL')
        expected = json.loads(json.dumps(spec))
        expected['tasks']['A']['mock_mode'] = 'complete'
        self.assertEqual(result['spec'], expected)
        proposal_id = next(iter(after['proposals']))
        self.command('accept-revision', proposal=proposal_id, digest=result['digest'])
        for _ in range(8):
            self.command('step')
            if self.snapshot()['state']['project_status'] == 'COMPLETED': break
        final = self.snapshot()['state']
        self.assertEqual(final['project_status'], 'COMPLETED')
        self.assertTrue(any(r.get('verification', {}).get('verdict') == 'FAIL' for r in final['runs'].values()))

    def test_mindmap_prototype_is_explicit_and_does_not_create_tasks(self):
        status, html = self.request('/')
        self.assertEqual(status, 200)
        for text in ('Nori 自我迭代', '可操作原型',
                     '交互原型示例 · 不代表真实执行 · 不会自动修改自身',
                     'Agent 自报', '独立验收'):
            self.assertIn(text, html)
        for selector in ('prototypeGraph', 'nodeDetails', 'graphZoomIn', 'graphZoomOut',
                         'graphFit', 'collapseGraph', 'workBoard', 'decisionQueue', 'riskList'):
            self.assertIn('id="' + selector + '"', html)
        self.assertIn('node.dataset.nodeId=item.id', html)
        self.assertIn('不来自实时监控', html)
        self.assertEqual(self.request('/api/projects')[1]['projects'], [])
        self.assertEqual(self.request('/api/drafts')[1]['drafts'], [])

    def test_interactive_map_http_persistence_and_separate_acceptance(self):
        status, maps = self.request('/api/maps')
        self.assertEqual(status, 200)
        self.assertTrue(maps['maps'])
        status, p = self.request('/api/map', {'action':'create', 'title':'HTTP 深层脑图'})
        self.assertEqual(status, 200, p)
        root = p['root_id']
        current = root
        def mutate(action, **values):
            nonlocal p
            code, result = self.request('/api/map', dict(id=p['id'], action=action,
                revision=p['revision'], node_id=current, **values))
            self.assertEqual(code, 200, result)
            p = result
            return result
        mutate('update', title='已改名的脑图')
        for depth in range(5):
            prior = {n['id'] for n in p['nodes']}
            mutate('add_child', title='第'+str(depth+1)+'层')
            current = next(n['id'] for n in p['nodes'] if n['id'] not in prior)
        mutate('update', status='自报完成', progress=100, agent_report='我已经做完（模拟）')
        node = next(n for n in p['nodes'] if n['id']==current)
        self.assertEqual(node['acceptance'], '未验收')
        count = len(p['nodes'])
        mutate('propose_decomposition')
        self.assertEqual(len(p['nodes']), count)
        self.assertTrue(p['proposal'])
        mutate('confirm_decomposition', proposal_id=p['proposal']['id'])
        self.assertGreater(len(p['nodes']), count)
        mutate('update', acceptance='通过（模拟）')
        status, reopened = self.request('/api/map/'+p['id'])
        self.assertEqual(reopened, p)
        code, _ = self.request('/api/map', dict(id=p['id'], action='delete',
            revision=p['revision'], node_id=current))
        self.assertEqual(code, 400)
        mutate('delete', confirmed=True)
        self.assertLess(len(p['nodes']), count)
        code, _ = self.request('/api/map', dict(id=p['id'], action='delete',
            revision=p['revision'], node_id=root, confirmed=True))
        self.assertEqual(code, 400)
        self.assertEqual(self.request('/api/projects')[1]['projects'], [])
        self.assertEqual(self.request('/api/drafts')[1]['drafts'], [])
        self.assertEqual(self.request('/api/map/'+p['id'], headers={'X-Nori-Token':'wrong'})[0], 403)
        self.assertEqual(self.request('/api/map/../escape')[0], 400)

    def test_pause_resume_cancel_and_stale_revision(self):
        self.command('create')
        self.command('approve', digest=self.snapshot()['approval_digest'])
        status, result = self.request('/api/command', {'project':'simulation','action':'pause','revision':-1})
        self.assertEqual(status, 400)
        self.assertIn('Stale', result['error'])
        self.command('pause')
        self.assertEqual(self.snapshot()['state']['project_status'], 'PAUSED')
        self.command('resume')
        self.command('cancel', reason='synthetic test finished')
        self.assertEqual(self.snapshot()['state']['project_status'], 'CANCELLED')

    def test_failure_blocks_dependency_then_revision_repairs(self):
        spec = example_spec()
        spec['tasks']['A']['mock_mode'] = 'incomplete'
        self.command('create', spec=spec)
        self.command('approve', digest=self.snapshot()['approval_digest'])
        self.command('step')
        self.command('step')
        state = self.snapshot()['state']
        self.assertEqual(state['tasks']['A']['evidence']['verdict'], 'FAIL')
        self.assertEqual(state['tasks']['B']['attempts'], 0)
        spec['tasks']['A']['mock_mode'] = 'complete'
        proposal = self.command('propose', proposal='repair', spec=spec, reason='restore missing line')['result']
        self.command('accept-revision', proposal='repair', digest=proposal['digest'])
        for _ in range(8):
            self.command('step')
            if self.snapshot()['state']['project_status'] == 'COMPLETED': break
        state = self.snapshot()['state']
        self.assertEqual(state['project_status'], 'COMPLETED')
        self.assertTrue(any(r.get('verification', {}).get('verdict') == 'FAIL' for r in state['runs'].values()))

    def test_background_pause_and_status_remain_responsive(self):
        spec = example_spec()
        spec['tasks']['A']['mock_mode'] = 'hold'
        self.command('create', spec=spec)
        self.command('approve', digest=self.snapshot()['approval_digest'])
        self.command('run', max_steps=100)
        for _ in range(40):
            snap = self.snapshot()
            if snap['state']['lease']: break
            time.sleep(.02)
        # Pause may conflict with a concurrently committed step; refresh/retry explicitly.
        for _ in range(10):
            revision = self.snapshot()['state']['revision']
            code, result = self.request('/api/command', {'project':'simulation', 'action':'pause', 'revision':revision})
            if code == 200: break
            self.assertIn('Stale', result['error'])
        self.assertEqual(code, 200, result)
        for _ in range(40):
            snap = self.snapshot()
            if not snap['job']['running']: break
            time.sleep(.02)
        self.assertFalse(snap['job']['running'])
        self.assertEqual(snap['state']['project_status'], 'PAUSED')
        self.assertIsNotNone(snap['state']['lease'])

    def test_stale_pause_does_not_stop_background_monitor(self):
        spec = example_spec()
        spec['tasks']['A']['mock_mode'] = 'hold'
        self.command('create', spec=spec)
        self.command('approve', digest=self.snapshot()['approval_digest'])
        self.command('run', max_steps=100)
        for _ in range(50):
            if self.snapshot()['state']['lease']: break
            time.sleep(.02)
        for action in ('pause', 'cancel'):
            code, result = self.request('/api/command', {'project':'simulation','action':action,'revision':-1,'reason':'stale request'})
            self.assertEqual(code, 400)
            self.assertIn('Stale', result['error'])
            self.assertFalse(self.server.app.jobs['simulation']['stop'].is_set())
            self.assertTrue(self.snapshot()['job']['running'])
        self.server.app.jobs['simulation']['stop'].set()

    def test_security_host_origin_token_paths_and_json(self):
        for headers in ({'Host':'evil.example'}, {'Origin':'https://evil.example'}, {'X-Nori-Token':'wrong'}):
            status, _ = self.request('/api/command', {'project':'simulation','action':'create'}, headers)
            self.assertEqual(status, 403)
        self.assertEqual(self.request('/api/projects', headers={'X-Nori-Token':''})[0], 403)
        self.assertEqual(self.request('/api/command', {'project':'../escape','action':'create'})[0], 400)
        self.assertEqual(self.request('/api/command', {'project':'simulation','action':'create'}, {'Content-Type':'text/plain'})[0], 400)
        self.assertEqual(self.request('/etc/passwd')[0], 404)
        self.assertFalse((Path(self.temp.name).parent / 'escape').exists())

    def test_background_errors_are_visible(self):
        self.command('create')
        # Unapproved execution must block rather than approve implicitly.
        self.command('run')
        for _ in range(40):
            snap = self.snapshot()
            if not snap['job']['running']: break
            time.sleep(.025)
        self.assertEqual(snap['state']['project_status'], 'AWAITING_APPROVAL')
        self.assertEqual(snap['job']['result']['result']['status'], 'blocked')
        self.assertEqual(snap['state']['runs'], {})


if __name__ == '__main__':
    unittest.main()
