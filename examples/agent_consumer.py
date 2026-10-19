"""Runnable consumer; demo handler only produces an explicit text receipt.

Run with: python examples/agent_consumer.py --config ... --chat-id ... --once
Replace handle() with the actual agent, keeping the lease/idempotency contract.
"""
import argparse
import json
import time

from wechat_bridge.config import load_config
from wechat_bridge.common import require
from wechat_bridge.transport import remote_call


def handle(message):
    # Deliberately no model call, shell execution, URL fetch or file access from
    # incoming text. Bind the real agent using trusted local instructions.
    return '已收到消息（接口演示）：' + message['text'][:120]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--chat-id', required=True)
    parser.add_argument('--consumer', default='demo-agent-v1')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    cfg = load_config(args.config)
    require(cfg.source_mode == 'fixture' and cfg.sending_mode in ('preview', 'mock'),
            'demo_only', '演示消费者只允许运行于隔离来源；接入真实智能体时需另行定义触发与授权规则。')
    remote_call(cfg, 'wechat_listen', {'chat_id': args.chat_id, 'from_now': False})
    while True:
        messages = remote_call(cfg, 'wechat_events', {'consumer': args.consumer, 'chat_ids': [args.chat_id], 'limit': 10, 'lease_seconds': 300})['messages']
        for msg in messages:
            receipt = {'event_id': msg['event_id'], 'receipt': msg['receipt']}
            try:
                if not msg['is_self'] and msg['type'] == 'text':
                    # One stable command per source event: retrying this consumer
                    # must never create a second outbound command.
                    result = remote_call(cfg, 'wechat_send_text', {'chat_id': msg['chat_id'], 'text': handle(msg),
                        'idempotency_key': args.consumer + ':' + str(msg['event_id'])})
                    print(json.dumps({'event_id': msg['event_id'], 'outbox_id': result['id'], 'state': result['state']}))
                    if result['state'] in ('failed', 'unknown', 'cancelled'):
                        raise RuntimeError('发送需要人工处理，不能用新幂等键自动重发')
                # Ack means durable work was accepted; track outbox separately.
                remote_call(cfg, 'wechat_ack', {'consumer': args.consumer, 'items': [receipt]})
            except Exception:
                remote_call(cfg, 'wechat_ack', {'consumer': args.consumer, 'items': [receipt], 'failed': True})
                raise
        if args.once:
            return
        time.sleep(2)


if __name__ == '__main__':
    main()
