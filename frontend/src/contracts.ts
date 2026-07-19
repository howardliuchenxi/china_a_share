export type AnalysisStatus = "success" | "partial_success" | "error";
export type QueryStatus = "success" | "error";
export type ErrorSource = "tushare" | "deepseek" | "system";

export interface AnalysisRequest {
  /** Natural-language description of the requested A-share data. */
  prompt: string;
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

export interface TushareQuery {
  /** Request-local identifier used to match a result to this query. */
  query_id: string;
  /** Allowlisted Tushare stock API name. */
  api_name: string;
  /** Validated keyword arguments passed to the Tushare API. */
  params: Record<string, unknown>;
  /** Requested output fields; an empty list uses the API defaults. */
  fields: string[];
  /** Short explanation of why this query is required. */
  purpose: string;
  /** Controlled local counts calculated from the returned table. */
  aggregations: ConditionalCount[];
}

export interface QueryPlan {
  /** Fixed market boundary enforced for every analysis request. */
  market: "A_SHARE";
  /** Concise interpretation of the user's data request. */
  interpretation: string;
  /** Ordered Tushare calls required to satisfy the request. */
  queries: TushareQuery[];
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
  /** Identifier of the Tushare query that produced this result. */
  query_id: string;
  /** Tushare API name used for this result. */
  api_name: string;
  /** Whether this individual query succeeded or failed. */
  status: QueryStatus;
  /** Ordered table column names returned by Tushare. */
  columns: string[];
  /** JSON-compatible table rows returned by Tushare. */
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
  /** Overall request status across planning and query execution. */
  status: AnalysisStatus;
  /** Validated query plan when planning completed successfully. */
  plan: QueryPlan | null;
  /** Ordered results corresponding to the plan queries. */
  results: QueryResult[];
  /** Planning or system error when no query-level result applies. */
  error: ServiceError | null;
}
