# 来源与许可证

新增服务、队列、工具接口、文档采用根目录 MIT 许可证。下列派生部分保留 Apache-2.0；不能用根目录 MIT 声明替换它们的许可。

读取模块复用 [CH3SH-LC/dsh-better-wechat](https://github.com/CH3SH-LC/dsh-better-wechat) 0.6.0 的 `extract_local.py`、`wal_snapshot.py`、`export_records.py`、`html_viewer.py`、`selection.py`，主要修改为包内相对导入。来源与打包 SHA-256 在 `src/wechat_bridge/vendor/provenance.json`；原 MIT、Apache-2.0 许可证及归属说明保留在该目录。WAL/SHM 合成测试也来自该项目，修改了导入路径。vendor 中的原归属文件保留其原工程路径表述，本包许可证实际位于 vendor/LICENSE-*.txt。

数据库加密格式及解码方法参考并改写自 Apache-2.0 的 [fanyuantaier/wechatauto-replica 数据库模块](https://github.com/fanyuantaier/wechatauto-replica/blob/62503d76a0db069171cb4e42e87090ad285db489/wechatauto/db.py)。该上游引用版本没有单独 NOTICE 文件。

0.2.0rc2 已移除此前的 Qt 无障碍兼容和界面发送代码。历史版本的归属说明保留于 Git 历史。

依赖由用户另行安装：cryptography（Apache-2.0 OR BSD-3-Clause）、zstandard（BSD-3-Clause）、pywin32（PSF）、psutil（BSD-3-Clause）。分发者应保留各依赖随包提供的许可证。SQLite 由 Python 标准库提供。

微信名称仅用于描述兼容目标；本项目不是微信官方接口或官方产品。发布包不含微信客户端、真实账号配置、聊天内容、令牌或密钥。
