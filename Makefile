.PHONY: install run test e2e lint fmt docker up

install:
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

run:
	.venv/bin/uvicorn app.main:create_app --factory --reload --port 8000

test:
	.venv/bin/pytest -q

# Live check of all 7 escalation scenarios. Needs two servers running; see scripts/e2e_scenarios.py.
e2e:
	.venv/bin/python scripts/e2e_scenarios.py

lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

fmt:
	.venv/bin/ruff check . --fix && .venv/bin/ruff format .

docker:
	docker build -t oncallpilot . && docker run --rm -p 8000:8000 --env-file .env oncallpilot

up:
	docker compose up --build
