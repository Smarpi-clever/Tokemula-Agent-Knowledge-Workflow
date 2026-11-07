# Tokemula Agent Knowledge Workflow

[中文](README.md) | English

Tokemula Agent Knowledge Workflow (`akf`) helps Codex maintain Markdown project knowledge with traceable sources after completing everyday tasks. It reuses the current session's agent, model, and permissions, with no additional model API, provider, or credentials required.

The CLI is the primary installation entry point. An optional Codex plugin provides the same setup wizard. Your knowledge base can be a regular Markdown directory or an Obsidian vault.

## Installation

Version 0.3.1 is a locally accepted candidate. On macOS arm64 with Codex CLI 0.166.0, direct Hooks and an existing local plugin adapter completed ordinary test tasks, model-driven maintenance, and new-session recall. Native evidence queries on this machine require formal tool approval. Public installation uses a published release; see the [compatibility matrix](docs/compatibility.md) for version-specific results.

Python 3.11+, Git, and Codex Desktop are required. Verifying native activation also requires the Codex CLI. See the [compatibility matrix](docs/compatibility.md) for client, integration, and upgrade coverage.

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

New projects default to `~/Agent-Knowledge/<project-name>-<short-id>/`. Press Enter in the wizard to accept, or choose a custom location or an existing Obsidian vault. Repeated setup reuses the existing binding. Hooks provide the agent with the knowledge entry point, so you and the agent use the same Markdown files.

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

## Find knowledge and review pending facts

Search for relevant excerpts, then read details at the returned path and line numbers:

```sh
akf search --query '中文请求上限 src/limits.py' --limit 5 --budget-bytes 6000
akf read --path topics/limits.md --start-line 1 --end-line 80 --budget-bytes 6000
akf context --task 'Investigate request limit errors'
akf search --query 'request limit' --history
akf index --rebuild
```

Search covers titles, note bodies, source paths, and error messages in Chinese, English, or mixed queries. Progress on the same matter is organized by stage, with the latest relevant stage prominent by default; `--history` includes earlier stages and superseded versions. Source behavior, tests, commits, releases, installation, and deployment have separate records. Conflicting states prompt review. Results distinguish manual notes, tool-verified facts, and superseded history. File changes, edits to managed blocks, and explicit expiry rules prompt review. `--budget-bytes` caps structured output in UTF-8 bytes. A query with no matches returns `results: []`. The local SQLite index follows note edits and deletions and can be rebuilt.

```sh
akf pending
akf explain --job JOB_ID
akf review --job JOB_ID --item topics/limits.md:request-limit --action show
akf review --job JOB_ID --item topics/limits.md:request-limit --action revalidate < update.json
akf review --job JOB_ID --item topics/limits.md:request-limit --action reject
```

Review shows each candidate's evidence gaps, conflicts, and proposed change. Revalidation uses the existing evidence and write checks, preserving facts already saved in the same job. Use `--reason` to retain the decision rationale or `link-history` to associate an identical candidate with a verified saved version. Put `--json` before the subcommand for scripts. See [installation and usage](docs/install.md) for review, fact freshness, and upgrade/rollback steps.

## Data and permissions

- Your knowledge base is stored separately from the program, configuration, and state directories.
- Automatic updates write only to tool-managed blocks in `topics/*.md`, preserving manually written content, indexes, and existing history.
- New setups use task receipts and explicitly selected file or commit evidence, without creating a content snapshot of the entire project.
- Test conclusions use trusted receipts from the corresponding task. Missing exit codes trigger bounded Codex native queries with session, turn, tool, project, and command identity checks. Remaining gaps retain their reasons. Test detection includes `pnpm exec vitest run ...`.
- Private release reports use `select-report --path` to select one exact file. Report contents, running installation state, and deployment outcomes have separate verification.
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

The evaluation uses 15 synthetic topics and 12 fixed queries. Eleven queries contribute to Recall@5 and MRR@5, comparing the fixed v0.2.2 baseline with current retrieval. Run it from the source repository root:

```sh
python3 -B scripts/evaluate_retrieval.py --output .local/v0.3.1/evaluation
```

See the [fixed evaluation in the source repository](https://github.com/Smarpi-clever/Tokemula-Agent-Knowledge-Workflow/blob/main/evaluation/README.md) for the baseline, scoring rules, and output reports.

See [reproducible builds](docs/reproducible-build.md) for packaging instructions, [CONTRIBUTING.md](CONTRIBUTING.md) for contributions and issue reporting, [dependencies and licenses](docs/dependencies.md) for dependency records, and [CHANGELOG.md](CHANGELOG.md) for release history.

The project code is licensed under the [MIT License](LICENSE). The names and trademarks of Codex, OpenAI, Python, Git, and Obsidian belong to their respective owners.
