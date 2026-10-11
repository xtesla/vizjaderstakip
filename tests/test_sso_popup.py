"""Offline reproduction of Extranet's modal above a duplicate SIS link."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_monitor as monitor

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

HTML = '''<!doctype html>
<html><head><title>Extranet test</title><style>
  #background {position:fixed;left:12px;top:95px}
  .ui-widget-overlay {position:fixed;inset:0;background:#0009;z-index:100}
  .ui-dialog {position:fixed;left:35%;top:25%;width:360px;height:180px;
              background:white;border:1px solid #555;z-index:101;padding:20px}
</style></head><body>
<form id="login-form"><input id="login"/><input type="password" id="haslo"/><input type="submit"/></form>
<div id="background"><a href="/pulpit">Log in to the new SIS</a></div>
<div id="modal-root"></div>
<script>
document.querySelector('#login-form').addEventListener('submit', e => {
 e.preventDefault();e.target.remove();
 setTimeout(() => document.querySelector('#modal-root').innerHTML = `
 <div class="ui-widget-overlay"></div>
 <section class="ui-dialog" role="dialog" aria-label="Student Information System">
   <h3>Student Information System</h3>
   <a id="sis-real" href="#" onclick="event.preventDefault();window.__clicked='sis'">Log in to the new SIS</a>
 </section>`, 300);
});
</script></body></html>'''


@unittest.skipIf(sync_playwright is None, 'Playwright not installed')
class SsoModalTests(unittest.TestCase):
    def test_prefers_clickable_modal_anchor_over_background(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=monitor.chrome_binary(), args=['--no-sandbox'])
            try:
                ctx = browser.new_context()
                page = ctx.new_page()
                page.set_content(HTML)
                page.locator('#login').fill('test')
                page.locator('#haslo').fill('test')
                page.locator('input[type=submit]').click()
                page.wait_for_selector('.ui-dialog #sis-real')
                candidates = monitor.get_sis_candidates(ctx, extranet_domain='about:blank')
                self.assertEqual(len(candidates), 2)
                chosen = next(c for c in candidates if c[1]['unobstructed'] and c[1]['modal'])
                self.assertTrue(chosen[1]['modal'])
                self.assertTrue(chosen[1]['unobstructed'])
                monitor.click_sis_anchor(ctx, chosen)
                self.assertEqual(page.evaluate('window.__clicked'), 'sis')
            finally:
                browser.close()

if __name__ == '__main__':
    unittest.main()
