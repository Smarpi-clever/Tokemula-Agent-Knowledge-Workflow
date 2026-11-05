# 安装与使用

## 从安装包开始

从 [GitHub Releases](https://github.com/Smarpi-clever/Tokemula-Agent-Knowledge-Workflow/releases) 下载 macOS 包和同版本的 `SHA256SUMS`，核对 SHA-256 后解压，双击 `Install.command`。

需要 Python 3.11+、Git、Codex Desktop，以及用于原生启用验证的 Codex CLI。依赖齐全时，向导会完成检测、路径选择和配置。依赖缺失时，从下列官方来源安装，再重新打开同一入口继续：

- [Python for macOS](https://www.python.org/downloads/macos/)
- [Git for macOS](https://git-scm.com/download/mac)
- [Codex CLI](https://learn.chatgpt.com/docs/cli)

安装器不捆绑 Python，也不自动安装系统依赖。实际验证范围见[兼容性矩阵](compatibility.md)。

向导会询问项目、知识库、同步方式和三个独立权限，并展示改动预览。默认选择本地保存，自动更新、自动提交和自动推送均关闭。

新项目的知识库默认位于 `~/Agent-Knowledge/<项目名>-<短ID>/`。向导显示完整路径，回车接受，也可输入自定义目录或选择已有知识库。短 ID 为项目规范化绝对路径的 SHA-256 前 12 位，用于区分同名项目。同一路径重复设置时沿用已有绑定，包括自定义位置；已有知识库保持原路径。

CLI 与插件共用上述规则。命令行省略 `--knowledge` 时使用已有绑定或新项目默认位置，使用 `--knowledge '/absolute/path/to/knowledge'` 指定其他目录。其余非交互设置参数和三个授权仍需明确提供。预览阶段保留文件原状，确认应用后创建所选目录；选择 `commit` 或 `push` 时仍需已有知识 Git 仓库。更改知识库路径会建立新绑定，原笔记保留在原位置。

普通 Markdown 目录可直接使用。Obsidian 为可选项；向导仅读取 Vault 注册信息和用户指定范围的目录名来发现知识库，不读取发现目录中的笔记正文，也不启动 Obsidian。

安装结果会显示完整 `akf` 路径。PATH 已包含 `~/.local/bin` 时可直接运行 `akf`；否则使用显示的完整路径。安装器保留已有同名文件，不修改 shell 启动文件。需要专用启动目录时，可使用 `AKF_BIN_DIR`。

## 审查权限与启用

首次使用建议先保持自动提交和自动推送关闭，只在确认读取及写入范围后开启知识更新。

1. 完成安装和项目绑定，运行 `akf doctor`。
2. 在 Codex 原生 Hooks 管理界面审阅当前定义并完成信任；CLI 可使用 `/hooks`。
3. 用 `akf setup` 明确允许该项目的自动知识更新。
4. 在项目中新建聊天，完成一次普通任务。
5. 运行 `akf doctor --verify`，核对实际原生维护结果。

普通 `doctor` 只诊断；`doctor --verify` 会保存本次原生触发验证状态。

自动维护在当前任务内、最终答复前完成。正常保存和 `no_change` 不另发聊天消息；需要处理权限、证据缺口或笔记冲突时，在当前答复中说明。内部维护指令通过 Hook 上下文传给 Agent，任务结束后不再追加用户消息。

| 状态 | 含义 |
|---|---|
| Installed | 程序和入口完整，校验通过 |
| Configured | 项目和知识库绑定正确 |
| Trusted | 当前五个 Hooks 已通过原生信任查询 |
| Enabled | 更新授权有效，未暂停或禁用，真实任务触发验证通过 |
| Sync Enabled | 分别报告自动本地提交和自动远端推送 |

更新授权有效、原生维护已完成且确认无新增事实时，`no_change` 也可完成 Enabled 验证；该结果不新增知识。仅有 pending、取消或错误记录时，继续显示待验证。

启用验证同时核对当前 Hook 信任、安装包、任务与实际内部上下文来源。客户端未提供所需原生记录时，保留待验证状态。

安装或启用插件后，仍需原生信任当前 Hook 定义。定义变化后，按 Codex 提示重新审查。原生状态不可查询时，诊断保留待确认状态。旧聊天可能保留安装前的 Hook 状态，接入验证请使用新聊天。

Hook 信任与 Agent 的工具、文件写入权限是两层确认。使用项目外的知识库及专属状态目录时，如原生审批阻塞维护，请由用户在当前原生聊天或审批界面直接批准这些限定路径的维护写入，保留项目源码只读和现有审批策略。其他聊天的确认或助手转述，不能替代当前原生流程要求的直接人类确认；安装器不绕过此流程。日常普通任务无需反复提醒维护，遇到实际权限提示时按原生要求批准限定范围。参阅[原生权限请求](https://learn.chatgpt.com/docs/app-server#permission-requests)与[审批机制](https://learn.chatgpt.com/docs/sandboxing/auto-review)。

Setup 预览同时显示知识库和状态目录。允许自动更新时，应用结果会提醒用户按实际原生审批批准维护写入；新聊天由 SessionStart Hook 提供实际知识库路径和按需读取入口。用户与 Agent 共用这些 Markdown 文件，自动更新保留人工正文。

自动知识更新、本地提交、远端推送分别授权。允许更新不会自动允许提交或推送。需要自动 GitHub 推送时，先安装并登录 [GitHub CLI](https://cli.github.com/)，由向导校验已有私有知识仓库的远端和分支；工具不创建远端仓库。

## 日常操作

在配置项目或其子目录执行：

```sh
akf setup
akf doctor
akf status
akf pause
akf enable
akf disable
akf upgrade --from /path/to/unpacked-package
akf uninstall
akf rollback --transaction TRANSACTION_ID
```

`pause`、`disable` 停止本工具后续自动维护，保留其他 Hooks。`enable` 复用已有有效授权并重新进入待验证状态；新增授权用 `setup` 单独确认。`resume --job ID` 用于恢复指定知识任务。

从项目子目录运行时会自动匹配映射。多个自动接入项目应互不重叠；设置时会拒绝父子范围冲突。同一项目可以重新设置。需要明确目标时，在子命令前使用顶层选项：

```sh
akf --mapping MAPPING_ID status
akf --config /path/to/retained-config.json status
```

`--config` 优先。高级配置模板见 [examples](../examples/README.md)。

## 先检索，再读取详情

```sh
akf search --query '中文请求上限 request limit' --limit 5 --budget-bytes 6000
akf read --path topics/limits.md --start-line 1 --end-line 80 --budget-bytes 6000
akf --json search --query 'src/limits.py ValueError' --history
akf context --task '排查请求限流'
akf index --rebuild
```

`search` 查询本地标题、正文、源码路径及错误信息。每项结果给出相关片段、来源、事实状态、笔记路径和行号；`read` 按位置继续读取。默认优先展示当前事实，`--history` 纳入已替代版本。人工正文标为人工笔记；托管块被手工修改后，结果提示重新核验。无匹配时返回空的 `results`。

`--limit` 接受 1–100 条结果，默认 5 条。`--budget-bytes` 接受 512–1000000 UTF-8 字节，默认 6000 字节，覆盖结构化 JSON 结果；响应中的 `output_bytes` 给出实际大小，`has_more` 提示还有内容可读取。普通查询通过文件元信息识别变化，只读取新增或改变的笔记；删除文件后同步清除索引。`index --rebuild` 从 Markdown 和事实记录重建派生索引。

`context --task` 使用同一检索入口。SessionStart Hook 继续只提供元信息与按需入口，Agent 按当前任务检索和读取。索引、查询和片段提取在本机执行。

## 查看事实状态与时效

事实状态分为三项：

| 字段 | 值与含义 |
|---|---|
| `evidence_status` | `tool_verified` 为已通过工具证据验证，`pending`／`unverified` 为待审候选，`unknown` 为缺少对应信息 |
| `lifecycle` | `current` 为当前版本，`superseded` 为已替代版本，`candidate` 为候选；人工正文等未绑定版本的内容显示 `unknown` |
| `review_status` | `clear` 为本次检查未触发复核，`review_required` 为待复核，`unknown` 为信息缺失 |

`review_reasons` 解释复核原因；任务本身的 completed、partial 等评估单独保留。同项目、环境及稳定 `claim_id` 的新有效证据可更新当前事实，旧内容保留历史及替代关系。独立事实使用各自的 `claim_id`。相同内容重新核验时只更新最近核验信息，保留一份内容。

候选可用带时区的 `review_after` 指定复核截止时间，例如 `2026-12-01T00:00:00Z`。当前文件与已记录证据不一致或超过截止时间时，结果提示待复核。固定 commit 的证据保留对应版本的含义；当前工作树变化不会自行替换该历史内容。旧记录缺失的状态显示 `unknown`，缺失的最近核验时间保留为 `null`；后续有效核验可补齐。

## 逐项处理 pending

先查看任务，再选择候选：

```sh
akf pending
akf --json status --job JOB_ID
akf explain --job JOB_ID
akf review --job JOB_ID --item topics/limits.md:request-limit --action show
```

`explain` 和 `review` 展示候选内容、目标事实、来源、证据缺口、冲突原因、拟议变化及可执行操作。候选 ID 为 `主题路径:claim_id`，以实际结果中的 ID 为准。

从 `explain` 的 `items[].candidate` 取出完整的单条更新对象，保存为 `update.json`；补充与本任务对应的证据后重新验证：

```sh
akf review --job JOB_ID --item topics/limits.md:request-limit --action revalidate < update.json
akf review --job JOB_ID --item topics/limits.md:request-limit --action confirm
akf review --job JOB_ID --item topics/limits.md:request-limit --action reject
akf review --job JOB_ID --item topics/limits.md:request-limit --action withdraw
akf status --job JOB_ID
```

`revalidate` 仍检查原任务回执、文件或 commit 证据、原笔记摘要和托管块归属。人工修改引起冲突时，保留当前笔记，在新任务中重新读取后提交；原候选继续保留原笔记摘要。`confirm` 记录人工确认，工具核验状态由实际证据决定。测试与执行结论继续要求相应的可信回执。`reject` 拒绝单条候选，`withdraw` 撤回单条候选，两者均保存简短处理记录；同任务中已成功保存的事实保留。

`resume --job ID` 恢复既有写入或同步，`undo --job ID` 按恢复机制撤销工具拥有的变化。完成目标随当前配置确定：仅本地保存时，保存成功且没有遗留候选的任务从 context 待办移除；启用提交或推送时，相关同步问题单独展示。

## 升级、迁移与回滚

再次运行安装器会识别已有安装并保留用户数据。升级使用已经校验的解压包；检查预览后执行 `akf upgrade --from /path/to/unpacked-package`。

升级后，当前聊天的下一条工具活动会刷新新包的内部维护提示。原生启用验证继续核对升级后的实际维护记录。

旧直接 Hooks 可通过 `akf setup --migrate-config /path/to/old-config.json` 明确迁移。先预览再应用，只替换精确匹配的本工具 handler。旧配置和 state 保留，归属不明的条目供人工处理。

向导新建配置默认关闭整个项目内容快照。旧配置保持原有行为；升级后通过设置向导检查实际读取范围，详见[隐私说明](privacy.md)。

安装操作返回事务 ID。失败或中断后，按事务恢复；共享 JSON 只撤销本工具的增量。用户后续编辑产生冲突时，保留当前文件和恢复材料供审阅。

`akf uninstall` 处理整个工具安装：禁用已登记的全部项目映射，并移除本工具拥有的接入和启动入口。只暂停某个项目时，使用该项目的 `pause` 或 `disable`。

卸载保留笔记、配置、pending、状态和版本恢复副本。启动入口已卸载时，从保留的安装包恢复：

```sh
/bin/sh /path/to/package/Install.command rollback --transaction TRANSACTION_ID
```

更改同步绑定或有效期会保留旧 state 和 pending，创建新绑定。同项目、同知识库、同环境的已管理事实及历史，经来源与完整托管块校验后延续。旧任务、执行回执和权限留在原绑定。

需要处理旧绑定中的任务时，用保留的 `--config` 查看状态，重新核对该绑定的授权和有效期，再按需 `enable` 及 `resume --job ID`。恢复知识任务不会恢复已取消的产品工作。

### 0.3.0 的状态与索引

升级前保留所选配置、`state_dir` 与知识库的副本；在隔离副本上先完成检索、审阅和回退检查。0.3.0 沿用 Markdown 与原事实记录，读取旧字段时保留已知信息，缺失的状态显示 `unknown`。检索索引属于可重建数据，升级后可执行 `akf index --rebuild`。

安装升级返回的事务 ID 用于回滚程序和安装接入，升级后新增的知识和 pending 保留。知识写入使用 `undo --job ID` 的恢复记录单独处理。需要恢复升级前的数据快照时，先另存当前知识库和状态，在隔离副本核对后选择恢复范围。

## 可选 Codex 插件

CLI 独立工作。可选插件通过同一引擎和向导完成设置，使用默认状态目录 `~/Library/Application Support/AgentKnowledgeWorkflow/`。自定义 `AKF_HOME` 的安装请选择 `direct` 模式。

从同一 Release 下载 `TokemulaAgentKnowledgeWorkflow-0.3.0-codex-plugin.zip`，核对摘要并解压。注册其中的 marketplace 目录：

```sh
codex plugin marketplace add /path/to/agent-knowledge-workflow-marketplace
```

重开插件界面，选择 “Tokemula Agent Knowledge Workflow”，安装插件并运行 Setup。向导选择 `plugin` 模式后，由插件内置五个 Hooks 接入。同一映射只选择 `direct` 或 `plugin` 一种方式。

插件的 Setup 开关管理设置技能。暂停自动维护请运行 `akf pause`，或在原生“钩子”设置中关闭本插件的五项 Hooks。卸载插件时先在插件界面移除，再移除专用来源：

```sh
codex plugin marketplace remove agent-knowledge-workflow
```

程序安装及保留的数据仍可通过 `akf uninstall` 和事务恢复管理。

插件支持经过验证的手动 marketplace 安装路径。主要分发渠道为 CLI 和 GitHub Release。

原生信任依据：[官方 Hooks 说明](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)。插件分发依据：[官方插件说明](https://learn.chatgpt.com/docs/plugins)。产品行为和客户端版本范围见[兼容性矩阵](compatibility.md)。

## Hook 诊断

`akf doctor` 使用独立的只读 Codex stdio 连接查询 Hook 和任务记录，无需先启动 daemon。Hook 已信任与维护已启用分别显示：`Trusted` 来自原生 Hook 状态；`Enabled` 继续要求已有授权和真实任务的维护验证。

执行回执的 `exit_code_state` 会说明退出码是已捕获、缺失、非法或冲突。仅结构化整数退出码参与执行结论验证；缺少可信字段的宿主输出保留为待审证据。文件事实继续按文件或固定 commit 证据核验，与测试候选分别保存。
