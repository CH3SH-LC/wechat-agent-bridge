"""Local read-only discovery. No message/key scan and no accessibility write."""
from __future__ import annotations

import os
from pathlib import Path
import re

from .common import require, file_hash, atomic_json


def discover():
    require(os.name == 'nt', 'desktop_unavailable', '微信实时接入仅支持 Windows。')
    import psutil
    import win32api
    import win32gui
    import win32process
    from .qt_accessibility import module
    windows = {}
    def visit(handle, _):
        if win32gui.IsWindowVisible(handle):
            pid = win32process.GetWindowThreadProcessId(handle)[1]
            windows.setdefault(pid, []).append({'handle': handle, 'title': win32gui.GetWindowText(handle),
                                                'class': win32gui.GetClassName(handle)})
    win32gui.EnumWindows(visit, None)
    result = []
    for proc in psutil.process_iter(['pid', 'name', 'exe']):
        if (proc.info['name'] or '').lower() != 'weixin.exe':
            continue
        try:
            roots = set()
            for item in proc.open_files():
                path = Path(item.path)
                if path.name.lower() == 'contact.db' and path.parent.name.lower() == 'contact' and path.parent.parent.name.lower() == 'db_storage':
                    roots.add(path.parent.parent.parent)
            if not roots:
                continue
            _, dll = module(proc.pid)
            version = win32api.GetFileVersionInfo(str(dll), '\\')
            parts = [version['FileVersionMS'] >> 16, version['FileVersionMS'] & 65535,
                     version['FileVersionLS'] >> 16, version['FileVersionLS'] & 65535]
            result.append({'pid': proc.pid, 'exe': proc.info['exe'], 'version': '.'.join(map(str, parts)),
                           'dll_sha256': file_hash(dll), 'windows': windows.get(proc.pid, []),
                           'accounts': [{'source_dir': str(root), 'suggested_account_id':
                             (re.match(r'^(wxid_[A-Za-z0-9]+)_', root.name).group(1)
                              if re.match(r'^(wxid_[A-Za-z0-9]+)_', root.name) else None)} for root in sorted(roots)]})
        except (psutil.Error, OSError) as exc:
            result.append({'pid': proc.pid, 'error': type(exc).__name__})
    return {'candidates': result, 'read_only': True, 'contains_private_local_paths': True}


def initialize_live(directory, pid, window, account_id, scoped_qt=False):
    from .config import Config
    from .desktop_identity import verify_account
    candidates = [c for c in discover()['candidates'] if c['pid'] == pid and 'error' not in c]
    require(len(candidates) == 1, 'identity_unavailable', '没有找到唯一、已登录的微信主进程。')
    found = candidates[0]
    require(len(found['accounts']) == 1, 'identity_unavailable', '进程账号目录不唯一。')
    require(any(w['handle'] == window for w in found['windows']), 'identity_changed', '窗口不属于该微信进程。')
    root = Path(directory).resolve()
    require(not root.exists(), 'exists', '初始化目标已存在，不覆盖。')
    data = {'state_dir': str(root / 'state'), 'source_mode': 'live', 'source_dir': found['accounts'][0]['source_dir'],
            'account_id': account_id, 'sending_mode': 'preview', 'send_allowlist': [], 'send_target_names': {},
            'file_roots': [str(root / 'uploads')], 'weixin_exe': found['exe'], 'desktop_pid': pid,
            'desktop_window': window, 'desktop_identity': 'database_handles',
            'desktop_accessibility': 'scoped_qt' if scoped_qt else 'existing',
            'desktop_dll_sha256': found['dll_sha256'] if scoped_qt else ''}
    cfg = Config(**data).validate()
    verify_account(cfg)
    root.mkdir(parents=True)
    (root / 'uploads').mkdir()
    atomic_json(root / 'config.local.json', data)
    return {'config': str(root / 'config.local.json'), 'sending_mode': 'preview',
            'version': found['version'], 'next': 'serve, list chats, then bind exact IDs and names locally before enabling desktop mode'}
