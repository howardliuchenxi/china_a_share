import { useEffect, useMemo, useState } from "react";

type ChartKind = "line" | "bar" | "scatter";
type ViewerValue = string | number | boolean | null;

interface ResearchVisualization {
  /** Human-readable title generated with the research workbook. */
  title: string;
  /** Ordered dataset columns available for chart and table controls. */
  columns: string[];
  /** Columns containing at least one finite numeric value. */
  numeric_columns: string[];
  /** Bounded result rows retained for interactive exploration. */
  rows: Array<Record<string, ViewerValue>>;
  /** Complete result count before the viewer row limit. */
  source_row_count: number;
  /** Whether the complete workbook contains additional rows. */
  truncated: boolean;
  /** Initial horizontal-axis column selected by the backend. */
  suggested_x: string;
  /** Initial vertical-axis column selected by the backend. */
  suggested_y: string;
}

interface ResearchVisualizationResponse {
  /** Stable identifier for the completed Feishu research task. */
  task_id: string;
  /** Text conclusion produced by the research model. */
  answer: string;
  /** Data and display defaults for the interactive viewer. */
  visualization: ResearchVisualization;
  /** Workbook filename available from the protected download endpoint. */
  artifact_name: string | null;
  /** ISO timestamp after which the bearer link becomes invalid. */
  expires_at: string;
}

const CHART_WIDTH = 920;
const CHART_HEIGHT = 420;
const CHART_PADDING = { top: 24, right: 28, bottom: 62, left: 76 };
const TABLE_ROW_LIMIT = 100;
const POINT_LIMIT_OPTIONS = [30, 60, 120, 250];

function numericValue(value: ViewerValue): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function displayValue(value: ViewerValue): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    return value.toLocaleString("zh-CN", { maximumFractionDigits: 6 });
  }
  return String(value);
}

function ResearchChart({
  rows,
  xColumn,
  yColumn,
  kind,
}: {
  rows: Array<Record<string, ViewerValue>>;
  xColumn: string;
  yColumn: string;
  kind: ChartKind;
}) {
  const points = rows
    .map((row, index) => ({
      index,
      x: displayValue(row[xColumn]),
      y: numericValue(row[yColumn]),
    }))
    .filter((point): point is { index: number; x: string; y: number } => point.y !== null);

  if (points.length === 0) {
    return <div className="research-chart-empty">当前筛选范围没有可绘制的数值。</div>;
  }

  const values = points.map((point) => point.y);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const span = rawMax - rawMin || Math.max(Math.abs(rawMax), 1);
  const min = rawMin - span * 0.08;
  const max = rawMax + span * 0.08;
  const plotWidth = CHART_WIDTH - CHART_PADDING.left - CHART_PADDING.right;
  const plotHeight = CHART_HEIGHT - CHART_PADDING.top - CHART_PADDING.bottom;
  const xPosition = (index: number) => (
    CHART_PADDING.left
    + (points.length === 1 ? plotWidth / 2 : (index / (points.length - 1)) * plotWidth)
  );
  const yPosition = (value: number) => (
    CHART_PADDING.top + ((max - value) / (max - min)) * plotHeight
  );
  const linePath = points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${xPosition(index)} ${yPosition(point.y)}`)
    .join(" ");
  const tickIndexes = Array.from(
    new Set([0, 1, 2, 3, 4, 5].map((step) => (
      Math.round((step / 5) * (points.length - 1))
    ))),
  );
  const barWidth = Math.max(2, Math.min(28, (plotWidth / points.length) * 0.68));
  const zeroY = yPosition(Math.max(min, Math.min(max, 0)));

  return (
    <div className="research-chart-wrap">
      <svg
        className="research-chart"
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
        aria-label={`${yColumn} 按 ${xColumn} 展示的${kind === "bar" ? "柱状图" : kind === "scatter" ? "散点图" : "折线图"}`}
      >
        <defs>
          <linearGradient id="research-line-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#2f6fed" stopOpacity="0.22" />
            <stop offset="100%" stopColor="#2f6fed" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0, 1, 2, 3, 4].map((step) => {
          const y = CHART_PADDING.top + (step / 4) * plotHeight;
          const value = max - (step / 4) * (max - min);
          return (
            <g key={step}>
              <line
                x1={CHART_PADDING.left}
                x2={CHART_WIDTH - CHART_PADDING.right}
                y1={y}
                y2={y}
                className="research-chart-grid"
              />
              <text x={CHART_PADDING.left - 12} y={y + 4} textAnchor="end" className="research-chart-tick">
                {displayValue(value)}
              </text>
            </g>
          );
        })}
        {kind === "line" && (
          <>
            <path d={linePath} className="research-chart-line" />
            {points.map((point, index) => (
              <circle
                key={`${point.index}-${point.x}`}
                cx={xPosition(index)}
                cy={yPosition(point.y)}
                r={points.length > 120 ? 2.2 : 3.8}
                className="research-chart-point"
              >
                <title>{`${point.x}\n${yColumn}: ${displayValue(point.y)}`}</title>
              </circle>
            ))}
          </>
        )}
        {kind === "scatter" && points.map((point, index) => (
          <circle
            key={`${point.index}-${point.x}`}
            cx={xPosition(index)}
            cy={yPosition(point.y)}
            r={5}
            className="research-chart-scatter"
          >
            <title>{`${point.x}\n${yColumn}: ${displayValue(point.y)}`}</title>
          </circle>
        ))}
        {kind === "bar" && points.map((point, index) => {
          const y = yPosition(point.y);
          const top = Math.min(y, zeroY);
          const height = Math.max(1, Math.abs(zeroY - y));
          return (
            <rect
              key={`${point.index}-${point.x}`}
              x={xPosition(index) - barWidth / 2}
              y={top}
              width={barWidth}
              height={height}
              rx={2}
              className="research-chart-bar"
            >
              <title>{`${point.x}\n${yColumn}: ${displayValue(point.y)}`}</title>
            </rect>
          );
        })}
        {tickIndexes.map((index) => (
          <text
            key={index}
            x={xPosition(index)}
            y={CHART_HEIGHT - CHART_PADDING.bottom + 24}
            textAnchor="middle"
            className="research-chart-tick research-chart-x-tick"
          >
            {points[index].x.length > 12 ? `${points[index].x.slice(0, 11)}…` : points[index].x}
          </text>
        ))}
        <text
          x={CHART_PADDING.left + plotWidth / 2}
          y={CHART_HEIGHT - 10}
          textAnchor="middle"
          className="research-chart-axis-label"
        >
          {xColumn}
        </text>
        <text
          x={18}
          y={CHART_PADDING.top + plotHeight / 2}
          textAnchor="middle"
          transform={`rotate(-90 18 ${CHART_PADDING.top + plotHeight / 2})`}
          className="research-chart-axis-label"
        >
          {yColumn}
        </text>
      </svg>
    </div>
  );
}

export default function ResearchVisualizationPage() {
  const taskId = window.location.pathname.split("/").filter(Boolean).at(-1) ?? "";
  const token = new URLSearchParams(window.location.search).get("token") ?? "";
  const [payload, setPayload] = useState<ResearchVisualizationResponse | null>(null);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [xColumn, setXColumn] = useState("");
  const [yColumn, setYColumn] = useState("");
  const [kind, setKind] = useState<ChartKind>("line");
  const [pointLimit, setPointLimit] = useState(60);
  const [windowStart, setWindowStart] = useState(0);

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
        setXColumn(result.visualization.suggested_x || result.visualization.columns[0] || "");
        setYColumn(result.visualization.suggested_y || result.visualization.numeric_columns[0] || "");
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
    return rows.filter((row) => Object.values(row).some((value) => (
      displayValue(value).toLocaleLowerCase("zh-CN").includes(normalizedSearch)
    )));
  }, [payload, search]);

  useEffect(() => {
    setWindowStart(0);
  }, [search, pointLimit, xColumn, yColumn]);

  const maximumStart = Math.max(0, filteredRows.length - pointLimit);
  const boundedStart = Math.min(windowStart, maximumStart);
  const chartRows = filteredRows.slice(boundedStart, boundedStart + pointLimit);
  const tableRows = filteredRows.slice(0, TABLE_ROW_LIMIT);

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
  const workbookUrl = `/api/research/visualizations/${encodeURIComponent(taskId)}/workbook?token=${encodeURIComponent(token)}`;

  return (
    <main className="research-viewer-shell">
      <header className="research-viewer-header">
        <div>
          <div className="research-viewer-eyebrow">A股研究助手 · 交互结果</div>
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

      {visualization.numeric_columns.length > 0 && (
        <section className="research-viewer-panel">
          <div className="research-viewer-panel-heading">
            <div>
              <h2>交互图表</h2>
              <p>切换字段、图形和数据区间；将鼠标停在数据点上可查看具体数值。</p>
            </div>
          </div>
          <div className="research-viewer-controls">
            <label>
              图形
              <select value={kind} onChange={(event) => setKind(event.target.value as ChartKind)}>
                <option value="line">折线图</option>
                <option value="bar">柱状图</option>
                <option value="scatter">散点图</option>
              </select>
            </label>
            <label>
              横轴
              <select value={xColumn} onChange={(event) => setXColumn(event.target.value)}>
                {visualization.columns.map((column) => <option key={column}>{column}</option>)}
              </select>
            </label>
            <label>
              数值
              <select value={yColumn} onChange={(event) => setYColumn(event.target.value)}>
                {visualization.numeric_columns.map((column) => <option key={column}>{column}</option>)}
              </select>
            </label>
            <label>
              单屏点数
              <select value={pointLimit} onChange={(event) => setPointLimit(Number(event.target.value))}>
                {POINT_LIMIT_OPTIONS.map((value) => <option key={value} value={value}>{value}</option>)}
              </select>
            </label>
          </div>
          {maximumStart > 0 && (
            <label className="research-viewer-range">
              <span>数据区间：第 {boundedStart + 1}–{Math.min(boundedStart + pointLimit, filteredRows.length)} 条</span>
              <input
                type="range"
                min={0}
                max={maximumStart}
                value={boundedStart}
                onChange={(event) => setWindowStart(Number(event.target.value))}
              />
            </label>
          )}
          <ResearchChart rows={chartRows} xColumn={xColumn} yColumn={yColumn} kind={kind} />
        </section>
      )}

      <section className="research-viewer-panel">
        <div className="research-viewer-panel-heading research-viewer-table-heading">
          <div>
            <h2>结果明细</h2>
            <p>表格显示当前搜索结果的前 {TABLE_ROW_LIMIT} 条，完整数据请下载 Excel。</p>
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
              <tr>{visualization.columns.map((column) => <th key={column}>{column}</th>)}</tr>
            </thead>
            <tbody>
              {tableRows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {visualization.columns.map((column) => (
                    <td key={column}>{displayValue(row[column])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {tableRows.length === 0 && <div className="research-chart-empty">没有匹配的结果。</div>}
        </div>
      </section>

      <footer className="research-viewer-footer">
        只读链接有效至 {new Date(payload.expires_at).toLocaleString("zh-CN")}。链接持有者可以查看和下载结果。
      </footer>
    </main>
  );
}
