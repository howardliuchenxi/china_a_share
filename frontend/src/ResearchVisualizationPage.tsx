import { useEffect, useMemo, useState } from "react";
import { columnNote } from "./researchColumnNotes";
import { isSecurityCodeColumn, securityQuotePageUrl } from "./securityLinks";

type ViewerValue = string | number | boolean | null;

interface ResearchVisualization {
  /** Human-readable title generated with the research workbook. */
  title: string;
  /** Ordered dataset columns available for the result table. */
  columns: string[];
  /** Bounded result rows retained for interactive exploration. */
  rows: Array<Record<string, ViewerValue>>;
  /** Complete result count before the viewer row limit. */
  source_row_count: number;
  /** Whether the complete workbook contains additional rows. */
  truncated: boolean;
  /** Recorded workbook explanation per column, shown as hover notes. */
  column_notes?: Record<string, string>;
  /** Bounded methodology text recorded with the workbook. */
  methodology?: string;
}

interface ResearchVisualizationResponse {
  /** Stable identifier for the completed Feishu research task. */
  task_id: string;
  /** Text conclusion produced by the research model. */
  answer: string;
  /** Data retained for the searchable result table. */
  visualization: ResearchVisualization;
  /** Workbook filename available from the protected download endpoint. */
  artifact_name: string | null;
  /** ISO timestamp after which the bearer link becomes invalid. */
  expires_at: string;
}

const TABLE_ROW_LIMIT = 100;

function isDateColumn(column: string): boolean {
  const normalized = column.trim().toLocaleLowerCase("zh-CN").replaceAll(" ", "_");
  return ["date", "日期", "时间", "报告期", "收盘日", "交易日"].some((suffix) => (
    normalized.endsWith(suffix)
  ));
}

function compactCalendarDate(value: ViewerValue): string | null {
  const text = typeof value === "number" && Number.isInteger(value)
    ? String(value)
    : typeof value === "string" ? value.trim() : "";
  if (!/^\d{8}$/.test(text)) return null;
  const year = Number(text.slice(0, 4));
  const month = Number(text.slice(4, 6));
  const day = Number(text.slice(6, 8));
  const parsed = new Date(Date.UTC(year, month - 1, day));
  if (
    parsed.getUTCFullYear() !== year
    || parsed.getUTCMonth() !== month - 1
    || parsed.getUTCDate() !== day
  ) return null;
  return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`;
}

function displayValue(value: ViewerValue, column = ""): string {
  if (value === null || value === undefined) return "—";
  const calendarDate = isDateColumn(column) ? compactCalendarDate(value) : null;
  if (calendarDate) return calendarDate;
  if (typeof value === "number") {
    return value.toLocaleString("zh-CN", { maximumFractionDigits: 6 });
  }
  return String(value);
}

export default function ResearchVisualizationPage() {
  const taskId = window.location.pathname.split("/").filter(Boolean).at(-1) ?? "";
  const token = new URLSearchParams(window.location.search).get("token") ?? "";
  const [payload, setPayload] = useState<ResearchVisualizationResponse | null>(null);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");

  useEffect(() => {
    if (!taskId || !token) {
      setError("链接不完整，请从飞书研究结果中重新打开。");
      return;
    }
    const controller = new AbortController();
    fetch(`/api/research/visualizations/${encodeURIComponent(taskId)}?token=${encodeURIComponent(token)}`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) {
          if (response.status === 410) throw new Error("该研究链接已经过期，请在飞书中重新生成。");
          throw new Error("无法打开研究结果，请检查链接是否完整。");
        }
        return response.json() as Promise<ResearchVisualizationResponse>;
      })
      .then((result) => {
        setPayload(result);
        document.title = `${result.visualization.title} · A股研究助手`;
      })
      .catch((requestError: Error) => {
        if (requestError.name !== "AbortError") setError(requestError.message);
      });
    return () => controller.abort();
  }, [taskId, token]);

  const filteredRows = useMemo(() => {
    const rows = payload?.visualization.rows ?? [];
    const normalizedSearch = search.trim().toLocaleLowerCase("zh-CN");
    if (!normalizedSearch) return rows;
    return rows.filter((row) => Object.entries(row).some(([column, value]) => (
      displayValue(value, column).toLocaleLowerCase("zh-CN").includes(normalizedSearch)
    )));
  }, [payload, search]);

  if (error) {
    return (
      <main className="research-viewer-shell research-viewer-state">
        <div className="research-viewer-state-card">
          <span className="research-viewer-state-icon">!</span>
          <h1>研究结果暂时不可用</h1>
          <p>{error}</p>
        </div>
      </main>
    );
  }

  if (!payload) {
    return (
      <main className="research-viewer-shell research-viewer-state">
        <div className="research-viewer-state-card">
          <span className="research-viewer-loader" />
          <h1>正在打开研究结果</h1>
          <p>无需填写内容，数据加载完成后会直接展示。</p>
        </div>
      </main>
    );
  }

  const { visualization } = payload;
  const tableRows = filteredRows.slice(0, TABLE_ROW_LIMIT);
  const workbookUrl = `/api/research/visualizations/${encodeURIComponent(taskId)}/workbook?token=${encodeURIComponent(token)}`;
  const linkableColumns = new Set(
    visualization.columns.filter((column) => (
      isSecurityCodeColumn(column, visualization.rows)
    )),
  );
  const methodologyText = (visualization.methodology ?? "").trim();

  return (
    <main className="research-viewer-shell">
      <header className="research-viewer-header">
        <div>
          <div className="research-viewer-eyebrow">A股研究助手 · 研究结果</div>
          <h1>{visualization.title}</h1>
          <p>
            {visualization.source_row_count.toLocaleString("zh-CN")} 条结果
            {visualization.truncated ? `，页面展示前 ${visualization.rows.length.toLocaleString("zh-CN")} 条` : ""}
          </p>
        </div>
        {payload.artifact_name && (
          <a className="research-viewer-download" href={workbookUrl}>下载 Excel</a>
        )}
      </header>

      <section className="research-viewer-summary">
        <h2>研究结论</h2>
        <div>{payload.answer}</div>
      </section>

      {methodologyText && (
        <details className="research-viewer-methodology">
          <summary>研究口径</summary>
          <div>{methodologyText}</div>
        </details>
      )}

      <section className="research-viewer-panel">
        <div className="research-viewer-panel-heading research-viewer-table-heading">
          <div>
            <h2>结果明细</h2>
            <p>把鼠标移到列名上可以查看每一列的定义和口径；完整数据请下载 Excel。</p>
          </div>
          <label className="research-viewer-search">
            <span>搜索结果</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="代码、名称或任意字段"
            />
          </label>
        </div>
        <div className="research-viewer-table-wrap">
          <table className="research-viewer-table">
            <thead>
              <tr>
                {visualization.columns.map((column) => {
                  const note = columnNote(column, visualization.column_notes);
                  return (
                    <th key={column} title={note}>
                      <span className="research-viewer-th-label" data-note={note}>
                        {column}
                      </span>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {tableRows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {visualization.columns.map((column) => {
                    const quoteUrl = linkableColumns.has(column)
                      ? securityQuotePageUrl(row[column])
                      : null;
                    return (
                      <td key={column}>
                        {quoteUrl ? (
                          <a
                            className="research-viewer-code-link"
                            href={quoteUrl}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {displayValue(row[column], column)}
                          </a>
                        ) : (
                          displayValue(row[column], column)
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          {tableRows.length === 0 && <div className="research-viewer-empty">没有匹配的结果。</div>}
        </div>
      </section>

      <footer className="research-viewer-footer">
        只读链接有效至 {new Date(payload.expires_at).toLocaleString("zh-CN")}。链接持有者可以查看和下载结果。
      </footer>
    </main>
  );
}
