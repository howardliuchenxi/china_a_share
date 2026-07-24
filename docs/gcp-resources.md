# Google Cloud Resource Inventory

This document is the source of truth for Google Cloud resources used by the
A-Share Laboratory. It records live infrastructure, security boundaries, and
expected cost impact without storing credential values.

Last verified: **2026-07-24**

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
| Latest ready revision | `china-a-share-lab-00014-8xb` |
| Traffic | 100% to the latest revision |
| Billing mode | Request-based |
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

### Runtime configuration

| Environment variable | Source |
| --- | --- |
| `TUSHARE_TOKEN` | Secret Manager secret `tushare-token`, version `latest` |
| `DEEPSEEK_API_KEY` | Secret Manager secret `deepseek-api-key`, version `latest` |
| `ZAI_API_KEY` | Secret Manager secret `zai-api-key`, version `latest` |
| `TUSHARE_CACHE_BUCKET` | Plain value `china-a-share-lab-cache-asia-east2` |

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

## Cloud Storage

### Persistent Tushare cache

| Setting | Value |
| --- | --- |
| Bucket | `gs://china-a-share-lab-cache-asia-east2` |
| Region | `asia-east2` |
| Storage class | Standard |
| Current logical size | 3,437,647 bytes at last verification |
| Public access prevention | Enforced |
| Uniform bucket-level access | Enabled |
| Soft delete | Disabled |
| Object versioning | Disabled |
| Lifecycle | Delete objects under `cache/` after 90 days |
| Runtime access | `roles/storage.objectUser` for the Cloud Run runtime identity |

This bucket is the persistent L2 cache for successful Tushare responses. It
survives Cloud Run scale-to-zero events and deployments. It is not a general
query database.

### Cloud Run source uploads

| Setting | Value |
| --- | --- |
| Bucket | `gs://run-sources-china-a-share-lab-asia-east2` |
| Region | `asia-east2` |
| Storage class | Standard |
| Current logical size | 34,096,836 bytes at last verification |
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
| Current size | 522.727 MB as reported by Artifact Registry at last verification |
| Vulnerability scanning | Disabled because the Container Scanning API is not enabled |
| Build access | `roles/artifactregistry.writer` for the default compute service account |

The repository is created and used by Cloud Run source deployments. Its current
size remains below the 0.5 GiB monthly Artifact Registry free allowance.

## Secret Manager

| Secret | Active version | Replication | Runtime access |
| --- | --- | --- | --- |
| `tushare-token` | Version 1, enabled | Automatic | Cloud Run runtime identity only |
| `deepseek-api-key` | Version 1, enabled | Automatic | Cloud Run runtime identity only |
| `zai-api-key` | Version 1, enabled | Automatic | Cloud Run runtime identity only |

All three secrets grant `roles/secretmanager.secretAccessor` directly to
`china-a-share-runner@china-a-share-lab.iam.gserviceaccount.com`. Secret values
must never be added to this document.

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

- Reads the three application secrets through secret-level IAM grants.
- Creates, reads, updates, and deletes objects in the private cache bucket.
- Does not have a broad project-level role.

### Source-build identity

`1079739428171-compute@developer.gserviceaccount.com`

- Reads uploaded sources from the Cloud Run source bucket.
- Writes container images to the source-deployment Artifact Registry repository.
- Writes build logs through `roles/logging.logWriter` at project level.

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
- Cloud Scheduler job or Cloud Run job

## Cost posture

- Cloud Run scales to zero and is capped at one instance.
- Current storage volumes are small and are expected to remain within or close
  to applicable free allowances.
- Three active secret versions are within the Secret Manager free allowance.
- The persistent cache has a 90-day deletion lifecycle to prevent unbounded
  object accumulation.
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
