.PHONY: check test install-frontend install-backend run-backend run-frontend

check:
	python scripts/check_repository.py

test:
	python -m unittest discover -s tests

install-frontend:
	cd haiheliuyubaoyuagent-master/chainlitexam && python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

install-backend:
	cd haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp && python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

run-backend:
	bash scripts/run_mcp_backend.sh

run-frontend:
	bash scripts/run_chainlit_frontend.sh
