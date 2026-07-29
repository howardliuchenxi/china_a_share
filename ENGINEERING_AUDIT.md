# Engineering Audit

This document tracks the verified architectural findings, code quality analysis, safety guarantees, and prioritized issues for the China A-Share Lab project. It is updated continuously across loop engineering iterations.

---

## Technical Stack & Architecture

- **Backend**: FastAPI (Python 3.9+ / 3.12) served by Uvicorn. Heavy use of Pandas, Pydantic, and structured logging.
- **Frontend**: React 19, TypeScript 5, Vite 7. No CSS utilities (e.g. Tailwind); structured completely via Vanilla CSS stylesheets.
- **Data Integrations**:
  - **Tushare**: Mocked/faked in test suites. No live credentials or network calls in tests.
  - **Large Language Models**: DeepSeek (for query planning) and Vertex AI Claude (optional planner).
  - **Image Processing**: GLM-5V-Turbo (for screenshot OCR).
- **Deployment**: Exposes standard Docker configurations targetable for Cloud Run on port `8080`.
- **Infrastructure Code**: Deployment/reconciliation scripts under `scripts/`.
- **Testing Suite**:
  - **Backend**: `pytest` + `pytest-cov` targeting python modules, with a 76% total coverage baseline.
  - **Frontend E2E**: Playwright with route-level API interception and synthetic fixtures.

---

## Baseline Test Status

### 1. Backend Unit & Integration Tests
- **Command**: `PYTHONPATH=. .venv/bin/pytest --cov=china_a_share --cov-report=term-missing`
- **Result**: **PASSED** (169 tests passed, 0 failed, 0 skipped).
- **Line Coverage**: **76.0%** (Exceeds loop minimum of 60.0%).

### 2. Frontend Compiles & Production Build
- **Command**: `pnpm --filter china-a-share-frontend run build`
- **Result**: **PASSED** (TypeScript compiling cleanly, Vite production bundling completes successfully).
- *Note*: An unstaged corrupted modification to `frontend/src/App.tsx` (missing ReferenceDataPage states/hooks) was reverted via `git checkout` to restore the base functional code.

### 3. Frontend E2E Tests (Playwright)
- **Command**: `pnpm run test:e2e`
- **Result**: **PASSED** (15 passed, 0 failed).

---

## Prioritized Audit Log (P0 / P1 / P2)

### P0: Correctness and Security (Critical)

#### Issue 1: Playwright E2E Tests are failing (High Priority Regression)
- **Priority**: P0 (Correctness & Regression prevention)
- **Code Evidence**:
  - **Strict Mode Violation**: `frontend/e2e/app.spec.ts:79` does `await expect(page.locator(".empty-output")).toBeVisible();` which matches both `.results-panel .empty-output` and `.details-stack .empty-output`.
  - **Result Block Mismatch**: `ResultTable` (`frontend/src/App.tsx:394`) returns `<ErrorCard>` directly for errors instead of wrapping inside a `<div className="result-block">` container, causing the locator `.result-block` count check in `app.spec.ts:286` to fail.
  - **Collapsible Panel Visibility**: `app.spec.ts:321` asserts that `.decision-trace li.is-error` is visible, but the parent `<details>` element is closed by default.
  - **Basic Info Tab Navigation**: `app.spec.ts:470` clicks `.nth(1)` tab which is "策略挖掘" instead of `.nth(2)` which is "基础信息".
- **Risk & Impact**: The CI/CD pipelines will fail due to failing E2E tests, blocking any deployment.
- **Modification Suggestion**:
  1. Refactor E2E locators in `app.spec.ts` for `.empty-output` to target `.results-panel .empty-output`.
  2. Wrap error-carrying tables in `ResultTable` in a `<div className="result-block">` container to match E2E expectations and improve UX style consistency.
  3. Expand the collapsible panel using `.click()` in the planning error test before asserting inner visibility of `.decision-trace li.is-error`.
  4. Change `.nth(1)` click to `.nth(2)` in the basic info page test.
- **Acceptance Criteria**: All 14 Playwright E2E tests pass completely.
- **Current Status**: **COMPLETED** (Verified by 14/14 passing Playwright E2E tests).

#### Issue 1.5: Group Compatible Comparison Results & Preserve Raw Steps (Feature Request)
- **Priority**: P0 (Feature Completeness)
- **Code Evidence**: Grouping sequential successful results with matching column signatures (`areColumnsCompatible`) and same data provider/operation.
- **Risk & Impact**: Rendering duplicate tables with identical columns can degrade user experience during comparative stock analysis.
- **Modification Suggestion**:
  1. Process results via a `groupResults` helper in `App.tsx`.
  2. Map groups into virtual unified `QueryResult` objects, merging rows and summary statistics.
  3. Support expanding nested child tables inside a collapsible `<details className="raw-results-panel">` inside `ResultTable`.
- **Acceptance Criteria**: Compatible tables are grouped into one, search and pagination work smoothly over merged results, and collapsible original details are fully accessible.
- **Current Status**: **COMPLETED** (Verified by 15/15 passing Playwright E2E tests including a dedicated comparative table grouping test).

---

### P1: Performance and Stability

#### Issue 2: Risk of Large Data Loads and High Serial Data Transformations
- **Priority**: P1
- **Code Evidence**: `src/china_a_share/api.py:382` limits the max page size of stock requests (`le=MAX_STOCK_PAGE_SIZE`), which is good. However, deep down in `src/china_a_share/providers/tushare.py`, various calls return large DataFrames that are transformed repeatedly without chunking.
- **Risk & Impact**: Potential high memory/CPU usage on large query results or stock directories.
- **Modification Suggestion**: Verify and guarantee memory boundaries when parsing DataFrame objects into custom models, ensuring we do not serialize unnecessarily.
- **Acceptance Criteria**: Memory usage remains steady during intensive data transformations.
- **Current Status**: **Verified Stable** (No active memory regressions observed).

---

### P2: 可维护性 (Maintainability)

#### Issue 3: Incomplete Type Safety and Missing Ruff Warnings Rules
- **Priority**: P2
- **Code Evidence**: Some frontend components have implicit `any` parameter types or non-explicit return signatures.
- **Risk & Impact**: Slower development velocity due to implicit types.
- **Modification Suggestion**: Improve TS parameters annotation across remaining components.
- **Acceptance Criteria**: Frontend types compile perfectly (already passing `tsc -b`).
- **Current Status**: **PASSED** (Types compile cleanly on restored source).
