# A-Share Laboratory

A local laboratory for exploring mainland China A-share data with natural
language, deterministic safety checks, and transparent upstream results.

The project is designed for data experiments rather than trade execution or
investment advice. It currently covers equities listed on the Shanghai,
Shenzhen, and Beijing exchanges.

## Data foundation

The first market-data provider is Tushare Pro. Its stock-data catalog defines
the current upstream API boundary:

- **Official stock OpenAPI documentation:**
  [Tushare Pro Stock Data](https://tushare.pro/document/2?doc_id=14)
- Supported security suffixes: `.SH`, `.SZ`, and `.BJ`
- Current local allowlist: 108 stock-data API names
- Upstream permission, quota, and service errors are preserved for inspection

The official catalog includes security masters, trading calendars, historical
and real-time prices, valuation metrics, financial statements, company events,
shareholder data, margin data, money flow, and market-behavior datasets.

## What the laboratory does

Enter a request such as:

```text
How many A-share stocks rose or fell on July 17, 2026?
```

The application will:

1. interpret the request with DeepSeek;
2. produce a provider-neutral structured query plan;
3. validate the plan against the selected provider's operation catalog and the
   A-share boundary;
4. call the selected market-data provider from the local Python backend;
5. compute supported local summaries;
6. display the plan, source rows, and any upstream errors in the browser.

## Architecture

```mermaid
flowchart LR
    UI["Local React UI"] --> API["FastAPI backend"]
    API --> WORKFLOW["Provider-neutral workflow"]
    WORKFLOW --> PLANNER_PORT["QueryPlanner port"]
    PLANNER_PORT --> PLANNER["DeepSeek adapter"]
    PLANNER --> VALIDATOR["A-share plan validator"]
    VALIDATOR --> PROVIDER_PORT["MarketDataProvider port"]
    PROVIDER_PORT --> EXECUTOR["Tushare adapter"]
    EXECUTOR --> L1["Bounded in-memory cache"]
    L1 --> L2["Cloud Storage cache"]
    L2 --> TUSHARE["Tushare Pro stock APIs"]
    EXECUTOR --> LOCAL["Local summaries"]
    TUSHARE --> UI
    LOCAL --> UI
```

DeepSeek does not call Tushare directly. It only returns a JSON query plan
containing provider-neutral operation names, parameters, requested fields,
purposes, and optional controlled aggregations. The local backend validates and
executes that plan. Market-data result rows are not sent back to DeepSeek.

The application core depends on two replaceable ports:

- `QueryPlanner` translates natural language into a `QueryPlan`; DeepSeek is
  the current adapter.
- `MarketDataProvider` publishes an operation catalog and executes `DataQuery`
  objects; Tushare is the current adapter.

Provider-specific HTTP payloads, credentials, errors, operation names, and
cache-expiration rules remain inside their adapters. Adding another model or
data provider therefore does not require changing the API or orchestration
workflow. Provider selection is currently fixed during application startup;
runtime selection can be added later without changing these contracts.

## Current capabilities

| Area | Examples |
| --- | --- |
| Security reference | Listed stocks, company information, name changes, IPOs |
| Market data | Daily, weekly, monthly, adjusted prices, limits, suspensions |
| Valuation | Turnover, PE, PB, total market value, circulating market value |
| Financials | Income statement, balance sheet, cash flow, financial indicators |
| Company events | Dividends, repurchases, pledges, share unlocks |
| Market behavior | Top lists, block trades, margin data, money flow |
| Local summaries | Controlled numeric counts such as advanced, declined, and unchanged |
| Failure inspection | Original safe DeepSeek and Tushare error bodies |

Most Tushare APIs use one generic provider adapter. The transport, token use,
operation catalog, market validation, result conversion, and error handling are
local code. DeepSeek currently decides which operation to use and which
parameters and fields to request.

## Configuration

Create a local environment file:

```bash
cp .env.example .env
```

Add the three credentials and the private cache bucket:

```dotenv
TUSHARE_TOKEN=your_real_token
DEEPSEEK_API_KEY=your_real_key
ZAI_API_KEY=your_real_zai_key
TUSHARE_CACHE_BUCKET=your_private_cache_bucket
```

The `.env` file is ignored by Git. Credentials are read by the backend and are
never included in browser responses.

The web backend requires Cloud Storage access for persistent Tushare caching.
For local runs, authenticate Application Default Credentials before starting
the server:

```bash
gcloud auth application-default login
```

Successful market-data responses are cached by provider name, operation name,
normalized parameters, ordered fields, and cache schema version. Including the
provider prevents collisions when another data source is added. The
process-local L1 cache is bounded to 256 records and 128 MiB. The persistent L2
cache stores gzip-compressed JSON objects and survives Cloud Run scale-to-zero
events and deployments. Persistent Tushare caching is explicitly profiled for
every allowlisted operation:

- real-time and current intraday operations bypass both cache layers;
- reference data and unbounded latest disclosures refresh every 24 hours;
- the trading calendar refreshes every 30 days;
- fixed historical daily, intraday, and disclosure windows persist for 90 days;
- current end-of-day data refreshes around its documented publication window.

The Tushare adapter calculates expiration in `Asia/Shanghai`. Adding an
allowlisted Tushare operation without a cache profile fails fast at startup and
in tests, so future interfaces cannot silently inherit an unsafe default.
Upstream and cache errors are never stored as successful responses.

## Installation

Create the Python environment and install the backend:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Install and build the frontend:

```bash
cd frontend
pnpm install
pnpm run build
cd ..
```

## Run locally

Start the combined production-style application:

```bash
a-share-web
```

Open [http://127.0.0.1:8000/analysis](http://127.0.0.1:8000/analysis) for
data analysis or [http://127.0.0.1:8000/basic](http://127.0.0.1:8000/basic) for
reference data. The root path redirects to `/analysis`.

For frontend development, keep the backend running and start Vite separately:

```bash
cd frontend
pnpm run dev
```

Open [http://127.0.0.1:5173/analysis](http://127.0.0.1:5173/analysis) or
[http://127.0.0.1:5173/basic](http://127.0.0.1:5173/basic). Vite proxies
`/api` to the Python backend on port `8000`.

## Deploy to Google Cloud Run

The repository includes a multi-stage `Dockerfile`. It builds the React
frontend and serves the resulting files from the same FastAPI container, so a
deployment exposes only one HTTPS service.

The verified live-resource inventory, IAM boundaries, lifecycle policies, and
cost posture are maintained in
[`docs/gcp-resources.md`](docs/gcp-resources.md).

Recommended initial Cloud Run settings:

- Region: `asia-east2` (Hong Kong)
- Request-based billing
- 1 vCPU and 1 GiB memory
- Minimum instances: 0
- Maximum instances: 1
- Concurrency: 4
- Request timeout: 300 seconds
- Health endpoint: `/api/health`

Store `TUSHARE_TOKEN`, `DEEPSEEK_API_KEY`, and `ZAI_API_KEY` in Secret
Manager and expose them to the service as environment variables. Configure
`TUSHARE_CACHE_BUCKET` as a plain environment variable because it is a resource
identifier, not a secret.
Do not upload `.env`; it is excluded from Git, the Docker build context, and the
`gcloud` source upload.

### Routine delivery commands

Use the repository Makefile for validated delivery:

```bash
make check
make deploy
make merge
make release
```

`make deploy` accepts only a clean local `main` whose commit exactly matches
`origin/main`. It builds the frontend, runs the backend test suite, deploys that
commit to the existing Cloud Run service, records the full Git commit in the
service and worker environments, updates the asynchronous analysis Cloud Run
Job to the same immutable image, reapplies its job-scoped IAM and task lifecycle
policy, verifies 100% traffic and the public health endpoint, and updates
`docs/gcp-resources.md` with the live revision, Git source, and storage usage.

`make merge` must be run from a clean feature branch. It runs the same checks,
updates the local `main` branch from `origin/main` with fast-forward-only
semantics, creates a non-fast-forward merge commit, and pushes `main`. It stops
instead of committing untracked changes, resolving conflicts, or force-pushing.

`make release` is the one-command production path. It validates the current
workspace, rejects sensitive-looking or oversized files, stages and commits
the release changes, merges and pushes the feature branch into `main` when
needed, deploys that exact `main` commit, and commits the verified deployment
inventory update. Override the default commit subject when useful:

```bash
make release RELEASE_MESSAGE="Fix industry cohort analysis"
```

The command stops on test failures, merge conflicts, remote divergence,
deployment failures, or unexpected files created during deployment. It never
force-pushes or resolves conflicts automatically.

Production also has a scheduled reconciliation workflow declared in
`cloudbuild.reconcile.yaml`. Cloud Scheduler invokes its manual Cloud Build
trigger every ten minutes. The build compares the deployed `APP_GIT_SHA` with
the latest `main` commit and deploys only when `main` is strictly ahead.
Identical commits are a no-op; behind or diverged histories fail visibly.
An inventory-only change to `docs/gcp-resources.md` is also a no-op so recording
a verified deployment cannot trigger another deployment. Unlike `make deploy`,
scheduled reconciliation never writes deployment state back to Git.

After authenticating the Google Cloud CLI and selecting the project, a source
deployment can be created manually for recovery with:

```bash
gcloud run deploy china-a-share-lab \
  --source . \
  --project china-a-share-lab \
  --region asia-east2 \
  --allow-unauthenticated \
  --cpu 1 \
  --memory 1Gi \
  --min 0 \
  --max 1 \
  --concurrency 4 \
  --timeout 300 \
  --service-account china-a-share-runner@china-a-share-lab.iam.gserviceaccount.com \
  --set-env-vars TUSHARE_CACHE_BUCKET=china-a-share-lab-cache-asia-east2 \
  --set-secrets TUSHARE_TOKEN=tushare-token:latest,DEEPSEEK_API_KEY=deepseek-api-key:latest,ZAI_API_KEY=zai-api-key:latest
```

Create the three named secrets and the private regional cache bucket before
deployment. After provisioning the Z.AI secret, update the verified live
inventory in `docs/gcp-resources.md`. Apply `infra/cache-lifecycle.json`, disable
object versioning and soft delete, and grant the Cloud Run service account
`roles/storage.objectUser` on that bucket only. Making the service public with
`--allow-unauthenticated` should only be done after login and request-rate
protection are enabled, or for a short controlled connectivity test.

Cloud Run supplies the `PORT` environment variable. The container binds to
`0.0.0.0` through `APP_HOST`, while ordinary local runs continue to default to
`127.0.0.1:8000`.

## Suggested experiments

### Market and valuation

```text
Retrieve daily prices for 000001.SZ from July 1 through July 17, 2026.
```

```text
Retrieve turnover rate, PE, PB, and total market value for 000001.SZ on July 17, 2026.
```

### Financial statements

```text
Retrieve the 2025 annual income statement for 600519.SH.
```

```text
Retrieve ROE, gross margin, net margin, and EPS for 600519.SH for the 2025 annual period.
```

### Events and market behavior

```text
Retrieve recent share-repurchase records for 600519.SH.
```

```text
Retrieve A-share block trades on July 17, 2026.
```

### Permission error handling

```text
Use moneyflow_ths to retrieve all A-share money-flow data for July 17, 2026.
```

If the Tushare account lacks access, the UI should show the upstream error code,
message, HTTP status when available, and safe raw response.

## Command-line utilities

The original direct data checks remain available:

```bash
a-share check
a-share daily --code 000001.SZ --start 20240101 --end 20240131
a-share stocks --output data/stocks.csv
```

## Quality checks

Run backend tests:

```bash
pytest
```

Run the original limit-up event-study regression against the real DeepSeek and
Tushare APIs:

```bash
make live-check
```

This opt-in check loads `DEEPSEEK_API_KEY` and `TUSHARE_TOKEN` from `.env`,
uses an in-process cache without Google Cloud credentials, and consumes real
upstream API quota. The default test suite skips it so routine tests remain
deterministic and offline.

Validate the frontend production build:

```bash
cd frontend
pnpm run build
```

## Current limitations

- DeepSeek has detailed guidance for the most common APIs but not yet a complete
  parameter and field schema for every allowlisted Tushare interface.
- Uncommon requests may select the correct API but produce an invalid parameter
  or field; the resulting Tushare error remains visible for diagnosis.
- The backend returns raw tables and controlled conditional counts. It does not
  yet provide general joins, ranking, formulas, chart generation, or portfolio
  analysis.
- Large results are returned by the API in full, while the browser displays the
  first 100 rows.
- The in-memory cache is lost when Cloud Run scales to zero. The Cloud Storage
  cache preserves successful responses, but it is not a general-purpose query
  database and does not provide cross-query range merging.
- Availability depends on the permissions, points, quotas, and rate limits of
  the configured Tushare account.

## Roadmap

1. Build a local schema registry for every supported Tushare stock interface.
2. Add a deterministic rule-based planner for common experiments.
3. Support `rules`, `deepseek`, and `hybrid` planning modes.
4. Add pagination and downloadable result artifacts.
5. Add controlled joins, ranking, time-series summaries, and charts.
6. Package local storage and remote deployment without changing API contracts.

The long-term goal is a reproducible A-share research workspace where model
assistance is optional, data access is explicit, and every result can be traced
back to a validated Tushare query.
