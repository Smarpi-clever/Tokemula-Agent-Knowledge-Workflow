# 依赖与许可证

## Python 代码

运行时、打包脚本和测试使用 Python 标准库及本项目模块。当前源清单未引入第三方 Python 包，无需 `pip install`。安装包不捆绑 Python 解释器、标准库、Git、Codex 或 Obsidian。

项目代码的许可见 [LICENSE](../LICENSE)。第三方名称、外部程序和服务分别适用其各自条款。

## 外部环境

| 项目 | 用途 | 分发方式与来源 |
|---|---|---|
| Python 3.11+ | 运行 CLI、安装器和测试 | 用户单独安装；[Python 许可](https://docs.python.org/3/license.html) |
| Git | 确认项目和知识库身份；可选本地提交和同步 | 用户单独安装；[Git 项目](https://git-scm.com/) |
| Codex Desktop／CLI | 当前 Agent、原生 Hooks、信任及触发验证 | 用户单独安装；[官方 CLI 说明](https://learn.chatgpt.com/docs/cli) |
| GitHub CLI | GitHub 自动推送时确认私有仓库身份 | 按需单独安装；[项目与安装入口](https://cli.github.com/) |
| Obsidian／CLI | 可选 Vault 搜索和读回 | 按需单独安装；[Obsidian](https://obsidian.md/) |

调用已安装程序与将其二进制或源码放入安装包分别检查。本项目当前安装包只包含项目自身的显式文件集合。

## CI 专用依赖

GitHub Actions 在 GitHub 托管的 `macos-15` runner 中运行，源码工作流固定以下完整 commit。2026-10-31 已核对对应官方 Action 定义和许可证：

| 组件 | 固定版本与来源 | 许可与运行要求 |
|---|---|---|
| `actions/checkout` | v7.0.1，`3d3c42e5aac5ba805825da76410c181273ba90b1`；[Action 定义](https://github.com/actions/checkout/blob/3d3c42e5aac5ba805825da76410c181273ba90b1/action.yml) | [MIT](https://github.com/actions/checkout/blob/3d3c42e5aac5ba805825da76410c181273ba90b1/LICENSE)，声明 `node24` |
| `actions/setup-python` | v7.0.0，`5fda3b95a4ea91299a34e894583c3862153e4b97`；[Action 定义](https://github.com/actions/setup-python/blob/5fda3b95a4ea91299a34e894583c3862153e4b97/action.yml) | [MIT](https://github.com/actions/setup-python/blob/5fda3b95a4ea91299a34e894583c3862153e4b97/LICENSE)，声明 `node24`；本工作流选择 Python 3.11 |
| Node.js 24 | 上述 Actions 的 JavaScript 运行环境，由 runner 提供 | [Node.js 24 许可及所含第三方声明](https://github.com/nodejs/node/blob/v24.x/LICENSE)：Node.js 代码采用 MIT，附带组件各自保留许可 |

这些组件只在 CI 中执行，不随本工具安装包分发。安装和运行本工具无需 Node.js；安装包继续只包含项目自身的显式文件集合。

## 来源记录

项目采用 [MIT 许可证](../LICENSE)，使用 [OSI MIT 许可](https://opensource.org/license/mit) 的标准文本。Python 官方说明其软件与文档的许可及附带第三方声明，详见 [Python History and License](https://docs.python.org/3/license.html)。

当前源码定向检查未发现 vendored 第三方实现或第三方图标素材。新增复制代码、资产、依赖或打包运行时时，应记录来源、版本、许可、必要声明和再分发条件，并在发布前复核。
