"""Browser acceptance of persisted requirements and honest synthetic workflows."""
from pathlib import Path
import base64
import sys
import shutil
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nori.web import create_server
from playwright.sync_api import sync_playwright, expect


class FixtureUpdater:
    """Synthetic updater: never connects to a network or replaces executable files."""
    def __init__(self):
        self.pending = False
        self.installed = True
        self.fail = False
        self.calls = []

    def status(self):
        return dict(installed=self.installed, current_version='fixture-1', pending=self.pending,
                    rollback_available=True, next_version='fixture-2' if self.pending else None,
                    previous_version='fixture-0', pending_version='fixture-2' if self.pending else None,
                    rollback_action='cancel_pending' if self.pending else 'rollback',
                    rollback_target_version='fixture-1' if self.pending else 'fixture-0',
                    rollback_token='pending-token' if self.pending else 'current-token')

    def check(self):
        self.calls.append('check')
        time.sleep(.4)
        if self.fail:
            raise RuntimeError('模拟下载源不可用')
        return dict(self.status(), latest_version='fixture-2', commit='a'*40,
                    manifest_digest='b'*64, update_available=True)

    def stage_update(self, expected_commit=None, expected_manifest=None):
        assert expected_commit == 'a'*40 and expected_manifest == 'b'*64
        self.calls.append('stage')
        time.sleep(.4)
        self.pending = True
        return self.status()

    def stage_rollback(self, expected_state=None):
        assert expected_state == self.status()['rollback_token']
        self.calls.append('cancel_pending' if self.pending else 'rollback')
        self.pending = not self.pending
        return self.status()


def main():
    with tempfile.TemporaryDirectory(prefix='nori-browser-') as directory:
        updater = FixtureUpdater()
        server = create_server(Path(directory) / 'data', port=0, updater=updater)
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
                def handle_dialog(dialog):
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
                page.locator('#stageUpdate').click()
                expect(page.locator('#stageUpdate')).to_be_disabled()
                expect(page.locator('#updateStatus')).to_contain_text('已暂存')
                expect(page.locator('#updateCurrent')).to_have_text('fixture-1')
                assert updater.calls == ['check', 'stage']
                expect(page.locator('#rollbackUpdate')).to_have_text('取消待生效更新')
                expect(page.locator('#rollbackTarget')).to_contain_text('fixture-1')
                page.locator('#rollbackUpdate').click()
                expect(page.locator('#rollbackUpdate')).to_have_text('回退到上一版')
                assert updater.calls[-1] == 'cancel_pending'
                page.locator('#backUpdate').click()
                expect(page.locator('#updateDialog')).not_to_be_visible()
                updater.pending = False
                updater.fail = True
                page.locator('#openUpdate').click()
                expect(page.locator('#checkUpdate')).to_be_enabled()
                page.locator('#checkUpdate').click()
                expect(page.locator('#updateError')).to_contain_text('模拟下载源不可用')
                expect(page.locator('#updateCurrent')).to_have_text('fixture-1')
                updater.fail = False
                expect(page.locator('#rollbackTarget')).to_contain_text('fixture-0')
                page.locator('#rollbackUpdate').click()
                expect(page.locator('#updateStatus')).to_contain_text('已暂存')
                assert updater.calls[-1] == 'rollback'
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
                page.locator('#newChat').click()
                requirement = '做一个本地阅读清单，记录书名和阅读状态。先保存需求，不要执行。'
                page.locator('#prompt').fill(requirement)
                page.locator('#openUpdate').click()
                page.locator('#backUpdate').click()
                expect(page.locator('#prompt')).to_have_value(requirement)
                page.locator('#saveDraft').click()
                expect(page.locator('#messages')).to_contain_text(requirement)
                expect(page.locator('#messages')).to_contain_text('尚未规划')
                expect(page.locator('#projectView')).to_be_hidden()
                page.reload()
                expect(page.locator('#draftList button')).to_have_count(1)
                page.locator('#draftList button').click()
                expect(page.locator('#messages')).to_contain_text(requirement)
                page.locator('#messages button').filter(has_text='补充需求').click()
                page.locator('#prompt').fill('补充：数据只保存在本机。')
                page.locator('#saveDraft').click()
                expect(page.locator('#messages')).to_contain_text('补充：数据只保存在本机。')
                expect(page.locator('#messages')).to_contain_text(requirement)
                screenshot('requirement')
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
                browser.close()
                print('Browser acceptance PASS: recursive CRUD, sibling/delete, pan/zoom/collapse, separate report and acceptance, confirmed-only decomposition, persistence; original requirements and kernel synthetic workflow retained.')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__':
    main()
