# Zcode Handoff

Snapshot date: 2026-09-19 (America/Los_Angeles).

This document records the non-obvious state that cannot be recovered safely from
the repository structure alone. It intentionally contains no credential values.

## Executive summary

There are two independent bodies of work in this checkout:

1. Ten commits on `codex/technical-pattern-studies` add U.S. market-data routing,
   model-routing changes, and a new Feishu strategy scanner/interactive workflow.
   The strategy feature is not deployed and should not be treated as production
   ready without further correction.
2. Five tracked files contain uncommitted deterministic-planning fixes for unlock
   rankings and earnings-forecast growth rankings. These changes are a work in
   progress. Targeted unit tests pass, but the complete pre-push gate and required
   live regressions have not been run.

Do not merge this branch wholesale into `main`. At this snapshot the branch is 61
commits behind `main` and 10 commits ahead, and a read-only merge-tree inspection
shows many overlapping/conflicting paths. Port the wanted behavior onto a fresh
branch from current `main` in small, reviewed units.

## 1. Work in progress

### Uncommitted deterministic analysis work

The tracked working-tree changes are:

- `live_cases.json`
- `src/china_a_share/application/workflow.py`
- `src/china_a_share/capabilities.py`
- `tests/test_components.py`
- `tests/test_execution_answer_fields.py`

Their intended behavior is:

- Compile market-wide unlock rankings at security grain, aggregating all unlock
  tranches per security before sorting and limiting.
- Make the previously timeless live prompt explicit:
  `列出2026年解禁股数占总股本比例最高的10只股票`.
- Compile H1 earnings-forecast growth rankings deterministically, retain only the
  latest disclosure per company, rank the positive `p_change_max` bound, and join
  company names from `stock_basic`.
- Audit bounded `forecast` reads as exact `ann_date` fan-out rather than treating
  them as one provider range query.
- Avoid misclassifying audited date-fan-out queries as security-fan-out templates.
- Recognize a complete weekday membership constraint as covered by the enclosing
  native date range while rejecting sparse membership.

Current diff size is 544 additions and 14 deletions across those five files.

Status: **WIP, not release-ready**.

Evidence completed on this snapshot:

- `git diff --check`: passed.
- `pytest -q tests/test_components.py tests/test_execution_answer_fields.py`:
  256 passed.

Still required before treating the work as complete:

- Reconcile the changes onto current `main`; the present branch is stale.
- Review why unlock-ranking construction exists in both `_compile_intent` and
  `_compile_known_unlock_ranking`; keep both only if the two entry paths genuinely
  require duplicate construction.
- Recheck the weekday-membership equivalence against the intended constraint
  semantics and exchange holidays.
- Run the complete `make pre-push` gate on the exact candidate commit.
- Run the exact live cases for `share_unlock_boundaries-03` and
  `earnings_guidance-02` against the configured real model and provider, as required
  by `AGENTS.md` for production-reported prompts. No current live run satisfies this.

### Duplicate safety stash

`stash@{0}` is named `On main: preserve pre-existing unlock ranking work` and was
created on 2026-08-16. It contains a 306-line subset of the current working changes:
the unlock-ranking compiler, the weekday-membership validator, related tests, and
the dated live-case prompt.

The working tree includes that subset plus later forecast-ranking and fan-out work.
Do not pop the stash into this checkout or a fresh `main`; doing so would duplicate
changes and create conflicts. Keep it only as a safety backup until the working
changes have been preserved and reviewed, then it is safe to drop deliberately.

### Committed strategy branch work

The six strategy-specific commits are `df0f7f82` through `61d1feb2`. They add:

- strategy models and Cloud Storage persistence;
- QFQ data loading and rule evaluation;
- Feishu strategy cards and draft interactions;
- a scheduled daily-scan HTTP endpoint;
- notification deduplication and retry-oriented state recording;
- a Cloud Scheduler resource entry.

Targeted tests passed on this snapshot:

- `pytest -q tests/test_strategy_engine.py tests/test_strategy_integration.py
  tests/test_strategy_api_and_states.py tests/test_feishu_strategy.py`: 14 passed.

These tests do not establish production readiness. Known correctness and security
problems are listed below.

## 2. Untracked scripts

The following files are disposable repair/smoke scripts, not product assets:

- `fix_test.py` and `fix_test2.py` mechanically repaired test arrays in
  `tests/test_strategy_engine.py`.
- `fix_test3.py` through `fix_test5.py` iteratively patched the strategy API/state
  tests.
- `fix_test6.py` through `fix_test10.py` iteratively added and repaired Feishu
  trigger, `@all` fallback, and at-least-once delivery tests. Some intermediate
  scripts contain invalid or abandoned code; the corrected test implementations
  are already present in tracked test files.
- `test_tushare_cal.py` and `test_tushare_daily.py` are ad hoc live smoke probes.
  They print provider output and are not pytest-quality tests. Their app bootstrap
  path also requires the configured GCS cache and is unsuitable as a standalone
  token check.

None contains unique behavior that must be retained. After preserving this handoff,
all 12 can be deleted rather than committed. A direct read-only Tushare probe was
run separately and is recorded under External state.

## 3. Recommended next plan

1. Start from current `main`, not from a merge of this branch.
2. Preserve the five tracked WIP files as a dedicated safety commit or patch before
   changing branches. Do not include the 12 temporary scripts.
3. Port and finish the deterministic unlock/forecast work first. It is narrower,
   already has focused unit coverage, and is independent of the strategy feature.
4. Run the exact two live cases, then `make pre-push`, before pushing that unit.
5. Reassess the strategy product contract and fix the P0/P1 issues below before
   porting only the strategy-specific code onto a fresh branch from `main`.
6. Keep current `main`'s mature Feishu research agent, push-trigger deployment,
   sandbox, query-shape metadata, and seven-hour worker configuration. Do not choose
   stale branch versions during conflict resolution.
7. Reconcile `docs/gcp-resources.md` only after the strategy resource decision. The
   `main` version is much newer than this branch's version but still omits the paused
   strategy Scheduler and has a stale top-level latest-revision row.

The old `.loop/backlog.md`, `.loop/handoff.md`, and README roadmap describe July-era
work and are not the current milestone. No additional reliable oral product decision
or hidden user commitment is available in the current Codex conversation context.

Recommended branch decision: let Zcode first preserve the five-file analysis WIP,
then create fresh branches from current `main` for (a) the analysis fix and (b) any
strategy work. Do not continue development or merge from
`codex/technical-pattern-studies` directly.

## 4. Known issues and failed assumptions

### Strategy feature: P0

- The daily-scan endpoint accepts any `Authorization: Bearer ...` value after the
  administrator-token check fails. It does not validate the Google OIDC token,
  issuer, audience, or scheduler identity. Because the Cloud Run service is public,
  this endpoint is externally triggerable by an arbitrary bearer string.
- `StrategyScanner.run_daily_scan` catches a market-data loading exception and
  returns normally. The API consequently returns success and Cloud Scheduler cannot
  retry that failure, despite commit messages claiming exact failure propagation.
- QFQ adjustment factors use dataframe-wide `ffill()` after sorting by security and
  date. Missing values can cross a security boundary; fill must be grouped by
  `ts_code` or rejected.

### Strategy feature: P1

- Rule evaluation raises on an individual security with insufficient history. A
  newly listed security can therefore fail an entire full-market strategy instead
  of being reported as ineligible.
- The Cloud Storage deduplication path is a non-atomic exists-then-write sequence.
  Concurrent invocations can both send. A crash after send and before mark can also
  duplicate delivery. This is at-least-once behavior, not strict exactly-once
  notification.
- Interactive creation is only a partial state machine. The card asks the user to
  reply with strategy details, but normal message routing does not complete those
  draft fields; save fills a preset when data is missing.
- Result rows use the security code as `stock_name`; no `stock_basic` name join is
  implemented.
- The full-market synchronous scan has not been load-tested against the 300-second
  web request timeout. The branch commit message's performance claim is not backed
  by a recorded benchmark.
- Strategy code is absent from current `main` and from the deployed production
  revision. Existing passing unit tests exercise only the stale feature branch.

### Branch integration

- The branch diverged before substantial Feishu agent, sandbox, U.S. provider, and
  deployment work landed on `main`.
- Read-only `git merge-tree` inspection reports many changed-in-both and
  added-in-both paths, including `Makefile`, `docs/gcp-resources.md`, provider files,
  Feishu code, and tests.
- The first four branch-only commits semantically overlap later `main` work but are
  not patch-identical. Prefer current `main` behavior and port only missing strategy
  functionality.

### Earlier smoke-test failure

Running the two ad hoc Tushare scripts inside the restricted sandbox first failed
while trying to refresh Google credentials for the persistent GCS cache. This was an
environment/network limitation, not evidence of an invalid Tushare token. A direct
Tushare probe outside that cache path succeeded afterward.

## 5. External state

### Local credentials

The local `.env` contains these keys: `TUSHARE_TOKEN`, `DEEPSEEK_API_KEY`,
`ZAI_API_KEY`, `TUSHARE_CACHE_BUCKET`, `GOOGLE_CLOUD_PROJECT`, and
`GOOGLE_CLOUD_LOCATION`. It does not contain local Feishu credentials.

The machine has an active `gcloud` user login for project `china-a-share-lab`.
No separate Codex-only application secret was found. Codex process variables contain
no extra Tushare, model-provider, Feishu, Massive, Finnhub, or GitHub secret.

### Tushare

The local token was valid at this snapshot for direct read-only calls:

- `trade_cal`: 6 rows for 2023-01-01 through 2023-01-10 open sessions;
- `daily`: 5,065 rows for 2023-01-10;
- `adj_factor`: 5,156 rows for 2023-01-10.

The exact account points, per-interface quota, rate limit, and any commercial expiry
were not exposed by the client and remain unverified. Successful calls prove current
token validity and permission for only those tested endpoints.

### Feishu

Production Cloud Run binds `FEISHU_APP_ID` and the three Feishu application secrets
from Secret Manager. Recent Cloud Run request logs show repeated 200 responses from
`/api/integrations/feishu/events` during the last 72 hours, with no matching
`feishu_research_turn_failed`, `feishu_interactive_card_failed`, or
`strategy_daily_scan_failed` event found in the same query window.

This proves that the existing production research callback is active. It does not
prove the new strategy UI works: strategy code is not deployed, no strategy callback
was production-tested, and the strategy Scheduler is paused.

### Google Cloud live state

Read-only `gcloud` inspection found:

- Project: `china-a-share-lab`, region `asia-east2`.
- Production service: `china-a-share-lab`, latest ready revision
  `china-a-share-lab-00260-c8w`, 100% traffic.
- Deployed application source: `main@f8efb2fb140abcf4c8cca7c2d2db9c8a890c45be`.
  Current `main` is one later documentation-only commit, so the deployed application
  code is current for non-documentation changes.
- Runtime: 1 vCPU, 1 GiB, concurrency 4, 300-second request timeout, CPU throttling
  disabled, zero minimum instances, and service-level maximum scale 1.
- Worker: `china-a-share-analysis-worker`, 1 vCPU, 4 GiB, one task, one retry,
  25,200-second timeout, synchronized to the same deployed source SHA.
- Private research sandbox: live as `china-a-share-research-sandbox`; it is documented
  on current `main` but absent from this stale branch's inventory.
- `china-a-share-deploy-main-push` is enabled for pushes to `main`.
- `china-a-share-reconcile-main` is paused intentionally after push-trigger
  deployment replaced polling.
- `china-a-share-strategy-daily-scan` exists with OIDC, weekday 16:30
  Asia/Shanghai scheduling, and the intended strategy endpoint, but is **PAUSED**.
- The project also contains unrelated `video-factory` resources; do not treat them as
  part of this application's inventory.

Inventory discrepancies:

- This branch's `docs/gcp-resources.md` is substantially stale and incorrectly marks
  both Scheduler jobs as enabled.
- Current `main` documents the research sandbox, seven-hour worker, push trigger,
  and paused reconciliation Scheduler, but its top-level latest revision still says
  `00257-scz`; its change log correctly records `00260-c8w`.
- Current `main` does not include the newly created, paused strategy Scheduler.

No cloud resource was mutated during this handoff audit.

## Safe cleanup after preservation

After the five-file WIP is safely committed or patched and this handoff is retained:

- delete the 12 untracked repair/smoke scripts;
- drop `stash@{0}` only after confirming the preserved WIP contains the desired
  unlock changes;
- leave both Scheduler jobs paused until the strategy endpoint is securely ported,
  deployed, and verified.
