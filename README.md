# Tokemula Agent Knowledge Workflow

中文 | [English](README.en.md)

Tokemula Agent Knowledge Workflow（命令为 `akf`）让 Codex 在完成普通任务后，维护有来源的 Markdown 项目知识。它复用当前会话的 Agent、模型和权限，不需要额外的模型 API、Provider 或凭据。

CLI 是主要安装入口，可选 Codex 插件提供相同的设置向导。知识库可以是普通 Markdown 目录，也可以是 Obsidian Vault。

## 安装

当前已验证的平台为 macOS arm64。0.3.0 已在 Codex CLI 0.166.0 和 ChatGPT Desktop 26.1029.61003 的直接 Hooks 接入中完成同轮维护与新会话知识召回。ChatGPT Desktop 26.1103.32239 中已登记项目的本地插件适配入口也已完成这两个流程。

需要 Python 3.11+、Git 和 Codex Desktop；原生启用验证还需要 Codex CLI。客户端、接入方式和升级验证见[兼容性矩阵](docs/compatibility.md)。

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

新项目的知识库默认保存到 `~/Agent-Knowledge/<项目名>-<短ID>/`。向导回车接受，也可自定义位置或选择已有 Obsidian Vault；再次设置沿用已有绑定。Agent 通过 Hook 自动获得知识入口，用户与 Agent 共用同一份 Markdown。

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

## 按任务查知识与审阅

先查相关片段，再按返回的路径和行号读取详情：

```sh
akf search --query '中文请求上限 src/limits.py' --limit 5 --budget-bytes 6000
akf read --path topics/limits.md --start-line 1 --end-line 80 --budget-bytes 6000
akf context --task '排查 request limit 错误'
akf search --query '请求上限' --history
akf index --rebuild
```

检索覆盖标题、正文、源码路径和错误信息，支持中文、英文及混合查询。结果区分人工笔记、工具核验事实和已替代历史；文件变化、人工修改托管块或明确到期规则会提示复核。`--budget-bytes` 以 UTF-8 字节限制结构化结果，便于 Agent 控制上下文；空结果返回 `results: []`。本地 SQLite 索引会跟随笔记修改和删除更新，可随时重建。

```sh
akf pending
akf explain --job JOB_ID
akf review --job JOB_ID --item topics/limits.md:request-limit --action show
akf review --job JOB_ID --item topics/limits.md:request-limit --action revalidate < update.json
akf review --job JOB_ID --item topics/limits.md:request-limit --action reject
```

审阅逐项显示证据缺口、冲突和拟议变化。重新验证复用原有证据与写入检查；同任务中已保存的事实保持原状。`--json` 放在子命令前，供脚本读取。完整审阅、事实时效和升级回退步骤见[安装与使用](docs/install.md)。

## 数据与权限

- 用户知识库与程序、配置和状态目录分开保存。
- 自动更新只写 `topics/*.md` 的工具托管块，保留人工正文、索引和已有历史。
- 新设置默认通过任务回执和明确选择的文件或 commit 证据工作，不生成整个项目的内容快照。
- 测试结论需要可信执行回执。宿主缺少结构化退出码时，测试候选保留为 pending，文件事实继续独立核验。计划、推断、源码状态和部署状态分别记录。
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

效果评测使用 15 个合成主题和 12 条固定查询，其中 11 条纳入 Recall@5、MRR@5，比较固定 v0.2.2 基线与当前检索。语料、评分和复现方法见[源码仓库中的固定评测](https://github.com/Smarpi-clever/Tokemula-Agent-Knowledge-Workflow/blob/main/evaluation/README.md)。在源码仓库根目录运行：

```sh
python3 -B scripts/evaluate_retrieval.py --output .local/v0.3.0/evaluation
```

打包方法见[可复现构建](docs/reproducible-build.md)。贡献和问题反馈见 [CONTRIBUTING.md](CONTRIBUTING.md)，依赖记录见[依赖与许可证](docs/dependencies.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

项目代码采用 [MIT 许可证](LICENSE)。Codex、OpenAI、Python、Git 和 Obsidian 的名称及商标属于各自权利人。
