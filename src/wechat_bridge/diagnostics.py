"""Read-only process/account discovery. No window or accessibility APIs."""
from __future__ import annotations

import os
from pathlib import Path
import re

from .common import require, file_hash, atomic_json


def module_path(pid):
    import win32api
    import win32process
    handle = win32api.OpenProcess(0x410, False, pid)  # QUERY_INFORMATION | VM_READ
    try:
        found = []
        for address in win32process.EnumProcessModules(handle):
            path = Path(win32process.GetModuleFileNameEx(handle, address))
            if path.name.lower() == 'weixin.dll':
                found.append(path)
        require(len(found) == 1, 'identity_changed', '无法唯一定位微信主模块。')
        return found[0]
    finally:
        handle.Close()


def discover():
    require(os.name == 'nt', 'platform_unavailable', '微信实时接入仅支持 Windows。')
    import psutil
    import win32api
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
            dll = module_path(proc.pid)
            version = win32api.GetFileVersionInfo(str(dll), '\\')
            parts = [version['FileVersionMS'] >> 16, version['FileVersionMS'] & 65535,
                     version['FileVersionLS'] >> 16, version['FileVersionLS'] & 65535]
            result.append({'pid': proc.pid, 'exe': proc.info['exe'], 'version': '.'.join(map(str, parts)),
                           'dll_sha256': file_hash(dll),
                           'accounts': [{'source_dir': str(root), 'suggested_account_id':
                             (re.match(r'^(wxid_[A-Za-z0-9]+)_', root.name).group(1)
                              if re.match(r'^(wxid_[A-Za-z0-9]+)_', root.name) else None)} for root in sorted(roots)]})
        except (psutil.Error, OSError) as exc:
            result.append({'pid': proc.pid, 'error': type(exc).__name__})
    return {'candidates': result, 'read_only': True, 'contains_private_local_paths': True}


def initialize_live(directory, pid, account_id):
    from .config import Config
    from .desktop_identity import verify_account
    candidates = [c for c in discover()['candidates'] if c['pid'] == pid and 'error' not in c]
    require(len(candidates) == 1, 'identity_unavailable', '没有找到唯一、已登录的微信主进程。')
    found = candidates[0]
    require(len(found['accounts']) == 1, 'identity_unavailable', '进程账号目录不唯一。')
    root = Path(directory).resolve()
    require(not root.exists(), 'exists', '初始化目标已存在，不覆盖。')
    data = {'state_dir': str(root / 'state'), 'source_mode': 'live', 'source_dir': found['accounts'][0]['source_dir'],
            'account_id': account_id, 'sending_mode': 'disabled', 'send_allowlist': [], 'send_target_names': {},
            'file_roots': [str(root / 'uploads')], 'weixin_exe': found['exe'], 'desktop_pid': pid}
    cfg = Config(**data).validate()
    verify_account(cfg)
    root.mkdir(parents=True)
    (root / 'uploads').mkdir()
    atomic_json(root / 'config.local.json', data)
    return {'config': str(root / 'config.local.json'), 'sending_mode': 'disabled',
            'version': found['version'], 'next': 'serve, list chats, listen; background sending is not implemented'}
