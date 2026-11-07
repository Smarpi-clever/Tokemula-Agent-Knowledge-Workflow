# 平台与客户端兼容性

## 0.3.0 当前验证范围

以下结果于 2026-11-05 和 2026-11-07 在 macOS arm64 的已有机器上分别验证。客户端及实际运行时按验收日期列出。运行时和测试使用 Python 标准库；本地检索采用 SQLite FTS5。

| 环境 | 记录 |
|---|---|
| 操作系统与架构 | macOS 27.2，arm64 |
| Python | 3.12.9；程序最低要求 3.11 |
| Git | 2.54.0 Apple Git-157 |
| CLI 直接 Hooks（2026-11-05） | Codex CLI 0.166.0 |
| Desktop 直接 Hooks（2026-11-05） | ChatGPT Desktop 26.1029.61003，build 24614；实际运行时 0.166.0-alpha.6 |
| Desktop 已登记项目的本地插件适配入口（2026-11-07） | ChatGPT Desktop 26.1103.32239，build 25471；实际运行时 0.167.0-alpha.3.2 |

| 平台／场景 | 状态 |
|---|---|
| CLI 0.166.0、直接 Hooks、普通开发任务 | 已实测：五项 Hook 发现、启用和原生信任；同轮自动保存、静默收尾、`doctor --verify` 通过 |
| CLI 0.166.0、直接 Hooks、新会话召回 | 已实测：普通问题触发知识检索、读取及来源与当前状态核对 |
| macOS arm64 的实际共享安装升级 | 已实测：知识、事实历史、pending、项目绑定、暂停状态和授权保留 |
| v0.2.2 数据兼容、升级与回退 | 隔离副本及实际安装包流程通过 |
| 0.2.1-fix.1 私有修补版升级与回退 | 已用有效安装包副本验证至 0.3.0 的升级、事务回退与数据保留 |
| 可选发行插件的手动 marketplace 注册、安装、移除 | 隔离验证通过 |
| Desktop 直接 Hooks、只读普通任务与新会话召回（2026-11-05） | 已实测：检索、读取既有知识并对照源码；同轮维护为 `no_change`，`doctor --verify` 通过 |
| Desktop 已登记项目的本地插件适配入口、只读普通任务与新会话召回（2026-11-07） | 已实测：五项 Hook 启用并受信任，检索、读取既有知识并对照源码；同轮维护为 `no_change`，`doctor --verify` 通过 |
| 干净 macOS VM／独立机器首次安装与缺失依赖恢复 | 待环境验收 |
| macOS Intel | 待验收 |
| Windows、Linux Desktop 接入 | 待验收 |
| 其他 Agent、IDE 扩展和云端工作环境 | 待验收 |

插件模型闭环通过已有本地适配入口验证，使用共享的 0.3.0 引擎；发行插件包的注册、安装和移除已单独通过隔离验证。发行包通过 Setup 选择项目并分别授权。

### 执行回执与事实核验

宿主提供结构化整数退出码时，测试与执行候选可使用对应任务的可信回执核验。本机 CLI 实测中，部分 `PostToolUse` 回执缺少此字段；这些测试候选保留为 pending，文件行为事实按匹配的文件证据独立核验。原生启用验证继续核对 Hook 信任、当前安装包与真实任务上下文。

### 固定合成检索评测

评测使用 15 个合成主题与 12 条固定查询，其中 11 条计入相关性评分，比较固定 v0.2.2 基线与 0.3.0 检索。覆盖中文、混合查询、正文和路径／错误命中、替代历史、待复核、空结果及人工修改。语料规模、Recall@5、MRR@5、上下文 UTF-8 bytes 与本地耗时口径见[源码仓库中的固定评测](https://github.com/Smarpi-clever/Tokemula-Agent-Knowledge-Workflow/blob/main/evaluation/README.md)。

## 0.2.0 历史验证记录

0.2.0 安装候选于 2026-10-31 完成本机 macOS arm64 的 CLI 和可选插件流程验证，使用合成项目与独立知识目录。原生信任、普通任务自动维护、停止维护、卸载及恢复均保留了对应证据。

| 环境 | 记录 |
|---|---|
| 操作系统与架构 | macOS 27.2，arm64 |
| Python | 3.12.9；程序最低要求 3.11 |
| Git | 2.54.0 Apple Git-157 |
| Desktop | ChatGPT Desktop 26.1027.30824，build 22943 的 Codex 本地运行环境 |
| PATH 中 Codex CLI | 0.165.0 |
| Desktop bundled CLI | 0.166.0-alpha.5.1 |
| 当时实际 App Server | 0.162.1 |

| 平台／场景 | 状态 |
|---|---|
| 上述 macOS arm64 已有机器，CLI 安装与原生任务 | 已实测 |
| 上述 macOS arm64，可选原生插件及手动 marketplace 安装 | 已实测 |
| 临时 HOME、合成目录中的安装、冲突与恢复 | 隔离验证 |
| 干净 macOS VM／独立机器首次安装与缺失依赖恢复 | 待环境验收 |
| macOS Intel | 待验收 |
| Windows、Linux Desktop 接入 | 待验收 |
| 其他 Agent、IDE 扩展和云端工作环境 | 待验收 |

源码和 CI 的隔离测试单独报告。新增平台的安装支持以真实客户端验证为准。

## 0.2.1 静默维护补丁

0.2.1 将知识维护放在当前任务内部收尾，Stop 不再追加用户消息。该运行路径于 2026-11-01 在上述本机 macOS arm64 的直接 Hooks 接入中完成真实隐藏上下文、同轮维护及 Enabled 验证，并通过 194 项隔离回归和独立 ZIP 协议检查。

可选插件复用同版本运行代码及既有 legacy-only 包装。插件完整界面流程的实机记录保留为 0.2.0 历史验收；新包的同源构建和隔离校验单独记录。

内部来源验证使用 Codex 提供的聊天记录位置与 harness Hook 元数据。客户端缺少所需记录时显示待验证。上述待环境验收的平台和场景继续保留原状态。

## 安装条件

Python 3.11+、Git 和 Codex Desktop 为基础环境。Codex CLI 用于原生状态验证。Obsidian 可选；普通 Markdown 知识库可直接使用。GitHub 自动推送还需要已登录的 GitHub CLI 和已存在的私有知识仓库。

“一次启动向导完成设置”适用于依赖已经满足的环境。缺少依赖时，向导提供官方安装方式并保留设置进度；Hook 信任和三项自动化权限仍由用户确认。

## 原生插件布局

CLI 安装包保留 canonical 根 `plugin.json`。当前已验证的原生插件包由同一份元数据派生，使用单独的 `.codex-plugin/plugin.json` 布局。

当时测试的三个 runtime 对 legacy-only 插件发现了五个 Hooks；portable-only 与双 manifest 布局未加载这些 Hooks。因此发布包继续保留这条兼容路径。客户端或布局变化后，需重新验证实际发现、原生信任和普通任务触发。

插件入口通过手动 marketplace 安装。Setup 开关管理设置技能，自动维护的停止与恢复使用原生 Hooks 管理或 `akf` 命令。

官方说明：[Hooks 信任](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)、[插件与支持入口](https://learn.chatgpt.com/docs/plugins)。具体安装步骤见[安装与使用](install.md)。
