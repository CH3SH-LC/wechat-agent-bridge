"""Reject retired UI paths and unsupported real sends without touching WeChat."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_bridge import FixtureCase, ROOT
from wechat_bridge.api import tool_list
from wechat_bridge.common import atomic_json
from wechat_bridge.config import load_config
from wechat_bridge.desktop_identity import verify_paths
from wechat_bridge.diagnostics import initialize_live


class BackgroundBoundaryTests(FixtureCase):
    def test_disabled_text_and_file_never_enqueue_or_call_sender(self):
        self.cfg.sending_mode = 'disabled'
        self.bridge.sender = Mock()
        staged = self.bridge.stage(self.root / 'demo' / 'uploads' / 'hello.txt')
        self.reject('background_sender_unavailable', lambda: self.send())
        self.reject('background_sender_unavailable', lambda: self.bridge.send(
            'demo_team@chatroom', 'file', attachment_id=staged['id']))
        self.assertIsNone(self.bridge.store.next_job())
        self.bridge.sender.send.assert_not_called()

    def test_old_queued_desktop_job_cannot_execute_even_when_resumed(self):
        job = self.bridge.store.enqueue('old-ui-job', {
            'chat_id': 'demo_team@chatroom', 'title': '隔离测试群',
            'type': 'text', 'text': 'old', 'mode': 'desktop'}, 'queued')
        self.cfg.sending_mode = 'disabled'
        self.bridge.sender = Mock()
        self.bridge.store.set_paused(False)
        result = self.bridge.dispatch_once()
        self.assertEqual(result['id'], job['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['result']['reason'], 'background_sender_unavailable')
        self.bridge.sender.send.assert_not_called()

    def test_status_never_claims_preview_or_mock_is_real_sending(self):
        for mode in ('preview', 'mock', 'disabled'):
            self.cfg.sending_mode = mode
            caps = self.bridge.status()['capabilities']
            self.assertFalse(caps['real_send_text'])
            self.assertFalse(caps['real_send_file'])
            self.assertFalse(caps['ui_automation'])
            self.assertIsNone(caps['real_send_transport'])

    def test_legacy_read_config_ignores_ui_options_but_rejects_desktop_mode(self):
        raw = json.loads(self.config_path.read_text('utf-8'))
        raw.update(desktop_window=777, desktop_accessibility='scoped_qt',
                   desktop_dll_sha256='a'*64, send_hotkey='{Enter}')
        atomic_json(self.config_path, raw)
        cfg = load_config(self.config_path)
        self.assertEqual(cfg.sending_mode, 'preview')
        self.assertFalse(hasattr(cfg, 'desktop_accessibility'))
        raw['sending_mode'] = 'desktop'
        atomic_json(self.config_path, raw)
        self.reject('invalid_config', lambda: load_config(self.config_path))

    def test_retired_draft_tool_is_unavailable(self):
        self.assertNotIn('wechat_clear_failed_draft', [x['name'] for x in tool_list()])
        self.reject('unknown_tool', lambda: self.invoke('wechat_clear_failed_draft', id='old'))

    def test_no_ui_module_or_dependency_in_distributable_source(self):
        package = ROOT / 'src' / 'wechat_bridge'
        self.assertFalse((package / 'desktop.py').exists())
        self.assertFalse((package / 'qt_accessibility.py').exists())
        source = '\n'.join(p.read_text('utf-8') for p in package.glob('*.py'))
        for symbol in ('uiautomation', 'SetForegroundWindow', 'SendKeys', 'win32clipboard',
                       'win32gui'):
            self.assertNotIn(symbol, source)
        # Process calls are confined to the explicitly enabled native transport.
        reader_source = '\n'.join(p.read_text('utf-8') for p in package.glob('*.py') if p.name != 'native.py')
        for symbol in ('WriteProcessMemory', 'CreateRemoteThread'):
            self.assertNotIn(symbol, reader_source)
        self.assertNotIn('uiautomation', (ROOT / 'pyproject.toml').read_text('utf-8'))

    def test_live_initializer_needs_no_window_and_disables_sending(self):
        account = self.root / 'wxid_test_ab12'
        account.mkdir()
        candidate = {'pid': 123, 'version': '4.1.15.51', 'exe': 'synthetic.exe',
                     'accounts': [{'source_dir': str(account)}]}
        with patch('wechat_bridge.diagnostics.discover', return_value={'candidates': [candidate]}), \
             patch('wechat_bridge.desktop_identity.verify_account') as identity:
            result = initialize_live(self.root / 'new-live', 123, 'wxid_test')
        cfg = load_config(result['config'])
        self.assertEqual(cfg.sending_mode, 'disabled')
        self.assertFalse(hasattr(cfg, 'desktop_window'))
        identity.assert_called_once()

    def test_account_binding_checks_required_databases_and_other_accounts(self):
        cfg = SimpleNamespace(account_id='wxid_test', source_dir=self.root / 'wxid_test_ab12')
        paths = [cfg.source_dir / 'db_storage' / p for p in
                 ('contact/contact.db', 'session/session.db', 'message/message_0.db')]
        self.assertTrue(verify_paths(cfg, paths)['account_matched'])
        self.reject('identity_unavailable', lambda: verify_paths(cfg, paths[:-1]))
        self.reject('identity_changed', lambda: verify_paths(cfg, paths + [
            self.root / 'wxid_other' / 'db_storage' / 'contact/contact.db']))

    def test_target_renaming_and_partial_pinning_are_rejected(self):
        self.cfg.sending_mode = 'mock'
        self.cfg.send_target_names = {'demo_team@chatroom': '测试群'}
        self.reject('target_changed', lambda: self.send())
        self.assertIsNone(self.bridge.store.next_job())
        self.cfg.send_allowlist.append('other')
        self.reject('invalid_config', self.cfg.validate)
