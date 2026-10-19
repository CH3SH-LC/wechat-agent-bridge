"""Unified offline acceptance. Every source and sender below is synthetic."""
from contextlib import closing, nullcontext
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

from wechat_bridge.api import call, tool_list
from wechat_bridge.cli import initialize
from wechat_bridge.common import BridgeError, ProcessLock, atomic_json, file_hash, json_text
from wechat_bridge.config import Config, load_config
from wechat_bridge.service import Bridge
from wechat_bridge.sources import DatabaseSource, SnapshotSource, table_for
from wechat_bridge.store import Store
from wechat_bridge.transport import server, remote_call, mcp

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / '.test-output'
ARTIFACTS.mkdir(exist_ok=True)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='offline-', dir=ARTIFACTS))
        self.config_path = Path(initialize(self.root / 'demo')['config'])
        self.cfg = load_config(self.config_path)
        self.cfg.verify_seconds = 0
        self.cfg.send_interval_seconds = 0
        self.bridge = Bridge(self.cfg)
        self.bridge.poll()

    def tearDown(self):
        self.bridge.close()

    def invoke(self, name, **args):
        return call(self.bridge, name, args)

    def listen(self, from_now=False):
        return self.invoke('wechat_listen', chat_id='demo_team@chatroom', from_now=from_now)

    def append(self, **overrides):
        path = self.cfg.source_dir / 'fixture.json'
        data = json.loads(path.read_text('utf-8'))
        row = dict(seq=max(m['seq'] for m in data['messages']) + 1, chat_id='demo_team@chatroom', sender_id='other', timestamp=1700000001, type='text', text='new')
        row.update(overrides)
        data['messages'].append(row)
        atomic_json(path, data)
        return row

    def send(self, key='one', text='hello'):
        return self.invoke('wechat_send_text', chat_id='demo_team@chatroom', text=text, idempotency_key=key)

    def reject(self, code, fn):
        with self.assertRaises(BridgeError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)


class InboxTests(FixtureCase):
    def test_from_now_does_not_replay_old_history(self):
        self.listen(True)
        self.assertEqual(self.bridge.store.counts()['events'], 0)
        self.append()
        self.bridge.poll()
        self.assertEqual(self.bridge.store.history('demo_team@chatroom')[0]['text'], 'new')

    def test_history_subscription_burst_paginated_and_deduplicated(self):
        path = self.cfg.source_dir / 'fixture.json'
        data = json.loads(path.read_text('utf-8'))
        data['messages'] += [dict(seq=i, chat_id='demo_team@chatroom', sender_id='other', text=str(i)) for i in range(2, 1203)]
        atomic_json(path, data)
        self.listen()
        for _ in range(4): self.bridge.poll()
        self.assertEqual(self.bridge.store.counts()['events'], 1202)

    def test_restart_keeps_cursor_and_acked_receipts(self):
        self.listen()
        messages = self.invoke('wechat_events', consumer='worker')['messages']
        self.invoke('wechat_ack', consumer='worker', items=[{k: messages[0][k] for k in ('event_id', 'receipt')}])
        self.bridge.close()
        self.bridge = Bridge(self.cfg)
        self.bridge.poll()
        self.assertEqual(self.invoke('wechat_events', consumer='worker')['messages'], [])
        self.assertEqual(self.bridge.store.counts()['events'], 1)

    def test_expired_lease_replays_and_old_receipt_is_rejected(self):
        self.listen()
        first = self.invoke('wechat_events', consumer='a')['messages'][0]
        self.assertEqual(self.invoke('wechat_events', consumer='a')['messages'], [])
        with self.bridge.store.transaction() as db:
            db.execute('UPDATE deliveries SET lease_until=0')
        second = self.invoke('wechat_events', consumer='a')['messages'][0]
        self.assertNotEqual(first['receipt'], second['receipt'])
        self.reject('invalid_receipt', lambda: self.invoke('wechat_ack', consumer='a', items=[{k: first[k] for k in ('event_id', 'receipt')}]))

    def test_failed_queue_and_manual_retry(self):
        self.listen()
        item = self.invoke('wechat_events', consumer='a')['messages'][0]
        self.invoke('wechat_ack', consumer='a', items=[{k: item[k] for k in ('event_id', 'receipt')}], failed=True)
        self.assertEqual(self.invoke('wechat_events', consumer='a')['messages'], [])
        self.assertEqual(len(self.invoke('wechat_failed_events', consumer='a')['messages']), 1)
        self.invoke('wechat_retry_event', consumer='a', event_id=item['event_id'])
        self.assertEqual(len(self.invoke('wechat_events', consumer='a')['messages']), 1)

    def test_renew_and_batch_ack_are_atomic(self):
        self.listen()
        item = self.invoke('wechat_events', consumer='a')['messages'][0]
        receipt = {k: item[k] for k in ('event_id', 'receipt')}
        result = self.invoke('wechat_renew', consumer='a', items=[receipt], lease_seconds=900)
        self.assertGreater(result['lease_until'], item['lease_until'])
        invalid = dict(event_id=999999, receipt='wrong')
        self.reject('invalid_receipt', lambda: self.invoke('wechat_ack', consumer='a', items=[receipt, invalid]))
        self.invoke('wechat_ack', consumer='a', items=[receipt])

    def test_unsubscribe_keeps_existing_checkpoint(self):
        self.listen()
        self.invoke('wechat_unlisten', chat_id='demo_team@chatroom')
        self.append()
        self.bridge.poll()
        self.assertEqual(self.bridge.store.counts()['events'], 1)
        self.listen(True)
        self.assertEqual(self.bridge.store.counts()['events'], 2)

    def test_failed_source_keeps_old_cursor_and_reports_unavailable(self):
        self.listen()
        before = self.bridge.store.subscriptions()
        (self.cfg.source_dir / 'fixture.json').write_text('{broken', encoding='utf-8')
        self.reject('source_unavailable', self.bridge.poll)
        self.assertEqual(before, self.bridge.store.subscriptions())
        self.assertIsNotNone(self.bridge.status()['last_error'])

    def test_rewound_source_cannot_silently_advance(self):
        self.listen()
        path = self.cfg.source_dir / 'fixture.json'
        data = json.loads(path.read_text('utf-8'))
        data['messages'] = []
        atomic_json(path, data)
        self.reject('source_rewound', self.bridge.poll)

    def test_transaction_failure_rolls_back_event_and_checkpoint(self):
        self.listen()
        before = self.bridge.store.subscriptions()[0]['cursors']
        rows = [{'source_key': 'good', 'text': 'valid'}, {'text': 'missing source key'}]
        with self.assertRaises(KeyError):
            self.bridge.store.ingest('demo_team@chatroom', rows, {'fixture': 100})
        self.assertEqual(self.bridge.store.counts()['events'], 1)
        self.assertEqual(self.bridge.store.subscriptions()[0]['cursors'], before)

    def test_broken_subscription_does_not_starve_other_groups(self):
        self.listen()
        self.bridge.store.subscribe('aaa_broken@chatroom', {'fixture': 999})
        self.append()
        self.reject('source_rewound', self.bridge.poll)
        self.assertEqual(self.bridge.store.counts()['events'], 2)

    def test_consumers_are_independent_and_chat_filter_applies(self):
        self.listen()
        self.assertEqual(len(self.invoke('wechat_events', consumer='a')['messages']), 1)
        self.assertEqual(len(self.invoke('wechat_events', consumer='b')['messages']), 1)
        self.assertEqual(self.invoke('wechat_events', consumer='c', chat_ids=['demo_friend'])['messages'], [])


class OutboxTests(FixtureCase):
    def test_preview_never_invokes_sender_or_turns_into_queued(self):
        self.bridge.sender = Mock()
        job = self.send()
        self.assertEqual(job['state'], 'preview')
        self.cfg.sending_mode = 'mock'
        self.assertIsNone(self.bridge.dispatch_once())
        self.bridge.sender.send.assert_not_called()
        self.reject('idempotency_conflict', self.send)

    def test_mock_send_has_one_local_record_and_idempotent_receipt(self):
        self.cfg.sending_mode = 'mock'
        first = self.send()
        self.assertEqual(self.send()['id'], first['id'])
        result = self.bridge.dispatch_once()
        self.assertEqual(result['state'], 'locally_observed')
        self.assertFalse(result['result']['delivered'])
        self.assertEqual(self.send()['id'], first['id'])
        self.assertIsNone(self.bridge.dispatch_once())
        self.bridge.source.refresh()
        self.assertEqual(len(self.bridge.source.data['messages']), 2)

    def test_payload_collision_rejected(self):
        self.send()
        self.reject('idempotency_conflict', lambda: self.send(text='different'))

    def test_unknown_never_automatically_retries(self):
        self.cfg.sending_mode = 'mock'
        self.bridge.sender = Mock()
        self.bridge.sender.send.side_effect = RuntimeError('synthetic lost response')
        job = self.send()
        result = self.bridge.dispatch_once()
        self.assertEqual(result['state'], 'unknown')
        self.bridge.dispatch_once()
        self.bridge.sender.send.assert_called_once()
        self.assertEqual(self.send()['id'], job['id'])

    def test_crash_in_sending_recovers_unknown(self):
        self.cfg.sending_mode = 'mock'
        job = self.send()
        self.bridge.store.transition(job['id'], ['queued'], 'sending', baseline={'fixture': 1})
        self.bridge.close()
        self.bridge = Bridge(self.cfg)
        self.assertEqual(self.bridge.store.job(job['id'])['state'], 'unknown')
        self.assertIsNone(self.bridge.dispatch_once())

    def test_readonly_reconciliation_never_calls_sender(self):
        self.cfg.sending_mode = 'mock'
        self.bridge.sender = Mock()
        job = self.send()
        self.assertEqual(self.bridge.dispatch_once()['state'], 'unknown')
        self.append(sender_id=self.cfg.account_id, text='hello')
        result = self.invoke('wechat_reconcile_send', id=job['id'])
        self.assertEqual(result['state'], 'locally_observed')
        self.bridge.sender.send.assert_called_once()

    def test_ambiguous_duplicate_outgoing_records_stay_unknown(self):
        self.cfg.sending_mode = 'mock'
        def send(*_):
            self.append(sender_id=self.cfg.account_id, text='hello')
            self.append(sender_id=self.cfg.account_id, text='hello')
        self.bridge.sender = Mock()
        self.bridge.sender.send.side_effect = send
        self.send()
        self.assertEqual(self.bridge.dispatch_once()['state'], 'unknown')

    def test_whitelist_checked_at_submission_and_dispatch(self):
        self.cfg.sending_mode = 'mock'
        self.reject('chat_forbidden', lambda: self.invoke('wechat_send_text', chat_id='demo_friend', text='hello', idempotency_key='no'))
        self.send()
        self.cfg.send_allowlist = []
        self.assertEqual(self.bridge.dispatch_once()['state'], 'failed')

    def test_same_name_chat_is_blocked(self):
        self.cfg.sending_mode = 'mock'
        self.send()
        path = self.cfg.source_dir / 'fixture.json'
        data = json.loads(path.read_text('utf-8'))
        data['chats'].append({'id': 'duplicate@chatroom', 'name': '隔离测试群'})
        atomic_json(path, data)
        job = self.bridge.dispatch_once()
        self.assertEqual(job['result']['reason'], 'ambiguous_target')

    def test_pause_survives_restart_and_cancel_is_final(self):
        self.cfg.sending_mode = 'mock'
        job = self.send()
        self.invoke('wechat_pause')
        self.assertIsNone(self.bridge.dispatch_once())
        self.bridge.close()
        self.bridge = Bridge(self.cfg)
        self.assertTrue(self.bridge.store.paused())
        self.invoke('wechat_cancel_send', id=job['id'])
        self.invoke('wechat_resume')
        self.assertIsNone(self.bridge.dispatch_once())

    def test_file_spooled_with_original_name_and_verified_outgoing(self):
        self.cfg.sending_mode = 'mock'
        staged = self.invoke('wechat_stage_file', path=str(self.cfg.file_roots[0] / 'hello.txt'))
        self.assertNotIn('path', staged)
        job = self.invoke('wechat_send_file', chat_id='demo_team@chatroom', attachment_id=staged['id'], idempotency_key='file-one')
        observed = self.bridge.dispatch_once()
        self.assertEqual(observed['state'], 'locally_observed')
        self.assertEqual(job['payload']['file']['name'], 'hello.txt')

    def test_changed_spool_is_rejected_before_sending(self):
        self.cfg.sending_mode = 'mock'
        staged = self.bridge.stage(self.cfg.file_roots[0] / 'hello.txt')
        self.invoke('wechat_send_file', chat_id='demo_team@chatroom', attachment_id=staged['id'], idempotency_key='file-one')
        Path(self.bridge.store.attachment(staged['id'])['path']).write_text('changed')
        job = self.bridge.dispatch_once()
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['result']['reason'], 'file_changed')

    def test_file_roots_state_exclusion_and_size_limit(self):
        outside = self.root / 'private.txt'
        outside.write_text('secret')
        self.reject('file_forbidden', lambda: self.bridge.stage(outside))
        self.cfg.file_roots.append(self.cfg.state_dir)
        self.reject('file_forbidden', lambda: self.bridge.stage(self.cfg.state_dir / 'api-token'))
        self.cfg.max_file_bytes = 2
        self.reject('file_too_large', lambda: self.bridge.stage(self.cfg.file_roots[0] / 'hello.txt'))


class SnapshotTests(FixtureCase):
    def snapshot(self):
        root = self.root / 'capture'
        paths = {}
        for shard, me in [('message/message_0.db', 7), ('message/message_1.db', 2)]:
            path = root / 'decrypted' / shard
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE Name2Id(user_name TEXT)')
                db.executemany('INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)', [(me, self.cfg.account_id), (20, 'other'), (30, 'demo_team@chatroom')])
                db.execute(f'CREATE TABLE "{table_for("demo_team@chatroom")}"(local_id INTEGER PRIMARY KEY,local_type INTEGER,sort_seq INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content BLOB)')
                db.executemany(f'INSERT INTO "{table_for("demo_team@chatroom")}" VALUES(?,?,?,?,?,?)', [(1, 1, 10, me, 100, 'own'), (2, 1, 10, 20, 101, 'other')])
                db.commit()
            paths[shard] = {'status': 'verified', 'sha256': file_hash(path)}
        atomic_json(root / 'capture_report.json', {'status': 'verified', 'source': str(self.root / (self.cfg.account_id + '_ab12') / 'db_storage'), 'databases': paths})
        cfg = Config(state_dir=self.root / 'snapshot-state', account_id=self.cfg.account_id, source_mode='snapshot', source_dir=root).validate()
        return SnapshotSource(cfg)

    def test_per_shard_sender_map_and_composite_cursor(self):
        src = self.snapshot()
        src.refresh()
        self.assertEqual(src.chats()[0]['id'], 'demo_team@chatroom')
        first, cursor = src.read('demo_team@chatroom', {}, limit=1)
        self.assertEqual(len(first), 2)
        self.assertTrue(all(x['is_self'] for x in first))
        second, cursor = src.read('demo_team@chatroom', cursor, limit=1)
        self.assertEqual(len(second), 2)
        self.assertFalse(any(x['is_self'] for x in second))
        self.assertEqual(src.read('demo_team@chatroom', cursor)[0], [])

    def test_new_shard_and_lower_sort_values_are_not_skipped(self):
        src = self.snapshot()
        src.refresh()
        first = src.paths.pop('message/message_1.db')
        _, cursor = src.read('demo_team@chatroom', {})
        src.paths['message/message_1.db'] = first
        rows, _ = src.read('demo_team@chatroom', cursor)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(x['shard'].endswith('1.db') for x in rows))

    def test_missing_shard_rejected(self):
        src = self.snapshot()
        src.refresh()
        _, cursor = src.read('demo_team@chatroom', {})
        src.paths.pop('message/message_1.db')
        self.reject('source_rewound', lambda: src.read('demo_team@chatroom', cursor))

    def test_snapshot_tamper_and_account_mismatch(self):
        src = self.snapshot()
        src.refresh()
        with next(iter(src.paths.values())).open('ab') as f: f.write(b'changed')
        self.reject('invalid_source', src.refresh)
        src.cfg.account_id = 'different'
        self.reject('account_mismatch', src.refresh)

    def test_unknown_sender_and_binary_body_are_explicit(self):
        src = self.snapshot()
        src.refresh()
        with closing(sqlite3.connect(src.paths['message/message_0.db'])) as db:
            db.execute(f'INSERT INTO "{table_for("demo_team@chatroom")}" VALUES(3,1,11,999,102,?)', (b'\xff\xfe\x00',))
            db.commit()
        rows, _ = src.read('demo_team@chatroom', {})
        row = next(x for x in rows if x['local_id'] == 3)
        self.assertIsNone(row['sender_id'])
        self.assertFalse(row['is_self'])
        self.assertEqual(row['decode_status'], 'binary_undecoded')

    def test_group_prefix_and_file_metadata(self):
        src = self.snapshot()
        src.refresh()
        with closing(sqlite3.connect(src.paths['message/message_0.db'])) as db:
            db.execute(f'INSERT INTO "{table_for("demo_team@chatroom")}" VALUES(3,1,11,20,102,?)', ('other:\nhello',))
            db.execute(f'INSERT INTO "{table_for("demo_team@chatroom")}" VALUES(4,49,12,7,103,?)', ('<msg><appmsg><title>abc.txt</title><type>6</type><appattach><totallen>12</totallen></appattach></appmsg></msg>',))
            db.commit()
        rows, _ = src.read('demo_team@chatroom', {})
        self.assertEqual(next(x for x in rows if x['local_id'] == 3)['text'], 'hello')
        self.assertEqual(next(x for x in rows if x['local_id'] == 4)['file'], {'name': 'abc.txt', 'size': 12})


class TransportTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.cfg.port = 0
        self.http = server(self.bridge)
        self.cfg.port = self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        super().tearDown()

    def test_http_to_outbox_and_errors(self):
        value = remote_call(self.cfg, 'wechat_send_text', {'chat_id': 'demo_team@chatroom', 'text': 'HTTP 中文', 'idempotency_key': 'http'})
        self.assertEqual(value['state'], 'preview')
        self.reject('unknown_tool', lambda: remote_call(self.cfg, 'no_such_tool', {}))

    def request(self, headers, path='/health', method='GET', body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.cfg.port, timeout=3)
        try:
            conn.request(method, path, body, headers)
            res = conn.getresponse()
            return res.status, json.loads(res.read())
        finally: conn.close()

    def test_http_token_host_origin_guards(self):
        self.assertEqual(self.request({})[0], 401)
        auth = {'Authorization': 'Bearer ' + self.cfg.token}
        self.assertEqual(self.request({**auth, 'Origin': 'https://evil.invalid'})[0], 403)
        self.assertEqual(self.request({**auth, 'Host': 'evil.invalid'})[0], 403)
        self.assertEqual(self.request(auth)[0], 200)

    def test_http_bad_json_and_unknown_fields_rejected(self):
        auth = {'Authorization': 'Bearer ' + self.cfg.token, 'Content-Type': 'application/json'}
        self.assertEqual(self.request(auth, '/v1/call', 'POST', '{')[0], 400)
        self.assertEqual(self.request(auth, '/v1/call', 'POST', json_text({'name': 'wechat_status', 'arguments': {'extra': 1}}))[0], 400)

    def test_mcp_initialize_list_call_and_error(self):
        outgoing = io.StringIO()
        messages = [dict(jsonrpc='2.0', id=1, method='initialize', params={'protocolVersion': '2025-06-18'}),
                    dict(jsonrpc='2.0', method='notifications/initialized'),
                    dict(jsonrpc='2.0', id=2, method='tools/list'),
                    dict(jsonrpc='2.0', id=3, method='tools/call', params={'name': 'wechat_status', 'arguments': {}}),
                    dict(jsonrpc='2.0', id=4, method='tools/call', params={'name': 'wechat_send_text', 'arguments': {}})]
        mcp(self.cfg, io.StringIO('\n'.join(json_text(m) for m in messages) + '\n'), outgoing)
        replies = [json.loads(x) for x in outgoing.getvalue().splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertEqual(len(replies[1]['result']['tools']), 19)
        self.assertFalse(replies[2]['result']['isError'])
        self.assertTrue(replies[3]['result']['isError'])

    def test_mcp_real_subprocess_stdout_only_json(self):
        raw = json.loads(self.config_path.read_text('utf-8'))
        raw['port'] = self.cfg.port
        atomic_json(self.config_path, raw)
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'wechat_status', 'arguments': {}}}]
        process = subprocess.run([sys.executable, '-m', 'wechat_bridge', '--config', str(self.config_path), 'mcp'],
                                 input='\n'.join(json_text(m) for m in messages) + '\n', text=True, encoding='utf-8', capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, '')
        replies = [json.loads(x) for x in process.stdout.splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertFalse(replies[1]['result']['isError'])

    def test_proxy_environment_does_not_receive_token(self):
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
            self.assertEqual(remote_call(self.cfg, 'wechat_status', {})['config']['account_id'], self.cfg.account_id)


class ConfigAndContractTests(FixtureCase):
    def test_all_tools_reject_unknown_arguments(self):
        for tool in tool_list():
            with self.subTest(tool=tool['name']), self.assertRaises(BridgeError):
                self.invoke(tool['name'], unexpected=True)

    def test_bools_not_integer_limits_and_nonempty_chat_filter(self):
        self.reject('invalid_request', lambda: self.invoke('wechat_events', consumer='x', limit=True))
        self.reject('invalid_arguments', lambda: self.invoke('wechat_events', consumer='x', chat_ids=[]))

    def test_state_account_binding_and_source_separation(self):
        self.reject('account_mismatch', lambda: Store(self.cfg.state_dir, 'other'))
        self.cfg.source_dir = self.cfg.state_dir / 'nested'
        self.reject('invalid_config', self.cfg.validate)

    def test_configuration_cannot_enable_desktop_with_fixture(self):
        self.cfg.sending_mode = 'desktop'
        self.reject('invalid_config', self.cfg.validate)

    def test_single_instance_lock(self):
        first = ProcessLock(self.root / 'guard.lock')
        second = ProcessLock(self.root / 'guard.lock')
        first.acquire()
        try:
            self.reject('busy', second.acquire)
        finally: first.close()
        second.acquire()
        second.close()

    def test_vendor_hashes_and_no_native_automation_loaded(self):
        directory = ROOT / 'src' / 'wechat_bridge' / 'vendor'
        manifest = json.loads((directory / 'provenance.json').read_text('utf-8-sig'))
        for item in manifest:
            self.assertEqual(file_hash(directory / item['file']), item['packaged_sha256'])
        self.assertNotIn('uiautomation', sys.modules)
        self.assertNotIn('win32clipboard', sys.modules)


if __name__ == '__main__':
    unittest.main()
