"""Browser acceptance of persisted requirements and honest synthetic workflows."""
from pathlib import Path
import base64
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nori.web import create_server
from playwright.sync_api import sync_playwright, expect


def main():
    with tempfile.TemporaryDirectory(prefix='nori-browser-') as directory:
        server = create_server(Path(directory) / 'data', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={'width':1440,'height':1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('dialog', lambda dialog: dialog.accept('Synthetic repair') if dialog.type == 'prompt' else dialog.accept())
                def screenshot(name):
                    Path('dist').mkdir(exist_ok=True)
                    data = page.screenshot(type='jpeg', quality=75)
                    Path('dist/browser-'+name+'.jpg').write_bytes(data)
                    print('NORI_SCREENSHOT_'+name.upper()+'=' + base64.b64encode(data).decode())
                page.goto(f'http://127.0.0.1:{server.server_port}/#token={server.app.token}')
                expect(page.locator('#prototypeBoard')).to_be_visible()
                expect(page.locator('#prototypeGraph')).to_contain_text('Nori 自我迭代')
                expect(page.locator('#prototypeBoard')).to_contain_text('不会自动修改自身')
                page.locator('#graphFit').click()
                screenshot('home')
                page.locator('#prototypeGraph .mind-node').nth(2).click()
                expect(page.locator('#nodeDetails')).not_to_be_empty()
                page.locator('#graphZoomIn').click()
                page.locator('#graphZoomOut').click()
                page.locator('#collapseGraph').click()
                page.locator('#collapseGraph').click()
                page.locator('#graphFit').click()
                screenshot('prototype-detail')
                page.locator('#newChat').click()
                requirement = '做一个本地阅读清单，记录书名和阅读状态。先保存需求，不要执行。'
                page.locator('#prompt').fill(requirement)
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
                print('Browser acceptance PASS: persistent original requirement and supplement; no fake execution; approval; synthetic failure/dependency block/repair approval/final PASS.')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__':
    main()
