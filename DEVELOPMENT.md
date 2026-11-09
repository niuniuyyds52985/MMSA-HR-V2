# Developer guide

MMSA-HR V2 is a Windows x64 local application. The user-facing installation path is documented in the root [README](README.md) and [installation guide](docs/v2-local-install.md). This file describes the source layout and the checks used before preparing a source package.

## Source layout

| Path | Purpose |
|---|---|
| `src/mmsa_v2/` | V2 API, interview workflow, scoring, reports, local media analysis and Qwen STDIO bridge |
| `web/v2/` | Candidate and HR web interfaces |
| `config/v2/` | Versioned question bank and choice wording |
| `scripts/` | First-run setup, readiness checks and optional Qwen import-file generation |
| `suit-demo/` | Compatibility modules, credential storage, media helpers and model installation locations required by V2 |
| `tests/v2/` | V2 unit, contract and portable-package tests |
| `docs/v2-local-install.md` | New-user installation and troubleshooting |
| `docs/v2-local-check.md` | Interview flow and report verification |

The `suit-demo/` directory contains older helper modules that V2 still imports. Its V1 workbench scripts are not the V2 startup path.

## Prepare the development environment

Run the root one-click initialization command on Windows, or follow the installation guide. The installer pins dependencies and downloaded assets, writes them under the project directory, and checks their hashes. Keep `.runtime/`, `.venv-suit/`, local credentials and `suit-demo/local-data/` out of Git.

Readiness check:

```powershell
.\.venv-suit\Scripts\python.exe -B -X utf8 scripts\install_v2.py --doctor
```

Run V2 tests after initialization:

```powershell
.\.venv-suit\Scripts\python.exe -B -X utf8 -m unittest discover -s tests\v2 -q
```

Tests use synthetic inputs and temporary data. The V2 test suite does not call paid model APIs or execute model weights. The portable-package test extracts the source bundle to a separate path and checks the eight-question flow and HTML exports.

## Build a source bundle

```powershell
.\.venv-suit\Scripts\python.exe -B -X utf8 suit-demo\delivery_tools.py build
```

The build writes an archive, checksum and manifest under the ignored `delivery/` directory. It scans for credential-like strings and marks the package as excluding runtime data and credentials. Review the resulting file list before publishing any release.

## Release checks

- Run the readiness check and V2 test suite.
- Build and inspect the source archive; confirm there are no resumes, media, reports, databases, credentials, downloaded weights or local environments.
- Verify the first-run flow on a clean Windows x64 computer and test the actual Qwen desktop import separately.
- Preserve `NOTICE.md` and each third-party license/source record. Do not add an overall project license until the project owner confirms the rights to all included and inherited code and materials.
- Describe model and assessment limitations accurately. Passing software tests does not establish interview-score validity or suitability for hiring decisions.
