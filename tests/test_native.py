import ctypes
import unittest
from pathlib import Path
from wechat_bridge.common import BridgeError
from wechat_bridge.config import Config
from wechat_bridge.native import NativeRequest, make_request


class NativeContractTests(unittest.TestCase):
    def test_wire_layout_matches_native_compiler(self):
        self.assertEqual(ctypes.sizeof(NativeRequest), 9300)
        self.assertEqual(NativeRequest.status.offset, 8784)
        self.assertEqual(NativeRequest.error.offset, 8788)

    def test_utf8_bounds_and_embedded_nul(self):
        value = make_request('isolated@chatroom', '后台文字')
        self.assertEqual(value.text.decode(), '后台文字')
        self.assertEqual(value.allowed_recipient, value.recipient)
        for recipient, text in [('x', 'x\0y'), ('x\0y', 'x'), ('x', '字' * 2731), ('x' * 256, 'x')]:
            with self.assertRaises(BridgeError):
                make_request(recipient, text)

    def test_probe_cannot_take_a_real_recipient(self):
        with self.assertRaises(BridgeError):
            make_request('filehelper', 'probe', True)
        self.assertEqual(make_request('wab_probe_nonexistent_contact', 'probe', True).operation, 1)

    def test_file_operation_is_distinct_and_never_a_probe(self):
        self.assertEqual(make_request('isolated@chatroom', 'D:/staged/附件.txt', file=True).operation, 3)
        with self.assertRaises(BridgeError):
            make_request('wab_probe_nonexistent_contact', 'D:/staged/a.txt', probe=True, file=True)

    def test_native_cannot_be_enabled_for_fixture(self):
        cfg = Config(Path('native-state'), 'account', source_dir=Path('native-source'),
                     sending_mode='native_experimental', native_dll_path='adapter.dll', native_dll_sha256='a' * 64,
                     send_allowlist=['test'], send_target_names={'test': 'test'})
        with self.assertRaises(BridgeError):
            cfg.validate()

    def test_native_requires_fixed_target_names(self):
        cfg = Config(Path('native-state'), 'account', source_mode='live', source_dir=Path('native-source'),
                     desktop_pid=1, sending_mode='native_experimental', native_dll_path='adapter.dll',
                     native_dll_sha256='a' * 64, send_allowlist=['test'])
        with self.assertRaises(BridgeError):
            cfg.validate()
