from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import uuid


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise BridgeError(code, message)


def now():
    return time.time()


def uid(prefix):
    return prefix + uuid.uuid4().hex


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("x", encoding="utf-8") as stream:
        stream.write(json_text(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def bounded_int(value, minimum=0, maximum=1000):
    require(type(value) is int and minimum <= value <= maximum, "invalid_request", "整数参数超出范围。")
    return value


def string(value, maximum=4096, empty=False):
    require(isinstance(value, str) and len(value) <= maximum and (empty or bool(value.strip()))
            and "\x00" not in value, "invalid_request", "文本参数无效。")
    return value


def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


class ProcessLock:
    """Lifetime lock: only one gateway may operate a state directory."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, 2)
        if not self.stream.tell():
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise BridgeError("busy", "同一数据目录已有服务运行。") from None

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None
