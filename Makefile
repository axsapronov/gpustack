# Detect operating system
ifeq ($(OS),Windows_NT)
    PLATFORM_SHELL := powershell
    SCRIPT_EXT := .ps1
    SCRIPT_DIR := hack/windows
else
    PLATFORM_SHELL := /bin/bash
    SCRIPT_EXT := .sh
    SCRIPT_DIR := hack
endif

# Borrowed from https://stackoverflow.com/questions/18136918/how-to-get-current-relative-directory-of-your-makefile
curr_dir := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

# Borrowed from https://stackoverflow.com/questions/2214575/passing-arguments-to-make-run
rest_args := $(wordlist 2, $(words $(MAKECMDGOALS)), $(MAKECMDGOALS))

$(eval $(rest_args):;@:)

# List targets based on script extension and directory
ifeq ($(OS),Windows_NT)
    targets := $(shell powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -Path $(curr_dir)/$(SCRIPT_DIR) | Select-Object -ExpandProperty BaseName")
else
	targets := $(shell ls $(curr_dir)/$(SCRIPT_DIR) | grep $(SCRIPT_EXT) | sed 's/$(SCRIPT_EXT)$$//')
endif

$(targets):
	@$(eval TARGET_NAME=$@)
ifeq ($(PLATFORM_SHELL),/bin/bash)
	$(curr_dir)/$(SCRIPT_DIR)/$(TARGET_NAME)$(SCRIPT_EXT) $(rest_args)
else
	powershell -NoProfile -ExecutionPolicy Bypass "$(curr_dir)/$(SCRIPT_DIR)/$(TARGET_NAME)$(SCRIPT_EXT) $(rest_args)"
endif

help:
	#
	# Usage:
	#
	#   * [dev] `make install`, install development tools, like uv, pre-commit hooks and so on.
	#
	#   * [dev] `make deps`, prepare all dependencies.
	#
	#   * [dev] `make generate`, generate codes.
	#
	#   * [dev] `make lint`, check style.
	#
	#   * [dev] `make test`, execute unit testing.
	#
	#   * [dev] `make build`, execute building.
	#
	#   * [dev] `make build-docs`, build docs, not supported on Windows.
	#
	#   * [dev] `make serve-docs`, serve docs, not supported on Windows.
	#
	#   * [ci]  `make package`, build container images, not supported on Windows.
	#
	#   * [ci]  `make ci`, execute `make install`, `make deps`, `make lint`, `make test`, `make build`.
	#
	@echo



GRAFANA_MONITORING_DIR := docker-compose/grafana/grafana_dashboards
GRAFANA_DASHBOARD_JSONS := \
	$(GRAFANA_MONITORING_DIR)/gpustack-lb.json
GRAFANA_DASHBOARD_JSONNETS := \
	$(GRAFANA_MONITORING_DIR)/gpustack-lb.jsonnet
GRAFANA_SCRIPTS := $(GRAFANA_MONITORING_DIR)/scripts
NODE_MODULES_STAMP := node_modules/.install-stamp
DASHBOARDS_STAMP := $(GRAFANA_MONITORING_DIR)/.dashboards.stamp

install-dashboards: $(NODE_MODULES_STAMP)

$(NODE_MODULES_STAMP): package.json package-lock.json
	npm ci
	@touch $(NODE_MODULES_STAMP)

dashboards: install-dashboards $(GRAFANA_DASHBOARD_JSONS) validate-dashboard

$(DASHBOARDS_STAMP): $(GRAFANA_DASHBOARD_JSONNETS) package.json package-lock.json $(GRAFANA_SCRIPTS)/*.mjs $(NODE_MODULES_STAMP)
	npm run dashboard:generate
	@touch $(DASHBOARDS_STAMP)

$(GRAFANA_DASHBOARD_JSONS): $(DASHBOARDS_STAMP)
	@:

validate-dashboard: $(GRAFANA_DASHBOARD_JSONS) install-dashboards
	npm run dashboard:validate && npm run dashboard:test

.DEFAULT_GOAL := build
.PHONY: $(targets)
