import { FormEvent, useMemo, useState } from "react";

import { submitAnalysis } from "./api";
import type { AnalysisResponse } from "./contracts";

type ExpectedFeasibility = "supported" | "unsupported";

interface EndToEndCase {
  /** Browser-local stable identifier used for selection and updates. */
  id: string;
  /** Short operator-facing label for the regression scenario. */
  name: string;
  /** Exact natural-language request sent through the public analysis workflow. */
  prompt: string;
  /** Expected planner feasibility used to decide whether the run passed. */
  expectedFeasibility: ExpectedFeasibility;
}

interface EndToEndRunResult {
  /** Case definition captured when the run started. */
  testCase: EndToEndCase;
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

const CASE_STORAGE_KEY = "china-a-share.end-to-end-cases.v1";

function loadCases(): EndToEndCase[] {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(CASE_STORAGE_KEY) ?? "[]");
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is EndToEndCase => {
      if (!item || typeof item !== "object") return false;
      const candidate = item as Partial<EndToEndCase>;
      return typeof candidate.id === "string"
        && typeof candidate.name === "string"
        && typeof candidate.prompt === "string"
        && (candidate.expectedFeasibility === "supported"
          || candidate.expectedFeasibility === "unsupported");
    });
  } catch {
    return [];
  }
}

function failureFromResponse(
  response: AnalysisResponse,
  expectedFeasibility: ExpectedFeasibility,
): string {
  if (response.error) return `${response.error.source}: ${response.error.message}`;
  if (!response.plan) return "The response did not include a query plan.";
  if (response.plan.feasibility !== expectedFeasibility) {
    return `Expected feasibility ${expectedFeasibility}, received ${response.plan.feasibility}.`;
  }
  const failedResult = response.results.find((result) => result.status === "error");
  if (failedResult?.error) {
    return `${failedResult.operation}: ${failedResult.error.message}`;
  }
  if (expectedFeasibility === "supported" && response.status !== "success") {
    return `Expected a successful response, received status ${response.status}.`;
  }
  if (expectedFeasibility === "unsupported" && response.status !== "error") {
    return `Expected an unsupported error response, received status ${response.status}.`;
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
      `${index + 1}. ${result.passed ? "PASS" : "FAIL"} — ${result.testCase.name}`,
      `Prompt: ${result.testCase.prompt}`,
      `Expected feasibility: ${result.testCase.expectedFeasibility}`,
      `Duration: ${result.durationMs} ms`,
      `Request ID: ${result.requestId || "unavailable"}`,
      `Runtime: ${result.runtime || "unavailable"}`,
      `Failure reason: ${result.failureReason || "none"}`,
      "",
    );
  });
  return lines.join("\n").trim();
}

export function EndToEndCasesPage() {
  const [cases, setCases] = useState<EndToEndCase[]>(loadCases);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [editingId, setEditingId] = useState("");
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [expectedFeasibility, setExpectedFeasibility] = useState<ExpectedFeasibility>("supported");
  const [results, setResults] = useState<EndToEndRunResult[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [completedCount, setCompletedCount] = useState(0);
  const [message, setMessage] = useState("");

  const selectedCases = useMemo(
    () => cases.filter((testCase) => selectedIds.has(testCase.id)),
    [cases, selectedIds],
  );

  function persist(nextCases: EndToEndCase[]) {
    window.localStorage.setItem(CASE_STORAGE_KEY, JSON.stringify(nextCases));
    setCases(nextCases);
  }

  function resetEditor() {
    setEditingId("");
    setName("");
    setPrompt("");
    setExpectedFeasibility("supported");
  }

  function saveCase(event: FormEvent) {
    event.preventDefault();
    const normalizedName = name.trim();
    const normalizedPrompt = prompt.trim();
    if (!normalizedName || !normalizedPrompt) return;
    const testCase: EndToEndCase = {
      id: editingId || crypto.randomUUID(),
      name: normalizedName,
      prompt: normalizedPrompt,
      expectedFeasibility,
    };
    persist(editingId
      ? cases.map((item) => item.id === editingId ? testCase : item)
      : [...cases, testCase]);
    resetEditor();
    setMessage(editingId ? "用例已更新。" : "用例已新增。");
  }

  function editCase(testCase: EndToEndCase) {
    setEditingId(testCase.id);
    setName(testCase.name);
    setPrompt(testCase.prompt);
    setExpectedFeasibility(testCase.expectedFeasibility);
    setMessage("");
  }

  function deleteCase(testCase: EndToEndCase) {
    if (!window.confirm(`确认删除用例“${testCase.name}”？`)) return;
    persist(cases.filter((item) => item.id !== testCase.id));
    setSelectedIds((current) => {
      const next = new Set(current);
      next.delete(testCase.id);
      return next;
    });
    if (editingId === testCase.id) resetEditor();
    setMessage("用例已删除。");
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
        const failureReason = failureFromResponse(response, testCase.expectedFeasibility);
        nextResults.push({
          testCase,
          passed: failureReason === "",
          durationMs: Math.round(performance.now() - startedAt),
          requestId: response.request_id,
          failureReason,
          runtime: `${response.planner} / ${response.data_provider}`,
        });
      } catch (error) {
        nextResults.push({
          testCase,
          passed: false,
          durationMs: Math.round(performance.now() - startedAt),
          requestId: "",
          failureReason: error instanceof Error ? error.message : "Unknown browser execution failure.",
          runtime: "",
        });
      }
      setResults([...nextResults]);
      setCompletedCount(nextResults.length);
    }
    setIsRunning(false);
  }

  async function copyReport() {
    await navigator.clipboard.writeText(reportText(results));
    setMessage("运行报告已复制，可以直接粘贴给 Codex。");
  }

  return (
    <div className="e2e-page" data-feedback-id="end-to-end-cases">
      <section className="reference-panel e2e-editor" aria-labelledby="e2e-editor-heading">
        <div className="reference-view-heading">
          <h2 id="e2e-editor-heading">{editingId ? "更新用例" : "新增用例"}</h2>
          <span>用例保存在当前浏览器</span>
        </div>
        <form onSubmit={saveCase}>
          <label><span>名称</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label><span>问题</span><textarea rows={4} value={prompt} onChange={(event) => setPrompt(event.target.value)} /></label>
          <label><span>预期可行性</span>
            <select value={expectedFeasibility} onChange={(event) => setExpectedFeasibility(event.target.value as ExpectedFeasibility)}>
              <option value="supported">支持</option><option value="unsupported">不支持</option>
            </select>
          </label>
          <div className="e2e-actions">
            <button type="submit" disabled={!name.trim() || !prompt.trim()}>{editingId ? "保存更新" : "新增用例"}</button>
            {editingId && <button type="button" className="secondary-button" onClick={resetEditor}>取消</button>}
          </div>
        </form>
      </section>

      <section className="reference-panel" aria-labelledby="e2e-list-heading">
        <div className="reference-view-heading">
          <h2 id="e2e-list-heading">端到端用例</h2><span>共 {cases.length} 个，已选 {selectedCases.length} 个</span>
        </div>
        <div className="e2e-toolbar">
          <button type="button" className="secondary-button" disabled={cases.length === 0 || isRunning} onClick={() => setSelectedIds(selectedIds.size === cases.length ? new Set() : new Set(cases.map((item) => item.id)))}>
            {selectedIds.size === cases.length && cases.length > 0 ? "取消全选" : "全选"}
          </button>
          <button type="button" disabled={selectedCases.length === 0 || isRunning} onClick={() => void runSelectedCases()}>
            {isRunning ? `运行中 ${completedCount}/${selectedCases.length}` : "一键运行所选用例"}
          </button>
        </div>
        {message && <p className="e2e-message" role="status">{message}</p>}
        {cases.length === 0 ? <p className="empty-state">暂无用例，请先新增一个端到端问题。</p> : (
          <div className="stock-table-scroll"><table className="stock-table"><thead><tr><th>选择</th><th>名称</th><th>问题</th><th>预期</th><th>操作</th></tr></thead>
            <tbody>{cases.map((testCase) => <tr key={testCase.id}>
              <td><input type="checkbox" aria-label={`选择 ${testCase.name}`} checked={selectedIds.has(testCase.id)} disabled={isRunning} onChange={() => setSelectedIds((current) => { const next = new Set(current); next.has(testCase.id) ? next.delete(testCase.id) : next.add(testCase.id); return next; })} /></td>
              <td><strong>{testCase.name}</strong></td><td>{testCase.prompt}</td><td>{testCase.expectedFeasibility === "supported" ? "支持" : "不支持"}</td>
              <td><div className="e2e-row-actions"><button type="button" className="text-button" disabled={isRunning} onClick={() => editCase(testCase)}>编辑</button><button type="button" className="text-button danger" disabled={isRunning} onClick={() => deleteCase(testCase)}>删除</button></div></td>
            </tr>)}</tbody>
          </table></div>
        )}
      </section>

      {results.length > 0 && <section className="reference-panel" aria-labelledby="e2e-report-heading">
        <div className="reference-view-heading"><h2 id="e2e-report-heading">运行报告</h2><button type="button" className="secondary-button" onClick={() => void copyReport()}>复制给 Codex</button></div>
        <div className="e2e-summary">通过 {results.filter((result) => result.passed).length} / {results.length}</div>
        <div className="stock-table-scroll"><table className="stock-table"><thead><tr><th>结果</th><th>用例</th><th>耗时</th><th>追踪 ID</th><th>失败原因</th></tr></thead>
          <tbody>{results.map((result) => <tr key={result.testCase.id} className={result.passed ? "e2e-pass" : "e2e-fail"}><td><strong>{result.passed ? "成功" : "失败"}</strong></td><td>{result.testCase.name}</td><td>{(result.durationMs / 1000).toFixed(2)} 秒</td><td><code>{result.requestId || "—"}</code></td><td>{result.failureReason || "—"}</td></tr>)}</tbody>
        </table></div>
      </section>}
    </div>
  );
}
