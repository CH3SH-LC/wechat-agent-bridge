"""Regression for identity binding and scoped Qt/UIA adaptation, no real UI."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechat_bridge.common import BridgeError
from wechat_bridge.desktop import DesktopSender
from wechat_bridge.desktop_identity import verify_paths
from wechat_bridge.qt_accessibility import RuntimeGate, plan_for


class CompatibilityTests(unittest.TestCase):
    def config(self):
        return SimpleNamespace(account_id='wxid_test', source_dir=Path(tempfile.gettempdir()) / 'wxid_test_ab12')

    def paths(self, cfg):
        return [cfg.source_dir / 'db_storage' / p for p in ('contact/contact.db', 'session/session.db', 'message/message_0.db')]

    def test_active_account_requires_all_three_database_types(self):
        cfg = self.config()
        self.assertTrue(verify_paths(cfg, self.paths(cfg))['account_matched'])
        with self.assertRaises(BridgeError):
            verify_paths(cfg, self.paths(cfg)[:-1])

    def test_other_open_account_and_mismatched_directory_rejected(self):
        cfg = self.config()
        with self.assertRaises(BridgeError):
            verify_paths(cfg, self.paths(cfg) + [cfg.source_dir.parent / 'wxid_other' / 'db_storage' / 'contact/contact.db'])
        cfg.account_id = 'wxid_other'
        with self.assertRaises(BridgeError):
            verify_paths(cfg, self.paths(cfg))

    def test_dll_hash_mismatch_never_attempts_signature_or_process_write(self):
        with patch('wechat_bridge.qt_accessibility.file_hash', return_value='b' * 64), patch('wechat_bridge.qt_accessibility.candidates') as scan:
            with self.assertRaises(BridgeError):
                plan_for('synthetic.dll', 1, 2, 'a' * 64)
            scan.assert_not_called()

    def test_gate_restores_even_on_operation_exception(self):
        gate = RuntimeGate(SimpleNamespace())
        gate.handle, gate.original, gate.changed = 123, 0, True
        gate.write, gate.kernel = Mock(), Mock()
        gate.__exit__(RuntimeError, RuntimeError('dispatch failed'), None)
        gate.write.assert_called_once_with(0)
        gate.kernel.CloseHandle.assert_called_once_with(123)
        self.assertTrue(gate.evidence['restored'])
        self.assertIsNone(gate.handle)

    def test_restore_failure_is_not_hidden_and_handle_is_closed(self):
        gate = RuntimeGate(SimpleNamespace())
        gate.handle, gate.original, gate.changed = 123, 0, True
        gate.write, gate.kernel = Mock(side_effect=RuntimeError('failed')), Mock()
        with self.assertRaises(RuntimeError):
            gate.__exit__(None, None, None)
        gate.kernel.CloseHandle.assert_called_once_with(123)

    def node(self, cls='', aid='', typ='GroupControl', children=()):
        return SimpleNamespace(ClassName=cls, AutomationId=aid, ControlTypeName=typ, GetChildren=lambda: children)

    def test_deep_chat_editor_visible_without_traversing_article_and_history(self):
        editor = self.node('mmui::ChatInputField', 'chat_input_field', 'EditControl')
        deep = editor
        for _ in range(25):
            deep = self.node(children=[deep])
        poison = Mock(side_effect=AssertionError('unrelated subtree read'))
        article, history = self.node('Chrome_WidgetWin_0'), self.node(aid='chat_message_list')
        article.GetChildren = history.GetChildren = poison
        root = self.node(children=[article, history, deep])
        self.assertIn(editor, DesktopSender.nodes(root))
        poison.assert_not_called()

    def test_unique_input_requires_exact_target_name(self):
        control = self.node('mmui::ChatInputField', 'chat_input_field', 'EditControl')
        control.Name = '测试群2'
        with self.assertRaises(BridgeError):
            DesktopSender.input_for([control], '测试群')

    def test_file_export_requires_one_exact_path_and_wechat_file_type(self):
        path = str(Path(tempfile.gettempdir()) / 'acceptance.txt')
        xml = ('<WeChatRichEditFormat><EditElement type="3" filepath="'+path+'" /></WeChatRichEditFormat>').encode()
        DesktopSender.validate_file_export({'path':path}, (path,), xml)
        for paths, data in [((path,path),xml), ((path+'other',),xml), ((path,),xml.replace(b'type="3"',b'type="2"')),
                            ((path,),xml.replace(b'</WeChatRichEditFormat>',b'<EditElement type="1">extra</EditElement></WeChatRichEditFormat>'))]:
            with self.assertRaises(BridgeError):
                DesktopSender.validate_file_export({'path':path}, paths, data)

