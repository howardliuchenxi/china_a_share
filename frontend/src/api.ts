import type {
  AnalysisRequest,
  AnalysisResponse,
  ServiceError,
  StockListErrorResponse,
  StockListQuery,
  StockListResponse,
} from "./contracts";

export class StockListRequestError extends Error {
  /** Structured backend failure associated with the rejected stock request. */
  readonly serviceError: ServiceError;

  constructor(serviceError: ServiceError) {
    super(serviceError.message);
    this.name = "StockListRequestError";
    this.serviceError = serviceError;
  }
}

export async function submitAnalysis(
  request: AnalysisRequest,
): Promise<AnalysisResponse> {
  const response = await fetch("/api/analysis", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  const responseText = await response.text();
  let payload: AnalysisResponse | { detail?: unknown };
  try {
    payload = JSON.parse(responseText) as AnalysisResponse | { detail?: unknown };
  } catch {
    if (!response.ok) {
      throw new Error(
        responseText.trim() ||
          `\u672c\u5730 API \u8fd4\u56de HTTP ${response.status}\u3002`,
      );
    }
    throw new Error("\u672c\u5730 API \u8fd4\u56de\u4e86\u65e0\u6548\u7684 JSON \u54cd\u5e94\u3002");
  }
  if (!response.ok) {
    const detail = "detail" in payload ? payload.detail : null;
    throw new Error(
      typeof detail === "string" ? detail : `本地 API 返回 HTTP ${response.status}。`,
    );
  }
  return payload as AnalysisResponse;
}

export async function fetchStocks(
  query: StockListQuery,
  signal?: AbortSignal,
): Promise<StockListResponse> {
  const parameters = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.page_size),
  });
  if (query.search) parameters.set("search", query.search);
  if (query.exchange) parameters.set("exchange", query.exchange);
  if (query.industry) parameters.set("industry", query.industry);

  const response = await fetch(`/api/stocks?${parameters.toString()}`, { signal });
  const payload = (await response.json()) as StockListResponse | StockListErrorResponse;
  if (!response.ok) {
    if ("error" in payload) throw new StockListRequestError(payload.error);
    throw new Error(`本地 API 返回 HTTP ${response.status}。`);
  }
  return payload as StockListResponse;
}
