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
    | "count_by_trade_date"
    | "top_count_by_trade_date"
    | "count_by_ts_code"
    | "top_10_count_by_ts_code"
    | "count_by_industry"
    | "top_20_by_amount"
    | "top_20_by_turnover_rate"
    | "top_20_total_amount_by_ts_code"
    | "period_return_by_ts_code"
    | "top_10_by_dv_ratio"
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
  /** Optional deterministic cross-query calculation. */
  result_transform:
    | "two_limit_up_next_day_probability"
    | "dimension_monthly_turnover_decline"
    | "healthcare_retail_cohort_return"
    | "industry_retail_cohort_return"
    | null;
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
  summary: Record<string, number>;
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
}

export type AnalysisTaskStatus = "queued" | "running" | "succeeded" | "failed";

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

export interface TradingCalendarQuery {
  /** Requested calendar month formatted as YYYY-MM. */
  month: string;
  /** Exchange scope used for both trading status and breadth counts. */
  exchange: "ALL" | "SSE" | "SZSE" | "BSE";
}

export interface TradingCalendarBreadth {
  /** Securities with a positive daily percentage change. */
  advanced: number;
  /** Securities with a negative daily percentage change. */
  declined: number;
  /** Securities with a zero daily percentage change. */
  unchanged: number;
  /** Securities with valid daily market data included in the counts. */
  traded: number;
  /** Advanced divided by declined, or null when no security declined. */
  advance_decline_ratio: number | null;
}

export interface TradingCalendarDay {
  /** Calendar date formatted as an ISO date. */
  date: string;
  /** Whether the reference exchange is open for trading on this date. */
  is_open: boolean;
  /** Most recent preceding open trading date when supplied by Tushare. */
  previous_trading_date: string | null;
  /** End-of-day breadth, unavailable on closed, future, or unpopulated dates. */
  breadth: TradingCalendarBreadth | null;
}

export interface TradingCalendarResponse {
  /** Identifier used to correlate client requests and server logs. */
  request_id: string;
  /** Fixed mainland A-share market boundary represented by this calendar. */
  market: "A_SHARE";
  /** Requested calendar month formatted as YYYY-MM. */
  month: string;
  /** Exchange scope selected for the calendar and breadth counts. */
  exchange: "ALL" | "SSE" | "SZSE" | "BSE";
  /** Exchange calendars and securities included in this response. */
  source_exchanges: Array<"SSE" | "SZSE" | "BSE">;
  /** Chronological trading status for every calendar date in the month. */
  days: TradingCalendarDay[];
}

export interface TradingCalendarErrorResponse {
  /** Identifier used to correlate client requests and server logs. */
  request_id: string;
  /** Safe provider or application error details. */
  error: ServiceError;
}
