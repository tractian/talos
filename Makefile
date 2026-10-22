.PHONY: install dev test lint weights demo-plc demo-proxy demo-agent clean

PY ?= python3
VENV ?= .venv

install:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -e ".[onnx,modbus]"

dev:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -e ".[dev,onnx,http,modbus]"

test:
	$(VENV)/bin/python -m pytest -q

lint:
	$(VENV)/bin/ruff check .

weights:
	$(VENV)/bin/talos-pull --dest weights

demo-plc:
	$(VENV)/bin/python examples/fake_plc.py

demo-proxy:
	$(VENV)/bin/talos-proxy --upstream 127.0.0.1:15020 --listen 15502 --world examples/world.json --weights weights --report-every 200 --agent dashboard=127.0.0.1:1:hmi --agent prober=127.0.0.1:2:copilot --agent flood=127.0.0.1:3:copilot --agent drifter=127.0.0.1:4:optimizer

demo-agent:
	$(VENV)/bin/python examples/bad_agent.py --target 127.0.0.1:15502 --script $(or $(SCRIPT),flood)

clean:
	rm -rf build dist *.egg-info .pytest_cache talos-ledger.jsonl
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
