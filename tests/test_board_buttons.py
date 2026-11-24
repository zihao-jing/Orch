import unittest
from pathlib import Path

try:
    from playwright.sync_api import Error, expect, sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, 'browser tests require Playwright')
class TestBoardButtons(unittest.TestCase):
    def setUp(self):
        driver = sync_playwright().start()
        self.addCleanup(driver.stop)
        try:
            browser = driver.chromium.launch()
        except Error as exc:
            if 'Executable doesn\'t exist' in str(exc):
                self.skipTest('Playwright Chromium is not installed')
            raise
        self.addCleanup(browser.close)
        self.page = browser.new_page()
        html = (Path(__file__).resolve().parents[1] / 'orch/board.html').read_text()
        self.page.route('http://board.test/', lambda route: route.fulfill(
            content_type='text/html', body=html.replace('__MONITORS__', '["example"]')))
        self.page.add_init_script('''
          window.snapshot = {monitor:'example', run_id:'1', status:'running', managed:{},
            discussion:true, discussion_supported:true, night:false};
          window.requests = [];
          window.EventSource = class extends EventTarget { constructor(){ super(); window.events = this; } close(){} };
          window.fetch = async (url, options) => {
            if (options?.method === 'POST') {
              requests.push(JSON.parse(options.body));
              return new Promise(resolve => window.reply = (status = 200) => resolve(
                new Response(JSON.stringify(status === 200 ? {ok:true} : {error:'Rejected'}), {status})));
            }
            return new Response(JSON.stringify(url.startsWith('api/state') ? snapshot : {}));
          };
        ''')
        self.page.goto('http://board.test/')
        expect(self.page.locator('#title')).to_have_text('example')

    def snapshot(self, changes):
        self.page.evaluate("changes => {Object.assign(snapshot, changes); events.dispatchEvent(new Event('state'));}", changes)

    def test_night_waits_for_state_not_just_post(self):
        night = self.page.locator('#night')
        night.click()
        self.page.locator('#ask-ok').click()
        expect(night).to_have_attribute('aria-busy', 'true')
        self.page.evaluate('reply()')
        self.snapshot({'monitor': 'still off'})
        expect(self.page.locator('#title')).to_have_text('still off')
        expect(night).to_be_disabled()
        self.snapshot({'night_pending': True})
        expect(night).to_have_text('Turning night mode on…')
        self.snapshot({'night_pending': None, 'night': True})
        expect(night).to_have_text('🌙 Night: on')
        expect(night).not_to_have_attribute('aria-busy', 'true')

    def test_apply_locks_both_entry_points_and_recovers_from_error(self):
        apply = self.page.locator('#applydiscussion')
        self.snapshot({'managed': {'discussion_saved': True}})
        panel_apply = self.page.locator('[data-applydiscussion]')
        panel_apply.click()
        expect(apply).to_have_attribute('aria-busy', 'true')
        self.snapshot({'managed': {'discussion_saved': True, 'discussion_error': 'Changed gate'}})
        expect(self.page.locator('#needs')).to_contain_text('Changed gate')
        expect(panel_apply).to_be_disabled()
        self.page.locator('#discuss').evaluate('button => button.click()')
        apply.evaluate('button => button.click()')
        self.assertEqual(len(self.page.evaluate('requests')), 1)
        self.page.evaluate('reply(409)')
        expect(apply).to_be_enabled()
        expect(panel_apply).to_be_enabled()
        expect(apply).not_to_have_attribute('aria-busy', 'true')
        apply.evaluate('button => button.click()')
        self.page.evaluate('reply()')
        self.snapshot({'discussion': False, 'discussion_applying': True, 'managed': {}})
        expect(apply).to_have_text('Processing discussion…')
        expect(apply).to_have_attribute('aria-busy', 'true')
        self.snapshot({'discussion_applying': False})
        expect(apply).to_be_hidden()
