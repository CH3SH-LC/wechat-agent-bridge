"""Real CLI daemon -> HTTP -> MCP -> durable mock outgoing records."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from wechat_bridge.cli import initialize
from wechat_bridge.common import atomic_json, json_text
from wechat_bridge.config import load_config
from wechat_bridge.transport import remote_call


class ProcessAcceptance(unittest.TestCase):
    def test_daemon_and_agent_consumer_complete_mock_roundtrip(self):
        root = Path(tempfile.mkdtemp(prefix='process-', dir=Path(__file__).resolve().parents[1] / '.test-output'))
        path = Path(initialize(root / 'demo')['config'])
        config = json.loads(path.read_text('utf-8'))
        config.update(port=0, sending_mode='mock', poll_seconds=1, verify_seconds=0, send_interval_seconds=0)
        atomic_json(path, config)
        proc = subprocess.Popen([sys.executable, '-m', 'wechat_bridge', '--config', str(path), 'serve'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        try:
            startup = json.loads(proc.stderr.readline())
            config['port'] = int(startup['listening'].rsplit(':', 1)[1])
            atomic_json(path, config)
            cfg = load_config(path)
            remote_call(cfg, 'wechat_refresh_source', {})
            consumer = Path(__file__).resolve().parents[1] / 'examples' / 'agent_consumer.py'
            run = subprocess.run([sys.executable, str(consumer), '--config', str(path), '--chat-id', 'demo_team@chatroom', '--once'], capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            receipt = json.loads(run.stdout.strip())
            deadline = time.monotonic() + 10
            while True:
                result = remote_call(cfg, 'wechat_send_status', {'id': receipt['outbox_id']})
                if result['state'] not in ('queued', 'sending') or time.monotonic() > deadline: break
                time.sleep(0.1)
            self.assertEqual(result['state'], 'locally_observed')
            remote_call(cfg, 'wechat_refresh_source', {})
            events = remote_call(cfg, 'wechat_events', {'consumer': 'audit', 'chat_ids': ['demo_team@chatroom']})['messages']
            self.assertEqual(len(events), 2)
            self.assertTrue(events[-1]['is_self'])
            # Second consumer invocation acknowledges own output without replying.
            second = subprocess.run([sys.executable, str(consumer), '--config', str(path), '--chat-id', 'demo_team@chatroom', '--once'], capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout, '')
            attachment = remote_call(cfg, 'wechat_stage_file', {'path': str(cfg.file_roots[0] / 'hello.txt')})
            file_job = remote_call(cfg, 'wechat_send_file', {'chat_id': 'demo_team@chatroom', 'attachment_id': attachment['id'], 'idempotency_key': 'process-file'})
            deadline = time.monotonic() + 10
            while True:
                file_result = remote_call(cfg, 'wechat_send_status', {'id': file_job['id']})
                if file_result['state'] not in ('queued', 'sending') or time.monotonic() > deadline: break
                time.sleep(0.1)
            self.assertEqual(file_result['state'], 'locally_observed')
            self.assertEqual(file_result['payload']['file']['name'], 'hello.txt')
            (root / 'receipt.json').write_text(json_text({'startup': startup, 'reply': result, 'file_reply': file_result, 'events_before_file': len(events), 'real_wechat_access': False}), encoding='utf-8')
        finally:
            proc.terminate()  # only the synthetic child created above
            proc.communicate(timeout=10)


if __name__ == '__main__': unittest.main()
