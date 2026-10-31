# 安装与使用

## 从安装包开始

从 [GitHub Releases](https://github.com/Smarpi-clever/Tokemula-Agent-Knowledge-Workflow/releases) 下载 macOS 包和同版本的 `SHA256SUMS`，核对 SHA-256 后解压，双击 `Install.command`。

需要 Python 3.11+、Git、Codex Desktop，以及用于原生启用验证的 Codex CLI。依赖齐全时，向导会完成检测、路径选择和配置。依赖缺失时，从下列官方来源安装，再重新打开同一入口继续：

- [Python for macOS](https://www.python.org/downloads/macos/)
- [Git for macOS](https://git-scm.com/download/mac)
- [Codex CLI](https://learn.chatgpt.com/docs/cli)

安装器不捆绑 Python，也不自动安装系统依赖。实际验证范围见[兼容性矩阵](compatibility.md)。

向导会询问项目、知识库、同步方式和三个独立权限，并展示改动预览。默认选择本地保存，自动更新、自动提交和自动推送均关闭。

普通 Markdown 目录可直接使用。Obsidian 为可选项；向导仅读取 Vault 注册信息和用户指定范围的目录名来发现知识库，不读取发现目录中的笔记正文，也不启动 Obsidian。

安装结果会显示完整 `akf` 路径。PATH 已包含 `~/.local/bin` 时可直接运行 `akf`；否则使用显示的完整路径。安装器保留已有同名文件，不修改 shell 启动文件。需要专用启动目录时，可使用 `AKF_BIN_DIR`。

## 审查权限与启用

首次使用建议先保持自动提交和自动推送关闭，只在确认读取及写入范围后开启知识更新。

1. 完成安装和项目绑定，运行 `akf doctor`。
2. 在 Codex 原生 Hooks 管理界面审阅当前定义并完成信任；CLI 可使用 `/hooks`。
3. 用 `akf setup` 明确允许该项目的自动知识更新。
4. 在项目中新建聊天，完成一次普通任务。
5. 运行 `akf doctor --verify`，核对实际触发和知识写入。

普通 `doctor` 只诊断；`doctor --verify` 会保存本次原生触发验证状态。

| 状态 | 含义 |
|---|---|
| Installed | 程序和入口完整，校验通过 |
| Configured | 项目和知识库绑定正确 |
| Trusted | 当前五个 Hooks 已通过原生信任查询 |
| Enabled | 更新授权有效，未暂停或禁用，真实任务触发验证通过 |
| Sync Enabled | 分别报告自动本地提交和自动远端推送 |

安装或启用插件后，仍需原生信任当前 Hook 定义。定义变化后，按 Codex 提示重新审查。原生状态不可查询时，诊断保留待确认状态。旧聊天可能保留安装前的 Hook 状态，接入验证请使用新聊天。

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

## 升级、迁移与回滚

再次运行安装器会识别已有安装并保留用户数据。升级使用已经校验的解压包；检查预览后执行 `akf upgrade --from /path/to/unpacked-package`。

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

## 可选 Codex 插件

CLI 独立工作。可选插件通过同一引擎和向导完成设置，使用默认状态目录 `~/Library/Application Support/AgentKnowledgeWorkflow/`。自定义 `AKF_HOME` 的安装请选择 `direct` 模式。

从同一 Release 下载 `TokemulaAgentKnowledgeWorkflow-0.2.0-codex-plugin.zip`，核对摘要并解压。注册其中的 marketplace 目录：

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
