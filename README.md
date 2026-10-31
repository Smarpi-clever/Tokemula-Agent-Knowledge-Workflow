# Tokemula Agent Knowledge Workflow

Tokemula Agent Knowledge Workflow（命令为 `akf`）让 Codex 在完成普通任务后，维护有来源的 Markdown 项目知识。它复用当前会话的 Agent、模型和权限，不需要额外的模型 API、Provider 或凭据。

CLI 是主要安装入口，可选 Codex 插件提供相同的设置向导。知识库可以是普通 Markdown 目录，也可以是 Obsidian Vault。

## 安装

当前已验证的平台为 macOS arm64。需要 Python 3.11+、Git 和 Codex Desktop；原生启用验证还需要 Codex CLI。详见[兼容性矩阵](docs/compatibility.md)。

1. 从 [GitHub Releases](https://github.com/Smarpi-clever/Tokemula-Agent-Knowledge-Workflow/releases) 下载 macOS 安装包，并按同一版本的 `SHA256SUMS` 核对摘要。
2. 解压后双击 `Install.command`，选择项目、知识库和同步方式，查看改动预览。
3. 分别选择自动知识更新、本地提交和远端推送权限。三项默认关闭；第一次使用建议选择本地保存。
4. 按向导指引在 Codex 原生界面审查和信任 Hooks。在项目中新建聊天完成一次普通任务，再运行 `akf doctor --verify`。

依赖齐全时，一次启动向导即可完成检测和配置，无需手工编辑 JSON。缺少依赖时，向导提供官方安装入口，保留已有设置供继续使用。

安装完成与自动维护启用分别显示：

| 状态 | 含义 |
|---|---|
| Installed | 工具和启动入口完整 |
| Configured | 项目和知识库绑定完成 |
| Trusted | 当前 Hooks 已通过 Codex 原生信任 |
| Enabled | 已授权，且真实任务触发验证通过 |
| Sync Enabled | 分别显示自动本地提交、自动远端推送 |

完整步骤、可选插件和故障恢复见[安装与使用](docs/install.md)。

## 日常使用

在已配置项目中正常工作即可。Agent 根据实际结果更新相关主题，并保留来源和历史。没有持久新事实时不创建笔记；来源不足或人工冲突会保留为 pending，等待审阅。

```sh
akf status
akf doctor
akf pause
akf enable
akf disable
```

`pause` 暂停自动维护，`enable` 复用已有有效授权恢复维护。插件详情的 Setup 开关控制设置技能；自动维护由原生 Hooks 设置和 `akf` 控制命令管理。

## 数据与权限

- 用户知识库与程序、配置和状态目录分开保存。
- 自动更新只写 `topics/*.md` 的工具托管块，保留人工正文、索引和已有历史。
- 新设置默认通过任务回执和明确选择的文件或 commit 证据工作，不生成整个项目的内容快照。
- 测试结论需要可信执行回执；计划、推断、源码状态和部署状态分别记录。
- 自动提交只处理本工具拥有的笔记；自动远端推送单独授权，并校验私有知识仓库绑定。
- 卸载保留笔记、配置、pending 和恢复材料。安装、升级和卸载按事务回滚。

数据流、旧配置差异和保存范围见[隐私说明](docs/privacy.md)。安全问题通过[私密报告渠道](SECURITY.md)反馈。

## 开发与贡献

运行时与测试使用 Python 标准库，无需安装 Python 包依赖：

```sh
python3 -m akf --help
make check
make test
```

测试使用合成项目与本地 Git fixtures。配置和 Hook 模板见 [examples](examples/README.md)，空白合成知识主题见 [synthetic-knowledge](synthetic-knowledge/topics/project-behavior.md)。普通安装请使用向导。

打包方法见[可复现构建](docs/reproducible-build.md)。贡献和问题反馈见 [CONTRIBUTING.md](CONTRIBUTING.md)，依赖记录见[依赖与许可证](docs/dependencies.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

项目代码采用 [MIT 许可证](LICENSE)。Codex、OpenAI、Python、Git 和 Obsidian 的名称及商标属于各自权利人。
