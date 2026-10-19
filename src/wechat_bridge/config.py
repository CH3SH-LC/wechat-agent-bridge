from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import secrets

from .common import atomic_json, inside, require, string


@dataclass
class Config:
    state_dir: Path
    account_id: str
    source_mode: str = "fixture"
    source_dir: Path | None = None
    sending_mode: str = "preview"
    send_allowlist: list[str] = field(default_factory=list)
    send_target_names: dict[str, str] = field(default_factory=dict)
    file_roots: list[Path] = field(default_factory=list)
    poll_seconds: float = 3.0
    send_interval_seconds: float = 3.0
    verify_seconds: float = 15.0
    max_file_bytes: int = 25 * 1024 * 1024
    port: int = 18769
    weixin_exe: str = r"D:\Program Files\Tencent\Weixin\Weixin.exe"
    desktop_pid: int | None = None
    native_dll_path: str | None = None
    native_dll_sha256: str = ""
    token: str = ""

    def validate(self):
        require(isinstance(self.file_roots, list), 'invalid_config', 'file_roots 必须为目录列表。')
        self.state_dir = Path(self.state_dir).resolve()
        if self.source_dir is not None:
            self.source_dir = Path(self.source_dir).resolve()
        self.file_roots = [Path(p).resolve() for p in self.file_roots]
        require(self.source_mode in {"fixture", "snapshot", "live"}, "invalid_config", "来源模式无效。")
        require(self.sending_mode in {"preview", "disabled", "mock", "native_experimental"}, "invalid_config", "发送模式无效；界面与网页发送均不受支持。")
        string(self.account_id, 200)
        require(bool(re.fullmatch(r"[A-Za-z0-9_@.\-]+", self.account_id)), "invalid_config", "账号标识无效。")
        require(self.source_dir is not None, "invalid_config", "必须显式选择读取来源。")
        require(not inside(self.state_dir, self.source_dir) and not inside(self.source_dir, self.state_dir),
                "invalid_config", "服务私有目录与微信/快照来源目录必须分离。")
        require(all(type(v) in (int, float) and math.isfinite(v) for v in
                    (self.poll_seconds, self.send_interval_seconds, self.verify_seconds)), 'invalid_config', '时间间隔必须为有限数值。')
        require(1 <= self.poll_seconds <= 3600 and 0 <= self.send_interval_seconds <= 3600
                and 0 <= self.verify_seconds <= 120, "invalid_config", "轮询或发送间隔无效。")
        require(type(self.port) is int and 0 <= self.port <= 65535, "invalid_config", "端口无效。")
        require(type(self.max_file_bytes) is int and 0 < self.max_file_bytes <= 1024**3,
                "invalid_config", "附件大小限制无效。")
        require(isinstance(self.send_allowlist, list) and all(isinstance(x, str) and x for x in self.send_allowlist)
                and "*" not in self.send_allowlist, "invalid_config", "发送白名单必须为明确的会话 ID。")
        require(isinstance(self.send_target_names, dict)
                and all(isinstance(k, str) and isinstance(v, str) and v and k in self.send_allowlist
                        for k, v in self.send_target_names.items()),
                'invalid_config', '固定名称必须绑定白名单中的会话 ID。')
        require(not self.send_target_names or set(self.send_target_names) == set(self.send_allowlist),
                'invalid_config', '启用固定名称后必须覆盖整个发送白名单。')
        if self.sending_mode == "mock":
            require(self.source_mode == "fixture", "invalid_config", "模拟发送只能搭配隔离数据。")
        if self.source_mode == 'live':
            require(type(self.desktop_pid) is int and self.desktop_pid > 0, 'invalid_config', '实时读取必须绑定微信主进程 PID。')
        if self.sending_mode == 'native_experimental':
            require(self.source_mode == 'live' and isinstance(self.native_dll_path, str) and self.native_dll_path
                    and bool(re.fullmatch(r'[a-f0-9]{64}', self.native_dll_sha256)),
                    'invalid_config', '原生发送必须绑定实时来源、明确的适配 DLL 及 SHA256。')
            require(bool(self.send_allowlist) and set(self.send_target_names) == set(self.send_allowlist),
                    'invalid_config', '原生发送必须固定所有白名单目标的名称。')
        return self

    def public(self):
        return {"source_mode": self.source_mode, "sending_mode": self.sending_mode,
                "account_id": self.account_id, "send_allowlist": self.send_allowlist,
                "send_target_names": self.send_target_names,
                "poll_seconds": self.poll_seconds}


def load_config(path):
    raw = json.loads(Path(path).read_text("utf-8-sig"))
    require(isinstance(raw, dict), 'invalid_config', '配置必须是对象。')
    for retired in ('desktop_window', 'desktop_account_anchor', 'desktop_identity',
                    'desktop_accessibility', 'desktop_dll_sha256', 'send_hotkey'):
        raw.pop(retired, None)
    require(isinstance(raw, dict) and not set(raw) - set(Config.__dataclass_fields__), "invalid_config", "配置包含未知字段。")
    cfg = Config(**raw).validate()
    token_path = cfg.state_dir / "api-token"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        cfg.token = token_path.read_text("ascii").strip()
    else:
        cfg.token = secrets.token_urlsafe(32)
        with token_path.open("x", encoding="ascii") as stream:
            stream.write(cfg.token)
        os.chmod(token_path, 0o600)
    require(len(cfg.token) >= 32, "invalid_config", "服务令牌无效。")
    return cfg


def write_config(path, value):
    require(not Path(path).exists(), "exists", "配置已存在，不覆盖。")
    atomic_json(path, value)
