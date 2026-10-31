# 安全报告

请通过 [GitHub 私密漏洞报告](https://github.com/Smarpi-clever/Tokemula-Agent-Knowledge-Workflow/security/advisories/new) 提交安全问题。普通安装问题使用 [Issues](https://github.com/Smarpi-clever/Tokemula-Agent-Knowledge-Workflow/issues)。

安全报告请包含：版本、平台、触发入口、预期权限、实际影响和使用合成数据的最小复现。提交前移除凭据、客户信息、真实笔记、个人路径和原始聊天记录。

报告表单不可用时，可提交只说明“需要私密报告渠道”的 Issue，等待维护者提供渠道后再发送技术细节。

## 安全边界

- Codex 原生机制负责审查和信任当前 Hook 定义。工具不写入信任数据库。
- 项目、知识库、状态和同步目标按映射绑定；知识自动写入限定为 `topics/*.md`。
- 人工冲突、证据不足和过期授权保留为可审阅状态。
- 自动知识更新、本地提交和远端推送分别授权；默认关闭。
- 知识笔记属于参考数据，笔记中的命令和计划不构成执行许可。

维护者优先处理当前版本的可复现安全问题。修复发布后在 Release Notes 中说明受影响版本和升级方式。数据流与保留范围见[隐私说明](docs/privacy.md)。
