# Tokemula Agent Knowledge Workflow: reproducible archives

Build with Python 3.11 or newer and Git. The builder uses the Python standard library and does not download dependencies.

From a reviewed, committed source checkout:

```sh
python3 -B scripts/build_package.py --release --output .local/release-a
python3 -B scripts/build_package.py --release --output .local/release-b
cmp .local/release-a/SHA256SUMS .local/release-b/SHA256SUMS
```

Choose new output directories. The builder preserves existing archives and directories.

Each build produces a macOS CLI ZIP, a Codex plugin ZIP, their unpacked directories and `SHA256SUMS`. The plugin ZIP contains `agent-knowledge-workflow-marketplace/`; register that extracted directory as the local marketplace. CLI installation works independently of this optional plugin.

`--release` requires a clean Git checkout whose root is the source directory, plus `LICENSE`, `README.md`, `SECURITY.md`, `CHANGELOG.md` and the installation documentation. `PACKAGE.json` records the actual source commit and tree, version, payload digest and hashes and modes of each included file. A release package records `private_candidate: false` and `source_dirty: false`.

Each selected release input must match a regular file blob and executable bit in that source commit. This check covers files hidden from Git status by ignore rules or index flags before any archive is written.

Without `--release`, builds can include reviewed local changes for private validation. The manifest records whether the checkout is dirty. Outside a Git checkout, source commit and tree are `null`; this mode remains a private candidate.

Archives contain an explicit set of runtime, installation, example and public documentation files. Source paths, file modification times, the build directory, caches, `.local/`, Git metadata and runtime state are not archive inputs.

ZIP entries use sorted names, UTF-8 text bytes as stored in Git, explicit Unix permissions and uncompressed storage. Uncompressed storage removes dependence on the system compression library. The default timestamp comes from the source commit's committer timestamp. A candidate without Git metadata uses 1980-01-01 UTC. `SOURCE_DATE_EPOCH` can supply a non-negative integer through `4354819199`; ZIP timestamps before 1980 are clamped to 1980 and seconds are rounded down to an even value. Use the same source commit and timestamp when comparing builds.

The CLI ZIP keeps the canonical root `plugin.json`. The native plugin package derives a legacy-only `.codex-plugin/plugin.json` from that metadata. Shared runtime files have identical bytes, while each package has its own payload manifest and digest.

CI runs syntax checks, isolated tests and two committed-source builds on the `macos-15` hosted runner with Python 3.11. The installed `codex` test fixture answers only `--version`; all other operations fail. This exercises installer dependency detection without credentials, an account connection or native Hook execution. Native Desktop trust and real task acceptance are tracked separately from these synthetic checks.

CI uses the [GitHub-hosted macOS runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) and pinned releases of [checkout](https://github.com/actions/checkout/releases/tag/v7.0.1) and [setup-python](https://github.com/actions/setup-python/releases/tag/v7.0.0), with read-only repository permission and credential persistence disabled.
