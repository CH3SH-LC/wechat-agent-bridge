"""Opt-in real API acceptance, restricted to 测试群 or 文件传输助手.

Private output directory must NOT be committed. No UI automation in this client;
all actions use the same authenticated API that an agent uses.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import time

from wechat_bridge.common import atomic_json, require
from wechat_bridge.config import load_config
from wechat_bridge.transport import remote_call


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--chat-name', choices=['测试群', '文件传输助手'], default='测试群')
    parser.add_argument('--output', required=True, help='New private evidence directory, outside the public release')
    parser.add_argument('--execute', action='store_true', help='Actually send one text and one harmless txt attachment')
    args = parser.parse_args()
    require(args.execute, 'explicit_execution_required', '真实发送需要 --execute。')
    cfg = load_config(args.config)
    output = Path(args.output).resolve()
    require(not output.exists(), 'exists', '验收目录已存在；先检查旧回执，不自动重发。')
    output.mkdir(parents=True)
    run = datetime.now().strftime('%Y%m%d-%H%M%S')
    def call(name, **kwargs):
        return remote_call(cfg, name, kwargs)
    def wait(job, label):
        atomic_json(output / (label + '-request.json'), job)
        deadline = time.monotonic() + 120
        while job['state'] in ('queued', 'sending') and time.monotonic() < deadline:
            time.sleep(.5)
            job = call('wechat_send_status', id=job['id'])
        atomic_json(output / (label + '-receipt.json'), job)
        require(job['state'] == 'locally_observed', 'acceptance_stopped',
                '发送未得到本地回读确认，已停止；检查私有回执，不自动重发。')
        print(json.dumps({'step': label, 'state': job['state']}, ensure_ascii=False), flush=True)
        return job
    status = call('wechat_status')
    caps = status.get('capabilities', {})
    require(caps.get('real_send_text') is True and caps.get('real_send_file') is True
            and caps.get('ui_automation') is False, 'background_sender_unavailable',
            '服务必须具备真实后台文字和文件发送能力；当前版本不具备，不执行验收。')
    names = status['config']['send_target_names']
    require(set(names) == set(status['config']['send_allowlist']) and names
            and set(names.values()) <= {'测试群', '文件传输助手'}, 'unsafe_acceptance_config', '本验收只接受两个指定名称且固定 ID 的白名单。')
    call('wechat_refresh_source')
    chats = call('wechat_list_chats', query=args.chat_name)['chats']
    matches = [c for c in chats if c['name'] == args.chat_name]
    require(len(matches) == 1 and names.get(matches[0]['id']) == args.chat_name,
            'ambiguous_target', '目标不唯一或未固定 ID。')
    chat = matches[0]['id']
    require(not any(status['outbox'].get(state, 0) for state in ('queued', 'sending')),
            'pending_sends', '先处理已有发送队列，避免恢复时启动其他请求。')
    call('wechat_listen', chat_id=chat)
    baseline = call('wechat_history', chat_id=chat, limit=10)
    atomic_json(output / 'reading-baseline.json', {'status': status, 'history': baseline})
    consumer = 'acceptance-' + run
    text = '[接口验收] 文字发送与群聊监听 ' + run
    try:
        call('wechat_resume')
        request = dict(chat_id=chat, text=text, idempotency_key=run+'-text')
        text_job = wait(call('wechat_send_text', **request), 'text')
        repeat = call('wechat_send_text', **request)
        require(repeat['id'] == text_job['id'], 'idempotency_failed', '重复请求没有返回原记录。')
        require(bool(cfg.file_roots), 'invalid_config', '需要允许的附件目录。')
        file = cfg.file_roots[0] / ('wechat-bridge-' + run + '.txt')
        file.write_text('WeChat Agent Bridge isolated API acceptance.\nRun: ' + run + '\nHarmless text attachment.\n', encoding='utf-8')
        staged = call('wechat_stage_file', path=str(file))
        atomic_json(output / 'file-staged.json', staged)
        file_job = wait(call('wechat_send_file', chat_id=chat, attachment_id=staged['id'], idempotency_key=run+'-file'), 'file')
        wanted = {j['result']['source_key'] for j in (text_job, file_job)}
        seen, records = set(), []
        deadline = time.monotonic() + 90
        while seen != wanted and time.monotonic() < deadline:
            call('wechat_refresh_source')
            batch = call('wechat_events', consumer=consumer, chat_ids=[chat], limit=100)['messages']
            for item in batch:
                if item['source_key'] in wanted:
                    require(item['chat_id'] == chat and item['is_self'], 'listener_mismatch', '监听消息目标或发送方不符。')
                    seen.add(item['source_key'])
                    records.append(item)
            if batch:
                call('wechat_ack', consumer=consumer, items=[{k:m[k] for k in ('event_id', 'receipt')} for m in batch])
            if seen != wanted:
                time.sleep(1)
        atomic_json(output / 'listener-events.json', records)
        require(seen == wanted, 'listener_timeout', '监听未完整收到两条回读记录。')
        summary = {'passed': True, 'run': run, 'target_name': args.chat_name,
                   'reading': True, 'text_locally_observed': True, 'file_locally_observed': True,
                   'listener_matched': len(seen), 'idempotency_verified': True,
                   'remote_delivery_or_read_verified': False, 'interface': 'authenticated_loopback_HTTP'}
        atomic_json(output / 'summary.json', summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        call('wechat_pause')


if __name__ == '__main__':
    main()
