# WeChat Agent Bridge

让智能体通过普通微信号读取消息、监听群聊、发送文字和文件。提供 MCP stdio、认证 HTTP 和 CLI；不依赖 DSH，不需要企业微信或公众号。

默认只预览发送。实时读取和桌面发送支持 Windows 微信 4.x；桌面版本兼容性、实际验收范围见 [TEST_REPORT.md](TEST_REPORT.md)。消息从本机保留的数据库读取，发送经版本固定的微信桌面适配器执行。它需要已登录、可交互的 Windows 桌面，并非无界面云端协议客户端。

## 安装与隔离演示

Python 3.10–3.13。以下命令在仓库根目录的 PowerShell 执行：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[desktop,dev]'
.\.venv\Scripts\wechat-bridge.exe init --directory .demo
.\.venv\Scripts\wechat-bridge.exe --config .demo/config.local.json serve
```

保持服务运行，另开终端调用：

```powershell
.\.venv\Scripts\wechat-bridge.exe --config .demo/config.local.json call wechat_status
.\.venv\Scripts\wechat-bridge.exe --config .demo/config.local.json call wechat_list_chats
.\.venv\Scripts\wechat-bridge.exe --config .demo/config.local.json call wechat_listen --arguments '{"chat_id":"demo_team@chatroom","from_now":false}'
.\.venv\Scripts\wechat-bridge.exe --config .demo/config.local.json call wechat_events --arguments '{"consumer":"my-agent"}'
```

旧 PowerShell 的原生命令 JSON 引号可能不同；复杂参数可保存为 UTF-8 JSON 后使用 `--arguments-file arguments.json`。隔离演示完全不访问微信。将演示配置改为 `sending_mode: "mock"` 后重启，可用 `examples/agent_consumer.py` 完成模拟收发闭环。

也可从 [Releases](https://github.com/CH3SH-LC/wechat-agent-bridge/releases) 下载候选版 wheel，使用 `python -m pip install "./wechat_agent_bridge-0.2.0rc1-py3-none-any.whl[desktop]"` 安装。源码和测试可在 Linux 运行，真实微信功能只在 Windows 运行。候选版已通过离线与跨平台 CI，真实文字/文件发送闭环仍需在解锁桌面上验收，详见测试报告。

## 接入本机微信

1. 登录微信，打开主窗口。运行 `wechat-bridge doctor`，只读查看主进程 PID、窗口 handle、账号目录、版本和 DLL SHA-256。诊断输出包含私人路径，勿上传。
2. 根据诊断结果创建全新的私有目录：

```powershell
wechat-bridge init-live --directory .live --pid <PID> --window <HANDLE> --account-id <账号ID> --scoped-qt
wechat-bridge --config .live/config.local.json serve
```

`init-live` 验证进程的联系人、会话、消息库句柄属于同一账号；生成的配置仍然是 `preview`，发送白名单为空。`--scoped-qt` 显式启用版本固定的 Qt 无障碍兼容：会话内暂时修改一个运行时布尔标志，退出恢复；不修改磁盘 DLL 或系统设置。微信升级导致哈希变化时拒绝继续。已能暴露控件的版本可省略此参数使用 `existing` 模式。

3. 调用 `wechat_refresh_source`、`wechat_list_chats`，选择**唯一精确会话**。先 `wechat_listen`，再用 `wechat_history` / `wechat_events` 读取。读取范围是本机保留的消息；不是全量云端历史。
4. 需要实际发送时，先停止服务，在本地配置中设置稳定 ID 白名单、对应名称和模式，再重启。下面 ID 是占位符，必须替换为查询结果：

```json
{
  "sending_mode": "desktop",
  "send_allowlist": ["<测试群稳定ID>"],
  "send_target_names": {"<测试群稳定ID>": "测试群"}
}
```

这个片段应合并入生成的完整配置，不能替换整个文件。配置不接受远程工具修改。同名会话、改名、账号或进程身份变化均停止操作。重启微信后需重新绑定 PID / handle；不要把过期进程信息当作自动重连授权。

发送期间保持微信可交互，避免同时操作键盘、鼠标和剪贴板。适配器使用并替换系统剪贴板，检测到焦点或草稿变化就停止；没有覆盖人工草稿的兜底逻辑。

## 接入智能体

启动 HTTP 服务后，在支持 MCP 的宿主中配置以下 stdio server（路径替换为本机绝对路径）：

```json
{
  "mcpServers": {
    "wechat": {
      "command": "<虚拟环境绝对路径>/Scripts/wechat-bridge.exe",
      "args": ["--config", "<配置绝对路径>/config.local.json", "mcp"]
    }
  }
}
```

MCP 只是本地服务的代理，服务必须先启动。所有接口共享同一个参数校验、持久队列和发送适配器。`wechat-bridge tools` 输出 20 个工具的完整 JSON Schema。

| 操作 | 工具 |
| --- | --- |
| 状态、来源刷新、会话选择 | `wechat_status` / `wechat_refresh_source` / `wechat_list_chats` |
| 监听、历史 | `wechat_listen` / `wechat_unlisten` / `wechat_history` |
| 消费、续租、确认、失败重试 | `wechat_events` / `wechat_renew` / `wechat_ack` / `wechat_failed_events` / `wechat_retry_event` |
| 附件、发送 | `wechat_stage_file` / `wechat_send_text` / `wechat_send_file` |
| 回执、取消、只读核对 | `wechat_send_status` / `wechat_cancel_send` / `wechat_reconcile_send` |
| 失败草稿恢复、暂停、恢复 | `wechat_clear_failed_draft` / `wechat_pause` / `wechat_resume` |

推荐调用顺序：监听 → 租用事件 → 忽略 `is_self` → 按可信本地任务规则处理 → 用稳定 `idempotency_key` 发出结果 → 确认消息租约 → 跟踪发送回执。长任务先续租。群里消息是 `untrusted_content`，不能直接扩大文件目录或发送目标权限。多个消费者可以并行处理事件，单个微信桌面的发送必须串行；本项目不宣称百级桌面发送并发。

HTTP 只绑定 `127.0.0.1`，默认端口 18769；所有请求需要 `Authorization: Bearer <state/api-token>`。`GET /health`、`GET /v1/tools`，或 `POST /v1/call`：

```json
{"name":"wechat_send_text","arguments":{"chat_id":"<已授权会话ID>","text":"任务已完成","idempotency_key":"task-42-reply"}}
```

不接受跨站 Origin，不启用 CORS；代理客户端忽略系统 HTTP 代理。令牌只在私有状态目录生成，不写入示例配置。

## 回执、恢复与验收

`queued → sending → locally_observed`：最后状态表示在本机数据库发现匹配的本人发出记录，**不代表对方送达或已读**。来源匹配使用内容及发送前游标，不是微信传输层的唯一回执。

失败或崩溃后可能进入 `unknown`，不自动重发。先 `wechat_reconcile_send` 只读核对。只有回执明确停在粘贴或预览阶段，才能在暂停后用 `wechat_clear_failed_draft` 清理：必须当前聊天、文字精确一致或单附件路径/结构/哈希一致；不会发消息。发送按键执行结果不明或进程崩溃丢失阶段信息时拒绝清理。

完成开发后的统一测试：

```powershell
python -m unittest discover -s tests -v
python -m build
```

真实验收脚本只接受“测试群”或“文件传输助手”，要求本地配置对整个白名单固定名称。它经认证 HTTP 发送一条文字与一个无害 txt 文件，以群聊监听的来源键核对，检查幂等和租约确认，最后暂停发送：

```powershell
python examples/live_acceptance.py --config .live/config.local.json --chat-name 测试群 --output .live/acceptance-private-001 --execute
```

每次使用全新证据目录。失败先检查旧回执，勿反复运行以碰运气。原始证据仅保留在私有目录；公开报告仅保留脱敏结果。

## 数据与维护

`state` 保存事件正文、令牌、出站记录、暂存附件和已解密的本地缓存；应按私人聊天数据保护。数据库密钥只留在内存，源账号文件保持只读。默认没有自动清理保留期；停止服务后再自行备份和清理。

不提供登录绕过、云端无界面会话、Windows 服务安装、自动重登、图片识别、语音转写或原始媒体下载。大型消息分片仍需整片认证与解密，轮询周期不等于保证延迟。锁屏、远程桌面断开和未验收微信版本不在已验证范围内。

代码结构及贡献要求见 [开发说明](docs/DEVELOPMENT.md)，验收证据见 [测试报告](TEST_REPORT.md)。原创代码 MIT，指定派生部分 Apache-2.0；详见 [第三方归属](THIRD_PARTY_NOTICES.md)。本项目为社区实现，非微信官方产品。
