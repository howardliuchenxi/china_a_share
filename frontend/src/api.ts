import type {
  AnalysisRequest,
  AnalysisResponse,
  AnalysisTask,
  AnalysisTaskProgress,
  AnalysisTaskSubmission,
  ServiceError,
  StockListErrorResponse,
  StockListQuery,
  StockListResponse,
} from "./contracts";

const ANALYSIS_TASK_POLL_INTERVAL_MS = 2_000;

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
  onProgress?: (progress: AnalysisTaskProgress) => void,
): Promise<AnalysisResponse> {
  const response = await fetch("/api/analysis", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  const responseText = await response.text();
  let payload: AnalysisResponse | AnalysisTaskSubmission | { detail?: unknown };
  try {
    payload = JSON.parse(responseText) as
      | AnalysisResponse
      | AnalysisTaskSubmission
      | { detail?: unknown };
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
  if (response.status === 202 && "status_url" in payload) {
    return pollAnalysisTask(payload, onProgress);
  }
  return payload as AnalysisResponse;
}

async function pollAnalysisTask(
  submission: AnalysisTaskSubmission,
  onProgress?: (progress: AnalysisTaskProgress) => void,
): Promise<AnalysisResponse> {
  let statusUrl = submission.status_url;
  while (true) {
    const response = await fetch(statusUrl);
    if (!response.ok) {
      throw new Error(`\u4efb\u52a1\u72b6\u6001\u67e5\u8be2\u8fd4\u56de HTTP ${response.status}\u3002`);
    }
    const task = (await response.json()) as AnalysisTask;
    onProgress?.({
      status: task.status,
      completedItems: task.completed_items,
      totalItems: task.total_items,
    });
    if (task.status === "succeeded") {
      if (!task.response) {
        throw new Error("\u4efb\u52a1\u5df2\u5b8c\u6210\uff0c\u4f46\u7f3a\u5c11\u5206\u6790\u7ed3\u679c\u3002");
      }
      return task.response;
    }
    if (task.status === "failed") {
      throw new Error(task.error?.message ?? "\u5f02\u6b65\u5206\u6790\u4efb\u52a1\u5931\u8d25\u3002");
    }
    await new Promise((resolve) => {
      window.setTimeout(resolve, ANALYSIS_TASK_POLL_INTERVAL_MS);
    });
    statusUrl = submission.status_url;
  }
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
