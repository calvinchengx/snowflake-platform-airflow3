# The platform, driven from a product repo:  make up PRODUCT=../my-product
#
# PRODUCT is a PATH, not a name. This Makefile contains no product identifier,
# which is the property that makes "a second product can use this unchanged" a
# fact rather than an aspiration.
SHELL := /bin/bash
PRODUCT ?= ./product
SOURCES ?= ../contoso-sources
PROJECT ?= airflow-snowflake
export PRODUCT_ABS := $(abspath $(PRODUCT))
export SOURCES_ABS := $(abspath $(SOURCES))
export PRODUCT_NAME := $(notdir $(PRODUCT_ABS))
include versions.env
export

FRAGMENT := .sources.generated.yml
# Where Airflow 3's simple auth manager writes the credential it generates.
PASSWORD_FILE := /opt/airflow/simple_auth_manager_passwords.json.generated
COMPOSE := PRODUCT=$(PRODUCT_ABS) PRODUCT_NAME=$(PRODUCT_NAME) SOURCES=$(SOURCES_ABS) PWD=$(CURDIR) \
           docker compose -p $(PROJECT) -f docker-compose.yml -f $(FRAGMENT)

.PHONY: help up down logs connections creds doctor sources trigger unpause verify pin manifest test lint
help: ## This list
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t20

up: doctor sources pin manifest ## Build the worker from the product's pyproject.toml and start the stack
	@echo "platform: product = $(PRODUCT_ABS)"
	@echo "platform: sources = $(SOURCES_ABS)"
	$(COMPOSE) up --build -d
	@echo "platform: Airflow on http://localhost:$${AIRFLOW_PORT:-18084}"

sources: ## Generate the compose fragment for the vendors a sources repo declares
	@test -f "$(SOURCES_ABS)/sources.yaml" || { \
	  echo "no sources.yaml at $(SOURCES_ABS) -- the product's vendors cannot be started"; exit 1; }
	@python3 scripts/sources.py "$(SOURCES_ABS)/sources.yaml" "$(SOURCES_ABS)" > $(FRAGMENT)
	@echo "platform: $$(python3 -c "import json;print(len(json.load(open('$(FRAGMENT)'))['services']))") vendor(s) declared"

trigger: ## Trigger a DAG and return immediately:  make trigger DAG=contoso_daily
	$(COMPOSE) exec -T airflow airflow dags trigger $(DAG)

unpause: ## Let a DAG be scheduled:  make unpause DAG=contoso_daily
# ITS OWN TARGET so `verify` can point at one short command instead of printing
# the whole expanded compose invocation, and so that letting a DAG schedule
# itself stays a thing someone chose to do.
	@test -n "$(DAG)" || { echo "usage: make unpause DAG=<dag_id>"; exit 2; }
	$(COMPOSE) exec -T airflow airflow dags unpause $(DAG)

# How long `verify` waits for a run, and how often it looks.
VERIFY_TIMEOUT ?= 3600
VERIFY_POLL ?= 15
# How long it waits for the DAG to EXIST before saying it does not. A stack
# that was just brought up has an empty metadata database, and the dag
# processor's first scan takes tens of seconds; asking sooner is asking early,
# not asking about a missing DAG.
VERIFY_PARSE_WAIT ?= 300

verify: ## Run a DAG and FAIL if it fails:  make verify DAG=contoso_daily
# WHAT `trigger` ONLY LOOKED LIKE IT DID. Its help said "and wait" and it
# returned the moment the run was queued, so NOTHING IN THIS REPOSITORY EVER
# EXITED NON-ZERO BECAUSE A PIPELINE FAILED. Every green this platform reported
# rested on a person reading run state by hand afterwards. DoD 3 asks for green
# THROUGH the orchestrator; a command that cannot go red does not establish it.
#
# IT WATCHES ITS OWN RUN, by an explicit --run-id. A scheduled run of the same
# DAG can be in flight at the same moment -- that happened while this platform
# was being witnessed -- and "the most recent run" would then be the other one,
# reporting a verdict for a pipeline this command did not start.
#
# IT ADOPTS A RUN ALREADY IN FLIGHT, when there is exactly one. A fresh stack
# starts a catch-up run of its own the moment the DAG is unpaused and scanned,
# and with max_active_runs 1 that run owns the only slot. This used to refuse
# and point at `make kill-runs`, which was honest and made every unattended run
# fail, because acceptance builds a fresh stack every time. The hatch could not
# simply be scripted either: the catch-up run only appears after the scan this
# target waits for, and killing it marks database state without stopping the
# worker's processes, so the trigger that followed would race a half-finished
# run for the same tables.
#
# Adopting is the STRONGER witness, not the weaker one. It is the same DAG on
# the same data from the same empty catalog, and the SCHEDULER dispatched it
# rather than a hand, which is closer to what DoD 3 asks for. The verdict is
# still for one explicit run-id, found before anything is triggered, so "the
# most recent run" never decides. Two or more in flight is the one case nobody
# can adopt, and that still refuses.
#
# Ported from fabric-platform-airflow3, where it was measured end to end: the
# adopted run SUCCEEDED in 13 minutes, against 45+ without finishing before the
# stack was gated. G47.
#
# IT DOES NOT UNPAUSE, deliberately. Unpausing changes the DAG's schedule and
# starts a catch-up run ALONGSIDE this one: two runs writing the same tables,
# which is how a witness stops being one. Refusing with the command printed is
# the smaller surprise.
	@test -n "$(DAG)" || { echo "usage: make verify DAG=<dag_id>"; exit 2; }
# IT WAITS FOR THE DAG TO EXIST, and that is not politeness. `make up` recreates
# the metadata database, so for the first tens of seconds afterwards EVERY DAG
# is absent -- and this check used to call that "no DAG called X", a hard exit 1
# that reads as a broken DAG. It cost three witness runs in one evening. The
# distinction it now makes is the one that matters: an IMPORT ERROR is reported
# immediately, because waiting cannot fix a traceback; absence alone is retried
# until VERIFY_PARSE_WAIT, because the scan may simply not have happened.
	@waited=0; \
	  while :; do \
	    paused=$$($(COMPOSE) exec -T airflow airflow dags list -o plain 2>/dev/null \
	      | awk -v d="$(DAG)" '$$1 == d {print $$4}'); \
	    test -n "$$paused" && break; \
	    errs=$$($(COMPOSE) exec -T airflow airflow dags list-import-errors -o plain 2>/dev/null \
	      | grep -vE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' \
	      | grep -v '^No data found' | grep -v '^filepath' | head -5); \
	    test -z "$$errs" || { \
	      echo "a DAG file cannot be parsed, so $(DAG) will never appear:"; \
	      echo "$$errs"; exit 1; }; \
	    test $$waited -lt $(VERIFY_PARSE_WAIT) || { \
	      echo "no DAG called $(DAG) after $(VERIFY_PARSE_WAIT)s, and no import"; \
	      echo "error to explain it -- check the dag bundle is mounted:  make logs"; \
	      exit 1; }; \
	    test $$waited -gt 0 || echo "platform: waiting for $(DAG) to be scanned"; \
	    sleep $(VERIFY_POLL); waited=$$((waited + $(VERIFY_POLL))); \
	  done; \
	  test "$$paused" = "False" || { \
	    echo "$(DAG) is paused, so a triggered run would sit queued forever."; \
	    echo "unpause it deliberately -- it starts a catch-up run as well:"; \
	    echo "  make unpause DAG=$(DAG)"; \
	    exit 1; }; \
	  busy=$$($(COMPOSE) exec -T airflow airflow dags list-runs $(DAG) -o plain 2>/dev/null \
	    | awk '$$3 == "running" || $$3 == "queued" {print $$2}' | head -3); \
	  nbusy=$$(echo "$$busy" | grep -c .); \
	  test "$$nbusy" -le 1 || { \
	    echo "$(DAG) has $$nbusy runs in flight, and max_active_runs is 1:"; \
	    for r in $$busy; do echo "  $$r"; done; \
	    echo "which of them is the witness is not this command's to guess, and"; \
	    echo "two runs writing one catalog is not a witness either. wait, or:"; \
	    echo "  make kill-runs DAG=$(DAG)"; \
	    exit 1; }; \
	  if test -n "$$busy"; then \
	    run="$$busy"; \
	    echo "platform: $(DAG) -> adopting the run already in flight, $$run"; \
	  else \
	    run="verify__$$(date -u +%Y%m%dT%H%M%SZ)"; \
	    echo "platform: $(DAG) -> $$run"; \
	    $(COMPOSE) exec -T airflow airflow dags trigger $(DAG) --run-id "$$run" >/dev/null; \
	  fi; \
	  waited=0; \
	  while :; do \
	    state=$$($(COMPOSE) exec -T airflow airflow dags list-runs $(DAG) -o plain 2>/dev/null \
	      | awk -v r="$$run" '$$2 == r {print $$3}'); \
	    case "$$state" in \
	      success) echo "platform: $$run SUCCEEDED"; exit 0;; \
	      failed) break;; \
	    esac; \
	    test $$waited -lt $(VERIFY_TIMEOUT) || { \
	      echo "platform: $$run is still $${state:-unqueued} after $(VERIFY_TIMEOUT)s."; \
	      echo "giving up WITHOUT a verdict -- this is not a pass:  make logs"; \
	      exit 1; }; \
	    sleep $(VERIFY_POLL); waited=$$((waited + $(VERIFY_POLL))); \
	  done; \
	  echo "platform: $$run FAILED."; \
	  echo "the ones that FAILED are the cause; the rest were blocked behind them:"; \
	  $(COMPOSE) exec -T postgres psql -U airflow -d airflow -t -A -c \
	    "select case when state = 'failed' then '  FAILED   ' else '  blocked  ' end \
	            || task_id from task_instance \
	     where dag_id='$(DAG)' and run_id='$$run' and coalesce(state,'x') <> 'success' \
	     order by (state = 'failed') desc, task_id;" 2>/dev/null || true; \
	  echo "logs:  make logs"; \
	  exit 1

kill-runs: ## Mark every in-flight run of a DAG failed:  make kill-runs DAG=contoso_daily
# THE ESCAPE HATCH `verify` POINTS AT. A fresh stack starts a CATCH-UP run of
# its own the moment the metadata database is created -- nobody unpaused
# anything -- and with max_active_runs 1 that run owns the only slot. Every
# later trigger queues behind it, which is what "still unqueued after 3600s"
# actually meant, three times in one evening.
#
# Deliberately not `dags backfill --reset-dagruns` or a pause: this only ends
# runs that are already in flight, so a witness starts from a quiet DAG
# without changing the schedule that produced them.
	@test -n "$(DAG)" || { echo "usage: make kill-runs DAG=<dag_id>"; exit 2; }
	@$(COMPOSE) exec -T postgres psql -U airflow -d airflow -q -c \
	  "update task_instance set state='failed' \
	    where dag_id='$(DAG)' and state in ('running','queued','scheduled','deferred'); \
	   update dag_run set state='failed', end_date=now() \
	    where dag_id='$(DAG)' and state in ('running','queued');" >/dev/null
	@echo "platform: in-flight runs of $(DAG) ended"

down: sources ## Stop and remove everything, volumes included
# `sources` FIRST, because COMPOSE names the generated fragment and docker
# compose refuses to run without it. A fresh clone -- or anyone who cleaned it
# up -- could not tear a stack down at all, which is the worst moment to
# discover a missing file.
	$(COMPOSE) down -v

logs: ## Follow the Airflow logs
	$(COMPOSE) logs -f airflow

connections: ## Show the connections the product can ask for by name
	$(COMPOSE) exec airflow airflow connections list

# NOT CONFIGURED ANYWHERE, and that is the point. This platform sets no Airflow
# admin credential, so Airflow 3's simple auth manager generates one on first
# start and writes it under AIRFLOW_HOME. Pinning a password in docker-compose
# would put a working credential in the repository for every consumer of this
# platform at once, and a default that everyone knows is not a login.
#
# The startup log prints it too, but only once -- by the time anyone needs it,
# it is thousands of lines back. Reading the file is the reliable path.
#
# IT CHANGES WHEN THE CONTAINER DOES. The file lives in the container, not in a
# volume, so `make down` (which takes volumes with it) and then `make up` issues
# a NEW password. That is the usual reason a saved one stops working.
creds: ## The Airflow admin login for this stack
	@$(COMPOSE) exec -T airflow test -f $(PASSWORD_FILE) 2>/dev/null || { \
	  echo "no generated password at $(PASSWORD_FILE)."; \
	  echo "is the stack up? try: make up"; exit 1; }
	@echo "url:      http://localhost:$${AIRFLOW_PORT:-18084}"
	@$(COMPOSE) exec -T airflow python3 -c "import json;d=json.load(open('$(PASSWORD_FILE)'));[print(f'user:     {u}\npassword: {p}') for u, p in d.items()]"

doctor: ## Refuse to start against a product that cannot work
	@test -d "$(PRODUCT_ABS)" || { echo "no product at $(PRODUCT_ABS)"; exit 1; }
	@test -f "$(PRODUCT_ABS)/pyproject.toml" || { \
	  echo "$(PRODUCT_ABS) has no pyproject.toml -- the worker would install nothing"; exit 1; }
	@test -d "$(PRODUCT_ABS)/dags" || { \
	  echo "$(PRODUCT_ABS) has no dags/ -- the bundle would be empty"; exit 1; }
	@grep -q "^\[build-system\]" "$(PRODUCT_ABS)/pyproject.toml" || { \
	  echo "$(PRODUCT_ABS)/pyproject.toml has no [build-system] -- it declares"; \
	  echo "  dependencies but is not an installable package, so its own modules"; \
	  echo "  would be missing from the worker and every DAG importing them would"; \
	  echo "  fail at run time with ModuleNotFoundError."; exit 1; }
	@echo "platform: $(PRODUCT_NAME) provides pyproject.toml and dags/"

pin: ## Refuse to run a product whose client comes from a different release
# THE PIN THE PLATFORM CANNOT SEE. versions.env pins the emulator IMAGE; the
# product pins the client WHEEL, and those live in two repositories. Nothing in
# either one alone can see the pair -- `make up` is the moment it exists,
# because the platform has been pointed at a product. A workspace binary and a
# client that disagree about the contract is the one mismatch a consumer
# repository exists to notice. Same check `snowflake-platform-tasks` runs.
	@python3 scripts/check_product_pin.py "$(PRODUCT_ABS)"

manifest: ## Build the dbt manifests cosmos renders the graph from
# NOT OPTIONAL, and not housekeeping. Cosmos's default is to run `dbt ls` at
# parse time; when it cannot find dbt -- which it never can in the worker, where
# dbt lives in a task venv -- it falls back SILENTLY to a deprecated parser that
# knows a subset of what dbt knows. A DAG rendered without the contracts looks
# exactly like a DAG that passes them (G29).
#
# The product owns the builder because the manifest describes the PRODUCT's
# models; the platform only makes sure it has been run before the stack starts.
	@test -f "$(PRODUCT_ABS)/scripts/manifest.py" || { 	  echo "$(PRODUCT_ABS) ships no scripts/manifest.py -- cosmos would fall back"; 	  echo "  to a parser that drops the contracts and render a healthy-looking DAG"; 	  exit 1; }
	cd "$(PRODUCT_ABS)" && uv run --frozen --group dbt python scripts/manifest.py

test: ## Repo-boundary tests (no Docker)
	uv run --frozen --group dev python -m pytest tests -q

lint: ## Lint this repository's own scripts and tests
# The PRODUCT's code is linted in the product repository. What is left here is
# the platform: scripts/ and tests/, and neither imports anything third-party.
	uv run --frozen --group dev python -m ruff check .
