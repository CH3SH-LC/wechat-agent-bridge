"""Authenticated synthetic pages, live-cache orchestration with process mocked."""
from contextlib import closing
import hashlib
import hmac
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from wechat_bridge.common import BridgeError, file_hash
from wechat_bridge.config import Config
from wechat_bridge.sources import LiveSource
from wechat_bridge.vendor import extract_local as reader

ROOT = Path(__file__).resolve().parents[1] / '.test-output'
ROOT.mkdir(exist_ok=True)


def encrypted_page(key, salt):
    iv = bytes(range(16))
    plain = bytes(4000)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    data = salt + cipher.update(plain) + cipher.finalize() + iv
    return data + hmac.new(reader.mkey(key, salt), data[16:] + struct.pack('<I', 1), hashlib.sha512).digest()


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='capture-', dir=ROOT))
        self.key, self.salt = bytes(range(32)), bytes(range(16))
        self.encrypted = encrypted_page(self.key, self.salt)

    def test_authenticated_decryption_and_tamper_rejection(self):
        source, saved, output = (self.root / x for x in ('source.db', 'saved.db', 'plain.db'))
        source.write_bytes(self.encrypted)
        info = reader.copy_stable(source, saved)
        # This test checks page authentication, not SQLite integrity of the
        # deliberately all-zero synthetic page. SQLite is covered separately.
        with patch.object(reader, 'inspect', side_effect=lambda p: {'sha256': file_hash(p), 'integrity_check': 'synthetic-page-only'}):
            reader._decrypt_snapshot(saved, output, self.key, info)
        self.assertEqual(output.read_bytes()[:16], b'SQLite format 3\0')
        self.assertEqual(info['base_pages_authenticated'], 1)
        changed = bytearray(self.encrypted)
        changed[200] ^= 1
        source.write_bytes(changed)
        info = reader.copy_stable(source, saved)
        with self.assertRaises(reader.SnapshotError):
            reader._decrypt_snapshot(saved, self.root / 'must-not-publish.db', self.key, info)
        self.assertFalse((self.root / 'must-not-publish.db').exists())

    def test_live_refresh_caches_keys_only_in_memory_and_reuses_unchanged_db(self):
        account = self.root / 'wxid_synthetic_ab12'
        for name in ('contact/contact.db', 'session/session.db', 'message/message_0.db'):
            path = account / 'db_storage' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.encrypted)
        cfg = Config(state_dir=self.root / 'state', account_id='wxid_synthetic', source_mode='live', source_dir=account, desktop_pid=987654).validate()
        source = LiveSource(cfg)
        def decrypt(snap, dest, key, info):
            self.assertEqual(key, self.key)
            dest.write_bytes(b'synthetic verified db')
        with patch.object(reader, 'ReadOnlyProcess') as process, patch.object(reader, 'scan', return_value={self.key}) as scan, patch.object(reader, '_decrypt_snapshot', side_effect=decrypt) as dec:
            source.refresh()
            first = dict(source.paths)
            source.refresh()
            self.assertEqual(source.paths, first)
            self.assertEqual(dec.call_count, 3)
            scan.assert_called_once()
            self.assertEqual(process.call_count, 2)
        self.assertEqual(len(source.keys), 3)
        for path in cfg.state_dir.rglob('*'):
            if path.is_file():
                self.assertNotIn(self.key.hex().encode(), path.read_bytes())

    def test_failed_live_refresh_does_not_publish_partial_generation(self):
        account = self.root / 'wxid_synthetic'
        for name in ('contact/contact.db', 'session/session.db', 'message/message_0.db'):
            path = account / 'db_storage' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.encrypted)
        cfg = Config(state_dir=self.root / 'state', account_id='wxid_synthetic', source_mode='live', source_dir=account, desktop_pid=987654).validate()
        source = LiveSource(cfg)
        def decrypt(snap, dest, key, info):
            dest.write_bytes(b'partial')
            raise reader.SnapshotError('synthetic failure')
        with patch.object(reader, 'ReadOnlyProcess'), patch.object(reader, 'scan', return_value={self.key}), patch.object(reader, '_decrypt_snapshot', side_effect=decrypt):
            with self.assertRaises(reader.SnapshotError): source.refresh()
        self.assertEqual(source.paths, {})
        self.assertEqual(list(source.cache.glob('*.sqlite')), [])


if __name__ == '__main__': unittest.main()
