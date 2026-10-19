# WeChat Agent Bridge

普通微信号的后台读取、群聊监听、文字和文件发送接口，提供 MCP stdio、认证 HTTP 和 CLI。读取直接使用本机数据库，发送调用客户端原生服务；不操作窗口，不需要焦点，不依赖 DSH，也不使用网页版。

**0.2.0rc3 提供实验性原生发送，严格绑定 Windows x64 微信 4.1.15.51 的完整 DLL 哈希。** 已通过隔离测试群的一条文字和两个实际文件（含中文无扩展名）的发送、后台监听回读；这是本机闭环验收，不代表对方收到或已读。默认禁用发送，其他版本拒绝运行。见 [原生适配说明](docs/BACKGROUND_ADAPTATION.md) 和 [验收记录](TEST_REPORT.md)。

## 安装与隔离演示

Python 3.10–3.13，在仓库根目录的 PowerShell 执行：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[live,dev]'
.\.venv\Scripts\wechat-bridge.exe init --directory .demo
.\.venv\Scripts\wechat-bridge.exe --config .demo/config.local.json serve
```

保持服务运行，另开终端调用：

```powershell
wechat-bridge --config .demo/config.local.json call wechat_status
wechat-bridge --config .demo/config.local.json call wechat_list_chats
wechat-bridge --config .demo/config.local.json call wechat_listen --arguments '{"chat_id":"demo_team@chatroom","from_now":false}'
wechat-bridge --config .demo/config.local.json call wechat_events --arguments '{"consumer":"my-agent"}'
```

若未激活虚拟环境，使用第一段中的完整命令路径。旧 PowerShell 的原生命令 JSON 引号可能不同；复杂参数可保存为 UTF-8 JSON 后使用 `--arguments-file arguments.json`。

隔离演示不访问微信。`preview` 只保存预览，`mock` 只允许合成 fixture 数据，`disabled` 拒绝发送请求。`examples/agent_consumer.py` 演示合成事件、队列和回执，不能证明微信发送能力。Windows 实时读取依赖 `.[live]`，没有 UI 自动化依赖。

## 接入本机微信

1. 保持微信已登录。运行 `wechat-bridge doctor`，只读查看主进程 PID、账号目录、版本和 DLL SHA-256。诊断包含私人路径，勿上传。
2. 创建全新的私有目录，不需要窗口 handle，也不需要恢复微信窗口：

```powershell
wechat-bridge init-live --directory .live --pid <PID> --account-id <账号ID>
wechat-bridge --config .live/config.local.json serve
```

`init-live` 验证进程打开的联系人、会话、消息库属于同一账号。新配置为 `sending_mode: "disabled"`，不会发送。旧读取配置中的窗口、快捷键、Qt 选项会被忽略；旧 `desktop` 发送模式拒绝启动，需改为 `disabled`。重启微信后必须重新核实并绑定 PID；不会自动选另一个登录账号。

3. 调用 `wechat_refresh_source`、`wechat_list_chats`，选择明确的会话 ID；调用 `wechat_listen`，再通过 `wechat_history` / `wechat_events` 读取。

范围仅限本机保留的数据库，不是全量云端历史。来源失败会报告错误，不能将错误当成没有消息。读取不依赖锁屏、焦点或窗口状态；当前版本未完成连续多日或所有锁屏组合的可靠性验收。

## 启用原生发送

需要 Visual Studio 2022 的 x64 C++ 编译工具。在源码目录运行 `./native/build.ps1`，生成 `.build-tmp/native/wab_native_v5.dll` 并输出 SHA-256。Python wheel 不包含已编译 DLL；源码包和 Git 仓库包含完整原生实现及构建脚本。已加载的 DLL 不能原位替换；开发时使用新的输出文件名并更新配置。

在私有配置中设置 `sending_mode: "native_experimental"`、`native_dll_path`（生成 DLL 的绝对路径）和 `native_dll_sha256`（小写 SHA-256）；同时固定 `send_allowlist` 中每个会话 ID 对应的 `send_target_names`，并设置 `file_roots`。重启桥接服务，检查 `wechat_status.capabilities`，再恢复队列。

本次部署仅允许固定 ID 的“测试群”，验收后保持暂停。初始化不会自动启用原生发送。适配器将自编译模块加载到已绑定的微信进程中；升级微信后哈希变化会拒绝执行，必须重新分析与验收。

## 接入智能体

先启动 HTTP 服务，在 MCP 宿主中配置 stdio 代理（替换绝对路径）：

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

`wechat-bridge tools` 输出 19 个工具的完整 Schema。所有入口共享参数校验、持久队列和权限配置。

| 操作 | 工具 |
| --- | --- |
| 状态、来源刷新、会话 | `wechat_status` / `wechat_refresh_source` / `wechat_list_chats` |
| 监听、历史 | `wechat_listen` / `wechat_unlisten` / `wechat_history` |
| 消费、续租、确认、失败重试 | `wechat_events` / `wechat_renew` / `wechat_ack` / `wechat_failed_events` / `wechat_retry_event` |
| 附件暂存、文字和文件发送 | `wechat_stage_file` / `wechat_send_text` / `wechat_send_file` |
| 回执、取消、只读核对 | `wechat_send_status` / `wechat_cancel_send` / `wechat_reconcile_send` |
| 暂停、恢复队列 | `wechat_pause` / `wechat_resume` |

先检查 `wechat_status.capabilities`：仅 `native_experimental` 配置报告 `real_send_text=true`、`real_send_file=true`；每次实际执行还检查账号、PID、微信 DLL 和适配 DLL 哈希。所有模式 `ui_automation=false`、`web_protocol=false`。`disabled` 模式拒绝发送、不入队。恢复队列不能修改发送模式，旧桌面任务不会执行。

消费顺序：监听 → 租用事件 → 忽略 `is_self` → 按可信本地任务规则处理 → 确认租约。长任务先续租。群消息是 `untrusted_content`，不能扩大允许文件目录或目标。多个消费者可以并行，但没有百级真实微信发送的验收结论。

HTTP 仅绑定 `127.0.0.1`，默认端口 18769。所有请求需要 `Authorization: Bearer <state/api-token>`。端点为 `GET /health`、`GET /v1/tools` 和 `POST /v1/call`。不接受跨站 Origin，不启用 CORS，客户端忽略系统 HTTP 代理。

## 回执与验证

已有 `unknown` 回执保留，不自动重发。`wechat_reconcile_send` 只读核对数据库；旧草稿不会被清空。`locally_observed` 只表示发现匹配的本机本人消息，不代表对方收到或已读，也不是唯一传输层回执。

完成一批开发后统一执行：

```powershell
python -m unittest discover -s tests -v
python -m build
```

`examples/live_acceptance.py` 提供真实发送验收步骤，只接受固定 ID 的“测试群”或“文件传输助手”。它检查真实后台能力，经 HTTP 发送一条文字和一个文件，再核对监听事件；原始证据只能存入私有目录。

## 数据与维护

`state` 保存事件正文、令牌、出站记录、附件和解密缓存，应按私人聊天数据保护。密钥只留在内存，源账号文件只读。不自动清理证据；停止服务后自行备份和清理。

尚不提供自动登录、登录绕过、Windows 服务安装、原始媒体下载、图片识别和语音转写。原生发送仅验收上述固定版本和小文本附件，未证明大文件、多日运行或百级真实发送并发。出站有意串行；大型消息分片仍需整片认证解密，轮询周期不保证延迟。

源码采用 MIT，指定派生读取模块保留 Apache-2.0，见 [第三方归属](THIRD_PARTY_NOTICES.md)。这是社区项目，非微信官方产品。维护信息见 [开发说明](docs/DEVELOPMENT.md)。
