# 实现结构与开发边界

`config.py` 加载本地账号、来源、模式、目录和白名单，远程工具不能修改。`cli.py` 持有服务目录锁及实时来源的 PID 锁。`diagnostics.py` 只读枚举进程、模块及账号数据库句柄，不枚举或操作窗口。`desktop_identity.py` 为历史文件名，内容仅检查数据库句柄所属账号。

`sources.py` 支持 fixture、verified snapshot 和 live。实时来源复用只读进程访问、密钥认证、WAL/SHM 快照和分片读取，密钥仅在内存。必要数据库全部成功后才发布新映射；失败不返回假空数据。轮询不是跨数据库原子事务，也不是仅解密新增页。

`store.py` 用 SQLite WAL 保存事件、分片游标、租约、附件和出站请求。入站去重包含账号、会话、分片、local_id、sort_seq。消费者是至少一次处理，failed 需要显式 retry；正文标为 untrusted_content。

`service.py` 分离采集和串行出站线程，共享来源锁。当前仅有隔离 mock 出站；preview 不执行，disabled 拒绝入队。旧 desktop 配置不能启动，旧 queued 任务不能执行；sending 崩溃记录恢复为 unknown，不自动重发。真实后台发送不存在，状态必须明确报告 false。测试注入 sender 也只允许在 mock + fixture 下执行。

界面发送器和 Qt 写入代码已从发行源码删除。旧恢复草稿 API、inspect-ui 命令、窗口 handle 参数及 UI 自动化依赖均移除。不能通过“自动兜底”重新引入界面路线。旧草稿和 unknown 回执保留，核对接口只读。对应历史可从 Git 旧提交追溯。

`api.py` 是 HTTP、CLI、MCP 共用严格协议。`transport.py` 提供认证回环 HTTP 和 stdio MCP，忽略代理，stdout 只输出 JSON-RPC。用 tools 导出 Schema。

## 开发与发布

统一运行 `python -m unittest discover -s tests -v`，再从干净发布 checkout 构建 `python -m build`。CI 覆盖 Windows/Linux × Python 3.10/3.13。真实发送脚本不是 CI 测试，必须先验证服务确有后台文字及文件发送能力。

只导出明确源码清单。不得发布 .live、.venv、配置、令牌、账号路径、解密缓存和真实聊天回执。公开验收记录只含脱敏统计或合成数据。不能把 mock、历史局部发送、其他版本的上游演示写成当前微信实机通过。

## 已知限制

后台发送尚未实现，见 [适配调查](BACKGROUND_ADAPTATION.md)。没有自动保留期清理、Windows 服务安装、自启动、自动重登、原始媒体下载或多日可靠性保证。发生变化的整个消息分片需要认证解密，大库轮询可能超过配置间隔。每轮补录存在分页上限，插入到旧游标之前的历史记录不会自动重播。
