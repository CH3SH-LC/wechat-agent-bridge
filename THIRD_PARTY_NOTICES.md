# 来源与许可证

新增服务、队列、工具接口、桌面适配器与文档采用根目录 MIT 许可证。下列派生部分保留 Apache-2.0；不能用根目录 MIT 声明替换它们的许可。

读取模块复用 [CH3SH-LC/dsh-better-wechat](https://github.com/CH3SH-LC/dsh-better-wechat) 0.6.0 的 `extract_local.py`、`wal_snapshot.py`、`export_records.py`、`html_viewer.py`、`selection.py`，主要修改为包内相对导入。来源与打包 SHA-256 在 `src/wechat_bridge/vendor/provenance.json`；原 MIT、Apache-2.0 许可证及归属说明保留在该目录。WAL/SHM 合成测试也来自该项目，修改了导入路径。vendor 中的原归属文件保留其原工程路径表述，本包许可证实际位于 vendor/LICENSE-*.txt。

数据库加密格式及解码方法参考并改写自 Apache-2.0 的 [fanyuantaier/wechatauto-replica 数据库模块](https://github.com/fanyuantaier/wechatauto-replica/blob/62503d76a0db069171cb4e42e87090ad285db489/wechatauto/db.py)。该上游引用版本没有单独 NOTICE 文件。

`src/wechat_bridge/qt_accessibility.py` 的 PE 特征及 Qt 无障碍标志定位方法改写自同一项目的 [accessibility 模块](https://github.com/fanyuantaier/wechatauto-replica/blob/d322c4a9b1124228820bab363e5676016f188fc6/wechatauto/accessibility.py)，本文件按 Apache-2.0 分发。修改包括 DLL SHA-256 固定、唯一候选校验、绑定进程与安装目录、会话内暂时设置一个布尔字节、退出恢复并回读。没有复制完整第三方发送库；不修改磁盘 DLL，不注入可执行代码，不修改系统无障碍设置。进程强制终止可能阻止 finally 恢复。

依赖由用户另行安装：cryptography（Apache-2.0 OR BSD-3-Clause）、zstandard（BSD-3-Clause）、uiautomation（Apache-2.0）、pywin32（PSF）、psutil（BSD-3-Clause）。分发者应保留各依赖随包提供的许可证。SQLite 由 Python 标准库提供。

微信名称仅用于描述兼容目标；本项目不是微信官方接口或官方产品。发布包不含微信客户端、真实账号配置、聊天内容、令牌或密钥。
