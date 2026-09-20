# Google Cloud Resource Inventory

This document is the source of truth for Google Cloud resources used by the
A-Share Laboratory. It records live infrastructure, security boundaries, and
expected cost impact without storing credential values.

Last verified: **2026-09-19**

## Project boundary

| Setting | Value |
| --- | --- |
| Project ID | `china-a-share-lab` |
| Project number | `1079739428171` |
| Primary region | `asia-east2` (Hong Kong) |
| Workload | Publicly accessible A-share research web application |
| Expected GCP cost | Approximately USD 0-1 per month at low traffic |

The estimate excludes Tushare, DeepSeek, and Z.AI model charges. Promotional credits and
free-tier allowances are billing-account concerns and are not infrastructure
resources.

## Cloud Run

| Setting | Value |
| --- | --- |
| Service | `china-a-share-lab` |
| Region | `asia-east2` |
| Service URL | <https://china-a-share-lab-1079739428171.asia-east2.run.app> |
| Latest ready revision | `china-a-share-lab-00261-mh6` |
| Deployed Git branch | `main` |
| Deployed Git commit | `5012c8e412ac145d73b63f3675f2bb1fc34d3717` |
| Traffic | 100% to the latest revision |
| Billing mode | Instance-based while an instance is active; scales to zero |
| CPU and memory | 1 vCPU, 1 GiB |
| Minimum instances | 0 |
| Service-level maximum instances | 1 |
| Container concurrency | 4 |
| Request timeout | 300 seconds |
| Container port | 8080 |
| Startup CPU boost | Enabled |
| Ingress | All |
| Invocation | Public; anonymous invocation enabled through `allUsers` |
| Runtime identity | `china-a-share-runner@china-a-share-lab.iam.gserviceaccount.com` |

The revision template retains Cloud Run's default revision-level maximum of 20,
but the service-level `run.googleapis.com/maxScale` setting caps the service at
one instance across traffic-serving revisions.

CPU throttling is disabled so authenticated Feishu research can continue after
the webhook acknowledges an event. The service still uses zero minimum instances
and one maximum instance, so it releases CPU and memory when Cloud Run scales the
idle service to zero.

### Runtime configuration

| Environment variable | Source |
| --- | --- |
| `TUSHARE_TOKEN` | Secret Manager secret `tushare-token`, version `latest` |
| `DEEPSEEK_API_KEY` | Secret Manager secret `deepseek-api-key`, version `latest` |
| `LLM_API_KEY` | Secret Manager secret `deepseek-api-key`, version `latest`; generic model credential alias |
| `ZAI_API_KEY` | Secret Manager secret `zai-api-key`, version `latest` |
| `MASSIVE_API_KEY` | Secret Manager secret `massive-api-key`, version `latest` |
| `FINNHUB_API_KEY` | Secret Manager secret `finnhub-api-key`, version `latest` |
| `GITHUB_FIX_TOKEN` | Secret Manager secret `github-fix-token`, version `latest` |
| `FEISHU_APP_SECRET` | Secret Manager secret `feishu-app-secret`, version `latest` |
| `FEISHU_VERIFICATION_TOKEN` | Secret Manager secret `feishu-verification-token`, version `latest` |
| `FEISHU_ENCRYPT_KEY` | Secret Manager secret `feishu-encrypt-key`, version `latest` |
| `TUSHARE_CACHE_BUCKET` | Plain value `china-a-share-lab-cache-asia-east2` |
| `GOOGLE_CLOUD_PROJECT` | Plain value `china-a-share-lab` |
| `CLOUD_RUN_REGION` | Plain value `asia-east2` |
| `ANALYSIS_JOB_NAME` | Plain value `china-a-share-analysis-worker` |
| `APP_GIT_BRANCH` | Plain Git branch recorded by the deployment workflow |
| `APP_GIT_SHA` | Plain full Git commit recorded by the deployment workflow |
| `ADMIN_EMAIL` | Plain administrator allowlist email for UI feedback |
| `GOOGLE_OAUTH_CLIENT_ID` | Public Google Web OAuth client identifier |
| `GITHUB_FIX_REPO` | Plain GitHub owner/repository used for UI feedback dispatch |
| `FEISHU_APP_ID` | Public Feishu custom-application identifier |
| `LLM_BASE_URL` | Plain OpenAI-compatible API base URL `https://api.deepseek.com` |
| `LLM_MODEL` | Plain provider-native model identifier `deepseek-flash` |
| `RESEARCH_SANDBOX_URL` | Plain private service URL `https://china-a-share-research-sandbox-45b3fkc7pa-df.a.run.app` |
| `PUBLIC_APP_URL` | Plain stable public origin `https://china-a-share-lab-1079739428171.asia-east2.run.app` used for token-protected research viewer links |

`LLM_API_SECRET` is supported by the generic model transport but is not
provisioned because the current API uses bearer-key authentication only.

### Public invocation access

| Setting | Value |
| --- | --- |
| Protection | IAP disabled on the Cloud Run service |
| Cloud Run invocation | `roles/run.invoker` granted to `allUsers` |
| Anonymous principal | Public access enabled |
| IAP service identity | Exists from the previous configuration but has no Cloud Run invoker binding |
| Expected cost impact | No standalone authentication or load-balancer charge |

Requests reach the application without Google login. Anyone with the service URL
can invoke model and market-data operations, so third-party API usage is not
protected from anonymous consumption.

### Private research sandbox

| Setting | Value |
| --- | --- |
| Service | `china-a-share-research-sandbox` |
| Region | `asia-east2` |
| Service URL | <https://china-a-share-research-sandbox-45b3fkc7pa-df.a.run.app> |
| Latest ready revision | `china-a-share-research-sandbox-00020-h4n` |
| Purpose | Execute restricted pandas and NumPy calculations for the Feishu research agent |
| Image | `asia-east2-docker.pkg.dev/china-a-share-lab/cloud-run-source-deploy/china-a-share-lab:5012c8e412ac145d73b63f3675f2bb1fc34d3717` |
| Command | `python -m uvicorn china_a_share.sandbox_server:app --host 0.0.0.0 --port 8080` |
| Traffic | 100% to the latest revision |
| CPU and memory | 1 vCPU, 2 GiB |
| Minimum and maximum instances | 0 and 1 |
| Container concurrency | 1 |
| Request timeout | 45 seconds |
| Invocation | Private; `roles/run.invoker` granted only to the application runtime identity |
| Runtime identity | `china-a-share-sandbox@china-a-share-lab.iam.gserviceaccount.com` |
| Runtime configuration | No application environment variables and no Secret Manager bindings |
| Expected cost impact | Usage-based CPU and 2 GiB memory while requests execute; no idle instance cost |

The service account has no project-level IAM role. The calculation runner
validates an allowlisted Python AST and executes it in a child process with CPU,
memory, file-size, row, column, request-size, and wall-clock bounds. The service
contains no application credentials. Network and file access are excluded from
the exposed Python contract; the Cloud Run platform itself does not apply a VPC
egress firewall to this service.

### Asynchronous analysis job

| Setting | Value |
| --- | --- |
| Job | `china-a-share-analysis-worker` |
| Region | `asia-east2` |
| Purpose | Execute durable long-running analysis tasks submitted by the web service |
| Image | Immutable digest copied from the latest ready web-service revision during deployment |
| Command | `python -m china_a_share.worker` |
| Tasks and parallelism | 1 task; parallelism 1 |
| CPU and memory | 1 vCPU, 4 GiB |
| Task timeout | 25,200 seconds |
| Maximum retries | 1 |
| Runtime identity | `china-a-share-runner@china-a-share-lab.iam.gserviceaccount.com` |
| Feishu delivery | Plain `FEISHU_APP_ID` plus Secret Manager `feishu-app-secret:latest` |
| Invocation | No public endpoint; executions are started through the Cloud Run API |
| Expected cost impact | Usage-based CPU and 4 GiB memory only while an analysis execution runs; no idle job cost |

The web service runtime identity has
`roles/run.jobsExecutorWithOverrides` on this job only. This permits the
service to supply one private task identifier per execution without granting
project-wide Cloud Run administration.

## Cloud Storage

### Persistent Tushare cache and analysis tasks

| Setting | Value |
| --- | --- |
| Bucket | `gs://china-a-share-lab-cache-asia-east2` |
| Region | `asia-east2` |
| Storage class | Standard |
| Current logical size | 457,895,933 bytes at last verification |
| Public access prevention | Enforced |
| Uniform bucket-level access | Enabled |
| Soft delete | Disabled |
| Object versioning | Disabled |
| Lifecycle | Delete `cache/` objects after 90 days, `analysis-jobs/` after 365 days, and `fix-requests/` after 30 days |
| Runtime access | `roles/storage.objectUser` for the Cloud Run runtime identity |

This bucket is the persistent L2 cache for successful Tushare responses and the
private status/result store for asynchronous analysis tasks. It survives Cloud
Run scale-to-zero events and deployments. It is not a general query database.

### Cloud Run source uploads

| Setting | Value |
| --- | --- |
| Bucket | `gs://run-sources-china-a-share-lab-asia-east2` |
| Region | `asia-east2` |
| Storage class | Standard |
| Current logical size | 38,969,500 bytes at last verification |
| Uniform bucket-level access | Enabled |
| Soft-delete retention | 7 days |
| Build access | `roles/storage.objectViewer` for the default compute service account |

This bucket is managed by the Cloud Run source-deployment workflow.

## Artifact Registry

| Setting | Value |
| --- | --- |
| Repository | `cloud-run-source-deploy` |
| Region | `asia-east2` |
| Format | Docker |
| Mode | Standard repository |
| Current size | 1068.462 MB as reported by Artifact Registry at last verification |
| Vulnerability scanning | Disabled because the Container Scanning API is not enabled |
| Build access | `roles/artifactregistry.writer` for the default compute service account |

The repository is created and used by Cloud Run source deployments. Its current
size exceeds the 0.5 GiB monthly Artifact Registry free allowance by roughly
0.5 GiB, with an expected low single-digit-cent monthly storage charge.

## Secret Manager

| Secret | Active version | Replication | Runtime access |
| --- | --- | --- | --- |
| `tushare-token` | Version 1, enabled | Automatic | Cloud Run runtime identity only |
| `deepseek-api-key` | Version 1, enabled | Automatic | Cloud Run runtime identity only |
| `zai-api-key` | Version 1, enabled | Automatic | Cloud Run runtime identity only |
| `github-fix-token` | Version 1, enabled | Automatic | Cloud Run runtime identity only |
| `feishu-app-secret` | Version 1, enabled | Automatic | Cloud Run runtime identity only |
| `feishu-verification-token` | Version 1, enabled | Automatic | Cloud Run runtime identity only |
| `feishu-encrypt-key` | Version 1, enabled | Automatic | Cloud Run runtime identity only |
| `feishu-bot-webhook` | Version 2, enabled | Automatic | Deployment automation identity only |
| `finnhub-api-key` | Version 1, enabled | Automatic | Cloud Run runtime identity only |
| `massive-api-key` | Version 1, enabled | Automatic | Cloud Run runtime identity only |

The nine application secrets grant `roles/secretmanager.secretAccessor`
directly to `china-a-share-runner@china-a-share-lab.iam.gserviceaccount.com`.
The Feishu webhook grants the same role only to the deployment automation
identity so verified production deployments can notify the administrator group.
The two U.S. market-data secrets were created manually on 2026-09-18 and use
the same secret-level runtime boundary as the other application credentials.
Secret values must never be added to this document.

## Logging and Monitoring

### Application observability dashboard

| Setting | Value |
| --- | --- |
| Dashboard | `A-Share API and Tushare Observability` |
| Dashboard ID | `4befc7ad-6a18-4b8d-b297-3f7bdf508232` |
| Console URL | <https://console.cloud.google.com/monitoring/dashboards/builder/4befc7ad-6a18-4b8d-b297-3f7bdf508232?project=china-a-share-lab> |
| Scope | Cloud Run revision metrics and structured application logs for `china-a-share-lab` |
| Widgets | Cache hit ratio, frontend request rate, cache outcomes, real Tushare call rate, Tushare P95 latency, and application observability logs |
| IAM boundary | Uses existing project-level Monitoring and Logging read permissions; no runtime IAM grant was added |
| Lifecycle | Manually managed custom dashboard; delete when the application observability contract is retired |
| Expected cost impact | No material standalone dashboard charge |

### User-defined log-based metrics

| Metric | Kind | Structured event | Low-cardinality labels |
| --- | --- | --- | --- |
| `logging.googleapis.com/user/frontend_request_total` | Counter | `http_request_completed` | `api_route`, `method`, `status_class` |
| `logging.googleapis.com/user/data_cache_lookup_total` | Counter | `cache_lookup_completed` | `api_route`, `provider`, `operation`, `outcome`, `cache_layer` |
| `logging.googleapis.com/user/provider_call_total` | Counter | `provider_call_completed` | `api_route`, `provider`, `operation`, `status` |
| `logging.googleapis.com/user/provider_call_latency_ms` | Distribution | `provider_call_completed` | `api_route`, `provider`, `operation`, `status` |

All four metrics filter Cloud Run revision logs for service
`china-a-share-lab`. The latency distribution extracts
`jsonPayload.duration_ms` and uses 20 exponential buckets with growth factor 2
and scale 1. The metrics are project-scoped, add no IAM grants, and should be
deleted together with the dashboard when this observability contract is
retired. User-defined metric ingestion is chargeable after the billing
account's monthly free allotment; expected low traffic should remain within the
150 MiB allowance.

## Service accounts and IAM boundaries

### Cloud Run runtime identity

`china-a-share-runner@china-a-share-lab.iam.gserviceaccount.com`

- Reads the nine application secrets through secret-level IAM grants, including
  `finnhub-api-key` and `massive-api-key` for Feishu U.S. market-data requests.
- Creates, reads, updates, and deletes objects in the private cache bucket.
- Executes only `china-a-share-analysis-worker` with per-execution overrides.
- Invokes only `china-a-share-research-sandbox` through service-scoped
  `roles/run.invoker`.
- Does not have a broad project-level role.

### Research sandbox runtime identity

`china-a-share-sandbox@china-a-share-lab.iam.gserviceaccount.com`

- Runs only the private research sandbox revision.
- Has no project-level IAM role, secret-level access, or application
  environment credentials.
- Can be attached to deployed revisions only by the dedicated deployment
  identity through service-account-level `roles/iam.serviceAccountUser`.

### Deployment automation identities

| Identity | Purpose | Access |
| --- | --- | --- |
| `china-a-share-deployer@china-a-share-lab.iam.gserviceaccount.com` | Run the scheduled reconciliation build, deploy verified `main` commits, and notify the administrator group | Project-level `roles/run.admin` and `roles/logging.logWriter`; `roles/artifactregistry.writer` on `cloud-run-source-deploy`; `roles/storage.objectViewer` on the source bucket; `roles/secretmanager.secretAccessor` on `feishu-bot-webhook`; `roles/iam.serviceAccountUser` on the application and sandbox runtime identities |
| `china-a-share-scheduler@china-a-share-lab.iam.gserviceaccount.com` | Invoke scheduled Cloud Build reconciliation | Project-level `roles/cloudbuild.builds.editor`; `roles/iam.serviceAccountUser` on the dedicated deployer identity only |

Neither identity has a user-managed key. The deployer can administer all Cloud
Run services and jobs in this project. The Scheduler identity can create,
inspect, and cancel builds across the project and can act only as the dedicated
deployment identity.

### Source-build identity

`1079739428171-compute@developer.gserviceaccount.com`

- Reads uploaded sources from the Cloud Run source bucket.
- Writes container images to the source-deployment Artifact Registry repository.
- Writes build logs through `roles/logging.logWriter` at project level.

## Deployment reconciliation

### Cloud Build trigger

| Setting | Value |
| --- | --- |
| Trigger | `china-a-share-reconcile-main-v2` |
| Trigger ID | `c3932b82-c9bb-4870-85af-c77eb2d24bbd` |
| Region | `asia-east2` |
| Source branch | `main` |
| Build configuration | `cloudbuild.reconcile.yaml` |
| Execution identity | `china-a-share-deployer@china-a-share-lab.iam.gserviceaccount.com` |
| Current state | Verified by successful build and deployment |
| Expected cost impact | Cloud Build usage only when invoked |

The obsolete first-generation trigger was deleted after the second-generation
trigger completed both a real deployment and a scheduled no-op reconciliation.

### Main push deployment trigger

| Setting | Value |
| --- | --- |
| Trigger | `china-a-share-deploy-main-push` |
| Trigger ID | `1ae0a769-e423-416e-910b-566ff800f430` |
| Region | `asia-east2` |
| Source event | Push to `main` (`^main$`) |
| Ignored files | `docs/**`, `README.md`, and `AGENTS.md` |
| Build configuration | `cloudbuild.reconcile.yaml` |
| Execution identity | `china-a-share-deployer@china-a-share-lab.iam.gserviceaccount.com` |
| Current state | Enabled; application-code push and resulting deployment verified end to end |
| Expected cost impact | Cloud Build usage only after a non-documentation push; no polling builds |

### GitHub connection

| Setting | Value |
| --- | --- |
| Connection | `china-a-share-github` |
| Region | `asia-east2` |
| Provider | GitHub through the Google-managed Cloud Build GitHub App |
| Installation state | Complete |
| Repository | `china-a-share`, linked to private GitHub repository `howardliuchenxi/china_a_share` |
| Credential storage | Google-managed Secret Manager secrets created for the connection |
| IAM boundary | The temporary project-level `roles/secretmanager.admin` used during setup was removed |
| Expected cost impact | Secret versions are expected to remain within the Secret Manager free allowance |

The GitHub App installation is limited by the repository selection made during
GitHub authorization.

### Cloud Scheduler

| Setting | Value |
| --- | --- |
| Job | `china-a-share-reconcile-main` |
| Region | `asia-east2` |
| Schedule | Every 10 minutes (`*/10 * * * *`, UTC), retained as a disabled fallback |
| Target | Cloud Build trigger `c3932b82-c9bb-4870-85af-c77eb2d24bbd` |
| Invocation identity | `china-a-share-scheduler@china-a-share-lab.iam.gserviceaccount.com` |
| State | Paused after the main push trigger replaced polling |
| Retry count | 1 |
| Expected cost impact | No execution or Cloud Build usage while paused; the retained job remains within the three-job Scheduler monthly free allowance |

| Setting | Value |
| --- | --- |
| Job | `china-a-share-strategy-daily-scan` |
| Region | `asia-east2` |
| Schedule | `30 16 * * 1-5` (Asia/Shanghai), after the A-share close |
| Target | POST `https://china-a-share-lab-1079739428171.asia-east2.run.app/api/analysis/tasks/strategy:daily-scan` |
| Invocation identity | `china-a-share-scheduler@china-a-share-lab.iam.gserviceaccount.com` (OIDC token, audience set to the full route URL; the API also accepts the optional `STRATEGY_SCAN_TOKEN` static bearer) |
| State | Enabled; resumed after the strategy scan deploy and two verified manual runs (HTTP 200) |
| Retry count | Scheduler default (5); the API returns 500 on any per-strategy failure so retries are at-least-once |
| Expected cost impact | One authenticated POST per weekday while enabled; remains within the monthly free allowance for Scheduler jobs |

Google-managed Cloud Run, Cloud Build, Artifact Registry, Container Registry,
and Pub/Sub service agents also exist. They are platform-managed identities and
are not application runtime identities.

## APIs actively required

- Cloud Run API: `run.googleapis.com`
- Cloud Build API: `cloudbuild.googleapis.com`
- Artifact Registry API: `artifactregistry.googleapis.com`
- Secret Manager API: `secretmanager.googleapis.com`
- Cloud Storage APIs: `storage.googleapis.com`, `storage-api.googleapis.com`
- IAM APIs: `iam.googleapis.com`, `iamcredentials.googleapis.com`
- Cloud Resource Manager API: `cloudresourcemanager.googleapis.com`
- Cloud Scheduler API: `cloudscheduler.googleapis.com`
- Logging and Monitoring APIs: `logging.googleapis.com`,
  `monitoring.googleapis.com`

The Identity-Aware Proxy API remains enabled after IAP was disabled on the
service. The project also has several Google Cloud default APIs enabled. An
enabled API is not by itself evidence that a billable resource exists.

## Not provisioned

The following services are not live resources for this project:

- Cloud SQL or any MySQL instance
- Memorystore or any Redis instance
- Serverless VPC Access connector
- Compute Engine virtual machine
- Load balancer, static IP address, or custom domain

## Cost posture

- Cloud Run scales to zero and is capped at one instance.
- Instance-based billing covers the short background-processing window after a
  Feishu acknowledgement. Active instances are billed during that window, but
  the zero minimum and one-instance maximum keep the expected low-volume cost
  bounded.
- The asynchronous Cloud Run Job has no idle instance cost and uses one task
  with bounded CPU, memory, timeout, and retries per execution.
- The private research sandbox has no idle instance cost, is capped at one
  concurrent 1-vCPU/2-GiB instance, and runs only for bounded calculation
  requests.
- Current storage volumes are small and are expected to remain within or close
  to applicable free allowances.
- Artifact Registry is approximately 0.5 GiB above its monthly free storage
  allowance, with a low single-digit-cent expected monthly charge.
- Ten active secret versions are expected to remain within or close to the
  Secret Manager free allowance at current access volume. The two U.S.
  market-data credentials add no continuous compute cost.
- The Feishu deployment webhook adds one low-volume secret access and one
  outbound request after each real deployment, with no material expected cost.
- The persistent cache has a 90-day deletion lifecycle to prevent unbounded
  object accumulation, while asynchronous task records expire after 365 days
  and private UI feedback records expire after 30 days.
- Four low-cardinality log-based metrics are expected to remain within the
  billing account's 150 MiB monthly user-defined metric allowance at low
  traffic.
- Adding Cloud SQL would introduce a continuous monthly instance charge and must
  be documented here before and after provisioning.

## Inventory maintenance procedure

For every Google Cloud resource creation, update, or deletion:

1. Read this inventory before making the change.
2. State the expected security and monthly cost impact.
3. Apply the resource change only after the user authorizes it.
4. Verify the resulting live configuration with a read-only Google Cloud query.
5. Update this document in the same task, including the verification date.
6. Never record credentials or secret payloads.

Manual changes made in the Google Cloud console cannot be automatically
enforced by this repository. They must be reconciled here when observed.

## Change log

| Date | Change |
| --- | --- |
| 2026-07-19 | Deployed the initial Cloud Run service and supporting source-build resources. |
| 2026-07-19 | Added the private persistent Tushare cache bucket and connected it to Cloud Run. |
| 2026-07-19 | Created this verified resource inventory and repository maintenance rule. |
| 2026-07-19 | Deployed revision `china-a-share-lab-00005-q4r` with replaceable planner and market-data provider adapters; no new resource types were added. |
| 2026-07-19 | Created four low-cardinality application log-based metrics and the `A-Share API and Tushare Observability` dashboard; removed an accidental empty dashboard after verifying the intended dashboard. |
| 2026-07-19 | Deployed revision `china-a-share-lab-00006-fvx` with GLM-5V-Turbo screenshot analysis, added `zai-api-key`, and protected the service with direct Cloud Run IAP access for two authorized Google accounts. |
| 2026-07-19 | Disabled direct Cloud Run IAP and restored public anonymous invocation through the `allUsers` Cloud Run invoker binding. |
| 2026-07-20 | Deployed revision `china-a-share-lab-00007-j7k` with interactive analysis-result sorting and search, official stock-name enrichment, and explicit percentage-change output guidance; no new resource types or material cost changes were introduced. |
| 2026-07-20 | Deployed revision `china-a-share-lab-00008-rmk` with fail-fast query feasibility evidence, deterministic numeric row filtering, and visual query decision traces; verified 100% traffic, public invocation, health status, runtime limits, source storage, and image storage with no new resource types or material cost changes. |
| 2026-07-21 | Deployed revision `china-a-share-lab-00009-p5q` with the current application changes, including the consolidated query-details presentation; verified 100% traffic, public invocation, health status, runtime limits, runtime identity, and secret bindings with no new resource types or material cost changes. |
| 2026-07-21 | Deployed revision `china-a-share-lab-00010-gn5` with deterministic float-holder reporting-period validation, as-of snapshot selection, and explicit partial CR10 results when source ratios are missing; verified the production query path, 100% traffic, public invocation, health status, runtime limits, runtime identity, and secret bindings with no new resource types or material cost changes. |
| 2026-07-22 | Deployed revision `china-a-share-lab-00011-5zw` with responsive single-row result cards, explicit missing-value explanations, and deterministic splitting of multi-security float-holder plans; production validation exposed a separate omitted-date path, so this revision was immediately superseded. |
| 2026-07-22 | Deployed revision `china-a-share-lab-00012-tcc` with deterministic latest-snapshot selection when the planner omits a date; verified the original three-security production query, 100% traffic, public invocation, health status, runtime limits, runtime identity, and secret bindings with no new resource types or material cost changes. |
| 2026-07-23 | Deployed revision `china-a-share-lab-00013-d96` from the authorized current workspace, including native `limit_list_d` limit-up planning and deterministic correction of invalid limit-up filters and code-count aggregations; verified 100% traffic, public health status, runtime limits, runtime identity, cache configuration, and secret bindings with no new resource types or material cost changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00014-8xb` with deterministic multi-day analysis transforms, bounded pagination, completed-trading-day normalization, planner contract retries, and fail-closed handling for unsupported joins and derived calculations; verified 100% traffic, public health status, runtime limits, runtime identity, cache configuration, secret bindings, and storage usage with no new resource types, IAM changes, or material cost changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00016-tbg` with explicit unsupported-result presentation, the approved non-top-ten float-holder retail-ratio proxy, categorical security-universe filtering, and deterministic cross-month average-turnover comparison; superseded revision `00015-fbj` after production verification exposed Tushare's rejection of full-market date ranges, then verified the replacement's 100% traffic, public health status, runtime limits, runtime identity, secret bindings, storage usage, and successful 254-row production analysis with no new resource types or IAM changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00017-bgk` with explicit persistent-cache profiles for all 108 allowlisted Tushare operations, Cloud Storage bypass for real-time and current intraday data, 90-day retention for fixed historical and disclosure queries, fail-fast coverage for future operations, and reader-friendly numeric units; verified 100% traffic, public health status, the new frontend asset, runtime limits, runtime identity, secret bindings, and storage usage with no new resource types or IAM changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00018-bfh` with browser-local prompt history, removal of the experiment-template panel, consolidated query and execution details, and the current backend fixes; verified 100% traffic, public health status, the deployed frontend asset, runtime limits, runtime identity, secret bindings, and storage usage with no new resource types, IAM changes, or material cost changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00019-rtk` with durable asynchronous analysis submission, Cloud Storage task polling, frontend progress reporting, and supported healthcare retail-proxy cohort analysis; created private job `china-a-share-analysis-worker`, granted the service identity job-scoped `roles/run.jobsExecutorWithOverrides`, added 7-day task-record deletion, and verified the original 517-security production request through a successful job execution. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00021-kzp` with public task-response redaction and the corrected deployment workflow; synchronized the asynchronous job to the immutable service image, then verified 100% traffic, public health status, the original 517-security analysis result, runtime configuration, and storage usage with no new resource types or IAM changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00022-8dt` with deterministic power-industry filtering, generalized industry retail-proxy cohort analysis, asynchronous routing for equivalent industry prompts, and bounded deployment-state verification retries; synchronized the worker image and verified the original 85-security power-industry request through a successful execution with no new resource types or IAM changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00023-w94` with deterministic phone-theme resolution through the `AI手机` and `华为手机` THS concept constituents, operation-aware THS index validation, normalized concept security universes, and GNU Make 3.81-compatible deployment and merge recipes; synchronized the worker image and verified the original 80-security phone-stock request through a successful execution with no new resource types or IAM changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00024-trh` through `make deploy`; recorded source `main@81905284404ffe8de2eb5c2caf9461c0d30f4d7a`, verified 100% traffic, public health status, runtime configuration, and storage usage with no new resource types or IAM changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00025-ddv` through `make deploy`; recorded source `main@6479d9c61ca9cf804d9e688ef55906350de1f101`, verified 100% traffic, public health status, runtime configuration, and storage usage with no new resource types or IAM changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00026-jx2` through `make deploy`; recorded source `main@44ab319a33233b300df90992f338e0f7a18d129b`, verified 100% traffic, public health status, runtime configuration, and storage usage with no new resource types or IAM changes. |
| 2026-07-24 | Deployed revision `china-a-share-lab-00027-qft` through `make deploy`; recorded source `main@79f4c6efec959e20b81c2edc6279a73ee992afa1`, verified 100% traffic, public health status, runtime configuration, and storage usage with no new resource types or IAM changes. |
| 2026-07-25 | Created `feishu-bot-webhook` with automatic replication for successful deployment notifications and granted secret-level access only to the deployment automation identity; expected cost remains negligible. |
| 2026-08-08 | Increased `china-a-share-analysis-worker` memory from 1 GiB to 4 GiB after a full-market multi-year discovery workload exceeded the previous limit; retained 1 vCPU, one task, two-hour timeout, one retry, the existing runtime identity, and usage-only billing with no idle job cost. |
| 2026-08-08 | Extended the `analysis-jobs/` Cloud Storage deletion lifecycle from 7 days to 365 days so asynchronous analysis and discovery task results remain available for historical review; retained the existing bucket, Standard storage class, access boundary, and usage-based cost model. |
| 2026-08-10 | Deployed revision `china-a-share-lab-00188-cfp` through scheduled reconciliation; recorded source `main@aab7db3f45e1fca977220f52b0339931dafaf698`, verified 100% traffic, public health status, runtime configuration, synchronized worker deployment, and storage usage with no new resource types or IAM changes. |
| 2026-08-11 | Rotated `feishu-bot-webhook` to enabled version 2 after the prior Lark bot was removed; the next scheduled reconciliation delivered its start notification and deployed revision `china-a-share-lab-00193-brg` from `main@395a633178fc57fc52b99a32964e3db83d4c3e5e`, with 100% traffic, public health status, and the synchronized worker verified. No IAM boundary, resource type, lifecycle policy, or material cost changed. |
| 2026-08-11 | Deployed revision `china-a-share-lab-00194-sfq` through scheduled reconciliation from `main@07917699db8aea5b9d4ea94e33031ffe5e45aa12`; verified 100% traffic and the reported market-return ranking in production with a complete required answer result. No IAM boundary, resource type, lifecycle policy, or material cost changed. |
| 2026-09-13 | Deployed revision `china-a-share-lab-00227-v47` through `make deploy`; recorded source `main@687c40654f42b73a8b6994cb1a6174a5ecd581f9`, verified 100% traffic, public health status, runtime configuration, and storage usage with no new resource types or IAM changes. |
| 2026-09-13 | Deployed revision `china-a-share-lab-00229-856` through `make deploy`; recorded source `main@81f87c6438a38fda10bb3742ca60a32da2be9892`, verified 100% traffic, public health status, runtime configuration, storage usage, and a successful Feishu URL-verification callback. Published Feishu application version `1.0.0` with availability limited to the application owner and external interaction disabled; no new GCP resource types or IAM changes were introduced. |
| 2026-09-13 | Deployed revision `china-a-share-lab-00230-6w6` through `make deploy`; recorded source `main@88ff7a0ec3f991b68ed435910a72d3937869cd53`, verified 100% traffic, public health status, runtime configuration, and storage usage with no new resource types or IAM changes. |
| 2026-09-13 | Deployed revision `china-a-share-lab-00235-qbh` through `make deploy`; recorded source `main@e1faec7f8df3626626fd9adf8d480dcfa17dda51`, verified 100% traffic, public health status, runtime configuration, storage usage, and successful single-turn and contextual multi-turn Feishu research responses with no new resource types or IAM changes. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00238-kl7` through scheduled reconciliation from `main@9643e4c1356f4459b1aa7a85e364a7eced21b6b2`; verified 100% traffic, public health, the synchronized `china-a-share-analysis-worker` image and Git SHA, and Worker access to the existing `FEISHU_APP_ID` and `feishu-app-secret:latest` bindings. The release adds the independent DeepSeek V4 Pro Feishu agent, deterministic recent-session market-return ranking, structured mention command routing, terminal Worker initialization failures, and the scoped Feishu release gate. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00239-wk7` through scheduled reconciliation from `main@a066fbef77e59b9c7dc78af822b3ed4ac390b411`; verified 100% traffic, public health, and the synchronized `china-a-share-analysis-worker` image and Git SHA. The release groups Feishu text and workbook replies in source-message threads and suppresses consecutive duplicate progress updates. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00240-4qc` through scheduled reconciliation from `main@edee3a98afcab9c68c2fdaee8b95d78a7b021c14`; verified 100% traffic and the synchronized Worker image and Git SHA. The release temporarily disables backend release tests at the operator's request. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Replaced periodic deployment polling with the `china-a-share-deploy-main-push` trigger for non-documentation pushes to `main`; restored the fallback Scheduler cadence to ten minutes, then paused `china-a-share-reconcile-main`. Verified the trigger and paused job configuration. The brief three-minute interval was reverted before a second high-frequency invocation; no recurring polling build cost remains. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00241-c8k` immediately through the verified `china-a-share-deploy-main-push` trigger from `main@d8e767f7c6272d404abd84e74851b09c86cd96ed`; verified 100% traffic, public health, and the synchronized Worker image and Git SHA. The release sends `reply_in_thread` in the Feishu reply body, deterministically executes recent-session market-return rankings, and bounds full-market reads by exact trading date. No IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00242-sjk` immediately through the main push trigger from `main@dfbecc6c6281957ab4e3424a5958a77b9f39f617`; verified 100% traffic, public health, and the synchronized Worker image and Git SHA. Created private service `china-a-share-research-sandbox` at revision `00001-62t` with 1 vCPU, 2 GiB, zero minimum and one maximum instance, concurrency 1, a 45-second timeout, no application environment or secret bindings, and the dedicated unprivileged `china-a-share-sandbox` runtime identity. Granted the application runtime identity service-scoped `roles/run.invoker` and the deployer service-account-level `roles/iam.serviceAccountUser`. The Feishu agent now uses generic OpenAI-compatible model configuration, provider-neutral data tools, bounded secretless DataFrame execution, deterministic validation, and tool-budget synthesis without prompt-specific ranking logic. Verified the reported five-day-return prompt with the live configured model, synthetic market data, and the private sandbox; the sandbox rejected an import attempt and accepted the corrected restricted calculation. Expected incremental GCP cost remains usage-based with no idle instance charge. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00243-d99` immediately through the main push trigger from `main@70167a158a141d20ef0aa936640b8ed8a1751117`; verified 100% traffic, public health, and the synchronized Worker image and Git SHA. The release replaces hidden Feishu thread progress with one visible source-message reply that is edited in place through completion, while retaining a separate file reply only when an artifact is generated. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00244-hss` immediately through the main push trigger from `main@9116f0cebfcc6b4d28ad39e2613ab61a6325fea9`; verified 100% traffic, public health, the synchronized Worker image and Git SHA, and the exact reported combined Feishu session-and-research prompt against the configured model, Tushare, and private sandbox. The release resolves named sessions to stable backend identifiers, supports create-and-query commands in one message, preserves context isolation across sessions, and executes complete full-market daily ranges through bounded exact-trading-date fanout within one agent tool call. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00245-fln` immediately through the main push trigger from `main@adeb224fb8607a807785772e81380442ff8277a0`; verified 100% traffic, public health, the synchronized `china-a-share-analysis-worker` image and Git SHA, and private sandbox revision `china-a-share-research-sandbox-00004-kvm`. The release adds a Feishu quick-menu card for mention-only messages, interactive research submission, and shortcuts for sessions and task status. Configured the existing Feishu callback endpoint for `card.action.trigger` and published application version `1.0.2` with the existing owner-only availability and external interaction restrictions. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00246-sw7` immediately through the main push trigger from `main@8f3e290d069f950c3abc2b719877d8462e78b427`; verified 100% traffic, public health, the synchronized `china-a-share-analysis-worker` image and Git SHA, and private sandbox revision `china-a-share-research-sandbox-00005-hj2`. The release accepts Feishu card callbacks that use the V2 event-header verification token without `X-Lark-*` signature headers while continuing to reject unsigned normal messages, partial signatures, invalid signatures, and invalid verification tokens. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00247-xnn` immediately through the main push trigger from `main@8d630e9589bfb001464b93f949261b0ad92affcb`; verified 100% traffic, the synchronized `china-a-share-analysis-worker` image and Git SHA, and private sandbox revision `china-a-share-research-sandbox-00006-9gk`. The release reads the card operator from Feishu's documented `event.operator.open_id` field. Verified the production fix by opening the `测试1` Feishu group, clicking the `会话列表` card button, observing the expected session-list reply, and confirming a 200 callback response from this revision. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00249-gq4` through the main reconciliation trigger from `main@498386dcc716d99281bde3bd3526be9b68734a29`; verified 100% traffic, the synchronized `china-a-share-analysis-worker` image, and private sandbox revision `china-a-share-research-sandbox-00008-86m`. The release adds recommended numbered clarification choices, exposes the complete audited market-data catalog to the research model, and normalizes DeepSeek DSML tool calls at the model transport boundary. Production verification in the `测试1` Feishu group used the full PE_TTM ranking prompt: the bot requested the missing A-share universe definition, accepted the short reply `1` as the recommended Shanghai/Shenzhen-only choice, queried `daily_basic`, executed the calculation in the private sandbox, and returned the requested ten-row result without exposing DSML markup. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00250-5p8` through the main push trigger from `main@f3e22c19e20d04d36afbcdf240088f6d5cc3bd79`; verified 100% traffic, public health, the synchronized `china-a-share-analysis-worker` image and Git SHA, and private sandbox revision `china-a-share-research-sandbox-00009-wpr`. The release replaces the bespoke bounded DeepSeek tool loop with the Codex SDK agent runtime, retains DeepSeek V4 Pro as the configured model, exposes generic retained-dataset capabilities through stdio MCP with complete schemas, and reports material MCP progress in one edited Feishu reply. The exact reported 30-trading-day ranking prompt passed a live pre-deployment regression against the configured model, Tushare, and private sandbox. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00251-t5f` through the main reconciliation trigger from `main@049b3f43bc3db4069e2c6c692f2d285f3a57be69`; verified 100% traffic, the synchronized `china-a-share-analysis-worker` image and Git SHA, and private sandbox revision `china-a-share-research-sandbox-00010-f97`. The release aligns final-response collection with the Codex SDK contract by accepting the latest agent message when a third-party model omits phase metadata, while preserving explicit final answers. A genuinely textless completed turn now produces recoverable numbered choices, or an attachment notice when an artifact exists, instead of exposing an internal empty-response failure. Production verification in the `测试1` Feishu group submitted the exact reported ambiguous PE-ranking prompt through the quick card and received three numbered valuation choices with a recommended PE(TTM) option. Existing conversation and named-session objects remained in the persistent Cloud Storage store. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00252-rms` through the main push trigger from `main@9f42c883d522a5632ee2d2c1f537719b5d442c3c`; verified 100% traffic, public health, the synchronized `china-a-share-analysis-worker` image and Git SHA, and private sandbox revision `china-a-share-research-sandbox-00011-ms9`. The release preserves successful research results when only Feishu attachment delivery fails and includes bounded Feishu API error details in diagnostics. Enabled the existing Feishu application's `im:resource` tenant permission after error `99991672`, then verified the file-upload API returned code `0` and a file key. No GCP resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-17 | Deployed revision `china-a-share-lab-00253-rfb` through the main push trigger from `main@bb88427436a5a6ac3eafb34cfe4737ff060f721d`; verified 100% traffic, public health, the synchronized `china-a-share-analysis-worker` image and Git SHA, private sandbox revision `china-a-share-research-sandbox-00012-xrh`, a successful public research-page shell response, and a non-disclosing 404 for an invalid visualization token. The release adds 30-day token-protected, login-free interactive research charts and workbook downloads. Viewer workbooks use the existing private `analysis-jobs/` namespace and its 365-day deletion lifecycle. No resource type, IAM boundary, lifecycle policy, fixed runtime cost, or material storage cost changed. |
| 2026-09-18 | Granted the Cloud Run runtime identity secret-level `roles/secretmanager.secretAccessor` on `finnhub-api-key` and `massive-api-key`; verified both IAM policies, retained automatic replication and enabled version 1, and introduced no continuous compute cost. |
| 2026-09-18 | Deployed revision `china-a-share-lab-00254-xn8` through the main push trigger from `main@cff707607d03917895b3f4b9b2512652e34d7ef8`; verified 100% traffic, public health, synchronized Worker image and Git SHA, and both U.S. market-data secret bindings on the service and Worker. Live provider checks returned one recent AAPL daily row from Massive, one AAPL company-profile row from Finnhub, and 12,562 rows from a Massive full-market daily snapshot. No new continuous compute resource or broad project-level IAM role was added. |
| 2026-09-19 | Deployed revision `china-a-share-lab-00256-m2m` immediately through the main push trigger from `main@074d9cafe0e2d2852557c6d054ebedc66a6ef897`; verified 100% traffic, public health, the synchronized `china-a-share-analysis-worker` image and Git SHA, and private sandbox revision `china-a-share-research-sandbox-00015-9mf`. The release removes the misleading generic chart from research result pages while retaining the searchable result table and workbook download. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-19 | Deployed revision `china-a-share-lab-00257-scz` immediately through the main push trigger from `main@0ec81fe1d2626c5f1c9ee1ddaebdad4ded1c46cc`; verified 100% traffic, public health, the synchronized `china-a-share-analysis-worker` image, private sandbox revision `china-a-share-research-sandbox-00016-vj9`, and the reported historical research link rendering `20221215` as `2022-12-15` without numeric thousands separators. The release normalizes valid compact calendar dates in date-semantic result columns for both new and retained research results. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-19 | Deployed revision `china-a-share-lab-00259-gcx` immediately through the main push trigger from `main@88ac653574d02993767c16e0406754a179a76450`; verified 100% traffic, public health, and the synchronized `china-a-share-analysis-worker` Git SHA. The release extends the bounded Codex research turn window from 15 minutes to one hour while retaining interruption and cleanup behavior. No resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-19 | Deployed revision `china-a-share-lab-00260-c8w` immediately through the main push trigger from `main@f8efb2fb140abcf4c8cca7c2d2db9c8a890c45be`; verified 100% traffic, public health, and the synchronized `china-a-share-analysis-worker` Git SHA and 25,200-second task timeout. The release extends the bounded Codex research turn window to six hours and the Worker execution window to seven hours for cleanup and result persistence. No resource type, IAM boundary, lifecycle policy, or fixed cost changed; maximum usage-based cost per long-running execution increases with the longer allowed runtime. |
| 2026-09-19 | Deployed revision `china-a-share-lab-00261-mh6` through the main push trigger from `main@5012c8e412ac145d73b63f3675f2bb1fc34d3717`; verified 100% traffic, the synchronized sandbox revision `china-a-share-research-sandbox-00020-h4n` running the same image digest, and the reported historical research link now returning the recorded workbook methodology through the read-only legacy enrichment. The release adds required per-column notes and security-code quote-page links to research workbooks, viewer column tooltips, a methodology section, and a sandbox helper that collapses overlapping same-security signal windows into one event; no resource type, IAM boundary, lifecycle policy, or material cost changed. |
| 2026-09-20 | Deployed revision `china-a-share-lab-00262-j7f` through the main push trigger from `main@a4d45d1bd8f0207f9fd0a5067a2337c92ca77822` (merge of `codex/strategy-scanner-clean`); verified 100% traffic and public health. The release adds configurable technical-pattern strategy scanning with Feishu authoring cards, owner-isolated Cloud Storage persistence under the existing bucket's new `strategies/` prefix, an authenticated `POST /api/analysis/tasks/strategy:daily-scan` entry, and `send_chat_card` on the Feishu client. Resumed `china-a-share-strategy-daily-scan` and verified the scheduler's real OIDC invocation returns 200 (invalid bearer tokens return 401). No resource type, IAM boundary, lifecycle policy, or fixed cost changed; storage grows only with user-created strategy objects. |
