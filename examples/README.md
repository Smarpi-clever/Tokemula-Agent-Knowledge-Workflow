# 合成示例

这些模板与合成主题用于理解协议和开发测试。普通用户请使用 [`akf setup` 向导](../docs/install.md)，由工具生成配置。

- [config.json](config.json)：相对路径和虚构私有 Git 远端的配置模板。自动更新、提交和推送均关闭，并显式设置 `project_snapshot=false`。路径相对于配置文件解析，开发者使用时应按隔离目录调整。
- [hooks.json](hooks.json)：未激活的 Hook 模板。实际安装由向导合并本工具 handler，并保留其他 Hooks。
- [空白合成知识主题](../synthetic-knowledge/topics/project-behavior.md)：人工拥有的简介，供合成任务追加有证据的知识。

示例中没有真实项目笔记或预设“任务已通过”的结论。测试应在临时项目、独立状态目录和本地 Git 远端中完成，保留人工内容保护及证据验证规则。
