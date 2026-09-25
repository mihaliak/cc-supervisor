SHELL := /bin/bash
.DEFAULT_GOAL := help

# resolve the real interpreter behind pyenv shims (ADR-0013: Python >= 3.12)
PY          ?= $(shell python3 -c 'import sys; print(sys.executable)' 2>/dev/null)
VENV        := python/.venv
VENV_BIN    := $(VENV)/bin
XCODEPROJ   := macos/CCSupervisor.xcodeproj
DERIVED     := build/xcode
APP_NAME    := CC Supervisor.app
APP_BUILT   := $(DERIVED)/Build/Products/Release/$(APP_NAME)
# Build number = commit count, so every upgrade gets a new CFBundleVersion and macOS
# refreshes its cached app icon (Notification Center, widget gallery).
BUILD_NUMBER := $(shell git rev-list --count HEAD 2>/dev/null || echo 1)
APP_DEST    := $(HOME)/Applications/$(APP_NAME)

.PHONY: help icon screenshots venv install-dev test test-python test-swift test-live lint fmt \
        xcodegen-check xcodebuild-check project app-build app clean \
        prereqs install uninstall upgrade

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

$(VENV_BIN)/pytest:
	@test -n "$(PY)" || { echo "python3 missing (need >= 3.12)"; exit 1; }
	$(PY) -m venv $(VENV)
	$(VENV_BIN)/pip install --quiet --upgrade pip
	$(VENV_BIN)/pip install --quiet -e './python[dev]'

venv: $(VENV_BIN)/pytest ## create python/.venv with dev tools (pytest, ruff, mypy)

install-dev: ## pipx editable install of ccs into ~/.local/bin
	@command -v pipx >/dev/null || { echo "pipx missing: brew install pipx"; exit 1; }
	@echo "using interpreter: $(PY)"
	pipx install --editable ./python --force --python "$(PY)"

test: test-python test-swift ## run all tests

test-python: venv ## run python tests
	$(VENV_BIN)/pytest python/tests

test-swift: ## run Swift unit tests (skip with SKIP_SWIFT=1)
	@if [ -n "$(SKIP_SWIFT)" ]; then echo "SKIP_SWIFT set: skipping Swift tests"; \
	elif ! ls macos/Tests/*.swift >/dev/null 2>&1; then echo "no Swift tests"; \
	else $(MAKE) --no-print-directory project && \
	  xcodebuild -project $(XCODEPROJ) -scheme CCSupervisor -configuration Debug \
	    -derivedDataPath $(DERIVED) -destination 'platform=macOS,arch=arm64' -quiet test; fi

test-live: venv ## run tests against the real claude (CCS_TEST_LIVE=1)
	CCS_TEST_LIVE=1 $(VENV_BIN)/pytest python/tests -m live

lint: venv ## ruff check + format check + mypy
	$(VENV_BIN)/ruff check python
	$(VENV_BIN)/ruff format --check python
	cd python && ../$(VENV_BIN)/mypy

fmt: venv ## ruff format + autofix
	$(VENV_BIN)/ruff format python
	$(VENV_BIN)/ruff check --fix python

xcodegen-check:
	@command -v xcodegen >/dev/null || { echo "xcodegen missing: brew install xcodegen"; exit 1; }

xcodebuild-check:
	@command -v xcodebuild >/dev/null || { echo "xcodebuild missing: install Xcode 26"; exit 1; }

project: xcodegen-check ## generate macos/CCSupervisor.xcodeproj
	cd macos && xcodegen generate --quiet

app-build: project xcodebuild-check ## build the app (Release, ad-hoc signed) into build/xcode
	xcodebuild -project $(XCODEPROJ) -scheme CCSupervisor -configuration Release \
	  -derivedDataPath $(DERIVED) -destination 'generic/platform=macOS' -quiet \
	  CURRENT_PROJECT_VERSION=$(BUILD_NUMBER) build

app: app-build ## build and install to ~/Applications/CC Supervisor.app
	mkdir -p "$(HOME)/Applications"
	rm -rf "$(APP_DEST)"
	ditto "$(APP_BUILT)" "$(APP_DEST)"
	@bash scripts/lsclean.sh "$(APP_DEST)"
	@# macOS keeps the old widget extension process running after an update; restart it so
	@# the new widget code (and its Edit Widget options) is used right away.
	@killall CCSupervisorWidgets 2>/dev/null || true
	@echo "installed: $(APP_DEST)"

icon: ## regenerate the app icon PNGs + SVG from macos/Branding/render-icon.swift
	swift macos/Branding/render-icon.swift

screenshots: venv ## regenerate README screenshots from an anonymized demo env (docs/assets/screenshots)
	@PY="$(abspath $(VENV_BIN))/python" bash scripts/screenshots/run.sh

prereqs: ## check install prerequisites (macOS/Xcode 26, xcodegen, python >= 3.12, pipx)
	@PY="$(PY)" sh scripts/check-prereqs.sh

install: prereqs ## install ccs + app + daemon (never touches ~/.claude* dirs)
	$(MAKE) --no-print-directory install-dev
	$(MAKE) --no-print-directory app
	@PY="$(PY)" bash scripts/install.sh

upgrade: ## reinstall ccs + app, restart daemon and app, refresh statusline scripts
	$(MAKE) --no-print-directory install-dev
	$(MAKE) --no-print-directory app
	@PY="$(PY)" bash scripts/upgrade.sh

uninstall: ## remove daemon, app, login item, ccs (YES=1 no prompts; PURGE=1 also config/state)
	@PY="$(PY)" YES="$(YES)" PURGE="$(PURGE)" bash scripts/uninstall.sh

clean: ## remove build outputs and caches
	rm -rf build $(XCODEPROJ) python/.pytest_cache python/.mypy_cache python/.ruff_cache
	find python -name __pycache__ -type d -prune -exec rm -rf {} +
