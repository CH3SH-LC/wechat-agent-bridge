"""Experimental version-locked native text/file transport. No GUI or web protocol.

The DLL remains loaded because native asynchronous callbacks may outlive a request.
Timeouts are unknown outcomes, never permission to repeat a message.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import threading
import uuid

from .common import require, BridgeError, file_hash

WEIXIN_SHA256 = '6c48a66cee5b03cc951e8ea1877313c3c7b044049d2c5f451281f7eaeb537b73'
TEXT_RVA = 0x55c4580
TEXT_PROLOGUE = bytes.fromhex('5541574156415541545657534881ec18030000')


class NativeRequest(C.Structure):
    _fields_ = [('magic', C.c_uint32), ('size', C.c_uint32), ('operation', C.c_uint32),
                ('reserved', C.c_uint32), ('recipient', C.c_char * 256),
                ('allowed_recipient', C.c_char * 256), ('text', C.c_char * 8192),
                ('request_id', C.c_char * 64), ('status', C.c_int32), ('error', C.c_char * 512)]


def make_request(recipient, text, probe=False, file=False):
    require(isinstance(recipient, str) and isinstance(text, str) and '\0' not in recipient + text,
            'invalid_request', '原生参数必须为不含 NUL 的字符串。')
    encoded, content = recipient.encode('utf-8'), text.encode('utf-8')
    require(0 < len(encoded) < 256 and 0 < len(content) < 8192,
            'invalid_request', '原生文字参数长度无效。')
    require(not probe or recipient == 'wab_probe_nonexistent_contact', 'invalid_request', '探针目标无效。')
    require(not (probe and file), 'invalid_request', '探针不得提交附件。')
    request = NativeRequest()
    request.magic, request.size, request.operation = 0x57414232, C.sizeof(NativeRequest), 1 if probe else (3 if file else 2)
    request.recipient = request.allowed_recipient = encoded
    request.text, request.request_id = content, uuid.uuid4().hex.encode('ascii')
    return request


class NativeTextSender:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()

    def send(self, payload, attachment, title, guard):
        require(payload['chat_id'] in self.cfg.send_allowlist, 'chat_forbidden', '目标不在白名单。')
        if payload['type'] == 'file':
            require(attachment is not None, 'invalid_request', '文件发送必须使用已暂存附件。')
            return self._execute(make_request(payload['chat_id'], attachment['path'], file=True), guard)
        return self._execute(make_request(payload['chat_id'], payload['text']), guard)

    def probe(self):
        return self._execute(make_request('wab_probe_nonexistent_contact', 'ABI probe', True), lambda: None)

    def _execute(self, request, guard):
        require(os.name == 'nt' and C.sizeof(C.c_void_p) == 8, 'platform_unavailable', '原生适配仅支持 Windows x64。')
        from .desktop_identity import verify_account
        from .diagnostics import module_path
        with self.lock:
            verify_account(self.cfg)
            module = module_path(self.cfg.desktop_pid)
            require(file_hash(module) == WEIXIN_SHA256, 'native_version_mismatch', '微信 DLL 不匹配已分析的版本。')
            dll = Path(self.cfg.native_dll_path).resolve(strict=True)
            require(file_hash(dll) == self.cfg.native_dll_sha256, 'native_adapter_changed', '原生适配 DLL 哈希变化。')
            guard()
            return _invoke(self.cfg.desktop_pid, dll, request, guard)


def _invoke(pid, dll, request, guard):
    import win32api
    import win32process
    kernel = C.WinDLL('kernel32', use_last_error=True)
    ptr, size = C.c_void_p, C.c_size_t
    signatures = {
        'OpenProcess': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        'VirtualAllocEx': ([W.HANDLE, ptr, size, W.DWORD, W.DWORD], ptr),
        'VirtualFreeEx': ([W.HANDLE, ptr, size, W.DWORD], W.BOOL),
        'WriteProcessMemory': ([W.HANDLE, ptr, ptr, size, C.POINTER(size)], W.BOOL),
        'ReadProcessMemory': ([W.HANDLE, ptr, ptr, size, C.POINTER(size)], W.BOOL),
        'CreateRemoteThread': ([W.HANDLE, ptr, size, ptr, ptr, W.DWORD, ptr], W.HANDLE),
        'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
        'GetExitCodeThread': ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        'CloseHandle': ([W.HANDLE], W.BOOL),
        'GetModuleHandleExW': ([W.DWORD, ptr, C.POINTER(W.HMODULE)], W.BOOL),
        'GetModuleFileNameW': ([W.HMODULE, W.LPWSTR, W.DWORD], W.DWORD),
        'LoadLibraryExW': ([W.LPCWSTR, W.HANDLE, W.DWORD], W.HMODULE),
        'GetProcAddress': ([W.HMODULE, C.c_char_p], ptr),
        'FreeLibrary': ([W.HMODULE], W.BOOL),
    }
    for name, (args, ret) in signatures.items():
        getattr(kernel, name).argtypes, getattr(kernel, name).restype = args, ret
    def check(value):
        if not value:
            raise OSError(C.get_last_error(), 'Native Windows API failed')
        return value
    process = check(kernel.OpenProcess(0x43a, False, pid))
    allocations, retain_allocations = [], False
    def modules():
        handle = win32api.OpenProcess(0x410, False, pid)
        try:
            return [(int(a), Path(win32process.GetModuleFileNameEx(handle, a)))
                    for a in win32process.EnumProcessModules(handle)]
        finally:
            handle.Close()
    def allocate(data):
        address = check(kernel.VirtualAllocEx(process, None, len(data), 0x3000, 0x04))
        allocations.append(address)
        written = size()
        buffer = C.create_string_buffer(data)
        check(kernel.WriteProcessMemory(process, address, buffer, len(data), C.byref(written)))
        require(written.value == len(data), 'native_write_incomplete', '原生请求写入不完整。')
        return address
    def run(address, argument, timeout):
        nonlocal retain_allocations
        thread = check(kernel.CreateRemoteThread(process, None, 0, address, argument, 0, None))
        try:
            if kernel.WaitForSingleObject(thread, timeout) != 0:
                retain_allocations = True
                raise BridgeError('native_outcome_unknown', '原生调用未结束，保留请求内存；禁止自动重试。')
            code = W.DWORD()
            check(kernel.GetExitCodeThread(thread, C.byref(code)))
            return code.value
        finally:
            kernel.CloseHandle(thread)
    try:
        loaded = modules()
        weixin = [a for a, p in loaded if p.name.lower() == 'weixin.dll']
        require(len(weixin) == 1, 'native_identity_changed', '微信模块不唯一。')
        actual = C.create_string_buffer(len(TEXT_PROLOGUE))
        read = size()
        check(kernel.ReadProcessMemory(process, weixin[0] + TEXT_RVA, actual, len(TEXT_PROLOGUE), C.byref(read)))
        require(read.value == len(TEXT_PROLOGUE) and actual.raw == TEXT_PROLOGUE,
                'native_fingerprint_mismatch', '运行时发送入口指纹不匹配。')
        current = [a for a, p in loaded if p.resolve() == dll]
        if not current:
            local_address = C.cast(kernel.LoadLibraryW, ptr).value
            owner = W.HMODULE()
            check(kernel.GetModuleHandleExW(6, local_address, C.byref(owner)))
            owner_path = C.create_unicode_buffer(32768)
            check(kernel.GetModuleFileNameW(owner, owner_path, len(owner_path)))
            owners = [a for a, p in loaded if p.name.lower() == Path(owner_path.value).name.lower()]
            require(len(owners) == 1, 'native_loader_unavailable', '加载器模块不唯一。')
            remote_loader = owners[0] + local_address - owner.value
            name = allocate((str(dll) + '\0').encode('utf-16-le'))
            run(remote_loader, name, 15000)
            current = [a for a, p in modules() if p.resolve() == dll]
        require(len(current) == 1, 'native_load_failed', '原生适配模块未加载。')
        local = check(kernel.LoadLibraryExW(str(dll), None, 1))  # no DLL initialization in controller
        try:
            entry = check(kernel.GetProcAddress(local, b'WabExecute')) - local + current[0]
        finally:
            kernel.FreeLibrary(local)
        remote = allocate(bytes(request))
        guard()
        code = run(entry, remote, 35000)
        result = NativeRequest()
        read = size()
        check(kernel.ReadProcessMemory(process, remote, C.byref(result), C.sizeof(result), C.byref(read)))
        require(read.value == C.sizeof(result), 'native_outcome_unknown', '原生响应不完整；禁止自动重试。')
        evidence = {'transport': 'native_4.1.15.51', 'ui_automation': False,
                    'thread_exit': code, 'callback_status': result.status,
                    'message': bytes(result.error).decode('utf-8', errors='replace'),
                    'delivered': False, 'automatic_retry': False}
        if code != 0 or result.status != 1:
            exc = BridgeError('native_rejected' if code == 0 and result.status == 2 else 'native_outcome_unknown',
                              '原生调用未确认受理，禁止自动重试。')
            exc.dispatch_evidence = evidence
            raise exc
        return evidence
    finally:
        if not retain_allocations:
            for address in allocations:
                kernel.VirtualFreeEx(process, address, 0, 0x8000)
        kernel.CloseHandle(process)
