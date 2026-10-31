---
name: setup
description: 安装或设置 Tokemula Agent Knowledge Workflow，选择项目、Markdown 知识库和独立权限。
---

# Tokemula Agent Knowledge Workflow 设置

本 Skill 只负责设置。复用当前 Agent 配置，不指定模型、推理强度或 Provider。

1. 确认本插件的实际安装根目录。使用包内 `scripts/python.sh` 启动 `scripts/install.py`。它安装独立 CLI，不要求用户编辑 JSON。若缺少 Python，展示启动器给出的官方安装地址。
2. 先用自然语言收集项目绝对路径、知识库绝对路径、同步方式 `local|commit|push`、已有 Git 远端名称。普通 Markdown 不需要 Obsidian。
3. 分别询问自动知识更新、本地自动提交、远端自动推送；默认均为关闭。每项使用用户的明确选择，通用“继续”不代表授权。
4. 调用共用设置接口预览。命令参数：`setup --non-interactive --project PROJECT --knowledge KNOWLEDGE --sync local|commit|push --hook-mode plugin --authorize-update yes|no --authorize-commit yes|no --authorize-push yes|no`。需要远端时增加 `--remote NAME`。安装包启动器自带 `setup`，调用 `scripts/install.py` 时省略该词。所有路径按 shell 参数单独引用。
5. 用简短中文展示读写范围、状态目录、Hook 来源和提交/网络行为。用户确认此预览后，用相同选择添加 `--apply`。不得触碰其他 Hooks、笔记、Git 改动、信任数据库或审批策略。
6. 引导用户在 Codex 原生 `/hooks` 审查和信任这五个 Hook。安装插件和运行设置都不授予信任。
7. 执行用户允许的普通任务，再调用 `akf doctor --verify`。分别报告 Installed、Configured、Trusted、Enabled 和两项同步状态。实际验证前显示“待验证”。

暂停使用 `akf pause`，恢复使用 `akf enable`。卸载使用 `akf uninstall`，并在插件界面卸载插件。保留知识库、配置和恢复记录。需要更改项目、知识库或 Git 绑定时重新设置，以新 state 保留原 pending jobs。
