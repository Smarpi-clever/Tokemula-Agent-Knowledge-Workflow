# Tokemula Agent Knowledge Workflow

[中文](README.md) | English

Tokemula Agent Knowledge Workflow (`akf`) helps Codex maintain Markdown project knowledge with traceable sources after completing everyday tasks. It reuses the current session's agent, model, and permissions, with no additional model API, provider, or credentials required.

The CLI is the primary installation entry point. An optional Codex plugin provides the same setup wizard. Your knowledge base can be a regular Markdown directory or an Obsidian vault.

## Installation

The currently verified platform is macOS arm64. Python 3.11+, Git, and Codex Desktop are required. Verifying native activation also requires the Codex CLI. See the [compatibility matrix](docs/compatibility.md).

1. Download the macOS package from [GitHub Releases](https://github.com/Smarpi-clever/Tokemula-Agent-Knowledge-Workflow/releases) and verify its checksum against `SHA256SUMS` from the same release.
2. Extract the package and double-click `Install.command`. Choose your project, knowledge base, and sync mode, then review the proposed changes.
3. Grant permissions for automatic knowledge updates, local commits, and remote pushes separately. All three are off by default. Local storage is recommended for first-time setup.
4. Follow the wizard to review and trust Hooks in the native Codex interface. Start a new chat in your project, complete an everyday task, then run `akf doctor --verify`.

With the required dependencies installed, one wizard session handles detection and configuration without manual JSON editing. If dependencies are missing, the wizard provides official installation links and preserves your existing settings so you can continue later.

Installation and automatic maintenance activation are shown separately:

| Status | Meaning |
|---|---|
| Installed | The tool and launch entry points are in place |
| Configured | The project and knowledge base are linked |
| Trusted | The current Hooks have been trusted through Codex's native interface |
| Enabled | Authorization is granted and verification through a real task has passed |
| Sync Enabled | Automatic local commits and automatic remote pushes are shown separately |

For full instructions, the optional plugin, and recovery steps, see [installation and usage](docs/install.md).

## Everyday use

Work normally in a configured project. The agent updates relevant topics based on actual results while preserving sources and history. It creates no notes when there are no new lasting facts. Items with insufficient sources or conflicts with manual edits remain pending for review.

```sh
akf status
akf doctor
akf pause
akf enable
akf disable
```

`pause` pauses automatic maintenance. `enable` resumes it using existing valid authorization. The Setup toggle in the plugin details controls the setup skill. Native Hooks settings and the `akf` control commands manage automatic maintenance.

## Data and permissions

- Your knowledge base is stored separately from the program, configuration, and state directories.
- Automatic updates write only to tool-managed blocks in `topics/*.md`, preserving manually written content, indexes, and existing history.
- New setups use task receipts and explicitly selected file or commit evidence, without creating a content snapshot of the entire project.
- Test conclusions require trusted execution receipts. Plans, inferences, source state, and deployment state are recorded separately.
- Automatic commits include only notes owned by this tool. Automatic remote pushes require separate authorization and validate the private knowledge repository binding.
- Uninstalling preserves notes, configuration, pending items, and recovery materials. Installation, upgrades, and uninstallation support transactional rollback.

See the [privacy notice](docs/privacy.md) for data flow, differences in older configurations, and what is retained. Report security issues through the [private reporting channel](SECURITY.md).

## Development and contributions

The runtime and tests use the Python standard library. No Python package dependencies need to be installed:

```sh
python3 -m akf --help
make check
make test
```

Tests use synthetic projects and local Git fixtures. See [examples](examples/README.md) for configuration and Hook templates, and [synthetic-knowledge](synthetic-knowledge/topics/project-behavior.md) for a blank synthetic knowledge topic. Use the wizard for a regular installation.

See [reproducible builds](docs/reproducible-build.md) for packaging instructions, [CONTRIBUTING.md](CONTRIBUTING.md) for contributions and issue reporting, [dependencies and licenses](docs/dependencies.md) for dependency records, and [CHANGELOG.md](CHANGELOG.md) for release history.

The project code is licensed under the [MIT License](LICENSE). The names and trademarks of Codex, OpenAI, Python, Git, and Obsidian belong to their respective owners.
