VENV_PYTHON ?= ../../venv/bin/python
PYTHON ?= $(VENV_PYTHON)
PYTHONPATH ?= src
PYTHONDONTWRITEBYTECODE ?= 1
export PYTHONDONTWRITEBYTECODE
MARTINCALL_BIND_ADDRESS ?= 127.0.0.1
UI_GATE_TESTS := \
	tests/test_ui_service_imports.py \
	tests/test_router_error_payloads.py \
	tests/test_background_tasks.py \
	tests/test_chart_history.py \
	tests/test_history_repair.py \
	tests/test_market_analysis_worker.py \
	tests/test_market_analysis_job.py \
	tests/test_market_analysis_store.py \
	tests/test_market_router.py \
	tests/test_storage_router.py \
	tests/test_reference_router.py \
	tests/test_gex_router.py \
	tests/test_telegram_router.py \
	tests/test_system_router.py \
	tests/test_ibkr_router.py \
	tests/test_ticks_router.py \
	tests/test_analysis_db.py \
	tests/test_tick_services.py \
	tests/test_api_smoke.py

.PHONY: check-python test test-postgres lint test-ui-gates storage-preflight smoke-api docker-up tick-feed health readiness run

check-python:
	@$(PYTHON) -c 'import sys, sysconfig; actual=sys.version_info[:2]; free_threaded=bool(sysconfig.get_config_var("Py_GIL_DISABLED")); sys.exit(0 if actual == (3, 14) and not free_threaded else f"Standard-GIL Python 3.14 is required; got {sys.version.split()[0]} (free-threaded={free_threaded})")'

test: check-python
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest -q

test-postgres: check-python
	@if [ -z "$$AEF_DATABASE_URL" ]; then echo "AEF_DATABASE_URL is required for test-postgres" >&2; exit 2; fi
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest tests/test_instrument_storage_contract.py tests/test_paper_postgres.py -q

lint: check-python
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m ruff check --no-cache .
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m ruff format --check --no-cache .

test-ui-gates: check-python
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest $(UI_GATE_TESTS) -q

storage-preflight: check-python
	@if [ -z "$$AEF_DATABASE_URL" ]; then echo "AEF_DATABASE_URL is required for storage-preflight" >&2; exit 2; fi
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/check_storage_runtime_contract.py

smoke-api: check-python
	PYTHON_BIN=$(PYTHON) bash scripts/smoke_api.sh

docker-up:
	./martincall-server.sh start

tick-feed:
	./martincall-server.sh tick-feed

health:
	curl -s http://127.0.0.1:8000/api/health

readiness:
	curl -fsS http://127.0.0.1:8000/api/ready

run: check-python
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m uvicorn aef_terminal.ui.app:app --host $(MARTINCALL_BIND_ADDRESS) --port 8000 --no-access-log
