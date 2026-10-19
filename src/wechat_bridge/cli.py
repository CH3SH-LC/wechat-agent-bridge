from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

from .common import BridgeError, ProcessLock, atomic_json, json_text, require
from .config import load_config, write_config


def initialize(directory):
    root = Path(directory).resolve()
    require(not root.exists(), 'exists', '初始化目标已经存在；请选择新目录。')
    root.mkdir(parents=True)
    fixture = root / 'fixture'
    fixture.mkdir()
    uploads = root / 'uploads'
    uploads.mkdir()
    atomic_json(fixture / 'fixture.json', {
        'account_id': 'wxid_demo_agent',
        'chats': [{'id': 'demo_team@chatroom', 'name': '隔离测试群'}, {'id': 'demo_friend', 'name': '隔离联系人'}],
        'messages': [{'seq': 1, 'chat_id': 'demo_team@chatroom', 'sender_id': 'demo_friend',
                      'timestamp': 1700000000, 'type': 'text', 'text': '这是隔离数据，可用于验证监听与预览。'}]})
    (uploads / 'hello.txt').write_text('WeChat bridge isolated file fixture.\n', encoding='utf-8')
    config = root / 'config.local.json'
    write_config(config, {'state_dir': str(root / 'state'), 'account_id': 'wxid_demo_agent',
                         'source_mode': 'fixture', 'source_dir': str(fixture), 'sending_mode': 'preview',
                         'send_allowlist': ['demo_team@chatroom'], 'file_roots': [str(uploads)], 'port': 18769})
    return {'config': str(config), 'sending_mode': 'preview', 'real_wechat_access': False}


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
        sys.stdin.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='普通微信号本地智能体接口；默认仅预览发送。')
    parser.add_argument('--config', default='config.local.json', help='本地配置文件')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='创建全新隔离演示目录，不访问微信')
    init.add_argument('--directory', required=True)
    sub.add_parser('doctor', help='只读列出登录进程、账号目录、版本和 DLL 哈希；输出包含私人路径')
    live = sub.add_parser('init-live', help='为明确指定的已登录账号生成实时读取配置，发送仍为预览')
    live.add_argument('--directory', required=True)
    live.add_argument('--pid', required=True, type=int)
    live.add_argument('--account-id', required=True)
    sub.add_parser('serve', help='启动本地 HTTP 服务和收发队列')
    sub.add_parser('mcp', help='通过 stdio 连接已运行服务')
    invoke = sub.add_parser('call', help='调用接口')
    invoke.add_argument('name')
    invoke.add_argument('--arguments', default='{}', help='JSON 参数；复杂内容可用 --arguments-file')
    invoke.add_argument('--arguments-file', type=Path)
    sub.add_parser('tools', help='输出完整工具定义')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            print(json_text(initialize(args.directory)))
            return 0
        if args.command == 'tools':
            from .api import tool_list
            print(json_text({'tools': tool_list()}))
            return 0
        if args.command == 'doctor':
            from .diagnostics import discover
            print(json_text(discover()))
            return 0
        if args.command == 'init-live':
            from .diagnostics import initialize_live
            print(json_text(initialize_live(args.directory, args.pid, args.account_id)))
            return 0
        cfg = load_config(args.config)
        if args.command == 'call':
            from .transport import remote_call
            arguments = json.loads(args.arguments_file.read_text('utf-8-sig') if args.arguments_file else args.arguments)
            print(json_text(remote_call(cfg, args.name, arguments)))
        elif args.command == 'mcp':
            from .transport import mcp
            mcp(cfg)
        elif args.command == 'serve':
            from .service import Bridge
            from .transport import server
            lock = ProcessLock(cfg.state_dir / 'service.lock')
            lock.acquire()
            bridge, http, desktop_lock = None, None, None
            try:
                if cfg.source_mode == 'live':
                    desktop_lock = ProcessLock(Path(tempfile.gettempdir()) / 'wechat-agent-bridge-locks' / f'pid-{cfg.desktop_pid}.lock')
                    desktop_lock.acquire()
                bridge = Bridge(cfg)
                http = server(bridge)
                bridge.start()
                print(json_text({'listening': f'http://127.0.0.1:{http.server_address[1]}', 'sending_mode': cfg.sending_mode}), file=sys.stderr, flush=True)
                http.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                pass
            finally:
                if http:
                    http.server_close()
                if bridge:
                    bridge.close()
                if desktop_lock:
                    desktop_lock.close()
                lock.close()
        return 0
    except (BridgeError, OSError, ValueError, TypeError) as exc:
        print(json_text({'error': {'code': getattr(exc, 'code', 'configuration_or_io_error'), 'message': str(exc)[:800]}}), file=sys.stderr)
        return 1
