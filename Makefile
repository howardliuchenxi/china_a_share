SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PROJECT_ID := china-a-share-lab
REGION := asia-east2
SERVICE := china-a-share-lab
ANALYSIS_JOB := china-a-share-analysis-worker
ANALYSIS_JOB_TIMEOUT := 2h
SERVICE_URL := https://china-a-share-lab-1079739428171.asia-east2.run.app
RUNTIME_SERVICE_ACCOUNT := china-a-share-runner@china-a-share-lab.iam.gserviceaccount.com
CACHE_BUCKET := china-a-share-lab-cache-asia-east2
SOURCE_BUCKET := run-sources-china-a-share-lab-asia-east2
TARGET_BRANCH := main
ADMIN_EMAIL := howardliuchenxi1@gmail.com
GOOGLE_OAUTH_CLIENT_ID := 1079739428171-f572obvnhd989gh04onlpp5fmai9cf5q.apps.googleusercontent.com
GITHUB_FIX_REPO := howardliuchenxi/china_a_share
RELEASE_MESSAGE ?= Release application changes
DEPLOYMENT_INVENTORY_MESSAGE := Record production deployment
DEPLOY_VERIFY_ATTEMPTS := 6
DEPLOY_VERIFY_DELAY_SECONDS := 5

GCLOUD := $(shell command -v gcloud 2>/dev/null || { test -x "$$HOME/google-cloud-sdk/bin/gcloud" && printf '%s' "$$HOME/google-cloud-sdk/bin/gcloud"; })
CLOUDSDK_PYTHON := $(shell \
	for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 \
		"$$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"; do \
		command -v "$$candidate" >/dev/null 2>&1 || test -x "$$candidate" || continue; \
		"$$candidate" -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 14)))' 2>/dev/null \
			&& { command -v "$$candidate" 2>/dev/null || printf '%s' "$$candidate"; break; }; \
	done)

.PHONY: help check live-check deploy merge release

help:
	printf '%s\n' \
		'make check   Build the frontend and run backend tests.' \
		'make live-check  Run the unified 100-case live matrix and regressions.' \
		'make deploy  Deploy the clean, pushed main commit and update the GCP inventory.' \
		'make merge   Validate, merge the current clean branch into main, and push main.' \
		'make release Merge the current branch into main, then deploy that exact commit.'

check:
	npm --prefix frontend run build
	.venv/bin/python -m pytest

live-check:
	RUN_LIVE_ANALYSIS=1 .venv/bin/python -m pytest tests/test_live_analysis.py -v -s

deploy: check
	test -n "$(GCLOUD)" || { echo "Google Cloud CLI was not found. Install gcloud or place it at ~/google-cloud-sdk/bin/gcloud." >&2; exit 1; }
	test -n "$(CLOUDSDK_PYTHON)" || { echo "gcloud requires Python 3.10-3.14. Install a compatible Python and retry." >&2; exit 1; }
	test -z "$$(git status --porcelain)" || { echo "The working tree must be clean before deployment." >&2; exit 1; }
	current_branch="$$(git branch --show-current)"; \
	test "$$current_branch" = "$(TARGET_BRANCH)" || { echo "Deployments must run from $(TARGET_BRANCH), not $$current_branch." >&2; exit 1; }
	git fetch origin "$(TARGET_BRANCH)"
	local_sha="$$(git rev-parse HEAD)"; \
	remote_sha="$$(git rev-parse origin/$(TARGET_BRANCH))"; \
	test "$$local_sha" = "$$remote_sha" || { echo "Local $(TARGET_BRANCH) must exactly match origin/$(TARGET_BRANCH) before deployment." >&2; exit 1; }
	CLOUDSDK_PYTHON="$(CLOUDSDK_PYTHON)" "$(GCLOUD)" run deploy "$(SERVICE)" \
		--source . \
		--project "$(PROJECT_ID)" \
		--region "$(REGION)" \
		--allow-unauthenticated \
		--cpu 1 \
		--memory 1Gi \
		--min 0 \
		--max 1 \
		--concurrency 4 \
		--timeout 300 \
		--service-account "$(RUNTIME_SERVICE_ACCOUNT)" \
		--set-env-vars TUSHARE_CACHE_BUCKET="$(CACHE_BUCKET)",GOOGLE_CLOUD_PROJECT="$(PROJECT_ID)",CLOUD_RUN_REGION="$(REGION)",ANALYSIS_JOB_NAME="$(ANALYSIS_JOB)",APP_GIT_BRANCH="$(TARGET_BRANCH)",APP_GIT_SHA="$$(git rev-parse HEAD)",ADMIN_EMAIL="$(ADMIN_EMAIL)",GOOGLE_OAUTH_CLIENT_ID="$(GOOGLE_OAUTH_CLIENT_ID)",GITHUB_FIX_REPO="$(GITHUB_FIX_REPO)" \
		--set-secrets TUSHARE_TOKEN=tushare-token:latest,DEEPSEEK_API_KEY=deepseek-api-key:latest,ZAI_API_KEY=zai-api-key:latest,GITHUB_FIX_TOKEN=github-fix-token:latest \
		--quiet
	image="$$(CLOUDSDK_PYTHON="$(CLOUDSDK_PYTHON)" "$(GCLOUD)" run services describe "$(SERVICE)" \
		--project "$(PROJECT_ID)" \
		--region "$(REGION)" \
		--format='value(spec.template.spec.containers[0].image)')"; \
	test -n "$$image" || { echo "The deployed service did not expose a container image." >&2; exit 1; }; \
	CLOUDSDK_PYTHON="$(CLOUDSDK_PYTHON)" "$(GCLOUD)" run jobs deploy "$(ANALYSIS_JOB)" \
		--image "$$image" \
		--project "$(PROJECT_ID)" \
		--region "$(REGION)" \
		--command python \
		--args=-m,china_a_share.worker \
		--tasks 1 \
		--parallelism 1 \
		--max-retries 1 \
		--task-timeout "$(ANALYSIS_JOB_TIMEOUT)" \
		--cpu 1 \
		--memory 4Gi \
		--service-account "$(RUNTIME_SERVICE_ACCOUNT)" \
		--set-env-vars TUSHARE_CACHE_BUCKET="$(CACHE_BUCKET)",GOOGLE_CLOUD_PROJECT="$(PROJECT_ID)",CLOUD_RUN_REGION="$(REGION)",ANALYSIS_JOB_NAME="$(ANALYSIS_JOB)",APP_GIT_BRANCH="$(TARGET_BRANCH)",APP_GIT_SHA="$$(git rev-parse HEAD)" \
		--set-secrets TUSHARE_TOKEN=tushare-token:latest,DEEPSEEK_API_KEY=deepseek-api-key:latest,ZAI_API_KEY=zai-api-key:latest \
		--quiet
	CLOUDSDK_PYTHON="$(CLOUDSDK_PYTHON)" "$(GCLOUD)" run jobs add-iam-policy-binding "$(ANALYSIS_JOB)" \
		--project "$(PROJECT_ID)" \
		--region "$(REGION)" \
		--member "serviceAccount:$(RUNTIME_SERVICE_ACCOUNT)" \
		--role roles/run.jobsExecutorWithOverrides \
		--quiet
	CLOUDSDK_PYTHON="$(CLOUDSDK_PYTHON)" "$(GCLOUD)" storage buckets update "gs://$(CACHE_BUCKET)" \
		--lifecycle-file infra/cache-lifecycle.json \
		--project "$(PROJECT_ID)" \
		--quiet
	revision="$$(CLOUDSDK_PYTHON="$(CLOUDSDK_PYTHON)" "$(GCLOUD)" run services describe "$(SERVICE)" \
		--project "$(PROJECT_ID)" \
		--region "$(REGION)" \
		--format='value(status.latestReadyRevisionName)')"; \
	traffic=""; \
	for ((attempt = 1; attempt <= $(DEPLOY_VERIFY_ATTEMPTS); attempt++)); do \
		traffic="$$(CLOUDSDK_PYTHON="$(CLOUDSDK_PYTHON)" "$(GCLOUD)" run services describe "$(SERVICE)" \
			--project "$(PROJECT_ID)" \
			--region "$(REGION)" \
			--format='value(status.traffic.percent)')"; \
		test "$$traffic" = "100" && break; \
		sleep "$(DEPLOY_VERIFY_DELAY_SECONDS)"; \
	done; \
	test "$$traffic" = "100" || { echo "Deployment verification failed: latest traffic is $$traffic%, expected 100%." >&2; exit 1; }; \
	curl --fail --silent --show-error "$(SERVICE_URL)/api/health"; \
	printf '\n'; \
	source_size="$$(CLOUDSDK_PYTHON="$(CLOUDSDK_PYTHON)" "$(GCLOUD)" storage du --summarize "gs://$(SOURCE_BUCKET)" --project "$(PROJECT_ID)" | awk '{print $$1}')"; \
	cache_size="$$(CLOUDSDK_PYTHON="$(CLOUDSDK_PYTHON)" "$(GCLOUD)" storage du --summarize "gs://$(CACHE_BUCKET)" --project "$(PROJECT_ID)" | awk '{print $$1}')"; \
	.venv/bin/python scripts/update_deployment_inventory.py \
		--revision "$$revision" \
		--source-size "$$source_size" \
		--cache-size "$$cache_size" \
		--git-branch "$(TARGET_BRANCH)" \
		--git-sha "$$(git rev-parse HEAD)"; \
	printf 'Deployed %s with 100%% traffic: %s\n' "$$revision" "$(SERVICE_URL)"

merge:
	test -z "$$(git status --porcelain)" || { echo "The working tree must be clean before merging." >&2; exit 1; }
	$(MAKE) check
	git fetch origin "$(TARGET_BRANCH)"
	current_branch="$$(git branch --show-current)"; \
	test -n "$$current_branch" || { echo "Detached HEAD cannot be merged." >&2; exit 1; }; \
	test "$$current_branch" != "$(TARGET_BRANCH)" || { echo "Run make merge from the feature branch, not $(TARGET_BRANCH)." >&2; exit 1; }; \
	if git show-ref --verify --quiet "refs/heads/$(TARGET_BRANCH)"; then \
		git switch "$(TARGET_BRANCH)"; \
		git merge --ff-only "origin/$(TARGET_BRANCH)"; \
	else \
		git switch --track -c "$(TARGET_BRANCH)" "origin/$(TARGET_BRANCH)"; \
	fi; \
	git merge --no-ff "$$current_branch" -m "Merge branch '$$current_branch' into $(TARGET_BRANCH)"; \
	git push origin "$(TARGET_BRANCH)"; \
	printf 'Merged %s into %s and pushed origin/%s.\n' "$$current_branch" "$(TARGET_BRANCH)" "$(TARGET_BRANCH)"

release:
	$(MAKE) check
	.venv/bin/python scripts/validate_release_files.py
	git add -A
	git diff --cached --check
	git diff --cached --quiet || git commit -m "$(RELEASE_MESSAGE)"
	current_branch="$$(git branch --show-current)"; \
	test -n "$$current_branch" || { echo "Detached HEAD cannot be released." >&2; exit 1; }; \
	if test "$$current_branch" = "$(TARGET_BRANCH)"; then \
		git fetch origin "$(TARGET_BRANCH)"; \
		git merge-base --is-ancestor "origin/$(TARGET_BRANCH)" HEAD || { echo "Local $(TARGET_BRANCH) has diverged from origin/$(TARGET_BRANCH)." >&2; exit 1; }; \
		git push origin "$(TARGET_BRANCH)"; \
	else \
		$(MAKE) merge; \
	fi
	$(MAKE) deploy
	.venv/bin/python scripts/validate_release_files.py \
		--allowed-only docs/gcp-resources.md
	git add docs/gcp-resources.md
	git diff --cached --quiet || git commit -m "$(DEPLOYMENT_INVENTORY_MESSAGE)"
	git push origin "$(TARGET_BRANCH)"
	test -z "$$(git status --porcelain)" || { echo "Release completed but the working tree is not clean." >&2; exit 1; }
