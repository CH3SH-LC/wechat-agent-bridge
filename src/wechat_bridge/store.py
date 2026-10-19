"""Durable inbox/outbox. Source checkpoints commit with their message rows."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading

from .common import digest, json_text, now, require, uid


class Store:
    def __init__(self, root, account_id):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "bridge.sqlite", isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS subscriptions(chat_id TEXT PRIMARY KEY,cursors TEXT NOT NULL,active INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,source_key TEXT UNIQUE NOT NULL,
            chat_id TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS event_chat ON events(chat_id,id);
          CREATE TABLE IF NOT EXISTS deliveries(consumer TEXT NOT NULL,event_id INTEGER NOT NULL REFERENCES events(id),
            state TEXT NOT NULL,lease TEXT,lease_until REAL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,
            PRIMARY KEY(consumer,event_id));
          CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,path TEXT NOT NULL,name TEXT NOT NULL,
            sha256 TEXT NOT NULL,size INTEGER NOT NULL,created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,
            payload_hash TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,
            created REAL NOT NULL,updated REAL NOT NULL,result TEXT,baseline TEXT);
        ''')
        try:
            with self.transaction() as db:
                row = db.execute("SELECT value FROM meta WHERE key='account'").fetchone()
                require(row is None or row[0] == account_id, "account_mismatch", "数据目录已经绑定其他账号。")
                db.execute("INSERT OR IGNORE INTO meta VALUES('account',?)", (account_id,))
                db.execute("INSERT OR IGNORE INTO meta VALUES('paused','false')")
        except Exception:
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def recover(self):
        with self.transaction() as db:
            db.execute("UPDATE outbox SET state='unknown',updated=?,result=? WHERE state='sending'",
                       (now(), json_text({"reason": "service_restarted_during_send", "automatic_retry": False})))

    def paused(self):
        with self.lock:
            return self.db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "true"

    def set_paused(self, value):
        with self.transaction() as db:
            db.execute("UPDATE meta SET value=? WHERE key='paused'", ("true" if value else "false",))

    def replace_chats(self, chats):
        with self.transaction() as db:
            db.execute("DELETE FROM chats")
            db.executemany("INSERT INTO chats VALUES(:id,:name,:kind)", chats)

    def chats(self, query="", kind=None):
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM chats ORDER BY name,id")]
        return [r for r in rows if (not query or query.casefold() in (r['name'] + r['id']).casefold())
                and (kind is None or r['kind'] == kind)]

    def subscribe(self, chat_id, cursors):
        with self.transaction() as db:
            old = db.execute("SELECT cursors FROM subscriptions WHERE chat_id=?", (chat_id,)).fetchone()
            if old:
                db.execute("UPDATE subscriptions SET active=1 WHERE chat_id=?", (chat_id,))
            else:
                db.execute("INSERT INTO subscriptions VALUES(?,?,1)", (chat_id, json_text(cursors)))

    def unsubscribe(self, chat_id):
        with self.transaction() as db:
            db.execute("UPDATE subscriptions SET active=0 WHERE chat_id=?", (chat_id,))

    def subscriptions(self):
        with self.lock:
            return [{"chat_id": r['chat_id'], "cursors": json.loads(r['cursors']), "active": bool(r['active'])}
                    for r in self.db.execute("SELECT * FROM subscriptions ORDER BY chat_id")]

    def ingest(self, chat_id, messages, cursors):
        with self.transaction() as db:
            row = db.execute("SELECT active FROM subscriptions WHERE chat_id=?", (chat_id,)).fetchone()
            if not row or not row[0]:
                return 0
            count = 0
            for message in messages:
                count += db.execute("INSERT OR IGNORE INTO events(source_key,chat_id,body,created) VALUES(?,?,?,?)",
                                    (message['source_key'], chat_id, json_text(message), now())).rowcount
            db.execute("UPDATE subscriptions SET cursors=? WHERE chat_id=?", (json_text(cursors), chat_id))
            return count

    def history(self, chat_id, before=None, limit=100):
        with self.lock:
            rows = self.db.execute("SELECT id,body FROM events WHERE chat_id=? AND id<? ORDER BY id DESC LIMIT ?",
                                   (chat_id, before if before is not None else 2**63-1, limit)).fetchall()
        return [{"event_id": r['id'], **json.loads(r['body'])} for r in reversed(rows)]

    def claim(self, consumer, limit, lease_seconds, chat_ids=None):
        with self.transaction() as db:
            sql = '''SELECT e.* FROM events e LEFT JOIN deliveries d ON d.event_id=e.id AND d.consumer=?
                     WHERE (d.state IS NULL OR d.state='pending' OR (d.state='leased' AND d.lease_until<?))'''
            args = [consumer, now()]
            if chat_ids:
                sql += " AND e.chat_id IN (" + ",".join("?" for _ in chat_ids) + ")"
                args.extend(chat_ids)
            sql += " ORDER BY e.id LIMIT ?"
            args.append(limit)
            rows = db.execute(sql, args).fetchall()
            result = []
            for r in rows:
                receipt = uid("lease_")
                expires = now() + lease_seconds
                db.execute('''INSERT INTO deliveries(consumer,event_id,state,lease,lease_until,attempts)
                  VALUES(?,?,'leased',?,?,1) ON CONFLICT(consumer,event_id) DO UPDATE SET
                  state='leased',lease=excluded.lease,lease_until=excluded.lease_until,attempts=attempts+1''',
                           (consumer, r['id'], receipt, expires))
                result.append({"event_id": r['id'], "receipt": receipt, "lease_until": expires,
                               **json.loads(r['body'])})
            return result

    def acknowledge(self, consumer, items, failed=False):
        with self.transaction() as db:
            for item in items:
                r = db.execute("SELECT * FROM deliveries WHERE consumer=? AND event_id=?", (consumer, item['event_id'])).fetchone()
                require(r and r['state'] == 'leased' and r['lease'] == item['receipt'] and r['lease_until'] >= now(),
                        "invalid_receipt", "消息租约已过期或不属于当前消费者。")
            for item in items:
                db.execute("UPDATE deliveries SET state=?,lease=NULL,lease_until=NULL,error=? WHERE consumer=? AND event_id=?",
                           ("failed" if failed else "acked", "consumer_failed" if failed else None, consumer, item['event_id']))

    def failed_events(self, consumer):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT event_id,attempts,error FROM deliveries WHERE consumer=? AND state='failed' ORDER BY event_id", (consumer,))]

    def renew(self, consumer, items, seconds):
        with self.transaction() as db:
            expiry = now() + seconds
            for item in items:
                cur = db.execute("UPDATE deliveries SET lease_until=? WHERE consumer=? AND event_id=? AND state='leased' AND lease=? AND lease_until>=?",
                                 (expiry, consumer, item['event_id'], item['receipt'], now()))
                require(cur.rowcount == 1, 'invalid_receipt', '消息租约已过期或不属于当前消费者。')
        return {'renewed': len(items), 'lease_until': expiry}

    def retry_event(self, consumer, event_id):
        with self.transaction() as db:
            result = db.execute("UPDATE deliveries SET state='pending',error=NULL WHERE consumer=? AND event_id=? AND state='failed'", (consumer, event_id))
            require(result.rowcount == 1, "not_found", "未找到失败消息。")

    def add_attachment(self, record):
        with self.transaction() as db:
            db.execute("INSERT INTO attachments VALUES(:id,:path,:name,:sha256,:size,:created)", record)

    def attachment(self, ident):
        with self.lock:
            row = self.db.execute("SELECT * FROM attachments WHERE id=?", (ident,)).fetchone()
        require(row is not None, "not_found", "附件不存在。")
        return dict(row)

    def enqueue(self, key, payload, state):
        with self.transaction() as db:
            old = db.execute("SELECT * FROM outbox WHERE request_key=?", (key,)).fetchone()
            if old:
                require(old['payload_hash'] == digest(payload), "idempotency_conflict", "同一幂等键已用于不同内容。")
                return self._job(old)
            ident = uid("send_")
            db.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?)",
                       (ident, key, digest(payload), json_text(payload), state, now(), now(), None, None))
            return self._job(db.execute("SELECT * FROM outbox WHERE id=?", (ident,)).fetchone())

    @staticmethod
    def _job(row):
        result = dict(row)
        for key in ('payload', 'result', 'baseline'):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        result.pop('payload_hash', None)
        return result

    def job(self, ident):
        with self.lock:
            row = self.db.execute("SELECT * FROM outbox WHERE id=?", (ident,)).fetchone()
        require(row is not None, "not_found", "发送记录不存在。")
        return self._job(row)

    def next_job(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM outbox WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
        return self._job(row) if row else None

    def transition(self, ident, old_states, new_state, result=None, baseline=None):
        with self.transaction() as db:
            marks = ','.join('?' for _ in old_states)
            cur = db.execute(f"UPDATE outbox SET state=?,result=?,baseline=COALESCE(?,baseline),updated=? WHERE id=? AND state IN ({marks})",
                             (new_state, json_text(result) if result is not None else None,
                              json_text(baseline) if baseline is not None else None, now(), ident, *old_states))
            require(cur.rowcount == 1, "invalid_state", "发送状态已经变化，不能执行该操作。")
        return self.job(ident)

    def counts(self):
        with self.lock:
            return {"events": self.db.execute("SELECT count(*) FROM events").fetchone()[0],
                    "outbox": {r[0]: r[1] for r in self.db.execute("SELECT state,count(*) FROM outbox GROUP BY state")},
                    "failed_deliveries": self.db.execute("SELECT count(*) FROM deliveries WHERE state='failed'").fetchone()[0]}
