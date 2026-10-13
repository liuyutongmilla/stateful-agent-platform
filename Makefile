.PHONY: setup dev-backend dev-frontend test loadtest build

setup:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd frontend && npm install

dev-backend:
	cd backend && .venv/bin/python -m app.main

dev-frontend:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/python -m pytest

loadtest:
	cd backend && .venv/bin/python loadtest/run.py

build:
	cd frontend && npm run build
