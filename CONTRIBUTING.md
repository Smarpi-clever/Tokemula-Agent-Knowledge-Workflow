# 参与贡献

普通问题和功能建议使用 [Issues](https://github.com/Smarpi-clever/Tokemula-Agent-Knowledge-Workflow/issues)。安全问题请走 [SECURITY.md](SECURITY.md) 中的私密渠道。

报告问题时提供本工具版本、操作系统与架构、Codex 客户端版本、相关命令、预期结果和实际结果。用合成项目复现，并删除个人路径、笔记、凭据和私人日志。

## 本地检查

需要 Python 3.11+ 和 Git。Python 代码仅使用标准库：

```sh
make check
make test
```

按修改范围运行相关测试即可。测试数据应来自可丢弃的合成目录和本地 Git 远端；不连接真实项目知识库、生产服务或模型 API。

打包步骤见[可复现构建](docs/reproducible-build.md)。公开包使用显式文件集合，不包含本机配置、`.local/`、状态库、日志或恢复材料。

## 修改原则

优先复用现有实现，保持改动可审阅。保留人工内容、原生 Hook 信任、证据验证、写入前比较、单写入者和独立 Git 授权。

改变安装、迁移、卸载或恢复路径时，提供数据保留和失败恢复的针对性验证。新增平台支持应附真实安装与任务触发证据，并更新[兼容性矩阵](docs/compatibility.md)。

提交内容应由你拥有或具有兼容的再分发权。保留必要的第三方声明，并按项目 [LICENSE](LICENSE) 提交贡献。
