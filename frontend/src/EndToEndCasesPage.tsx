import { FormEvent, useEffect, useMemo, useState } from "react";

import { fetchLiveCases, submitAnalysis, submitLiveCaseChange } from "./api";
import type { AnalysisResponse, LiveCase, LiveCaseListResponse } from "./contracts";
import {
  ADMIN_AUTHENTICATED_EVENT,
  ADMIN_ID_TOKEN_STORAGE_KEY,
} from "./UiFeedbackController";

interface EndToEndRunResult {
  /** Case definition captured when the run started. */
  testCase: LiveCase;
  /** Whether the live response matched the maintained expectation. */
  passed: boolean;
  /** End-to-end browser-observed duration in milliseconds. */
  durationMs: number;
  /** Request identifier used to find matching backend logs. */
  requestId: string;
  /** Concrete failure explanation suitable for a Codex repair request. */
  failureReason: string;
  /** Planner and provider evidence returned by the public workflow. */
  runtime: string;
}

interface LegacyBrowserCase {
  /** Browser-local identifier created by the first test-console version. */
  id: string;
  /** Operator-facing legacy scenario name. */
  name: string;
  /** Exact legacy prompt to migrate into the Git catalog. */
  prompt: string;
  /** Legacy feasibility expectation preserved during migration. */
  expectedFeasibility: "supported" | "unsupported";
}

const LEGACY_CASE_STORAGE_KEY = "china-a-share.end-to-end-cases.v1";

function loadLegacyCases(): LegacyBrowserCase[] {
  try {
    const value: unknown = JSON.parse(
      window.localStorage.getItem(LEGACY_CASE_STORAGE_KEY) ?? "[]",
    );
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is LegacyBrowserCase => {
      if (!item || typeof item !== "object") return false;
      const candidate = item as Partial<LegacyBrowserCase>;
      return typeof candidate.id === "string"
        && typeof candidate.name === "string"
        && typeof candidate.prompt === "string"
        && candidate.expectedFeasibility === "supported";
    });
  } catch {
    return [];
  }
}

function failureFromResponse(
  response: AnalysisResponse,
): string {
  if (response.error) return `${response.error.source}: ${response.error.message}`;
  if (!response.plan) return "The response did not include a query plan.";
  if (response.plan.feasibility !== "supported") {
    return `Expected a supported plan, received ${response.plan.feasibility}.`;
  }
  const failedResult = response.results.find((result) => result.status === "error");
  if (failedResult?.error) return `${failedResult.operation}: ${failedResult.error.message}`;
  if (response.status !== "success") {
    return `Expected a successful response, received status ${response.status}.`;
  }
  return "";
}

function reportText(results: EndToEndRunResult[]): string {
  const passed = results.filter((result) => result.passed).length;
  const lines = [
    "A-share live end-to-end report",
    `Summary: ${passed}/${results.length} passed; ${results.length - passed} failed`,
    "",
  ];
  results.forEach((result, index) => {
    lines.push(
      `${index + 1}. ${result.passed ? "PASS" : "FAIL"} — ${result.testCase.id}`,
      `Case ID: ${result.testCase.id}`,
      `Prompt: ${result.testCase.prompt}`,
      `Duration: ${result.durationMs} ms`,
      `Request ID: ${result.requestId || "unavailable"}`,
      `Runtime: ${result.runtime || "unavailable"}`,
      `Failure reason: ${result.failureReason || "none"}`,
      "",
    );
  });
  return lines.join("\n").trim();
}

function newCaseId(prompt: string): string {
  const slug = prompt.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return `${slug || "live-case"}-${crypto.randomUUID().slice(0, 8)}`;
}

export function EndToEndCasesPage() {
  const [idToken, setIdToken] = useState(() => window.sessionStorage.getItem(ADMIN_ID_TOKEN_STORAGE_KEY) ?? "");
  const [catalog, setCatalog] = useState<LiveCaseListResponse | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [editingId, setEditingId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [results, setResults] = useState<EndToEndRunResult[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [completedCount, setCompletedCount] = useState(0);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [legacyCases, setLegacyCases] = useState<LegacyBrowserCase[]>(loadLegacyCases);

  const cases = (catalog?.cases ?? []).filter(
    (testCase) => testCase.expected_feasibility === "supported",
  );
  const pendingDeletionIds = new Set(catalog?.pending_deletions ?? []);
  const selectedCases = useMemo(
    () => cases.filter((testCase) => selectedIds.has(testCase.id)),
    [cases, selectedIds],
  );

  async function loadCatalog(token = idToken) {
    if (!token) return;
    setError("");
    try {
      setCatalog(await fetchLiveCases(token));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "端到端用例加载失败。");
    }
  }

  useEffect(() => {
    const authenticated = () => {
      const token = window.sessionStorage.getItem(ADMIN_ID_TOKEN_STORAGE_KEY) ?? "";
      setIdToken(token);
      void loadCatalog(token);
    };
    window.addEventListener(ADMIN_AUTHENTICATED_EVENT, authenticated);
    if (idToken) void loadCatalog(idToken);
    return () => window.removeEventListener(ADMIN_AUTHENTICATED_EVENT, authenticated);
  }, []);

  function resetEditor() {
    setEditingId("");
    setPrompt("");
  }

  async function saveCase(event: FormEvent) {
    event.preventDefault();
    if (!catalog) return;
    const existing = cases.find((item) => item.id === editingId);
    const caseId = existing?.id ?? newCaseId(prompt);
    const generatedName = prompt.trim().replace(/\s+/g, " ").slice(0, 60);
    const desiredCase = {
      id: caseId,
      name: existing?.name ?? generatedName,
      family: existing?.family ?? "manual_regression",
      prompt: prompt.trim(),
      expected_feasibility: "supported" as const,
      tier: "supported" as const,
      operations: existing?.operations ?? [],
      quality_invariants: existing?.quality_invariants ?? [],
      source: existing?.source ?? "reported_regression" as const,
    };
    setIsSaving(true);
    setError("");
    try {
      const submission = await submitLiveCaseChange(idToken, {
        operation: existing ? "update" : "create",
        case: desiredCase,
        base_git_sha: catalog.git_sha,
      });
      resetEditor();
      setMessage(`变更 ${submission.change_id} 已提交，正在等待 GitHub 验证和发布。`);
      await loadCatalog();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "端到端用例保存失败。");
    } finally {
      setIsSaving(false);
    }
  }

  function editCase(testCase: LiveCase) {
    setEditingId(testCase.id);
    setPrompt(testCase.prompt);
    setMessage("");
  }

  async function deleteCase(testCase: LiveCase) {
    if (!catalog || !window.confirm(`确认提交删除这个用例？\n\n${testCase.prompt}`)) return;
    setIsSaving(true);
    setError("");
    try {
      const submission = await submitLiveCaseChange(idToken, {
        operation: "delete",
        case_id: testCase.id,
        base_git_sha: catalog.git_sha,
      });
      setSelectedIds((current) => {
        const next = new Set(current);
        next.delete(testCase.id);
        return next;
      });
      setMessage(`删除变更 ${submission.change_id} 已提交，发布前用例仍会保留。`);
      await loadCatalog();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "端到端用例删除失败。");
    } finally {
      setIsSaving(false);
    }
  }

  async function runSelectedCases() {
    if (selectedCases.length === 0) return;
    setIsRunning(true);
    setCompletedCount(0);
    setResults([]);
    setMessage("");
    const nextResults: EndToEndRunResult[] = [];
    for (const testCase of selectedCases) {
      const startedAt = performance.now();
      try {
        const response = await submitAnalysis({ prompt: testCase.prompt });
        const failureReason = failureFromResponse(response);
        nextResults.push({
          testCase,
          passed: failureReason === "",
          durationMs: Math.round(performance.now() - startedAt),
          requestId: response.request_id,
          failureReason,
          runtime: `${response.planner} / ${response.data_provider}`,
        });
      } catch (caught) {
        nextResults.push({
          testCase,
          passed: false,
          durationMs: Math.round(performance.now() - startedAt),
          requestId: "",
          failureReason: caught instanceof Error ? caught.message : "Unknown browser execution failure.",
          runtime: "",
        });
      }
      setResults([...nextResults]);
      setCompletedCount(nextResults.length);
    }
    setIsRunning(false);
  }

  async function importLegacyCases() {
    if (!catalog || legacyCases.length === 0) return;
    setIsSaving(true);
    setError("");
    try {
      for (const legacyCase of legacyCases) {
        await submitLiveCaseChange(idToken, {
          operation: "create",
          case: {
            id: `browser-${legacyCase.id}`,
            name: legacyCase.name,
            family: "browser_migration",
            prompt: legacyCase.prompt,
            expected_feasibility: "supported",
            tier: "supported",
            operations: [],
            quality_invariants: [],
            source: "reported_regression",
          },
          base_git_sha: catalog.git_sha,
        });
      }
      window.localStorage.removeItem(LEGACY_CASE_STORAGE_KEY);
      setLegacyCases([]);
      setMessage(`${legacyCases.length} 个浏览器本地用例已提交到 Git 发布队列。`);
      await loadCatalog();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "浏览器本地用例迁移失败。");
    } finally {
      setIsSaving(false);
    }
  }

  async function copyReport() {
    await navigator.clipboard.writeText(reportText(results));
    setMessage("运行报告已复制，可以直接粘贴给 Codex。");
  }

  if (!idToken) {
    return <section className="reference-panel"><p className="empty-state">正式用例目录仅对管理员开放。请先使用页面右上角的 Google 管理员登录；登录成功后会立即加载全部可运行用例。</p></section>;
  }

  return (
    <div className="e2e-page" data-feedback-id="end-to-end-cases">
      <section className="reference-panel e2e-editor" aria-labelledby="e2e-editor-heading">
        <div className="reference-view-heading"><h2 id="e2e-editor-heading">{editingId ? "更新用例" : "新增用例"}</h2><span>{catalog ? `正式版本 ${catalog.git_sha.slice(0, 8)}` : "正在加载…"}</span></div>
        <form onSubmit={saveCase}>
          <label><span>问题</span><textarea rows={4} value={prompt} onChange={(event) => setPrompt(event.target.value)} /></label>
          <div className="e2e-actions"><button type="submit" disabled={!catalog || !prompt.trim() || isSaving}>{isSaving ? "正在提交…" : editingId ? "提交更新" : "提交新增"}</button>{editingId && <button type="button" className="secondary-button" onClick={resetEditor}>取消</button>}</div>
        </form>
      </section>

      <section className="reference-panel" aria-labelledby="e2e-list-heading">
        <div className="reference-view-heading"><h2 id="e2e-list-heading">端到端用例</h2><span>共 {cases.length} 个，已选 {selectedCases.length} 个</span></div>
        <div className="e2e-toolbar"><button type="button" className="secondary-button" disabled={cases.length === 0 || isRunning} onClick={() => setSelectedIds(selectedIds.size === cases.length ? new Set() : new Set(cases.map((item) => item.id)))}>{selectedIds.size === cases.length && cases.length > 0 ? "取消全选" : "全选"}</button><button type="button" disabled={selectedCases.length === 0 || isRunning} onClick={() => void runSelectedCases()}>{isRunning ? `运行中 ${completedCount}/${selectedCases.length}` : "一键运行所选用例"}</button><button type="button" className="secondary-button" disabled={isRunning || isSaving} onClick={() => void loadCatalog()}>刷新发布状态</button>{legacyCases.length > 0 && <button type="button" className="secondary-button" disabled={isRunning || isSaving} onClick={() => void importLegacyCases()}>迁移 {legacyCases.length} 个浏览器本地用例</button>}</div>
        {message && <p className="e2e-message" role="status">{message}</p>}
        {error && <p className="error-card" role="alert">{error}</p>}
        {cases.length === 0 ? <p className="empty-state">正在加载正式端到端用例；如果持续为空，请刷新发布状态。</p> : <div className="stock-table-scroll"><table className="stock-table"><thead><tr><th>选择</th><th>问题</th><th>发布状态</th>{idToken && <th>操作</th>}</tr></thead><tbody>{cases.map((testCase) => {
          const pendingDeletion = pendingDeletionIds.has(testCase.id);
          const isPending = testCase.publication_status === "pending";
          const publicationInProgress = testCase.publication_status !== "published";
          const statusClass = testCase.publication_status === "failed"
            ? "status-failed"
            : isPending || pendingDeletion
              ? "status-pending"
              : "status-published";
          const statusLabel = pendingDeletion
            ? "等待删除"
            : isPending
              ? "等待发布"
              : testCase.publication_status === "failed"
                ? "提交失败"
                : "已发布";
          return <tr key={testCase.id}><td><input type="checkbox" aria-label={`选择 ${testCase.prompt}`} checked={selectedIds.has(testCase.id)} disabled={isRunning} onChange={() => setSelectedIds((current) => { const next = new Set(current); next.has(testCase.id) ? next.delete(testCase.id) : next.add(testCase.id); return next; })} /></td><td>{testCase.prompt}<br /><code>{testCase.id}</code></td><td><span className={statusClass}>{statusLabel}</span></td>{idToken && <td><div className="e2e-row-actions"><button type="button" className="text-button" disabled={isRunning || isSaving || pendingDeletion || publicationInProgress} onClick={() => editCase(testCase)}>编辑</button><button type="button" className="text-button danger" disabled={isRunning || isSaving || pendingDeletion || publicationInProgress} onClick={() => void deleteCase(testCase)}>删除</button></div></td>}</tr>;
        })}</tbody></table></div>}
      </section>

      {results.length > 0 && <section className="reference-panel" aria-labelledby="e2e-report-heading"><div className="reference-view-heading"><h2 id="e2e-report-heading">运行报告</h2><button type="button" className="secondary-button" onClick={() => void copyReport()}>复制给 Codex</button></div><div className="e2e-summary">通过 {results.filter((result) => result.passed).length} / {results.length}</div><div className="stock-table-scroll"><table className="stock-table"><thead><tr><th>结果</th><th>问题</th><th>耗时</th><th>追踪 ID</th><th>失败原因</th></tr></thead><tbody>{results.map((result) => <tr key={result.testCase.id} className={result.passed ? "e2e-pass" : "e2e-fail"}><td><strong>{result.passed ? "成功" : "失败"}</strong></td><td>{result.testCase.prompt}</td><td>{(result.durationMs / 1000).toFixed(2)} 秒</td><td><code>{result.requestId || "—"}</code></td><td>{result.failureReason || "—"}</td></tr>)}</tbody></table></div></section>}
    </div>
  );
}
