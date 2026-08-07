export type AnalysisStatus = "success" | "partial_success" | "error";
export type QueryStatus = "success" | "error";
export type ErrorSource = string;

export interface AnalysisImage {
  /** Validated MIME type used to reconstruct the screenshot data URL. */
  media_type: "image/png" | "image/jpeg" | "image/webp";
  /** Base64-encoded screenshot bytes, limited to 10 MiB before encoding. */
  base64_data: string;
}

export interface AnalysisRequest {
  /** Natural-language description of the requested A-share data. */
  prompt: string;
  /** Optional screenshot interpreted before the query plan is generated. */
  image?: AnalysisImage;
}

export interface ConditionalCount {
  /** Label displayed next to the computed count. */
  label: string;
  /** Numeric result field evaluated by the condition. */
  field: string;
  /** Supported local comparison operator. */
  operator: "gt" | "ge" | "eq" | "le" | "lt";
  /** Numeric threshold used by the comparison. */
  value: number;
}

export interface DataFilter {
  /** Scalar result field evaluated by the local row filter. */
  field: string;
  /** Supported local comparison operator. */
  operator: "gt" | "ge" | "eq" | "le" | "lt" | "in";
  /** Numeric threshold, or a string value for exact equality. */
  value: number | string | string[];
}

export interface DataQuery {
  /** Request-local identifier used to match a result to this query. */
  query_id: string;
  /** Provider-native operation selected from the active catalog. */
  operation: string;
  /** Validated keyword arguments passed to the active data provider. */
  params: Record<string, unknown>;
  /** Requested output fields; an empty list uses the API defaults. */
  fields: string[];
  /** Short explanation of why this query is required. */
  purpose: string;
  /** Optional deterministic transformation applied to the provider rows. */
  transform:
    | "cr10_float_trend"
    | "period_return_by_ts_code"
    | null;
  /** Deterministic local row filters applied after provider retrieval. */
  filters: DataFilter[];
  /** Controlled local counts calculated from the returned table. */
  aggregations: ConditionalCount[];
}

export interface RequirementCoverage {
  /** Atomic requirement extracted from the user's request. */
  requirement: string;
  /** Whether available data and deterministic operations satisfy it. */
  status: "covered" | "unsupported";
  /** Provider or local operation used to satisfy the requirement. */
  implementation: string | null;
  /** Concrete capability evidence supporting the decision. */
  evidence: string;
}

export interface QueryPlan {
  /** Fixed market boundary enforced for every analysis request. */
  market: "A_SHARE";
  /** Concise interpretation of the user's data request. */
  interpretation: string;
  /** Whether the complete request can be fulfilled without guessing. */
  feasibility: "supported" | "unsupported";
  /** Coverage evidence for each atomic user requirement. */
  requirements: RequirementCoverage[];
  /** Missing capabilities that prevent faithful execution. */
  limitations: string[];
  /** Complete alternative prompts that resolve material ambiguity. */
  clarification_options: string[];
  /** Optional deterministic operations applied to one or more query results. */
  result_pipeline?: {
    /** Query result consumed as the pipeline input. */
    source_query_id: string;
    /** Identifier assigned to the transformed result. */
    output_query_id: string;
    /** Ordered allowlisted relational operations. */
    steps: Array<{
      /** Allowlisted operation executed by the backend. */
      operation:
        | "latest_by_group"
        | "derive"
        | "drop_missing"
        | "filter"
        | "sort"
        | "limit"
        | "quantile_filter"
        | "aggregate"
        | "rolling_mean"
        | "rolling_sum"
        | "shift"
        | "match_at_offset"
        | "match_source"
        | "compare_fields"
        | "compare_scalar"
        | "summarize";
      field?: string | null;
      output_field?: string | null;
      matched_date_output_field?: string | null;
      right_field?: string | null;
      right_source_query_id?: string | null;
      join_on?: string[];
      fields?: string[];
      group_by?: string[];
      order_by?: string | null;
      direction?: "asc" | "desc";
      arithmetic_operator?:
        | "add"
        | "subtract"
        | "multiply"
        | "divide"
        | "constant_minus"
        | null;
      comparison?: "gt" | "ge" | "eq" | "le" | "lt" | null;
      value?: number | string | null;
      count?: number | null;
      quantile?: number | null;
      window?: number | null;
      min_periods?: number | null;
      periods?: number | null;
      offset_value?: number | null;
      offset_unit?: "day" | "week" | "month" | "year" | "trading_session" | null;
      require_consecutive?: boolean;
      aggregations?: Array<{
        output_field: string;
        label?: string | null;
        field: string;
        function: "count" | "sum" | "mean" | "min" | "max";
      }>;
    }>;
  } | null;
  /** Ordered provider-native reads required to satisfy the request. */
  queries: DataQuery[];
}

export interface DecisionTraceStep {
  /** Stable workflow stage that produced this decision. */
  stage: "requirements" | "capability" | "planning" | "validation" | "execution" | "result";
  /** Display status of this workflow decision. */
  status: "success" | "warning" | "error" | "skipped";
  /** Short label for the decision. */
  title: string;
  /** Concise explanation of what was decided and why. */
  detail: string;
  /** Concrete parameters, fields, rules, or outcomes supporting the decision. */
  evidence: string[];
  /** Whether this step issued a billable external API call. */
  external_call: boolean;
}

export interface ServiceError {
  /** Service or application layer that produced the error. */
  source: ErrorSource;
  /** Original upstream error code when one is available. */
  code: number | string | null;
  /** Original upstream message or a safe system error message. */
  message: string;
  /** HTTP response status returned by an upstream service. */
  http_status: number | null;
  /** Safe copy of the original upstream error body. */
  raw_response: Record<string, unknown> | null;
}

export interface QueryResult {
  /** Identifier of the query that produced this result. */
  query_id: string;
  /** Data provider that executed this query. */
  provider: string;
  /** Provider-native operation used for this result. */
  operation: string;
  /** Whether this individual query succeeded or failed. */
  status: QueryStatus;
  /** Ordered table column names returned by the provider. */
  columns: string[];
  /** JSON-compatible table rows returned by the provider. */
  rows: Array<Record<string, unknown>>;
  /** Number of rows returned for this query. */
  row_count: number;
  /** Controlled local counts calculated from this result. */
  summary: Record<string, number | null>;
  /** Calculation and formatting semantics keyed by summary entry label. */
  summary_metadata?: Record<string, {
    /** Stable machine-readable field containing the summary value. */
    output_field: string;
    /** Input field aggregated to produce the summary value. */
    source_field: string;
    /** Aggregation applied to the source field. */
    function: "count" | "sum" | "mean" | "min" | "max";
    /** Formatting and scaling semantics for the numeric value. */
    value_format: "number" | "percentage_points" | "ratio";
    /** Deterministic expression evaluated to produce the metric. */
    formula: string;
    /** Provider or source-result fields used by the expression. */
    source_fields: string[];
    /** Ordered operations executed before the final aggregation. */
    calculation_steps: Array<{
      /** Allowlisted operation that was actually executed. */
      operation: string;
      /** Existing fields read by this operation. */
      input_fields: string[];
      /** New fields produced by this operation. */
      output_fields: string[];
      /** Validated non-field parameters that control the operation. */
      parameters: Record<string, unknown>;
    }>;
    /** Rows available before the calculation pipeline started. */
    initial_sample_count: number | null;
    /** Non-null observations consumed by the final aggregation. */
    valid_sample_count: number | null;
  }>;
  /** Calculation semantics keyed by generated result column. */
  column_metadata?: Record<string, {
    /** Deterministic expression evaluated to produce the column. */
    formula: string;
    /** Provider or source-result fields used by the expression. */
    source_fields: string[];
    /** Ordered operations executed to produce the column. */
    calculation_steps: Array<{
      operation: string;
      input_fields: string[];
      output_fields: string[];
      parameters: Record<string, unknown>;
    }>;
    /** Formatting and scaling semantics for the column values. */
    value_format: "number" | "percentage_points" | "ratio";
  }>;
  /** Upstream error details when this query failed. */
  error: ServiceError | null;
}

export interface AnalysisResponse {
  /** Identifier used to correlate client requests and server logs. */
  request_id: string;
  /** Planner implementation used for this response. */
  planner: string;
  /** Market-data provider used for this response. */
  data_provider: string;
  /** Overall request status across planning and query execution. */
  status: AnalysisStatus;
  /** Validated query plan when planning completed successfully. */
  plan: QueryPlan | null;
  /** Ordered results corresponding to the plan queries. */
  results: QueryResult[];
  /** Ordered workflow decisions displayed by the client. */
  decision_trace: DecisionTraceStep[];
  /** Planning or system error when no query-level result applies. */
  error: ServiceError | null;
  /** Optional metrics tracking cache hits, misses, and bypasses during execution. */
  cache_metrics?: Record<string, number> | null;
}

export type AnalysisTaskStatus = "queued" | "running" | "succeeded" | "failed";

export interface DiscoveryTaskRequest {
  target_pool: string;
  train_start: string;
  train_end: string;
  val_start: string;
  val_end: string;
  factors: string[];
  prompt: string;
  max_generations: number;
  forward_days: number;
  target_return_pct: number;
  minimum_samples: number;
  minimum_trading_days: number;
  minimum_securities: number;
  minimum_outcome_coverage_pct: number;
  max_conditions: number;
}

export interface BacktestResult {
  win_rate: number;
  mean_return: number;
  max_drawdown: number | null;
  eval_time_ms: number;
  sample_count: number;
  matched_sample_count: number;
  eligible_sample_count: number;
  rule_support_rate: number;
  missing_outcome_count: number;
  outcome_coverage_rate: number;
  positive_count: number;
  median_return: number;
  return_p05: number;
  return_std: number;
  baseline_win_rate: number;
  baseline_sample_count: number;
  win_rate_lift: number;
  lift_confidence_lower: number;
  lift_confidence_upper: number;
  confidence_lower: number;
  confidence_upper: number;
  target_return: number;
  trading_day_count: number;
  security_count: number;
  max_security_event_share: number;
  cluster_standard_error: number;
  lift_standard_error: number;
  dependence_lag_days: number;
  return_price_basis: string;
  /** Bounded recent matched events retained for manual result auditing. */
  event_examples: Array<{
    /** Signal market date in YYYYMMDD format. */
    trade_date: string;
    /** Security code when available in the research dataset. */
    ts_code: string | null;
    /** Market date used to settle the forward-return label. */
    future_trade_date: string | null;
    /** Observed adjusted forward return as a ratio. */
    forward_return: number;
    /** Signal-date values for only the factors referenced by the rule. */
    factor_values: Record<string, number>;
  }>;
}

export interface FactorHypothesis {
  formula: string;
  description: string;
  reasoning: string;
  threshold_source: "unknown" | "quantile" | "observed_value" | "mixed";
  train_result: BacktestResult | null;
  val_result: BacktestResult | null;
  validation_score: number;
  generalization_gap: number;
  support_rate_gap: number;
  support_retention_ratio: number;
  p_value: number;
  q_value: number;
  fdr_family_size: number;
  validation_passed: boolean;
  validation_reason: "not_evaluated" | "training_lift_not_positive" | "insufficient_validation_samples" | "insufficient_validation_days" | "insufficient_validation_securities" | "insufficient_validation_coverage" | "validation_lift_not_positive" | "insufficient_significance_days" | "fdr_not_passed" | "passed";
}

export interface DiscoveryTaskProgress {
  current_generation: number;
  total_generations: number;
  formulas_tested: number;
  candidates_evaluated: number;
  current_log: string;
  current_stage: string;
  training_sample_count: number;
  training_samples_purged: number;
  validation_sample_count: number;
  training_factor_coverage: Record<string, number>;
  validation_factor_coverage: Record<string, number>;
  leaderboard: FactorHypothesis[];
}

export interface DiscoveryTaskStatusResponse {
  task_id: string;
  status: AnalysisTaskStatus;
  research_config: {
    target_pool: "A_SHARE";
    train_start: string;
    train_end: string;
    val_start: string;
    val_end: string;
    factors: string[];
    forward_days: number;
    target_return_pct: number;
    minimum_samples: number;
    minimum_trading_days: number;
    minimum_securities: number;
    minimum_outcome_coverage_pct: number;
    max_conditions: number;
  };
  progress: DiscoveryTaskProgress;
  error: ServiceError | null;
}

export interface AnalysisTaskSubmission {
  /** Stable task identifier returned by the asynchronous submission endpoint. */
  task_id: string;
  /** Initial or reused task lifecycle state. */
  status: AnalysisTaskStatus;
  /** Relative endpoint polled for progress and the terminal result. */
  status_url: string;
}

export interface AnalysisTask {
  /** Stable task identifier. */
  task_id: string;
  /** Current durable lifecycle state. */
  status: AnalysisTaskStatus;
  /** Number of security-specific items completed. */
  completed_items: number;
  /** Total security-specific items discovered. */
  total_items: number;
  /** Terminal response when execution succeeds. */
  response: AnalysisResponse | null;
  /** Terminal worker failure when execution cannot produce a response. */
  error: ServiceError | null;
}

export interface AnalysisTaskProgress {
  /** Stable task identifier used as the analysis trace identifier. */
  taskId: string;
  /** Current durable lifecycle state. */
  status: AnalysisTaskStatus;
  /** Number of security-specific items completed. */
  completedItems: number;
  /** Total security-specific items discovered. */
  totalItems: number;
}

export type StockExchange = "SSE" | "SZSE" | "BSE";

export interface StockListQuery {
  /** Current one-based page number. */
  page: number;
  /** Maximum number of securities returned on one page. */
  page_size: number;
  /** Optional code, name, or industry search text. */
  search: string;
  /** Optional Tushare exchange identifier. */
  exchange: StockExchange | "";
  /** Optional exact industry filter. */
  industry: string;
}

export interface StockListItem {
  /** Tushare security code including its exchange suffix. */
  code: string;
  /** Six-digit security symbol without an exchange suffix. */
  symbol: string;
  /** Official short company name returned by Tushare. */
  name: string;
  /** Registration area returned by Tushare when available. */
  area: string | null;
  /** Industry classification returned by Tushare when available. */
  industry: string | null;
  /** Listing board returned in the Tushare market field. */
  board: string | null;
  /** Tushare exchange identifier for the listed security. */
  exchange: StockExchange;
  /** Initial listing date formatted as an ISO calendar date. */
  listed_on: string;
}

export interface StockListResponse {
  /** Identifier used to correlate client requests and server logs. */
  request_id: string;
  /** Current one-based page number. */
  page: number;
  /** Maximum number of securities returned on one page. */
  page_size: number;
  /** Number of securities matching the active filters. */
  total: number;
  /** Number of pages matching the active filters. */
  total_pages: number;
  /** Sorted industries available across all currently listed securities. */
  available_industries: string[];
  /** Normalized securities contained in the current page. */
  items: StockListItem[];
}

export interface StockListErrorResponse {
  /** Identifier used to correlate client requests and server logs. */
  request_id: string;
  /** Safe provider or application error details. */
  error: ServiceError;
}

export interface UiFeedbackConfig {
  /** Whether every private administrator feedback dependency is configured. */
  enabled: boolean;
  /** Public OAuth client identifier used by Google Identity Services. */
  google_client_id: string;
  /** Source branch represented by the deployed application. */
  git_branch: string;
  /** Exact source commit represented by the deployed application. */
  git_sha: string;
}

export interface UiFeedbackRequest {
  /** Browser path where the administrator selected the content. */
  page_path: string;
  /** Stable application component identifier nearest the selection. */
  feedback_id: string;
  /** Visible text selected by the administrator or captured from an area. */
  selected_text: string;
  /** Optional administrator instruction for the requested improvement. */
  suggestion: string;
  /** Bounded discussion that supports the final improvement instruction. */
  conversation: UiFeedbackConversationMessage[];
  /** Viewport-relative selected area coordinates in CSS pixels. */
  rect: { x: number; y: number; width: number; height: number };
  /** Viewport and scroll state used to understand the selected area. */
  viewport: {
    width: number;
    height: number;
    scroll_x: number;
    scroll_y: number;
  };
}

export interface UiFeedbackConversationMessage {
  /** Participant that authored this discussion message. */
  role: "user" | "assistant";
  /** Plain-text question, reasoning, or conclusion. */
  content: string;
}

export interface UiFeedbackChatRequest {
  /** Browser path where the administrator selected the content. */
  page_path: string;
  /** Stable application component identifier nearest the selection. */
  feedback_id: string;
  /** Visible text captured from the selected region. */
  selected_text: string;
  /** Discussion ending with the newest administrator question. */
  conversation: UiFeedbackConversationMessage[];
}

export interface UiFeedbackChatResponse {
  /** Assistant reply to append to the discussion. */
  message: UiFeedbackConversationMessage;
}

export interface UiFeedbackSubmission {
  /** Durable identifier assigned to the feedback request. */
  feedback_id: string;
  /** Initial dispatch state stored with the feedback record. */
  status: "submitted" | "dispatch_failed";
  /** Repository Actions page where the administrator can inspect execution. */
  actions_url: string;
}
