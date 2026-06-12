.PHONY: test check lint build full deploy-sync deploy-check clean-artifacts

test:
	pytest -x -q

check:
	mypy scripts/ tests/

lint:
	ruff check scripts/ tests/

deploy-sync:
	python3 scripts/sync.py

deploy-check:
	python3 scripts/sync.py --check

clean-artifacts:
	@find scripts/ tests/ -type f \( -name '*,cover' -o -name '*.cover' \) -delete 2>/dev/null || true
	@rm -f .coverage .coverage.* 2>/dev/null || true
	@echo "artifacts cleaned"

build: deploy-sync
	python3 -c "import scripts.run_autopilot; import scripts.self_improving_loop; import scripts.meta_autopilot; import scripts.autocode_config; import scripts.kpi; import scripts.sync"

full: deploy-check lint check test
