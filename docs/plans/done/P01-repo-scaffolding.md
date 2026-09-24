# P01: Repo scaffolding & tooling

- Status: done
- Milestone: M1
- Depends on: –
- ADRs: [0001](../../decisions/0001-language-split.md), [0011](../../decisions/0011-macos-app.md), [0012](../../decisions/0012-widget-data-path.md), [0013](../../decisions/0013-python-engineering.md), [0014](../../decisions/0014-docs-and-workflow.md), [0016](../../decisions/0016-identifiers.md)

## Goal
- A buildable, testable, lintable empty skeleton for both halves (Python `ccs`, Swift app + widget) with one `Makefile` entry point, so every later plan only adds code.

## Scope
- The repo layout from ADR-0016: `python/`, `macos/`, `schema/`, `Makefile`, `.gitignore`, `.editorconfig`.
- The Python package skeleton, with `ccs --version` working after `make install-dev`.
- The fake `claude` skeleton for tests.
- The XcodeGen project skeleton: app + widget targets that compile, with placeholder views.
- The `schema/` folder with a README.

## Out of scope
- Any business logic (P02+). Real UI (P10–P12). Signing decisions beyond the ADR-0012 primary (P00-S1 may change entitlements later).

## Design
- **Tree:**
  ```
  python/pyproject.toml
  python/src/ccs/__init__.py        __version__ = "0.1.0"
  python/src/ccs/__main__.py        from ccs.cli import main; raise SystemExit(main())
  python/src/ccs/cli.py             main(argv=None) -> int; argparse; --version only
  python/tests/conftest.py          fixtures: tmp_xdg (sets XDG_CONFIG_HOME/XDG_STATE_HOME to tmp), fake_claude_path
  python/tests/test_cli_version.py
  python/tests/fake_claude/claude   executable python script (shebang: /usr/bin/env python3)
  python/tests/fake_claude/README.md
  python/tests/fixtures/.gitkeep
  macos/project.yml
  macos/App/CCSupervisorApp.swift   @main App, MenuBarExtra("CC Supervisor") { Text("Hello") }
  macos/App/Info.plist              LSUIElement = YES, CFBundleURLTypes ccsupervisor
  macos/App/App.entitlements        (no sandbox)
  macos/Widgets/CCSupervisorWidgets.swift   WidgetBundle with one placeholder StaticConfiguration widget
  macos/Widgets/Info.plist          NSExtension com.apple.widgetkit-extension
  macos/Widgets/Widgets.entitlements app-sandbox + temporary-exception read-only "/.local/state/ccs/widget/"
  macos/Shared/.gitkeep
  macos/Tests/.gitkeep
  schema/README.md
  schema/fixtures/.gitkeep
  Makefile
  .gitignore
  .editorconfig
  ```
- **pyproject:**
  - `[build-system] hatchling`; `[project] name="cc-supervisor"`, `requires-python=">=3.12"`, `dependencies=[]`
  - `[project.scripts] ccs="ccs.cli:main"`
  - `[project.optional-dependencies] dev=["pytest>=8","ruff>=0.6","mypy>=1.11"]`
  - `[tool.hatch.build.targets.wheel] packages=["src/ccs"]`
- **Tool config** (in pyproject):
  - ruff: `line-length=100`, `target-version="py312"`, select `E,F,I,UP,B,SIM,RUF`
  - ruff format: defaults
  - mypy: `strict=true`, `files=["src/ccs"]`
  - pytest: `testpaths=["tests"]`, `addopts="-q"`, marker `live` (real claude; skipped unless `CCS_TEST_LIVE=1`)
- **Dev environment:**
  - `make install-dev` runs `pipx install --editable ./python --force`. That puts `ccs` on `~/.local/bin` for manual testing.
  - A dev venv, `python/.venv`, is used for pytest/ruff/mypy: `python3 -m venv python/.venv && python/.venv/bin/pip install -e './python[dev]'`. Target `make venv`.
- **Fake claude protocol** (skeleton now; each later plan extends it):
  - The scenario file path comes from the env `FAKE_CLAUDE_SCENARIO`. It is JSON keyed by mode.
  - Dispatch on argv: `agents --json`, `auth status --json`, `auth login`, `-p … --input-format stream-json` (control requests), `-p <prompt>` (warm-up), `--version`, and default (interactive TUI mode).
  - Unknown modes exit 2 with a message.
  - Every invocation appends `{argv, env subset, cwd}` to `$FAKE_CLAUDE_LOG` (a JSON line) so tests can assert calls.
- **XcodeGen:**
  - `name: CCSupervisor`, `options.deploymentTarget.macOS: "26.0"`, `SWIFT_VERSION: 6.0`, `SWIFT_STRICT_CONCURRENCY: complete`
  - Targets:
    - `CCSupervisor` (application, bundle id `local.ccsupervisor.app`, `PRODUCT_NAME: "CC Supervisor"`)
    - `CCSupervisorWidgets` (app-extension, `local.ccsupervisor.app.widgets`, embedded in the app)
    - `CCSupervisorTests` (unit-test bundle, empty test)
  - Signing: `CODE_SIGN_IDENTITY: "-"`, `CODE_SIGN_STYLE: Manual`, no `DEVELOPMENT_TEAM`.
- **Makefile targets** (`.PHONY`, `SHELL := /bin/bash`):
  - `venv`: creates `python/.venv` with dev extras.
  - `install-dev`: runs pipx editable install.
  - `test`: `python/.venv/bin/pytest python/tests`, plus `xcodebuild test` when `macos/` has test sources and `SKIP_SWIFT` is unset.
  - `test-live`: `CCS_TEST_LIVE=1 pytest -m live`.
  - `lint`: `ruff check`, `ruff format --check`, `mypy`.
  - `fmt`: `ruff format` and `ruff check --fix`.
  - `xcodegen-check`: `command -v xcodegen || { echo "xcodegen missing: brew install xcodegen"; exit 1; }`.
  - `project`: `xcodegen-check`, then `cd macos && xcodegen generate`.
  - `app`: `project`, then `xcodebuild -project macos/CCSupervisor.xcodeproj -scheme CCSupervisor -configuration Release -derivedDataPath build/xcode build`, then `ditto` the built app to `~/Applications/CC Supervisor.app`.
  - `clean`: removes `build/`, caches, and the generated xcodeproj.
- **.gitignore:**
  - `macos/*.xcodeproj/`, `build/`, `DerivedData/`
  - `python/.venv/`, `.venv/`, `__pycache__/`, `*.pyc`
  - `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
  - `spikes/`, `.DS_Store`, `*.egg-info/`, `dist/`
- **.editorconfig:** utf-8, lf, final newline. 4 spaces for py/swift, 2 for yml/json/md, tabs for Makefile.

## Tasks
- [x] Create the tree above with minimal contents.
- [x] `python/pyproject.toml` with project metadata, scripts, dev extras, and ruff/mypy/pytest config.
- [x] `ccs/cli.py`: `main(argv: list[str] | None = None) -> int`. `--version` prints `ccs <version>`.
- [x] `tests/test_cli_version.py`: `main(["--version"])` prints the version and returns 0.
- [x] `tests/fake_claude/claude`: argv dispatch skeleton, scenario loading, call log. It is executable (`chmod +x`).
- [x] `tests/conftest.py`: `tmp_xdg` and `fake_claude_path` fixtures. The `fake_claude(scenario: dict)` fixture writes the scenario file and returns the env.
- [x] `tests/test_fake_claude.py`: the `--version` scenario output, the call-log line written, and exit 2 for an unknown mode.
- [x] `macos/project.yml` plus the placeholder Swift, plists, and entitlements. `make project app` builds successfully.
- [x] `Makefile` with all targets. Each fails with a clear message if a tool is missing (`pipx`, `xcodegen`, `xcodebuild`).
- [x] `.gitignore`, `.editorconfig`, `schema/README.md` (purpose: JSON contracts between Python and Swift, ADR-0001/0005; how to add a schema).
- [x] Verify `make venv test lint` passes on a clean clone.

## Tests
- `pytest`: CLI version, fake claude dispatch and call log.
- Build smoke: `make app` completes and the app launches, showing the menu bar item "CC Supervisor" (manual check once).

## Manual pages to update
- `01-installation.md`: prerequisites (Xcode 26, `brew install xcodegen`, pipx, Python ≥ 3.12) and `make` targets for developers (keep `Status: planned` until P13).

## Done when
- [x] `make venv test lint` passes.
- [x] `make install-dev && ccs --version` prints `ccs 0.1.0`.
- [x] `make app-build` builds the app (ad-hoc signed; the widget is embedded). The `~/Applications` install step (`make app`) exists but was not run, per the user's no-changes constraint. See Result.
- [x] The generated xcodeproj and build artifacts are ignored by git.

## Risks & mitigations
- **Ad-hoc signing breaks the widget embed build:** this plan only needs it to compile. Runtime loading is P00-S1/P12.
- **pyenv shims plus pipx pick the wrong interpreter:** `make install-dev` passes `--python "$(command -v python3)"` and prints the interpreter used.
- **Swift 6 strict concurrency errors in the template:** keep the placeholders `@MainActor`-annotated and minimal.

## Result
- **Shipped:**
  - Tree per Design: `python/` package with `ccs --version`, fake `claude` (modes version/agents/auth_status/auth_login/stream/print/interactive, call log, unknown → exit 2), `tests/conftest.py` (`tmp_xdg`, `fake_claude_path`, `fake_claude(scenario)` → `FakeClaude(path, env, log_path).calls()`, `live` marker auto-skip), `macos/` XcodeGen skeleton (app + widget + tests), `schema/README.md`, `Makefile`, `.gitignore`, `.editorconfig`.
  - Also `python/README.md`, which the pyproject `readme` field needs.
- **Verification:**
  - `make venv test lint`: 7 Python tests pass; ruff and mypy `--strict` are clean; the Swift `CCSupervisorTests` pass via `make test-swift`.
  - `make install-dev`: `~/.local/bin/ccs --version` → `ccs 0.1.0`. It uses `/opt/homebrew/opt/python@3.14/bin/python3.14`.
  - `make app-build`: builds `build/xcode/Build/Products/Release/CC Supervisor.app` with the widget `.appex` embedded, both ad-hoc signed. The widget entitlements (sandbox + home-relative read-only exception) and the app Info.plist (`LSUIElement`, `ccsupervisor` URL scheme) are verified with `codesign`/`plutil`.
- **Deviations:**
  - Split `make app` into `app-build` (build only) and `app` (build + `ditto` to `~/Applications`). The install step and the manual launch check weren't run: the user asked for no machine changes during autonomous implementation. The install is exercised later by P13 `make install`.
  - `PY` resolves the real interpreter via `python3 -c 'import sys; print(sys.executable)'`, because `PYENV_VERSION=2.7.18` makes `command -v python3` return a pyenv shim.
  - The Makefile adds `test-python`, `test-swift`, `xcodebuild-check`, and `help` targets, plus explicit `-destination` flags to silence the multiple-destination warning.
  - XcodeGen writes `App/Info.plist`, `App/App.entitlements`, `Widgets/Info.plist`, and `Widgets/Widgets.entitlements` from `project.yml`. They are committed, and `project.yml` is the source of truth.
  - `brew install xcodegen` was run (2.46.0).
- **For next plans:**
  - Run tests with `make test` (or `SKIP_SWIFT=1 make test` for Python only), lint with `make lint`.
  - Python tools live in `python/.venv/bin/` (pytest, ruff, mypy).
  - Tests import helpers via `from conftest import FakeClaude`.
- **Follow-ups:** none.
