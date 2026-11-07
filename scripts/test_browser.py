"""Browser acceptance of persisted requirements and honest synthetic workflows."""
from pathlib import Path
import base64
import json
import sys
import shutil
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nori.web import create_server
from playwright.sync_api import sync_playwright, expect


class FixtureUpdater:
    """Synthetic HTTP update/restart contract, never a real process or download.

    Real supervisor lifecycle and task-admission tests live in the Python suite.
    This fixture deliberately separates announced completion from process identity
    so the browser must independently verify all fields before claiming success.
    """
    def __init__(self):
        self.pending = None
        self.current = 'fixture-1'
        self.previous = 'fixture-0'
        self.installed = True
        self.supported = True
        self.block_reason = ''
        self.fail = False
        self.calls = []
        self.instance = 1
        self.restart_state = 'idle'
        self.restart_error = ''
        self.outcome = 'success'
        self.restart_started = None
        self.reject_restart = False
        self.clear_block_during_stage = False

    @staticmethod
    def sha(version):
        import hashlib
        return hashlib.sha256(version.encode()).hexdigest()

    def status(self):
        if self.restart_started and time.monotonic() - self.restart_started > 1:
            if self.outcome == 'success':
                self.previous, self.current = self.current, self.pending
                self.pending = None
                self.instance += 1
                self.restart_state = 'completed'
                self.restart_started = None
            elif self.outcome == 'rolled_back':
                self.pending = None
                self.instance += 1
                self.restart_state = 'rolled_back'
                self.restart_error = '模拟新版健康检查失败，已恢复原版本'
                self.restart_started = None
        result = dict(installed=self.installed, current_version=self.current,
                    active_version=self.current, pending=bool(self.pending),
                    rollback_available=True, next_version=self.pending or self.current,
                    previous_version=self.previous, pending_version=self.pending,
                    running_sha256=self.sha(self.current),
                    pending_sha256=self.sha(self.pending) if self.pending else None,
                    instance_id='fixture-process-'+str(self.instance),
                    restart_supported=self.supported,
                    restart_block_reason=self.block_reason,
                    restart_token='restart-'+str(self.instance)+'-'+str(self.pending),
                    restart_state=self.restart_state, restart_error=self.restart_error,
                    rollback_action='cancel_pending' if self.pending else 'rollback',
                    rollback_target_version=self.current if self.pending else self.previous,
                    rollback_token='pending-token' if self.pending else 'current-token')
        if self.restart_started and self.outcome in ('wrong_instance', 'wrong_digest'):
            result.update(restart_state='completed', current_version=self.pending,
                          running_sha256=self.sha(self.pending),
                          instance_id='fixture-process-'+str(self.instance + 1))
            if self.outcome == 'wrong_instance':
                result['instance_id'] = 'fixture-process-'+str(self.instance)
            else:
                result['running_sha256'] = self.sha(self.current)
        return result

    def action(self, body):
        action = body.get('action')
        if action == 'check':
            self.calls.append('check')
            time.sleep(.4)
            if self.fail:
                raise RuntimeError('模拟下载源不可用')
            return dict(self.status(), latest_version='fixture-2', commit='a'*40,
                        manifest_digest='b'*64, update_available=self.current != 'fixture-2')
        assert body.get('confirmed') is True
        if action == 'stage':
            assert body.get('commit') == 'a'*40 and body.get('manifest_digest') == 'b'*64
            self.calls.append('stage')
            time.sleep(.4)
            self.pending = 'fixture-2'
            if self.clear_block_during_stage:
                self.block_reason = ''
            self.restart_state = 'idle'
            self.restart_error = ''
            return self.status()
        if action == 'rollback':
            assert body.get('rollback_token') == self.status()['rollback_token']
            self.calls.append('cancel_pending' if self.pending else 'rollback')
            self.pending = None if self.pending else self.previous
            self.restart_state = 'idle'
            self.restart_error = ''
            return self.status()
        if action == 'restart':
            assert body.get('restart_token') == self.status()['restart_token']
            self.calls.append('restart')
            if self.reject_restart or self.block_reason:
                raise RuntimeError('模拟任务状态发生变化，重启被阻止')
            assert self.supported and self.pending
            self.restart_state = 'restarting'
            self.restart_started = time.monotonic()
            return dict(restarting=True, restart_state='restarting',
                        target_version=self.pending, target_sha256=self.sha(self.pending))
        raise RuntimeError('Unsupported synthetic update action')


def fixture_update_endpoint(server, updater):
    """Replace only update HTTP behavior; all other app routes remain real."""
    class FixtureHandler(server.RequestHandlerClass):
        def do_GET(self):
            if self.path != '/api/update':
                return super().do_GET()
            if self.allowed():
                self.send(200, updater.status())

        def do_POST(self):
            if self.path != '/api/update':
                return super().do_POST()
            if not self.allowed(mutate=True):
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.send(200, updater.action(body))
            except Exception as exc:
                self.send(400, {'error': str(exc)})
    server.RequestHandlerClass = FixtureHandler


def verify_real_installed_restart(page, screenshot):
    """Real installed supervisor and replacement process, synthetic local release.

    Only the public manifest/download steps are replaced. Restart admission,
    process teardown, candidate health, same-origin authentication, status polls,
    browser verification and the new HTML all use the actual installed program.
    """
    from tests.test_restart import RestartTests
    fixture = RestartTests('test_same_port_auth_identity_data_and_second_restart')
    route_pattern = None
    try:
        fixture.setUp()
        initial = fixture.request()[1]
        code, created = fixture.request({'action': 'create', 'title': 'Browser real restart preservation'}, '/api/map')
        assert code == 200, created
        fixture.data.joinpath('browser-preserve.txt').write_text('synthetic immutable project bytes')
        files = {str(path.relative_to(fixture.data)): path.read_bytes()
                 for path in fixture.data.rglob('*') if path.is_file()}
        actions = []
        offered_commit, offered_manifest = 'e'*40, 'f'*64
        route_pattern = fixture.origin + '/api/update'

        def local_release(route):
            if route.request.method != 'POST':
                route.continue_()
                return
            body = route.request.post_data_json
            actions.append(body['action'])
            if body['action'] == 'check':
                payload = dict(fixture.request()[1], latest_version='9.0.0-test',
                    commit=offered_commit, manifest_digest=offered_manifest, update_available=True)
                route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))
            elif body['action'] == 'stage':
                assert body['confirmed'] is True
                assert body['commit'] == offered_commit and body['manifest_digest'] == offered_manifest
                payload = fixture.stage()
                route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))
            else:
                # The critical restart action must reach the real installed server.
                route.continue_()

        page.route(route_pattern, local_release)
        page.goto(fixture.origin + '/#token=' + fixture.token)
        expect(page.locator('#mapList [data-map-id]').first).to_be_visible()
        page.locator('#openUpdate').click()
        expect(page.locator('#updateCurrent')).to_have_text(initial['current_version'])
        page.locator('#checkUpdate').click()
        expect(page.locator('#updateTarget')).to_have_text('9.0.0-test')
        page.locator('#stageUpdate').click()
        expect(page.locator('#updateStatus')).to_contain_text('已验证目标版本 9.0.0-test', timeout=30000)
        expect(page.locator('#updateCurrent')).to_have_text('9.0.0-test')
        assert actions == ['check', 'stage', 'restart'], actions
        after = fixture.request()[1]
        assert after['restart_state'] == 'completed'
        assert after['instance_id'] != initial['instance_id']
        assert after['running_sha256'] != initial['running_sha256']
        assert page.url == fixture.origin + '/#token=' + fixture.token
        assert fixture.request(headers={'X-Nori-Token': 'wrong'})[0] == 403
        for name, content in files.items():
            assert (fixture.data / name).read_bytes() == content, name
        screenshot('updates-real-restart')
        page.locator('#reloadUpdate').click()
        expect(page.locator('#mapList [data-map-id="'+created['id']+'"]').first).to_be_visible()
        page.locator('#openUpdate').click()
        expect(page.locator('#updateCurrent')).to_have_text('9.0.0-test')
        assert not page.locator('#updateStatus').inner_text().startswith('已验证'), 'A fresh page must not invent a verification receipt'
        screenshot('updates-real-new-page')
        page.locator('#closeUpdate').click()
        print('Real installed browser restart PASS: browser confirmation -> synthetic local staging -> real supervisor replacement -> same port/token -> verified new version/hash/instance -> refreshed HTML; project bytes retained. No network release download used.')
    finally:
        if route_pattern:
            page.unroute(route_pattern)
        assert fixture.doCleanups(), 'Installed browser fixture cleanup failed'


def main():
    with tempfile.TemporaryDirectory(prefix='nori-browser-') as directory:
        updater = FixtureUpdater()
        server = create_server(Path(directory) / 'data', port=0, updater=updater)
        fixture_update_endpoint(server, updater)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=shutil.which('chromium') or None)
                page = browser.new_page(viewport={'width':1440,'height':1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                prompt_answers = []
                confirm_answers = []
                confirm_messages = []
                def handle_dialog(dialog):
                    if dialog.type == 'confirm':
                        confirm_messages.append(dialog.message)
                    if dialog.type == 'prompt':
                        dialog.accept(prompt_answers.pop(0) if prompt_answers else 'Synthetic repair')
                    elif confirm_answers and not confirm_answers.pop(0):
                        dialog.dismiss()
                    else:
                        dialog.accept()
                page.on('dialog', handle_dialog)
                def screenshot(name):
                    Path('dist').mkdir(exist_ok=True)
                    data = page.screenshot(type='jpeg', quality=75, animations='disabled')
                    Path('dist/browser-'+name+'.jpg').write_bytes(data)
                    print('NORI_SCREENSHOT_'+name.upper()+'=' + base64.b64encode(data).decode())
                page.goto(f'http://127.0.0.1:{server.server_port}/#token={server.app.token}')
                expect(page.locator('#mapList [data-map-id]').first).to_be_visible()
                assert not updater.calls, 'Startup must not check or download updates'
                page.locator('#openUpdate').click()
                expect(page.locator('#updateDialog')).to_be_visible()
                expect(page.locator('#updateCurrent')).to_have_text('fixture-1')
                expect(page.locator('#stageUpdate')).to_be_disabled()
                assert not updater.calls, 'Opening panel must be local-only'
                page.locator('#checkUpdate').click()
                expect(page.locator('#checkUpdate')).to_be_disabled()
                expect(page.locator('#updateTarget')).to_have_text('fixture-2')
                expect(page.locator('#stageUpdate')).to_be_enabled()
                assert updater.calls == ['check'], 'Checking must not download'
                expect(page.locator('#updateCommit')).to_contain_text('a'*40)
                screenshot('updates')
                confirm_answers.append(False)
                page.locator('#stageUpdate').click()
                assert updater.calls == ['check'], 'Cancelled confirmation must not download'
                assert 'fixture-2' in confirm_messages[-1] and '自动' in confirm_messages[-1]
                page.locator('#stageUpdate').click()
                expect(page.locator('#stageUpdate')).to_be_disabled()
                expect(page.locator('#closeUpdate')).to_be_disabled()
                page.keyboard.press('Escape')
                expect(page.locator('#updateDialog')).to_be_visible()
                # Even direct repeated handler invocation cannot send a duplicate mutation.
                page.evaluate("document.getElementById('stageUpdate').onclick()")
                expect(page.locator('#updateCurrent')).to_have_text('fixture-1')
                expect(page.locator('#updateStatus')).to_contain_text('已验证目标版本 fixture-2', timeout=10000)
                expect(page.locator('#updateCurrent')).to_have_text('fixture-2')
                expect(page.locator('#reloadUpdate')).to_be_visible()
                assert updater.calls == ['check', 'stage', 'restart']
                screenshot('updates-restarted')

                # Rollback also applies automatically, only after target confirmation.
                confirm_answers.append(False)
                page.locator('#rollbackUpdate').click()
                assert updater.calls[-1] == 'restart'
                page.locator('#rollbackUpdate').click()
                expect(page.locator('#updateStatus')).to_contain_text('已验证目标版本 fixture-1', timeout=10000)
                assert updater.calls[-2:] == ['rollback', 'restart']
                expect(page.locator('#rollbackTarget')).to_contain_text('fixture-2')

                # Active and unknown task states permit staging, never implicit task stop.
                updater.block_reason = '活动任务尚在执行，请先暂停并确认退出。'
                page.locator('#checkUpdate').click()
                expect(page.locator('#stageUpdate')).to_be_enabled()
                expect(page.locator('#updateBlock')).to_contain_text('活动任务')
                page.locator('#stageUpdate').click()
                expect(page.locator('#updateStatus')).to_contain_text('已下载并校验')
                expect(page.locator('#restartUpdate')).to_be_disabled()
                expect(page.locator('#updateCurrent')).to_have_text('fixture-1')
                assert updater.calls[-1] == 'stage'
                updater.block_reason = '任务退出状态不明，请先核对退出。'
                page.locator('#refreshUpdate').click()
                expect(page.locator('#updateBlock')).to_contain_text('状态不明')
                expect(page.locator('#restartUpdate')).to_be_disabled()
                screenshot('updates-blocked')
                confirm_answers.append(False)
                page.locator('#rollbackUpdate').click()
                assert updater.calls[-1] == 'stage'
                page.locator('#rollbackUpdate').click()
                expect(page.locator('#updateStatus')).to_contain_text('已取消待生效更新')
                assert updater.calls[-1] == 'cancel_pending'
                updater.block_reason = ''

                # Permission to stage only does not become restart permission merely
                # because a blocker disappears while the download is in progress.
                updater.block_reason = '活动任务正在退出。'
                updater.clear_block_during_stage = True
                page.locator('#checkUpdate').click()
                expect(page.locator('#stageUpdate')).to_be_enabled()
                page.locator('#stageUpdate').click()
                expect(page.locator('#updateStatus')).to_contain_text('本次只获准下载')
                expect(page.locator('#restartUpdate')).to_be_enabled()
                assert updater.calls[-1] == 'stage'
                updater.clear_block_during_stage = False
                page.locator('#rollbackUpdate').click()
                expect(page.locator('#updateStatus')).to_contain_text('已取消待生效更新')

                # A task can become unsafe after checking/downloading. Rejection keeps
                # the staged candidate and offers a fresh explicit restart, not a retry.
                updater.reject_restart = True
                page.locator('#checkUpdate').click()
                expect(page.locator('#stageUpdate')).to_be_enabled()
                page.locator('#stageUpdate').click()
                expect(page.locator('#updateError')).to_contain_text('重启被阻止')
                expect(page.locator('#updateCurrent')).to_have_text('fixture-1')
                expect(page.locator('#restartUpdate')).to_be_enabled()
                rejected_calls = len(updater.calls)
                updater.reject_restart = False
                updater.outcome = 'rolled_back'
                page.locator('#restartUpdate').click()
                expect(page.locator('#updateError')).to_contain_text('健康检查失败', timeout=10000)
                expect(page.locator('#updateStatus')).to_contain_text('服务已回退')
                expect(page.locator('#updateCurrent')).to_have_text('fixture-1')
                expect(page.locator('#reloadUpdate')).to_be_hidden()
                assert len(updater.calls) == rejected_calls + 1

                # A completed label alone is insufficient: both a wrong release hash
                # and the old process identity must remain unverified until timeout.
                page.clock.install()
                for outcome in ('wrong_digest', 'wrong_instance'):
                    updater.outcome = outcome
                    page.locator('#checkUpdate').click()
                    expect(page.locator('#stageUpdate')).to_be_enabled()
                    page.locator('#stageUpdate').click()
                    expect(page.locator('#updateStatus')).to_contain_text('尚未匹配目标', timeout=10000)
                    expect(page.locator('#reloadUpdate')).to_be_hidden()
                    mutation_count = len(updater.calls)
                    page.clock.fast_forward(91000)
                    expect(page.locator('#updateError')).to_contain_text('90 秒内未核实', timeout=10000)
                    expect(page.locator('#retryReconnect')).to_be_visible()
                    expect(page.locator('#stageUpdate')).to_be_disabled()
                    screenshot('updates-unverified-'+outcome)
                    updater.outcome = 'success'
                    page.locator('#retryReconnect').click()
                    expect(page.locator('#updateStatus')).to_contain_text('已验证目标版本 fixture-2', timeout=10000)
                    assert len(updater.calls) == mutation_count, 'Reconnect must be read-only'
                    page.locator('#rollbackUpdate').click()
                    expect(page.locator('#updateStatus')).to_contain_text('已验证目标版本 fixture-1', timeout=10000)
                page.clock.resume()

                # Losing the POST response does not mean rejection. Reconnect, then
                # reload mid-handoff; session recovery never resends the mutation.
                updater.outcome = 'waiting'
                page.locator('#checkUpdate').click()
                expect(page.locator('#stageUpdate')).to_be_enabled()
                def lose_restart_response(route):
                    if route.request.method == 'POST' and route.request.post_data_json.get('action') == 'restart':
                        response = route.fetch()
                        assert response.ok
                        route.abort('connectionreset')
                    else:
                        route.continue_()
                page.route('**/api/update', lose_restart_response)
                page.locator('#stageUpdate').click()
                expect(page.locator('#updateStatus')).to_contain_text('等待服务重启', timeout=10000)
                mutation_count = len(updater.calls)
                page.reload()
                expect(page.locator('#updateDialog')).to_be_visible()
                expect(page.locator('#updateStatus')).to_contain_text('等待服务重启', timeout=10000)
                page.goto('about:blank')
                page.go_back()
                expect(page.locator('#updateDialog')).to_be_visible()
                expect(page.locator('#updateStatus')).to_contain_text('等待服务重启', timeout=10000)
                updater.outcome = 'success'
                expect(page.locator('#updateStatus')).to_contain_text('已验证目标版本 fixture-2', timeout=10000)
                assert len(updater.calls) == mutation_count, 'Reload/Back recovery must not repeat restart'
                page.unroute('**/api/update', lose_restart_response)
                expect(page.locator('#reloadUpdate')).to_be_visible()
                page.locator('#reloadUpdate').click()
                expect(page.locator('#updateDialog')).not_to_be_visible()
                expect(page.locator('#mapList [data-map-id]').first).to_be_visible()

                # Check failures preserve actual running identity and remain retryable.
                updater.fail = True
                page.locator('#openUpdate').click()
                expect(page.locator('#checkUpdate')).to_be_enabled()
                page.locator('#checkUpdate').click()
                expect(page.locator('#updateError')).to_contain_text('模拟下载源不可用')
                expect(page.locator('#updateCurrent')).to_have_text('fixture-2')
                updater.fail = False
                page.locator('#backUpdate').click()
                expect(page.locator('#updateDialog')).not_to_be_visible()

                # Legacy/source modes never promise an unsupported automatic restart.
                updater.supported = False
                updater.block_reason = '旧启动器需要最后一次停止并启动 Nori，无需重新安装。'
                page.locator('#openUpdate').click()
                expect(page.locator('#updateBlock')).to_contain_text('最后一次')
                expect(page.locator('#rollbackUpdate')).to_have_text('暂存上一版本')
                page.locator('#rollbackUpdate').click()
                expect(page.locator('#updateStatus')).to_contain_text('无需重新安装')
                expect(page.locator('#restartUpdate')).to_be_disabled()
                assert updater.calls[-1] == 'rollback'
                page.locator('#rollbackUpdate').click()
                expect(page.locator('#updateStatus')).to_contain_text('已取消待生效更新')
                page.locator('#closeUpdate').click()
                updater.installed = False
                page.set_viewport_size({'width':390,'height':844})
                expect(page.locator('#openUpdate')).to_be_visible()
                page.locator('#openUpdate').click()
                expect(page.locator('#updateStatus')).to_contain_text('源码运行不支持自更新')
                expect(page.locator('#checkUpdate')).to_be_disabled()
                page.keyboard.press('Escape')
                expect(page.locator('#updateDialog')).not_to_be_visible()
                page.set_viewport_size({'width':1440,'height':1000})
                screenshot('projects')
                page.locator('#mapList [data-map-id="nori-example"]').click()
                expect(page.locator('#prototypeBoard')).to_be_visible()
                expect(page.locator('#prototypeGraph')).to_contain_text('Nori 自我迭代')
                expect(page.locator('#prototypeBoard')).to_contain_text('不会自动修改自身')
                page.locator('#graphFit').click()
                screenshot('home')
                page.locator('#backMaps').click()
                prompt_answers.append('递归脑图交互验收')
                page.locator('#newMap').click()
                expect(page.locator('#nodeTitle')).to_have_value('递归脑图交互验收')
                page.locator('#nodeTitle').fill('递归脑图验收 · 已重命名')
                page.locator('#saveNode').click()
                expect(page.locator('#mapTitle')).to_have_text('递归脑图验收 · 已重命名')
                for level in range(1, 6):
                    title = f'第{level}层子任务'
                    prompt_answers.append(title)
                    page.locator('#addChild').click()
                    expect(page.locator('#nodeTitle')).to_have_value(title)
                expect(page.locator('#prototypeGraph .mind-node')).to_have_count(6)
                prompt_answers.append('待删除同级')
                page.locator('#addSibling').click()
                expect(page.locator('#nodeTitle')).to_have_value('待删除同级')
                page.locator('#deleteNode').click()
                expect(page.locator('#prototypeGraph .mind-node')).to_have_count(6)
                page.locator('#prototypeGraph .mind-node').filter(has_text='第5层子任务').click()
                page.locator('#nodeStatus').select_option('自报完成')
                page.locator('#nodeProgress').fill('100')
                page.locator('#nodeAgentReport').fill('原型自报已完成，尚未验收')
                page.locator('#saveReport').click()
                expect(page.locator('#nodeStatus')).to_have_value('自报完成')
                expect(page.locator('#nodeAcceptance')).to_have_value('未验收')
                count = page.locator('#prototypeGraph .mind-node').count()
                page.locator('#suggestSplit').click()
                expect(page.locator('#confirmSplit')).to_be_visible()
                expect(page.locator('#prototypeGraph .mind-node')).to_have_count(count)
                page.locator('#confirmSplit').click()
                expect(page.locator('#prototypeGraph .mind-node')).to_have_count(count + 3)
                expect(page.locator('#nodeDetails .childlink')).to_have_count(3)
                page.locator('#toggleNode').click()
                expect(page.locator('#prototypeGraph .mind-node')).to_have_count(count)
                page.locator('#toggleNode').click()
                expect(page.locator('#prototypeGraph .mind-node')).to_have_count(count + 3)
                page.locator('#graphZoomIn').click()
                page.locator('#graphZoomIn').click()
                viewport = page.locator('#prototypeViewport')
                box = viewport.bounding_box()
                transform_before = page.locator('#prototypeGraph').evaluate('(e)=>e.style.transform')
                page.mouse.move(box['x']+220, box['y']+12)
                page.mouse.down()
                page.mouse.move(box['x']+60, box['y']+12, steps=8)
                page.mouse.up()
                assert page.locator('#prototypeGraph').evaluate('(e)=>e.style.transform') != transform_before, 'Canvas did not pan'
                page.locator('#graphFit').click()
                screenshot('prototype-detail')
                page.reload()
                expect(page.locator('#mapList [data-map-id]')).to_have_count(2)
                page.locator('#mapList [data-map-id]').filter(has_text='递归脑图验收').click()
                expect(page.locator('#prototypeGraph .mind-node')).to_have_count(count + 3)
                page.locator('#prototypeGraph .mind-node').filter(has_text='第5层子任务').click()
                expect(page.locator('#nodeAgentReport')).to_have_value('原型自报已完成，尚未验收')
                expect(page.locator('#nodeAcceptance')).to_have_value('未验收')
                from nori.intake import STANDARD_TEMPLATE
                document = json.loads(json.dumps(STANDARD_TEMPLATE))
                document['title'] = '外部导入阅读清单'
                document['original_request'] = '<script>window.syntheticInjection=true</script> 做本地阅读清单'
                document['nodes'][0]['acceptance_criteria'] = ['能够看到保存的书名']
                raw = json.dumps(document, ensure_ascii=False)
                before_count = len(server.app.prototypes.list_projects())
                page.locator('#newChat').click()
                expect(page.locator('#rolePrompt')).not_to_have_value('')
                page.evaluate("Object.defineProperty(navigator, 'clipboard', {value:{writeText:async()=>{throw Error('synthetic denied')}}, configurable:true})")
                page.locator('#copyRolePrompt').click()
                expect(page.locator('#copyStatus')).to_contain_text('已选中全文')
                page.locator('#intakeJson').fill('{')
                page.locator('#previewIntake').click()
                expect(page.locator('#intakeError')).not_to_have_text('')
                expect(page.locator('#confirmIntake')).to_be_disabled()
                page.locator('#intakeFile').set_input_files({'name':'requirements.json','mimeType':'application/json','buffer':raw.encode()})
                expect(page.locator('#intakeJson')).to_have_value(raw)
                page.locator('#previewIntake').click()
                expect(page.locator('#intakeReadable')).to_contain_text('能够看到保存的书名')
                assert len(server.app.prototypes.list_projects()) == before_count
                page.locator('#cancelIntake').click()
                expect(page.locator('#intakePreview')).to_be_hidden()
                assert len(server.app.prototypes.list_projects()) == before_count
                page.locator('#intakeJson').fill(raw)
                page.locator('#previewIntake').click()
                expect(page.locator('#confirmIntake')).to_be_enabled()
                page.locator('#intakeJson').fill(raw + ' ')
                expect(page.locator('#confirmIntake')).to_be_disabled()
                page.locator('#previewIntake').click()
                expect(page.locator('#confirmIntake')).to_be_enabled()
                screenshot('intake-preview')
                def verify_pending_import(route):
                    for selector in ('#cancelIntake', '#intakeJson', '#intakeFile', '#closeEditor'):
                        expect(page.locator(selector)).to_be_disabled()
                    expect(page.locator('#intakeError')).to_contain_text('提交后不能撤回')
                    route.continue_()
                page.route('**/api/requirements/import', verify_pending_import, times=1)
                page.locator('#confirmIntake').click()
                expect(page.locator('#importedReadable')).to_contain_text(document['original_request'])
                expect(page.locator('#modeLabel')).to_contain_text('未批准')
                expect(page.locator('#nodeAcceptance')).to_have_count(0)
                expect(page.locator('#nodeAgentReport')).to_have_count(0)
                assert page.evaluate('window.syntheticInjection') is None
                imported = next(x for x in server.app.prototypes.list_projects() if x['title'] == document['title'])
                assert len(server.app.prototypes.list_projects()) == before_count + 1
                page.reload()
                page.locator('#mapList [data-map-id="'+imported['id']+'"]').click()
                expect(page.locator('#importedReadable')).to_contain_text('能够看到保存的书名')
                loaded = server.app.prototypes.get_project(imported['id'])
                server.app.prototypes.mutate(imported['id'], 'update', expected_revision=loaded['revision'],
                    node_id=loaded['root_id'], acceptance_criteria=['只属于当前编辑，不是原始条件'])
                page.reload()
                page.locator('#mapList [data-map-id="'+imported['id']+'"]').click()
                expect(page.locator('#nodeDetails')).to_contain_text('能够看到保存的书名')
                expect(page.locator('#nodeDetails')).not_to_contain_text('只属于当前编辑，不是原始条件')
                expect(page.locator('#importedReadable')).to_contain_text('能够看到保存的书名')
                screenshot('requirement')
                page.locator('#newChat').click()
                page.locator('#intakeJson').fill(raw)
                page.locator('#previewIntake').click()
                expect(page.locator('#confirmIntake')).to_be_enabled()
                page.locator('#confirmIntake').click()
                expect(page.locator('#importedReadable')).to_be_visible()
                assert len(server.app.prototypes.list_projects()) == before_count + 1
                assert not list((Path(directory)/'data').glob('*/control/state.json'))
                page.locator('#newChat').click()
                page.locator('#showDemo').click()
                page.locator('#newid').fill('browser-success')
                page.locator('#create').click()
                expect(page.locator('#conversationTitle')).to_contain_text('browser-success')
                expect(page.locator('[data-action="run"]')).to_be_disabled()
                page.locator('[data-action="approve"]').click()
                expect(page.locator('#status')).to_contain_text('ACTIVE')
                page.locator('[data-action="run"]').click()
                expect(page.locator('#status')).to_contain_text('COMPLETED', timeout=15000)
                expect(page.locator('#evidence')).to_contain_text('PASS')
                page.locator('#newChat').click()
                page.locator('#showDemo').click()
                page.locator('#newid').fill('browser-repair')
                page.locator('#createFailure').click()
                expect(page.locator('#conversationTitle')).to_contain_text('browser-repair')
                page.locator('[data-action="approve"]').click()
                page.locator('[data-action="run"]').click()
                expect(page.locator('#tasks')).to_contain_text('验收失败，待返工', timeout=15000)
                expect(page.locator('#tasks')).to_contain_text('依赖：A')
                expect(page.locator('[data-action="run"]')).to_be_disabled()
                expect(page.locator('#workBoard')).to_contain_text('A')
                expect(page.locator('#decisionQueue')).not_to_be_empty()
                expect(page.locator('#riskList')).not_to_be_empty()
                page.locator('#workBoard [data-node-id="task-A"]').click()
                expect(page.locator('#nodeDetails')).to_contain_text('真实记录')
                expect(page.locator('#nodeDetails')).to_contain_text('实际输出未满足验收要求')
                page.wait_for_timeout(1600)
                expect(page.locator('#nodeDetails')).to_contain_text('实际输出未满足验收要求')
                screenshot('risk')
                page.locator('#repairExample').click()
                expect(page.locator('#proposals button')).to_have_count(1)
                page.locator('#proposals button').click()
                expect(page.locator('#proposals button')).to_have_count(0)
                page.locator('[data-action="run"]').click()
                expect(page.locator('#status')).to_contain_text('COMPLETED', timeout=15000)
                expect(page.locator('#raw')).to_contain_text('FAIL')
                expect(page.locator('#evidence')).to_contain_text('PASS')
                assert not errors, errors
                expect(page.locator('#metricTotal')).to_have_text('2')
                expect(page.locator('#metricDone')).to_have_text('2')
                screenshot('result')
                verify_real_installed_restart(page, screenshot)
                assert not errors, errors
                browser.close()
                print('Browser acceptance PASS: synthetic update/restart confirmation, task blocks, rollback, identity verification, bounded reconnect, reload recovery and migration; recursive CRUD, sibling/delete, pan/zoom/collapse, separate report and acceptance, confirmed-only decomposition, persistence; original requirements and kernel synthetic workflow retained.')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__':
    main()
