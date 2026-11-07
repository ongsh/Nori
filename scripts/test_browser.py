"""Real browser acceptance on synthetic app-owned temporary projects (CI only)."""
from pathlib import Path
import json
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
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('dialog', lambda dialog: dialog.accept('Synthetic repair') if dialog.type == 'prompt' else dialog.accept())
                page.goto(f'http://127.0.0.1:{server.server_port}/#token={server.app.token}')
                page.locator('#newid').fill('browser-success')
                page.locator('#create').click()
                expect(page.locator('#title')).to_contain_text('browser-success')
                page.locator('[data-action="approve"]').click()
                expect(page.locator('#status')).to_contain_text('ACTIVE')
                page.locator('[data-action="run"]').click()
                expect(page.locator('#status')).to_contain_text('COMPLETED', timeout=15000)
                expect(page.locator('#evidence')).to_contain_text('PASS')
                page.locator('#newid').fill('browser-repair')
                page.locator('#createFailure').click()
                expect(page.locator('#title')).to_contain_text('browser-repair')
                page.locator('[data-action="approve"]').click()
                expect(page.locator('#status')).to_contain_text('ACTIVE')
                page.locator('[data-action="step"]').click()
                expect(page.locator('#status')).not_to_contain_text('运行许可 空闲')
                page.locator('[data-action="step"]').click()
                expect(page.locator('#tasks')).to_contain_text('REWORK')
                expect(page.locator('#tasks')).to_contain_text('waiting for A')
                plan = json.loads(page.locator('#spec').input_value())
                plan['tasks']['A']['mock_mode'] = 'complete'
                page.locator('#spec').fill(json.dumps(plan))
                page.locator('#propose').click()
                expect(page.locator('#proposals button')).to_have_count(1)
                page.locator('#proposals button').click()
                expect(page.locator('#proposals button')).to_be_disabled()
                page.locator('[data-action="run"]').click()
                expect(page.locator('#status')).to_contain_text('COMPLETED', timeout=15000)
                expect(page.locator('#raw')).to_contain_text('FAIL')
                expect(page.locator('#evidence')).to_contain_text('PASS')
                assert not errors, errors
                screenshot = Path('dist/browser-smoke.png')
                screenshot.parent.mkdir(exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=True)
                browser.close()
                print('Browser acceptance PASS: create/approve/run; failure/dependency block/revision/rework/final PASS; history retained.')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__':
    main()
