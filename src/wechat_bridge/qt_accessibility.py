"""Scoped Qt accessibility activation for an explicitly pinned Weixin DLL.

The PE signature/Qt gate discovery method is adapted from the Apache-2.0
wechatauto-replica accessibility module (d322c4a9b1124228820bab363e5676016f188fc6).
See THIRD_PARTY_NOTICES.md. This implementation does not change OS settings,
inject executable code, patch the on-disk DLL, or retain the changed byte.
"""
from __future__ import annotations

from contextlib import closing
import ctypes as C
from ctypes import wintypes as W
from functools import lru_cache
from pathlib import Path
import re
import struct

from .common import BridgeError, file_hash, require

EXECUTE, WRITE = 0x20000000, 0x80000000
GATE = re.compile(rb'\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>.{4})\x00\x0f\x84', re.DOTALL)
LEA = re.compile(rb'\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d](....)', re.DOTALL)


def sections(data):
    require(len(data) >= 64 and data[:2] == b'MZ', 'unsupported_dll', '微信模块不是有效 PE 文件。')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    require(pe + 24 <= len(data) and data[pe:pe+4] == b'PE\0\0' and struct.unpack_from('<H', data, pe+4)[0] == 0x8664,
            'unsupported_dll', '仅支持已验证的 64 位微信模块。')
    count, opt = struct.unpack_from('<H', data, pe+6)[0], struct.unpack_from('<H', data, pe+20)[0]
    require(0 < count < 100, 'unsupported_dll', '模块节表无效。')
    require(pe + 24 + opt + 40 * count <= len(data), 'unsupported_dll', '模块节表被截断。')
    result = []
    for i in range(count):
        off = pe + 24 + opt + 40 * i
        size, rva, raw_size, raw = struct.unpack_from('<IIII', data, off+8)
        flags = struct.unpack_from('<I', data, off+36)[0]
        require(raw + raw_size <= len(data), 'unsupported_dll', '模块节表越界。')
        result.append(dict(rva=rva, size=max(size, raw_size), raw=raw, raw_size=raw_size, flags=flags))
    return result


def candidates(data):
    secs = sections(data)
    def offset_rva(off):
        return next((s['rva'] + off - s['raw'] for s in secs if s['raw'] <= off < s['raw'] + s['raw_size']), None)
    marker = data.find(b'qt.accessibility.core')
    require(marker >= 0, 'unsupported_dll', '模块缺少 Qt 无障碍特征。')
    core_rva = offset_rva(marker)
    refs = []
    for s in secs:
        if not s['flags'] & EXECUTE:
            continue
        start, end = s['raw'], s['raw'] + s['raw_size']
        for m in LEA.finditer(data, start, end):
            rva = s['rva'] + m.start() - start
            if rva + 6 + struct.unpack('<i', m.group(1))[0] == core_rva:
                refs.append(rva)
    require(refs, 'unsupported_dll', '未找到 Qt 无障碍特征的代码引用。')
    found = {}
    for m in GATE.finditer(data):
        location = offset_rva(m.start())
        disp_rva = offset_rva(m.start('disp'))
        if location is None or disp_rva is None:
            continue
        code = next((s for s in secs if s['rva'] <= location < s['rva'] + s['size']), None)
        target = disp_rva + 5 + struct.unpack('<i', m.group('disp'))[0]
        dest = next((s for s in secs if s['rva'] <= target < s['rva'] + s['size']), None)
        distance = min(abs(location - ref) for ref in refs)
        if code and code['flags'] & EXECUTE and dest and dest['flags'] & WRITE and not dest['flags'] & EXECUTE and distance <= 0x2000:
            found[target] = min(distance, found.get(target, distance))
    return [{'rva': target, 'distance': dist} for target, dist in sorted(found.items(), key=lambda pair: pair[1])]


def module(pid):
    import win32api
    import win32process
    handle = win32api.OpenProcess(0x410, False, pid)
    try:
        found = []
        for address in win32process.EnumProcessModules(handle):
            path = Path(win32process.GetModuleFileNameEx(handle, address))
            if path.name.lower() == 'weixin.dll':
                found.append((int(address), path))
        require(len(found) == 1, 'identity_changed', '无法唯一定位微信主模块。')
        return found[0]
    finally:
        handle.Close()


@lru_cache(maxsize=4)
def plan_for(path, size, mtime, expected_hash):
    require(file_hash(path) == expected_hash, 'version_changed', '微信 DLL 哈希已变化，拒绝沿用旧版本兼容配置。')
    options = candidates(Path(path).read_bytes())
    require(len(options) == 1, 'unsupported_dll', 'Qt 无障碍标志不是唯一候选，未修改进程。')
    return options[0]


class RuntimeGate:
    def __init__(self, cfg):
        self.cfg, self.handle = cfg, None
        self.address, self.original, self.changed = None, None, False
        self.evidence = {'mode': 'existing', 'changed': False, 'restored': True}

    def read(self):
        buf, size = C.c_ubyte(), C.c_size_t()
        require(self.kernel.ReadProcessMemory(self.handle, C.c_void_p(self.address), C.byref(buf), 1, C.byref(size)) and size.value == 1,
                'accessibility_unavailable', '无法读取 Qt 无障碍运行标志。')
        return buf.value

    def write(self, value):
        buf, size = C.c_ubyte(value), C.c_size_t()
        require(self.kernel.WriteProcessMemory(self.handle, C.c_void_p(self.address), C.byref(buf), 1, C.byref(size)) and size.value == 1,
                'accessibility_unavailable', '无法设置 Qt 无障碍运行标志。')
        require(self.read() == value, 'accessibility_unavailable', 'Qt 无障碍标志回读不一致。')

    def __enter__(self):
        if self.cfg.desktop_accessibility == 'existing':
            return self
        require(self.cfg.desktop_accessibility == 'scoped_qt' and re.fullmatch('[0-9a-f]{64}', self.cfg.desktop_dll_sha256 or ''),
                'invalid_config', 'Qt 兼容模式必须固定当前 DLL 的 SHA-256。')
        from .vendor.extract_local import ReadOnlyProcess
        with closing(ReadOnlyProcess(self.cfg.desktop_pid, self.cfg.weixin_exe)):
            pass
        base, path = module(self.cfg.desktop_pid)
        require(Path(self.cfg.weixin_exe).resolve().parent in path.resolve().parents,
                'identity_changed', '模块路径不属于已绑定微信安装。')
        stat = path.stat()
        plan = plan_for(str(path), stat.st_size, stat.st_mtime_ns, self.cfg.desktop_dll_sha256)
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.kernel.OpenProcess.restype = W.HANDLE
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        for name in ('ReadProcessMemory', 'WriteProcessMemory'):
            fn = getattr(self.kernel, name)
            fn.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
            fn.restype = W.BOOL
        # Only this scoped UI compatibility path requests write access; the
        # collector still opens the process with QUERY_INFORMATION | VM_READ.
        self.handle = self.kernel.OpenProcess(0x438, False, self.cfg.desktop_pid)
        require(bool(self.handle), 'accessibility_unavailable', '无法开启本地 Qt 控件兼容会话。')
        try:
            require(module(self.cfg.desktop_pid) == (base, path), 'identity_changed', '微信模块在检查期间发生变化。')
            self.address = base + plan['rva']
            self.original = self.read()
            require(self.original in (0, 1), 'unsupported_dll', 'Qt 标志原值不是布尔值，未修改。')
            self.evidence = {'mode': 'scoped_qt', 'rva': plan['rva'], 'original': self.original,
                             'changed': self.original == 0, 'restored': self.original == 1,
                             'dll_sha256': self.cfg.desktop_dll_sha256}
            if self.original == 0:
                self.changed = True  # restore even when the write outcome is uncertain
                self.write(1)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.handle:
            try:
                if self.changed:
                    self.write(self.original)
                    self.evidence['restored'] = True
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
