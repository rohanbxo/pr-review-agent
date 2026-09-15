# Developer entry points. On Windows without make, run the commands on the right directly.
PY ?= backend/.venv/Scripts/python.exe
ifeq ($(OS),)
PY = backend/.venv/bin/python
endif

.PHONY: test test-backend test-eval eval-data eval-data-verify eval-baseline eval-sample

test: test-backend test-eval

test-backend:
	cd backend && $(abspath $(PY)) -m pytest -q

test-eval:
	$(PY) -m pytest -q eval/tests

# Regenerate the full splits (injected/reverted/clean/v1) from git history at the SHAs pinned in
# eval/data/manifest.json, and fail unless the bytes match the pinned hashes. No GitHub API calls;
# first run clones ~15 public repos into eval/.cache/.
eval-data:
	$(PY) -m eval.build_dataset --verify

# Re-pin every repo to its current default branch and rewrite manifest.json (changes the dataset).
eval-data-refresh:
	$(PY) -m eval.build_dataset --refresh

eval-baseline: eval/data/v1.jsonl
	$(PY) -m eval.run_eval --baseline --dataset eval/data/v1.jsonl --report eval/reports/baseline.json

# CI smoke: the committed 10-case sample through the baseline scorer; needs no clones.
eval-sample:
	$(PY) -m eval.run_eval --baseline --dataset eval/data/sample.jsonl --report eval/reports/sample-baseline.json

eval/data/v1.jsonl:
	$(MAKE) eval-data
