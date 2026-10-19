"""Bind the sending process to the live account through open database handles."""
from pathlib import Path

from .common import require
from .sources import account_matches


def verify_paths(cfg, paths):
    root = cfg.source_dir.resolve()
    require(account_matches(root.name, cfg.account_id), 'identity_changed', '读取目录与账号不匹配。')
    database = root / 'db_storage'
    opened = set()
    for raw in paths:
        path = Path(raw).resolve()
        if 'db_storage' not in [part.lower() for part in path.parts]:
            continue
        require(path.is_relative_to(database), 'identity_changed', '微信进程打开了另一个账号的数据库。')
        opened.add(path.relative_to(database).as_posix().lower())
    require({'contact/contact.db', 'session/session.db'} <= opened
            and any(p.startswith('message/message_') and p.endswith('.db') for p in opened),
            'identity_unavailable', '无法在绑定进程中确认当前账号的联系人、会话和消息库句柄。')
    return {'method': 'database_handles', 'account_matched': True, 'database_handle_count': len(opened)}


def verify_account(cfg):
    import psutil
    return verify_paths(cfg, [item.path for item in psutil.Process(cfg.desktop_pid).open_files()])
