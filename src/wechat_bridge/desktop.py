"""Opt-in Windows UIA sender with a scoped, version-pinned Qt compatibility gate.

This adapter deliberately refuses inaccessible/ambiguous controls. Its behavior
must be accepted on an isolated chat for each supported Weixin build.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
import os
from pathlib import Path
import struct
import time
import xml.etree.ElementTree as ET

from .common import BridgeError, require
from .qt_accessibility import RuntimeGate


class DesktopSender:
    def __init__(self, cfg):
        self.cfg = cfg
        self.evidence = {}

    @contextmanager
    def session(self):
        require(os.name == 'nt', 'desktop_unavailable', '桌面发送仅支持 Windows。')
        try:
            import uiautomation as auto
            import win32clipboard as clip
            import win32gui as gui
            import win32process as process
        except ImportError:
            raise BridgeError('desktop_unavailable', '请先安装本项目 desktop 可选依赖。') from None
        self.desktop_ready(gui, process)
        if self.cfg.desktop_identity == 'database_handles':
            from .desktop_identity import verify_account
            self.evidence['identity'] = verify_account(self.cfg)
        with auto.UIAutomationInitializerInThread(), RuntimeGate(self.cfg) as gate:
            self.evidence['accessibility'] = gate.evidence
            yield auto, clip, gui, process

    def desktop_ready(self, gui, process):
        """Fail before the Qt gate when Windows does not expose an input desktop."""
        import psutil
        foreground = gui.GetForegroundWindow()
        require(bool(foreground), 'desktop_locked', '没有可交互的前台桌面，请解锁本机会话。')
        foreground_pid = process.GetWindowThreadProcessId(foreground)[1]
        try:
            name = psutil.Process(foreground_pid).name().lower()
        except psutil.Error:
            raise BridgeError('desktop_unavailable', '无法确认前台桌面身份。') from None
        require(name not in {'lockapp.exe', 'logonui.exe'}, 'desktop_locked', 'Windows 已锁屏，请解锁后恢复微信主窗口。')
        require(gui.IsWindow(self.cfg.desktop_window), 'identity_changed', '绑定的微信窗口已不存在。')
        require(not gui.IsIconic(self.cfg.desktop_window), 'desktop_unavailable', '请恢复微信主窗口后重试。')

    @staticmethod
    def nodes(root, maximum=4000):
        stack, result = [(root, 0)], []
        while stack:
            node, depth = stack.pop()
            result.append(node)
            require(len(result) < maximum, 'ui_unsupported', '控件树超过安全扫描上限。')
            # Chat editors can be 18+ levels deep. Web/article and message
            # history subtrees are unrelated and must not hide the editor.
            if (depth < 40 and node.ControlTypeName != 'DocumentControl'
                    and node.ClassName != 'Chrome_WidgetWin_0'
                    and node.AutomationId != 'chat_message_list'):
                stack.extend((c, depth + 1) for c in reversed(node.GetChildren()))
        return result

    @staticmethod
    def value(control):
        for method in ('GetValuePattern', 'GetLegacyIAccessiblePattern'):
            try:
                pattern = getattr(control, method)()
                if pattern is not None and isinstance(pattern.Value, str):
                    return pattern.Value
            except Exception:
                pass
        raise BridgeError('ui_unsupported', '输入框不暴露可回读内容；停止发送。')

    def checked_window(self, api, foreground=False):
        auto, _, gui, process = api
        require(gui.IsWindow(self.cfg.desktop_window), 'identity_changed', '绑定的微信窗口已不存在。')
        require(process.GetWindowThreadProcessId(self.cfg.desktop_window)[1] == self.cfg.desktop_pid,
                'identity_changed', '微信窗口所属进程已变化。')
        from .vendor.extract_local import ReadOnlyProcess
        with closing(ReadOnlyProcess(self.cfg.desktop_pid, self.cfg.weixin_exe)):
            pass
        if foreground:
            require(gui.GetForegroundWindow() == self.cfg.desktop_window, 'focus_changed', '用户或其他窗口已取得前台焦点。')
        win = auto.ControlFromHandle(self.cfg.desktop_window)
        nodes = self.nodes(win)
        if self.cfg.desktop_identity == 'database_handles':
            from .desktop_identity import verify_account
            self.evidence['identity'] = verify_account(self.cfg)
        else:
            anchors = [n for n in nodes if n.AutomationId == self.cfg.desktop_account_anchor]
            require(len(anchors) == 1, 'identity_unavailable', '找不到唯一账号锚点。')
            anchor = anchors[0]
            identity = anchor.Name
            if identity != self.cfg.account_id:
                identity = self.value(anchor)
            require(identity == self.cfg.account_id, 'identity_changed', '账号锚点与配置的账号 ID 不匹配。')
        return win, nodes

    @staticmethod
    def input_for(nodes, title):
        matches = [n for n in nodes if n.AutomationId.split('.')[-1] == 'chat_input_field' and n.Name == title]
        require(len(matches) == 1, 'target_unavailable', '无法通过精确标题确认唯一聊天输入框。')
        return matches[0]

    def focused_input(self, api, title):
        _, nodes = self.checked_window(api, foreground=True)
        editor = self.input_for(nodes, title)
        focused = api[0].GetFocusedControl()
        require(focused is not None and tuple(focused.GetRuntimeId()) == tuple(editor.GetRuntimeId()),
                'focus_changed', '焦点已离开目标聊天输入框。')
        return editor

    def navigate(self, api, title):
        win, nodes = self.checked_window(api)
        require(not api[2].IsIconic(self.cfg.desktop_window), 'desktop_unavailable', '请手动恢复微信窗口后重试。')
        self.activate(api)
        self.checked_window(api, foreground=True)
        current = [n for n in nodes if n.AutomationId.split('.')[-1] == 'chat_input_field']
        # Never overwrite an existing draft, including a draft in another chat.
        for editor in current:
            require(not self.value(editor), 'draft_present', '聊天输入框已有草稿，先由用户处理。')
        if len(current) == 1 and current[0].Name == title:
            return current[0]
        rows = [n for n in nodes if n.ControlTypeName == 'ListItemControl'
                and n.AutomationId == 'session_item_' + title]
        require(len(rows) <= 1, 'ambiguous_target', '当前会话列表存在重复目标。')
        if rows:
            rows[0].Click()
            time.sleep(0.3)
            _, nodes = self.checked_window(api, foreground=True)
            editor = self.input_for(nodes, title)
            require(not self.value(editor), 'draft_present', '目标会话已有草稿，未修改。')
            return editor
        search = [n for n in nodes if n.ControlTypeName == 'EditControl' and n.ClassName == 'mmui::XValidatorTextEdit' and n.Name == '搜索']
        if not search:
            buttons = [n for n in nodes if n.ControlTypeName == 'ButtonControl' and n.Name == '搜索']
            require(len(buttons) == 1, 'ui_unsupported', '微信没有可用的搜索控件。')
            buttons[0].Click()
            _, nodes = self.checked_window(api, foreground=True)
            search = [n for n in nodes if n.ControlTypeName == 'EditControl' and n.ClassName == 'mmui::XValidatorTextEdit' and n.Name == '搜索']
        require(len(search) == 1, 'ui_unsupported', '微信搜索输入框不唯一。')
        pattern = search[0].GetValuePattern()
        require(pattern is not None, 'ui_unsupported', '搜索控件不支持精确赋值。')
        pattern.SetValue(title)
        time.sleep(0.8)
        _, nodes = self.checked_window(api, foreground=True)
        rows = [n for n in nodes if n.ControlTypeName == 'ListItemControl' and n.Name == title]
        require(len(rows) == 1, 'ambiguous_target', '搜索结果没有唯一精确名称，不自动选择近似结果。')
        rows[0].Click()
        time.sleep(0.5)
        _, nodes = self.checked_window(api, foreground=True)
        editor = self.input_for(nodes, title)
        require(not self.value(editor), 'draft_present', '目标会话已有草稿，未修改。')
        return editor

    def activate(self, api):
        """A background queue thread needs a temporary shared input queue."""
        import win32api
        _, _, gui, process = api
        window = self.cfg.desktop_window
        if gui.GetForegroundWindow() == window:
            return
        # Try the normal activation path first. AttachThreadInput requires
        # this worker to own a Win32 message queue (UIA initialization alone
        # does not guarantee that on every start).
        gui.PeekMessage(None, 0, 0, 0)
        try:
            gui.SetForegroundWindow(window)
        except Exception:
            pass
        if gui.GetForegroundWindow() == window:
            return
        current = win32api.GetCurrentThreadId()
        foreground = gui.GetForegroundWindow()
        other = process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
        attached = False
        try:
            if other and other != current:
                process.AttachThreadInput(current, other, True)
                attached = True
            gui.BringWindowToTop(window)
            gui.SetForegroundWindow(window)
        finally:
            if attached:
                process.AttachThreadInput(current, other, False)
        require(gui.GetForegroundWindow() == window, 'focus_changed', '无法激活绑定的微信窗口。')

    @staticmethod
    def set_clipboard(clip, text=None, path=None):
        clip.OpenClipboard()
        try:
            # The operator must release the desktop while sending; the clipboard
            # is intentionally not restored over potentially newer user content.
            clip.EmptyClipboard()
            if path:
                value = struct.pack('<IiiII', 20, 0, 0, 0, 1) + (str(path) + '\0\0').encode('utf-16-le')
                clip.SetClipboardData(15, value)  # CF_HDROP, Unicode DROPFILES
            else:
                clip.SetClipboardText(text, 13)  # CF_UNICODETEXT
        finally:
            clip.CloseClipboard()

    def send(self, payload, attachment, title, precommit):
        self.evidence['phase'] = 'before_navigation'
        try:
            return self._send(payload, attachment, title, precommit)
        except Exception as exc:
            exc.dispatch_evidence = dict(self.evidence)
            raise

    @staticmethod
    def validate_file_export(attachment, paths, rich):
        expected = Path(attachment['path']).resolve()
        require(isinstance(paths, (list, tuple)) and len(paths) == 1 and Path(paths[0]).resolve() == expected,
                'file_preview_mismatch', '微信草稿导出的文件路径与暂存附件不一致。')
        require(isinstance(rich, bytes) and len(rich) <= 65536, 'file_preview_unavailable', '微信草稿未暴露文件结构。')
        try:
            root = ET.fromstring(rich.rstrip(b'\0'))
        except ET.ParseError:
            raise BridgeError('file_preview_unavailable', '微信草稿文件结构无法解析。') from None
        elements = list(root)
        require(root.tag == 'WeChatRichEditFormat' and len(elements) == 1
                and elements[0].tag == 'EditElement' and elements[0].get('type') == '3'
                and Path(elements[0].get('filepath', '')).resolve() == expected
                and not list(elements[0]) and not ''.join(root.itertext()).strip(),
                'file_preview_mismatch', '草稿不是单个预期文件，未发送。')

    def verify_file_preview(self, api, title, attachment):
        editor = self.focused_input(api, title)
        require(self.value(editor) == '\ufffc', 'file_preview_mismatch', '文件草稿包含额外内容或多个附件。')
        _, clip, _, process = api
        rich_format = clip.RegisterClipboardFormat('WeChat_RichEdit_Format')
        # Clear our outgoing clipboard so a failed Copy cannot be confused
        # with the original CF_HDROP used for Paste.
        clip.OpenClipboard()
        try:
            clip.EmptyClipboard()
            sequence = clip.GetClipboardSequenceNumber()
        finally:
            clip.CloseClipboard()
        editor = self.focused_input(api, title)
        editor.SendKeys('{Ctrl}a{Ctrl}c', waitTime=0.05)
        deadline = time.monotonic() + 2
        while clip.GetClipboardSequenceNumber() == sequence and time.monotonic() < deadline:
            time.sleep(.05)
        require(clip.GetClipboardSequenceNumber() != sequence, 'file_preview_unavailable', '微信未导出文件草稿，停止发送。')
        clip.OpenClipboard()
        try:
            owner = clip.GetClipboardOwner()
            require(owner and process.GetWindowThreadProcessId(owner)[1] == self.cfg.desktop_pid,
                    'focus_changed', '文件草稿剪贴板来源不是绑定的微信进程。')
            require(clip.IsClipboardFormatAvailable(15) and clip.IsClipboardFormatAvailable(rich_format),
                    'file_preview_unavailable', '微信未同时导出附件路径及富文本结构。')
            self.validate_file_export(attachment, clip.GetClipboardData(15), clip.GetClipboardData(rich_format))
        finally:
            clip.CloseClipboard()
        editor = self.focused_input(api, title)
        require(self.value(editor) == '\ufffc', 'draft_mismatch', '草稿在核对期间发生变化。')
        editor.SendKeys('{End}', waitTime=0.05)
        self.evidence['file_preview'] = {'method': 'wechat_clipboard_roundtrip', 'exact_staged_path': True,
                                         'single_file': True, 'name': attachment['name']}

    def _send(self, payload, attachment, title, precommit):
        with self.session() as api:
            editor = self.navigate(api, title)
            self.evidence['phase'] = 'before_paste'
            editor.SetFocus()
            self.focused_input(api, title)
            require(not self.value(editor), 'draft_present', '输入框已有内容，停止发送。')
            precommit()
            self.set_clipboard(api[1], text=payload.get('text'), path=attachment['path'] if attachment else None)
            editor = self.focused_input(api, title)
            self.evidence['phase'] = 'pasting'
            editor.SendKeys('{Ctrl}v', waitTime=0.05)
            time.sleep(1.0 if attachment else 0.3)
            editor = self.focused_input(api, title)
            actual = self.value(editor).replace('\r\n', '\n')
            self.evidence['phase'] = 'preview_check'
            if attachment:
                self.verify_file_preview(api, title, attachment)
            else:
                require(actual == payload['text'].replace('\r\n', '\n'), 'draft_mismatch', '输入框内容与请求不一致，保留现场停止。')
            precommit()  # pause, allowlist and immutable file rechecked before Enter
            editor = self.focused_input(api, title)
            self.evidence['phase'] = 'issuing_send'
            editor.SendKeys(self.cfg.send_hotkey, waitTime=0.05)
            self.evidence['phase'] = 'send_issued'
        return {'dispatch': 'keypress_issued', 'delivered': False, **self.evidence}

    def clear_failed_draft(self, payload, attachment, guard):
        """No navigation and no send key: only touch the exact current draft."""
        title = payload['title']
        with self.session() as api:
            self.checked_window(api)
            self.activate(api)
            _, nodes = self.checked_window(api, foreground=True)
            editor = self.input_for(nodes, title)
            editor.SetFocus()
            if attachment:
                self.verify_file_preview(api, title, attachment)
            else:
                require(self.value(self.focused_input(api, title)).replace('\r\n', '\n') ==
                        payload['text'].replace('\r\n', '\n'), 'draft_mismatch', '当前草稿与失败请求不同，未修改。')
            guard()
            editor = self.focused_input(api, title)
            # Recheck immediately before destructive editing, even after Copy.
            expected = '\ufffc' if attachment else payload['text'].replace('\r\n', '\n')
            require(self.value(editor).replace('\r\n', '\n') == expected, 'draft_mismatch', '草稿在核对期间变化。')
            editor.SendKeys('{Ctrl}a{Delete}', waitTime=.1)
            require(self.value(self.focused_input(api, title)) == '', 'draft_not_cleared', '草稿清理未完成。')
        return {'draft_cleared': True, 'send_key_issued': False, **self.evidence}

    def inspect(self):
        """Read-only UI inventory, explicitly requested from the local CLI only."""
        with self.session() as api:
            win = api[0].ControlFromHandle(self.cfg.desktop_window)
            return [{'type': n.ControlTypeName, 'automation_id': n.AutomationId, 'name': n.Name}
                    for n in self.nodes(win)]
