from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import threading
import time

from .common import BridgeError, file_hash, inside, now, require, uid
from .sources import make_source
from .store import Store


class Bridge:
    def __init__(self, cfg, source=None, sender=None):
        self.cfg = cfg.validate()
        self.store = Store(cfg.state_dir, cfg.account_id)
        self.store.recover()
        self.source = source or make_source(cfg)
        self.sender = sender
        if self.sender is None and cfg.sending_mode == 'native_experimental':
            from .native import NativeTextSender
            self.sender = NativeTextSender(cfg)
        self.stop = threading.Event()
        self.send_lock = threading.Lock()
        self.threads = []
        self.last_error, self.last_poll, self.last_send = None, None, 0
        self.spool = cfg.state_dir / 'attachments'
        self.spool.mkdir(parents=True, exist_ok=True)

    def start(self):
        for name, target in [('wechat-inbox', self._poll_loop), ('wechat-outbox', self._send_loop)]:
            thread = threading.Thread(name=name, target=target, daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join()  # stop does not close SQLite beneath an active dispatch
        self.store.close()

    def _poll_loop(self):
        while not self.stop.is_set():
            try:
                self.poll()
            except Exception:
                pass  # poll exposes error status; cursors are not advanced
            self.stop.wait(self.cfg.poll_seconds)

    def _send_loop(self):
        while not self.stop.is_set():
            try:
                self.dispatch_once()
            except Exception as exc:
                self.last_error = {'code': getattr(exc, 'code', 'send_worker_error'), 'message': str(exc)[:600]}
            self.stop.wait(0.2)

    def poll(self):
        with self.source.lock:
            try:
                self.source.refresh()
                chats = self.source.chats()
                self.store.replace_chats(chats)
                added, subscription_errors = 0, []
                for sub in self.store.subscriptions():
                    if sub['active']:
                        try:
                            rows, cursors = self.source.read(sub['chat_id'], sub['cursors'])
                            added += self.store.ingest(sub['chat_id'], rows, cursors)
                        except Exception as exc:
                            subscription_errors.append((sub['chat_id'], exc))
                if subscription_errors:
                    chat_id, exc = subscription_errors[0]
                    raise BridgeError(getattr(exc, 'code', 'subscription_error'),
                                      f'{chat_id}: {str(exc)[:400]} (failed_subscriptions={len(subscription_errors)})')
                self.last_error, self.last_poll = None, now()
                return {'added': added, 'source': self.source.health()}
            except Exception as exc:
                self.last_error = {'code': getattr(exc, 'code', 'source_unavailable'), 'message': str(exc)[:600]}
                raise BridgeError(self.last_error['code'], self.last_error['message']) from None

    def status(self):
        return {'config': self.cfg.public(), 'paused': self.store.paused(), 'last_poll': self.last_poll,
                'last_error': self.last_error, 'source': self.source.health(),
                'capabilities': {'background_read': self.cfg.source_mode == 'live',
                                 'real_send_text': self.cfg.sending_mode == 'native_experimental',
                                 'real_send_file': self.cfg.sending_mode == 'native_experimental',
                                 'ui_automation': False, 'web_protocol': False,
                                 'real_send_transport': 'native_experimental' if self.cfg.sending_mode == 'native_experimental' else None,
                                 'send_blocker': None if self.cfg.sending_mode == 'native_experimental' else 'background_sender_not_implemented'},
                'subscriptions': self.store.subscriptions(), **self.store.counts()}

    def chat(self, ident):
        found = [c for c in self.store.chats() if c['id'] == ident]
        require(len(found) == 1, 'unknown_chat', '会话 ID 不在已读取来源中，请先刷新并列出会话。')
        return found[0]

    def listen(self, chat_id, from_now=True):
        with self.source.lock:
            self.source.refresh()
            self.store.replace_chats(self.source.chats())
            self.chat(chat_id)
            cursors = self.source.tail(chat_id) if from_now else {}
            self.store.subscribe(chat_id, cursors)
        self.poll()
        return {'subscriptions': self.store.subscriptions(), 'resubscribe': 'resumes_existing_checkpoint'}

    def stage(self, path):
        source = Path(path).resolve(strict=True)
        require(source.is_file() and any(inside(source, root) for root in self.cfg.file_roots),
                'file_forbidden', '文件必须位于本地配置的允许目录中。')
        require(not inside(source, self.cfg.state_dir) and not inside(source, self.cfg.source_dir),
                'file_forbidden', '不允许发送服务状态或微信数据库文件。')
        require(source.suffix.lower() not in {'.lnk', '.url'}, 'file_forbidden', '不接受快捷方式附件。')
        before = source.stat()
        require(0 < before.st_size <= self.cfg.max_file_bytes, 'file_too_large', '文件为空或超过大小限制。')
        ident = uid('file_')
        directory = self.spool / ident
        directory.mkdir()
        target = directory / source.name
        try:
            # Bounded copy; an actively growing input cannot exhaust the spool.
            with source.open('rb') as src, target.open('xb') as dst:
                total = 0
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    require(total <= self.cfg.max_file_bytes, 'file_too_large', '复制期间文件超过大小限制。')
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            after = source.stat()
            require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
                    and file_hash(source) == file_hash(target), 'file_changed', '文件在暂存期间发生变化。')
            record = dict(id=ident, path=str(target), name=source.name, sha256=file_hash(target), size=total, created=now())
            self.store.add_attachment(record)
            return {k: v for k, v in record.items() if k != 'path'}
        except Exception:
            target.unlink(missing_ok=True)
            directory.rmdir()
            raise

    def attachment(self, ident):
        item = self.store.attachment(ident)
        path = Path(item['path']).resolve()
        require(inside(path, self.spool) and path.is_file() and path.stat().st_size == item['size']
                and file_hash(path) == item['sha256'], 'file_changed', '暂存附件已变化，不予发送。')
        return item

    def send(self, chat_id, key, text=None, attachment_id=None):
        require(self.cfg.sending_mode in {'preview', 'mock', 'native_experimental'}, 'background_sender_unavailable',
                '后台直发尚未实现；界面发送已移除。本请求未入队、未发送。')
        if self.cfg.sending_mode == 'native_experimental':
            from .native import make_request
            content = self.attachment(attachment_id)['path'] if attachment_id else text
            make_request(chat_id, content, file=bool(attachment_id))
        require(self.cfg.sending_mode != 'mock' or self.cfg.source_mode == 'fixture',
                'invalid_config', '模拟发送不能访问真实微信。')
        chat = self.chat(chat_id)
        self._target_guard(chat)
        payload = dict(chat_id=chat_id, title=chat['name'], type='file' if attachment_id else 'text')
        if attachment_id:
            item = self.attachment(attachment_id)
            payload.update(attachment_id=attachment_id, file={k: item[k] for k in ('name', 'size', 'sha256')})
        else:
            payload['text'] = text
        if self.cfg.sending_mode != 'preview':
            require(chat_id in self.cfg.send_allowlist, 'chat_forbidden', '会话不在本地发送白名单中。')
        payload['mode'] = self.cfg.sending_mode
        return self.store.enqueue(key, payload, 'preview' if self.cfg.sending_mode == 'preview' else 'queued')

    def _target_guard(self, chat):
        if self.cfg.sending_mode != 'preview':
            require(chat['id'] in self.cfg.send_allowlist, 'chat_forbidden', '会话不在本地发送白名单中。')
            expected = self.cfg.send_target_names.get(chat['id'])
            require(expected is None or expected == chat['name'], 'target_changed', '会话名称与本地固定目标不一致。')

    def _guard(self, job):
        p = job['payload']
        require((self.cfg.sending_mode == 'mock' and self.cfg.source_mode == 'fixture')
                or (self.cfg.sending_mode == 'native_experimental' and self.cfg.source_mode == 'live'),
                'background_sender_unavailable', '当前版本没有真实后台发送器；拒绝执行旧队列。')
        require(not self.stop.is_set() and not self.store.paused(), 'paused', '发送已暂停。')
        require(p['mode'] == self.cfg.sending_mode and p['mode'] != 'preview', 'mode_changed', '发送模式已变化，不自动升级预览记录。')
        require(p['chat_id'] in self.cfg.send_allowlist, 'chat_forbidden', '会话已不在发送白名单中。')
        chat = self.chat(p['chat_id'])
        self._target_guard(chat)
        require(chat['name'] == p['title'], 'target_changed', '会话标题已变化，需重新确认。')
        require(len([c for c in self.store.chats() if c['name'] == chat['name']]) == 1,
                'ambiguous_target', '存在同名会话，需要明确目标。')
        return self.attachment(p['attachment_id']) if p['type'] == 'file' else None

    def _observe(self, job):
        p, baseline = job['payload'], job['baseline']
        require(baseline is not None, 'invalid_state', '发送没有来源基线，无法核对。')
        with self.source.lock:
            self.source.refresh()
            cursor = baseline
            matches = []
            # Iterate all pages; never silently inspect only the latest few rows.
            for _ in range(100):
                rows, updated = self.source.read(p['chat_id'], cursor)
                for row in rows:
                    if row['sender_id'] != self.cfg.account_id:
                        continue
                    matched = (p['type'] == 'text' and row['type'] == 'text' and row['text'] == p['text'])
                    if p['type'] == 'file':
                        matched = row.get('type') == 'file' and row.get('file') == {k: p['file'][k] for k in ('name', 'size')}
                    if matched:
                        matches.append(row['source_key'])
                if updated == cursor:
                    break
                cursor = updated
            else:
                raise BridgeError('verification_backlog', '核对积压超过单次上限，保持 unknown 等待后续核对。')
            if len(matches) == 1:
                return {'source_key': matches[0], 'evidence': 'local_outgoing_record',
                        'delivered': False, 'read': False,
                        'attribution': 'content_match_not_unique_transport_id'}
            return None

    def dispatch_once(self):
        with self.send_lock:
            if self.store.paused() or now() - self.last_send < self.cfg.send_interval_seconds:
                return None
            job = self.store.next_job()
            if job is None:
                return None
            try:
                with self.source.lock:
                    self.source.refresh()
                    self.store.replace_chats(self.source.chats())
                    attachment = self._guard(job)
                    baseline = self.source.tail(job['payload']['chat_id'])
                job = self.store.transition(job['id'], ['queued'], 'sending', baseline=baseline)
            except Exception as exc:
                if self.store.job(job['id'])['state'] == 'queued':
                    return self.store.transition(job['id'], ['queued'], 'failed', {'reason': getattr(exc, 'code', 'preflight_failed'), 'message': str(exc)[:500]})
                return self.store.job(job['id'])
            self.last_send = now()
            dispatch = None
            try:
                if self.sender:
                    dispatch = self.sender.send(job['payload'], attachment, job['payload']['title'], lambda: self._guard(job))
                elif self.cfg.sending_mode == 'mock':
                    self._guard(job)
                    self.source.append_outgoing(job['payload'], attachment)
                else:
                    raise BridgeError('background_sender_unavailable', '没有真实后台发送器。')
                # Older injected adapters need not return structured evidence.
                if not isinstance(dispatch, dict):
                    dispatch = None
                deadline = now() + self.cfg.verify_seconds
                while True:
                    observed = self._observe(job)
                    if observed:
                        if dispatch:
                            observed['adapter'] = dispatch
                        return self.store.transition(job['id'], ['sending'], 'locally_observed', observed)
                    if now() >= deadline or self.stop.wait(0.5):
                        break
                return self.store.transition(job['id'], ['sending'], 'unknown', {'reason': 'local_record_not_confirmed', 'automatic_retry': False, 'adapter': dispatch})
            except Exception as exc:
                adapter = getattr(exc, 'dispatch_evidence', dispatch)
                phase = adapter.get('phase') if isinstance(adapter, dict) else None
                state = 'failed' if phase in {'before_navigation', 'before_paste'} else 'unknown'
                return self.store.transition(job['id'], ['sending'], state,
                                             {'reason': getattr(exc, 'code', 'dispatch_exception'), 'message': str(exc)[:500],
                                              'automatic_retry': False, 'adapter': adapter})

    def reconcile(self, ident):
        with self.send_lock:
            job = self.store.job(ident)
            require(job['state'] == 'unknown', 'invalid_state', '仅核对 unknown 状态；不会重新发送。')
            evidence = self._observe(job)
            return self.store.transition(ident, ['unknown'], 'locally_observed', evidence) if evidence else job

