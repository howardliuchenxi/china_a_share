import type { AnalysisRequest, AnalysisResponse } from "./contracts";

export async function submitAnalysis(
  request: AnalysisRequest,
): Promise<AnalysisResponse> {
  const response = await fetch("/api/analysis", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  const payload = (await response.json()) as AnalysisResponse | { detail?: unknown };
  if (!response.ok) {
    const detail = "detail" in payload ? payload.detail : null;
    throw new Error(
      typeof detail === "string" ? detail : `本地 API 返回 HTTP ${response.status}。`,
    );
  }
  return payload as AnalysisResponse;
}
