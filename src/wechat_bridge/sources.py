"""Read-only local sources. Publication is all-or-nothing per refresh.

The original collector supplies authenticated SQLite/WAL replay. No capture runs
on import. Decrypted caches are private service data; candidate keys stay in RAM.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import tempfile
import threading
import xml.etree.ElementTree as ET

from .common import BridgeError, atomic_json, digest, file_hash, inside, now, require, uid


def account_matches(folder, account):
    return folder == account or bool(re.fullmatch(re.escape(account) + r"_[a-fA-F0-9]{4}", folder))


def table_for(chat_id):
    return "Msg_" + hashlib.md5(chat_id.encode()).hexdigest()


def kind(chat_id):
    return "group" if chat_id.endswith("@chatroom") else "direct"


class FixtureSource:
    """An explicit, entirely synthetic source also used by the mock sender."""
    def __init__(self, cfg):
        self.cfg, self.lock, self.data = cfg, threading.RLock(), None
        self.path = cfg.source_dir / "fixture.json"
        self.updated = None

    def refresh(self):
        with self.lock:
            data = json.loads(self.path.read_text("utf-8"))
            require(data.get('account_id') == self.cfg.account_id, "account_mismatch", "隔离数据账号不匹配。")
            require(isinstance(data.get('chats'), list) and isinstance(data.get('messages'), list), "invalid_source", "隔离数据格式错误。")
            self.data, self.updated = data, now()

    def chats(self):
        require(self.data is not None, "source_unavailable", "尚未读取来源。")
        return [dict(id=c['id'], name=c['name'], kind=kind(c['id'])) for c in self.data['chats']]

    def tail(self, chat_id):
        return {'fixture': max((int(m['seq']) for m in self.data['messages'] if m['chat_id'] == chat_id), default=0)}

    def read(self, chat_id, cursors, limit=500):
        current = int(cursors.get('fixture', 0))
        require(self.tail(chat_id)['fixture'] >= current, "source_rewound", "来源记录已回退，保留旧游标等待人工处理。")
        rows = sorted((m for m in self.data['messages'] if m['chat_id'] == chat_id and int(m['seq']) > current), key=lambda x: x['seq'])[:limit]
        messages = []
        for row in rows:
            value = dict(row)
            value.update(source_key=digest([self.cfg.account_id, 'fixture', chat_id, row['seq']]),
                         is_self=row['sender_id'] == self.cfg.account_id,
                         timestamp=row.get('timestamp', 0), text=row.get('text', ''),
                         type=row.get('type', 'text'), untrusted_content=True)
            messages.append(value)
        return messages, {'fixture': int(rows[-1]['seq']) if rows else current}

    def append_outgoing(self, payload, attachment=None):
        with self.lock:
            self.refresh()
            row = dict(chat_id=payload['chat_id'], sender_id=self.cfg.account_id,
                       seq=max((m['seq'] for m in self.data['messages']), default=0) + 1,
                       timestamp=now(), type=payload['type'], text=payload.get('text', ''))
            if attachment:
                row['file'] = {k: attachment[k] for k in ('name', 'size')}
            self.data['messages'].append(row)
            atomic_json(self.path, self.data)

    def health(self):
        return {'mode': 'fixture', 'last_refresh': self.updated, 'coverage': 'synthetic_only'}


class DatabaseSource:
    def __init__(self, cfg):
        self.cfg, self.lock = cfg, threading.RLock()
        self.paths, self.updated = {}, None

    @staticmethod
    def connect(path):
        from .vendor.export_records import connect
        return connect(path)

    @staticmethod
    def tables(db):
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def shards(self):
        return [(name, path) for name, path in sorted(self.paths.items()) if re.fullmatch(r'message/message_\d+\.db', name)]

    def chats(self):
        require(self.paths, 'source_unavailable', '尚未读取来源。')
        names, ids = {}, set()
        contact = self.paths.get('contact/contact.db')
        if contact:
            with self.connect(contact) as db:
                for table in ('contact', 'stranger'):
                    if table in self.tables(db):
                        for row in db.execute('SELECT * FROM ' + table):
                            value = dict(row)
                            if isinstance(value.get('username'), str):
                                ident = value['username']
                                names[ident] = value.get('remark') or value.get('nick_name') or ident
        session = self.paths.get('session/session.db')
        if session:
            with self.connect(session) as db:
                if 'SessionTable' in self.tables(db):
                    ids.update(r[0] for r in db.execute('SELECT username FROM SessionTable') if isinstance(r[0], str))
                if 'SessionNoContactInfoTable' in self.tables(db):
                    for row in db.execute('SELECT username,session_title FROM SessionNoContactInfoTable'):
                        ids.add(row[0])
                        names.setdefault(row[0], row[1] or row[0])
        for _, path in self.shards():
            with self.connect(path) as db:
                tables = self.tables(db)
                require('Name2Id' in tables, 'schema_unsupported', '消息库缺少 Name2Id，未推进游标。')
                ids.update(r[0] for r in db.execute('SELECT user_name FROM Name2Id')
                           if isinstance(r[0], str) and table_for(r[0]) in tables)
        return [dict(id=x, name=str(names.get(x, x)), kind=kind(x)) for x in sorted(ids)]

    def tail(self, chat_id):
        result, table = {}, table_for(chat_id)
        for name, path in self.shards():
            with self.connect(path) as db:
                row = db.execute(f'SELECT sort_seq,local_id FROM "{table}" ORDER BY sort_seq DESC,local_id DESC LIMIT 1').fetchone() if table in self.tables(db) else None
                result[name] = list(row) if row else [-1, -1]
        return result

    def read(self, chat_id, cursors, limit=500):
        from .vendor.export_records import body_text, render_details, DECODE_ERRORS
        messages, updated, table = [], dict(cursors), table_for(chat_id)
        present = {name for name, _ in self.shards()}
        require(not set(cursors) - present, 'source_rewound', '历史消息分片消失，未推进游标。')
        for shard, path in self.shards():
            with self.connect(path) as db:
                current = list(cursors.get(shard, [-1, -1]))
                if table not in self.tables(db):
                    require(current == [-1, -1], 'source_rewound', '已监听的消息表消失。')
                    updated[shard] = current
                    continue
                tail = db.execute(f'SELECT sort_seq,local_id FROM "{table}" ORDER BY sort_seq DESC,local_id DESC LIMIT 1').fetchone()
                require((list(tail) if tail else [-1, -1]) >= current, 'source_rewound', '来源消息已回退，未推进游标。')
                senders = {r[0]: r[1] for r in db.execute('SELECT rowid,user_name FROM Name2Id')}
                rows = db.execute(f'SELECT * FROM "{table}" WHERE sort_seq>? OR (sort_seq=? AND local_id>?) ORDER BY sort_seq,local_id LIMIT ?',
                                  (current[0], current[0], current[1], limit)).fetchall()
                for row in rows:
                    item = dict(row)
                    sender = senders.get(item['real_sender_id'])
                    body, encoding = body_text(item.get('message_content'))
                    if not body and item.get('compress_content'):
                        body, encoding = body_text(item['compress_content'])
                    typ = int(item['local_type'])
                    rendered, error = render_details(body, typ, sender, chat_id.endswith('@chatroom'))
                    value = dict(source_key=digest([self.cfg.account_id, chat_id, shard, item['local_id'], item['sort_seq']]),
                                 chat_id=chat_id, sender_id=sender, is_self=sender == self.cfg.account_id,
                                 timestamp=item['create_time'], text=rendered, raw_type=typ,
                                 type='text' if typ & 0xffffffff == 1 else 'structured',
                                 shard=shard, local_id=item['local_id'], sort_seq=item['sort_seq'],
                                 decode_status=encoding, parse_error=error, untrusted_content=True)
                    if encoding in DECODE_ERRORS:
                        value['text'] = '[正文解码失败；原始记录仍在来源库]'
                    if typ & 0xffffffff == 49:
                        try:
                            xml = body[body.index('<'):]
                            root = ET.fromstring(xml)
                            if root.findtext('.//appmsg/type') == '6':
                                value.update(type='file', file={'name': root.findtext('.//appmsg/title'),
                                                               'size': int(root.findtext('.//totallen') or -1)})
                        except (ValueError, ET.ParseError):
                            pass
                    messages.append(value)
                updated[shard] = [rows[-1]['sort_seq'], rows[-1]['local_id']] if rows else current
        messages.sort(key=lambda x: (x['timestamp'], x['shard'], x['sort_seq'], x['local_id']))
        return messages, updated

    def health(self):
        return {'mode': self.cfg.source_mode, 'last_refresh': self.updated,
                'shards': len(self.shards()), 'coverage': 'locally_retained_messages',
                'cross_database_atomic': False, 'late_history_below_cursor': 'not_replayed',
                'delivery_or_read_receipts': False}


class SnapshotSource(DatabaseSource):
    def refresh(self):
        with self.lock:
            root = self.cfg.source_dir
            report = json.loads((root / 'capture_report.json').read_text('utf-8-sig'))
            require(report.get('status') == 'verified', 'invalid_source', '仅接受完整 verified 采集快照。')
            source = str(report.get('source', '')).replace('\\', '/')
            folder = source.rstrip('/').split('/')[-2] if source.endswith('/db_storage') else ''
            require(account_matches(folder, self.cfg.account_id), 'account_mismatch', '快照账号与配置不匹配。')
            paths = {}
            for name, info in report.get('databases', {}).items():
                path = (root / 'decrypted' / name).resolve()
                require(inside(path, root / 'decrypted') and path.is_file() and info.get('status') == 'verified', 'invalid_source', '快照文件不完整。')
                require(file_hash(path) == info.get('sha256'), 'invalid_source', '快照哈希不匹配。')
                paths[name] = path
            require(any(re.fullmatch(r'message/message_\d+\.db', x) for x in paths), 'invalid_source', '没有验证过的消息分片。')
            self.paths, self.updated = paths, now()


class LiveSource(DatabaseSource):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.keys, self.fingerprints = {}, {}
        self.last_scan = 0
        self.cache = cfg.state_dir / 'source-cache'
        self.cache.mkdir(parents=True, exist_ok=True)

    def refresh(self):
        from .vendor import extract_local as reader
        with self.lock:
            account = self.cfg.source_dir
            require(account_matches(account.name, self.cfg.account_id), 'account_mismatch', '微信账号目录与配置不匹配。')
            require(type(self.cfg.desktop_pid) is int and self.cfg.desktop_pid > 0, 'invalid_config', '实时读取必须显式指定微信主进程 PID。')
            source = account / 'db_storage'
            # Open/identity-check on every pass; access flags are QUERY + VM_READ.
            with closing(reader.ReadOnlyProcess(self.cfg.desktop_pid, self.cfg.weixin_exe)):
                pass
            files = [p for p in reader._files(source) if re.fullmatch(r'message/message_\d+\.db', p.relative_to(source).as_posix())
                     or p.relative_to(source).as_posix() in ('contact/contact.db', 'session/session.db')]
            new_paths, fingerprints, created = {}, {}, []
            try:
                for path in files:
                    name = path.relative_to(source).as_posix()
                    # Include SHM bytes because it can change through mmap without mtime.
                    shm = Path(str(path) + '-shm')
                    fingerprint = [reader.stamp(path), reader.stamp(Path(str(path) + '-wal')),
                                   file_hash(shm) if shm.exists() else None]
                    fingerprints[name] = fingerprint
                    if name in self.paths and self.fingerprints.get(name) == fingerprint:
                        new_paths[name] = self.paths[name]
                        continue
                    with tempfile.TemporaryDirectory(prefix='capture-', dir=self.cache) as temp:
                        snap = Path(temp) / 'source.db'
                        info = reader.copy_stable(path, snap)
                        with snap.open('rb') as stream:
                            first = stream.read(reader.PAGE)
                        key = self.keys.get(name)
                        if key is None or not reader.authenticated(reader.mkey(key, first[:16]), first, 1):
                            require(now() - self.last_scan >= 30, 'key_unavailable', '密钥暂不可用；稍后重试，只保留内存缓存。')
                            self.last_scan = now()
                            candidates = reader.scan(self.cfg.desktop_pid, self.cfg.weixin_exe, progress=None)
                            try:
                                # Cache verified candidates for all selected databases, in memory only.
                                for candidate_path in files:
                                    with candidate_path.open('rb') as stream:
                                        page = stream.read(reader.PAGE)
                                    matched = next((k for k in candidates if reader.authenticated(reader.mkey(k, page[:16]), page, 1)), None)
                                    if matched is not None:
                                        self.keys[candidate_path.relative_to(source).as_posix()] = matched
                            finally:
                                candidates.clear()
                            key = self.keys.get(name)
                        require(key is not None, 'key_unavailable', '未找到能验证当前消息库的密钥。')
                        dest = self.cache / (uid('db_') + '.sqlite')
                        created.append(dest)
                        reader._decrypt_snapshot(snap, dest, key, info)
                        new_paths[name] = dest
                require(new_paths, 'source_unavailable', '没有可读取的消息数据库。')
                retired = set(self.paths.values()) - set(new_paths.values())
                self.paths, self.fingerprints, self.updated = new_paths, fingerprints, now()
                for old in retired:
                    if inside(old, self.cache):
                        try:
                            old.unlink(missing_ok=True)
                        except OSError:
                            pass  # a scanner may hold the retired private file
            except Exception:
                for path in created:
                    if inside(path, self.cache):
                        path.unlink(missing_ok=True)
                        path.with_suffix('.sqlite.unverified').unlink(missing_ok=True)
                raise


def make_source(cfg):
    return {'fixture': FixtureSource, 'snapshot': SnapshotSource, 'live': LiveSource}[cfg.source_mode](cfg)
