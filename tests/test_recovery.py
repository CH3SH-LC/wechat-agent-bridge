"""Regression for destination pinning and receipt-bound draft recovery."""
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace
import sys
from test_bridge import FixtureCase
from wechat_bridge.common import BridgeError
from wechat_bridge.qt_accessibility import sections
from wechat_bridge.desktop import DesktopSender


class RecoveryTests(FixtureCase):
    def failed(self, phase='preview_check', key='one'):
        self.cfg.sending_mode = 'desktop'
        self.cfg.send_target_names = {'demo_team@chatroom': '隔离测试群'}
        job = self.send(key=key)
        self.bridge.store.set_paused(True)
        return self.bridge.store.transition(job['id'], ['queued'], 'unknown', {'adapter': {'phase': phase}}, baseline={})

    def test_observed_message_is_never_cleared(self):
        job = self.failed()
        self.append(sender_id=self.cfg.account_id, text='hello')
        with patch('wechat_bridge.desktop.DesktopSender.clear_failed_draft') as clear:
            self.reject('already_observed', lambda: self.bridge.clear_failed_draft(job['id']))
            clear.assert_not_called()

    def test_ambiguous_dispatch_or_restart_is_never_cleared(self):
        for phase in ('issuing_send', 'send_issued', None, 'before_navigation'):
            with self.subTest(phase=phase):
                job = self.failed(phase, str(phase))
                with patch('wechat_bridge.desktop.DesktopSender.clear_failed_draft') as clear:
                    self.reject('unsafe_draft_recovery', lambda: self.bridge.clear_failed_draft(job['id']))
                    clear.assert_not_called()

    def test_recovery_requires_pause_and_preserves_job_on_refusal(self):
        job = self.failed()
        self.bridge.store.set_paused(False)
        self.reject('pause_required', lambda: self.bridge.clear_failed_draft(job['id']))
        self.assertEqual(self.bridge.store.job(job['id'])['state'], 'unknown')

    def test_verified_recovery_cancels_without_enqueuing_or_resending(self):
        job = self.failed()
        def clear(payload, attachment, guard):
            guard()
            return {'draft_cleared': True, 'send_key_issued': False}
        with patch('wechat_bridge.desktop.DesktopSender.clear_failed_draft', side_effect=clear) as adapter:
            result = self.invoke('wechat_clear_failed_draft', id=job['id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertFalse(result['result']['adapter']['send_key_issued'])
        self.assertIsNone(self.bridge.store.next_job())
        adapter.assert_called_once()

    def test_recovery_refuses_mismatch_and_keeps_unknown(self):
        job = self.failed()
        with patch('wechat_bridge.desktop.DesktopSender.clear_failed_draft', side_effect=BridgeError('draft_mismatch', 'different')):
            self.reject('draft_mismatch', lambda: self.bridge.clear_failed_draft(job['id']))
        self.assertEqual(self.bridge.store.job(job['id'])['state'], 'unknown')

    def test_renamed_allowed_chat_is_rejected_before_enqueue(self):
        self.cfg.sending_mode = 'mock'
        self.cfg.send_target_names = {'demo_team@chatroom': '测试群'}
        self.reject('target_changed', lambda: self.send())
        self.assertIsNone(self.bridge.store.next_job())

    def test_partial_target_pinning_is_invalid(self):
        self.cfg.send_target_names = {'demo_team@chatroom': '隔离测试群'}
        self.cfg.send_allowlist.append('other')
        self.reject('invalid_config', self.cfg.validate)

    def test_malformed_pe_reports_structured_error(self):
        for data in (b'', b'MZ', b'MZ' + b'\0' * 58 + b'\xff' * 4):
            with self.subTest(size=len(data)):
                self.reject('unsupported_dll', lambda: sections(data))

    def test_locked_desktop_is_rejected_before_accessibility_gate(self):
        gui, process, psutil = Mock(), Mock(), Mock()
        gui.GetForegroundWindow.return_value = 7
        process.GetWindowThreadProcessId.return_value = (1, 2)
        psutil.Process.return_value.name.return_value = 'LockApp.exe'
        psutil.Error = RuntimeError
        with patch.dict(sys.modules, {'psutil': psutil}):
            self.reject('desktop_locked', lambda: DesktopSender(self.cfg).desktop_ready(gui, process))
        gui.IsWindow.assert_not_called()

    def test_failure_before_paste_is_failed_not_ambiguous(self):
        self.cfg.sending_mode = 'mock'
        job = self.send()
        error = BridgeError('desktop_locked', 'locked')
        error.dispatch_evidence = {'phase': 'before_navigation'}
        self.bridge.sender = SimpleNamespace(send=Mock(side_effect=error))
        result = self.bridge.dispatch_once()
        self.assertEqual(result['id'], job['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['result']['automatic_retry'])
