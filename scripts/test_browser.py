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
                expect(page.locator('#showDemo')).to_be_visible()
                screenshot('home')
                requirement = '做一个本地阅读清单，记录书名和阅读状态。先保存需求，不要执行。'
                page.locator('#prompt').fill(requirement)
                page.locator('#saveDraft').click()
                expect(page.locator('#messages')).to_contain_text(requirement)
                expect(page.locator('#messages')).to_contain_text('尚未自动拆解计划')
                expect(page.locator('#projectView')).to_be_hidden()
                page.reload()
                expect(page.locator('#draftList button')).to_have_count(1)
                page.locator('#draftList button').click()
                expect(page.locator('#messages')).to_contain_text(requirement)
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
                expect(page.locator('#tasks')).to_contain_text('等待 A 验收通过')
                expect(page.locator('[data-action="run"]')).to_be_disabled()
                page.locator('#repairExample').click()
                expect(page.locator('#proposals button')).to_have_count(1)
                page.locator('#proposals button').click()
                expect(page.locator('#proposals button')).to_have_count(0)
                page.locator('[data-action="run"]').click()
                expect(page.locator('#status')).to_contain_text('COMPLETED', timeout=15000)
                expect(page.locator('#raw')).to_contain_text('FAIL')
                expect(page.locator('#evidence')).to_contain_text('PASS')
                assert not errors, errors
                screenshot('result')
                browser.close()
                print('Browser acceptance PASS: persistent original requirement and supplement; no fake execution; approval; synthetic failure/dependency block/repair approval/final PASS.')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__':
    main()
