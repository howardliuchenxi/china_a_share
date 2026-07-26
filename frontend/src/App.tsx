import {
  ChangeEvent,
  ClipboardEvent,
  FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { fetchStocks, StockListRequestError, submitAnalysis } from "./api";
import { UiFeedbackController } from "./UiFeedbackController";
import type {
  AnalysisImage,
  AnalysisResponse,
  AnalysisTaskProgress,
  DataQuery,
  QueryResult,
  ServiceError,
  StockExchange,
  StockListResponse,
} from "./contracts";

type ReferenceView = "stocks" | "calendar";
type PageView = "analysis" | "reference";

const workflowSteps = [
  "\u8bc6\u522b\u53ef\u9009\u622a\u56fe\u4e2d\u7684\u56fe\u8868\u4e0e\u6587\u5b57",
  "理解自然语言数据需求",
  "选择当前数据源中的白名单操作",
  "校验 A股 查询计划",
  "获取并展示表格结果",
];
const decisionStatusLabels = {
  success: "\u5df2\u5b8c\u6210",
  warning: "\u9700\u6ce8\u610f",
  error: "\u5931\u8d25",
  skipped: "\u5df2\u8df3\u8fc7",
} as const;
const MAX_ANALYSIS_IMAGE_BYTES = 10 * 1024 * 1024;
const SUPPORTED_ANALYSIS_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
]);
const RESULT_PAGE_SIZE = 100;
const MAX_PROMPT_HISTORY_ITEMS = 20;
const PROMPT_HISTORY_STORAGE_KEY = "china-a-share.prompt-history";
const STOCK_PAGE_SIZE = 20;
const exchangeLabels: Record<StockExchange, string> = {
  SSE: "上海",
  SZSE: "深圳",
  BSE: "北京",
};
const calendarWeekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const calendarDays = Array.from({ length: 35 }, (_, index) => {
  const day = index - 1;
  return day >= 1 && day <= 31 ? day : null;
});

const errorSourceLabels: Record<string, string> = {
  tushare: "Tushare",
  deepseek: "DeepSeek",
  glm: "GLM",
  system: "系统",
};

const resultColumnMetadata: Record<string, { label: string; description: string }> = {
  ts_code: { label: "股票代码", description: "证券在 Tushare 中使用的交易所限定代码。" },
  name: { label: "股票名称", description: "证券当前公开使用的简称。" },
  change: { label: "涨跌额", description: "当前收盘价相对前一交易日收盘价的变动金额。" },
  pct_chg: { label: "涨跌幅（%）", description: "涨跌额相对前一交易日收盘价的百分比。" },
  close: { label: "收盘价", description: "证券在该交易日的收盘价格。" },
  open: { label: "开盘价", description: "证券在该交易日的开盘价格。" },
  high: { label: "最高价", description: "证券在该交易日成交的最高价格。" },
  low: { label: "最低价", description: "证券在该交易日成交的最低价格。" },
  trade_date: { label: "交易日期", description: "该行行情或指标对应的交易日。" },
  end_date: { label: "报告期", description: "股东持股数据对应的报告期末日期。" },
  ann_date: { label: "公告日期", description: "该期股东数据首次对市场公开的日期。" },
  cr10_float_registered: {
    label: "CR10 流通股集中度",
    description: "前十大无限售流通股东的流通股本持股比例合计，按证券登记账户口径计算。",
  },
  non_top10_float_ratio: {
    label: "持股分散度代理",
    description: "100%减去CR10流通股集中度；包含散户和未进入前十的机构，不等于个人投资者持股比例。",
  },
  known_top_holder_float_ratio: {
    label: "已知股东比例",
    description: "仅汇总具有有效流通股持股比例的披露股东；当源数据缺失时，不代表完整CR10。",
  },
  uncovered_float_ratio_upper_bound: {
    label: "未覆盖比例上限",
    description: "100%减去已知股东比例；包含缺失比例的披露股东及其他股东，不能视为散户比例。",
  },
  omnibus_float_ratio: {
    label: "代理账户占比",
    description: "前十大流通股东中香港中央结算等代理账户的流通股持股比例；其背后可能对应多个实际投资者。",
  },
  holder_count: { label: "有效股东数", description: "参与本期CR10计算且名称唯一的披露股东数量，必须为10。" },
  ratio_holder_count: { label: "有效比例数", description: "具有可计算流通股持股比例的披露股东数量。" },
  missing_ratio_holders: { label: "比例缺失股东", description: "源数据未提供流通股持股比例、因而未纳入已知比例合计的股东。" },
  calculation_status: { label: "计算完整性", description: "complete表示完整计算；partial_missing_ratio表示源比例缺失，仅能给出部分统计。" },
};
const FINANCIAL_STATEMENT_OPERATIONS = new Set([
  "income",
  "balancesheet",
  "cashflow",
]);
const NON_MONETARY_FINANCIAL_FIELDS = new Set([
  "ts_code",
  "ann_date",
  "f_ann_date",
  "end_date",
  "report_type",
  "comp_type",
  "end_type",
  "update_flag",
  "basic_eps",
  "diluted_eps",
  "diluted2_eps",
  "total_share",
]);
const IDENTIFIER_COLUMN_PATTERN = /(^|_)(code|date|year|month|type|status|flag|count|num)$/;
const PERCENT_COLUMN_PATTERN = /(^pct_|_pct$|_pct_|_ratio$|_rate$|_yield$)/;

function formatAdaptiveNumber(value: number, unit = ""): string {
  const absoluteValue = Math.abs(value);
  if (absoluteValue >= 100_000_000) {
    return `${(value / 100_000_000).toFixed(2)}亿${unit}`;
  }
  if (absoluteValue >= 10_000) {
    return `${(value / 10_000).toFixed(2)}万${unit}`;
  }
  return `${value.toLocaleString("zh-CN", {
    maximumFractionDigits: 2,
  })}${unit}`;
}

function isPercentageColumn(column: string): boolean {
  return PERCENT_COLUMN_PATTERN.test(column)
    || [
      "pct_chg",
      "period_return_pct",
      "turnover_change_pct",
      "cr10_float_registered",
      "non_top10_float_ratio",
      "known_top_holder_float_ratio",
      "uncovered_float_ratio_upper_bound",
      "omnibus_float_ratio",
    ].includes(column);
}

function isFinancialStatementAmount(operation: string, column: string): boolean {
  return FINANCIAL_STATEMENT_OPERATIONS.has(operation)
    && !NON_MONETARY_FINANCIAL_FIELDS.has(column)
    && !isPercentageColumn(column);
}

function normalizeKnownCurrencyUnit(
  operation: string,
  column: string,
  value: number,
): number | null {
  if (isFinancialStatementAmount(operation, column)) return value;
  if (column === "amount" && ["daily", "weekly", "monthly"].includes(operation)) {
    return value * 1_000;
  }
  if (
    (column === "amount" && operation === "block_trade")
    || (["total_mv", "circ_mv"].includes(column) && operation === "daily_basic")
  ) {
    return value * 10_000;
  }
  return null;
}

function TermHelp({ label, description }: { label: string; description: string }) {
  return (
    <span className="term-help" tabIndex={0} aria-label={`${label}：${description}`}>
      <span aria-hidden="true">!</span>
      <span className="term-help-content" role="tooltip">{description}</span>
    </span>
  );
}

function ColumnHelp({ column }: { column: string }) {
  const metadata = resultColumnMetadata[column];
  const description = metadata?.description
    ?? `数据源返回的 ${column} 字段，具体统计口径遵循当前接口定义。`;
  return <TermHelp label={metadata?.label ?? column} description={description} />;
}

type SortDirection = "default" | "ascending" | "descending";

function nextSortDirection(direction: SortDirection): SortDirection {
  if (direction === "default") return "ascending";
  if (direction === "ascending") return "descending";
  return "default";
}

function compareResultValues(left: unknown, right: unknown): number {
  if (left == null || left === "") return right == null || right === "" ? 0 : 1;
  if (right == null || right === "") return -1;
  const leftNumber = typeof left === "number" ? left : Number(left);
  const rightNumber = typeof right === "number" ? right : Number(right);
  if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
    return leftNumber - rightNumber;
  }
  return String(left).localeCompare(String(right), "zh-CN", { numeric: true });
}

function formatResultValue(
  operation: string,
  column: string,
  value: unknown,
  row?: Record<string, unknown>,
): string {
  if (value == null || value === "") {
    if (row?.calculation_status === "partial_missing_ratio") {
      const missingHolders = Array.isArray(row.missing_ratio_holders)
        ? row.missing_ratio_holders.join("、")
        : "部分披露股东";
      if (column === "cr10_float_registered") {
        return `无法完整计算：${missingHolders}的持股比例缺失`;
      }
      if (column === "non_top10_float_ratio") {
        return "无法完整计算：完整CR10数据缺失";
      }
      if (column === "omnibus_float_ratio") {
        return "无法完整计算：代理账户持股比例缺失";
      }
    }
    return "—";
  }
  if (Array.isArray(value)) return value.length > 0 ? value.join("、") : "无";
  if (column === "calculation_status") {
    if (value === "complete") return "完整计算";
    if (value === "partial_missing_ratio") return "部分数据（比例缺失）";
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    if (IDENTIFIER_COLUMN_PATTERN.test(column)) return String(value);
    if (isPercentageColumn(column)) return `${value.toFixed(2)}%`;
    const currencyValue = normalizeKnownCurrencyUnit(operation, column, value);
    if (currencyValue != null) return formatAdaptiveNumber(currencyValue, "元");
    return formatAdaptiveNumber(value);
  }
  return String(value);
}

function readAnalysisImage(file: File): Promise<AnalysisImage> {
  if (!SUPPORTED_ANALYSIS_IMAGE_TYPES.has(file.type)) {
    return Promise.reject(
      new Error("\u4ec5\u652f\u6301 PNG\u3001JPEG \u6216 WebP \u622a\u56fe\u3002"),
    );
  }
  if (file.size > MAX_ANALYSIS_IMAGE_BYTES) {
    return Promise.reject(
      new Error("\u622a\u56fe\u4e0d\u80fd\u8d85\u8fc7 10 MiB\u3002"),
    );
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(
      new Error("\u65e0\u6cd5\u8bfb\u53d6\u622a\u56fe\u3002"),
    );
    reader.onload = () => {
      const result = reader.result;
      const prefix = `data:${file.type};base64,`;
      if (typeof result !== "string" || !result.startsWith(prefix)) {
        reject(new Error("\u622a\u56fe\u7f16\u7801\u65e0\u6548\u3002"));
        return;
      }
      resolve({
        media_type: file.type as AnalysisImage["media_type"],
        base64_data: result.slice(prefix.length),
      });
    };
    reader.readAsDataURL(file);
  });
}

function ErrorCard({ error }: { error: ServiceError }) {
  const sourceLabel = errorSourceLabels[error.source] ?? error.source;
  return (
    <div className="error-card" role="alert">
      <strong>{sourceLabel} 错误</strong>
      <p>{error.message}</p>
      <dl>
        {error.code != null && <><dt>错误码</dt><dd>{String(error.code)}</dd></>}
        {error.http_status != null && <><dt>HTTP 状态</dt><dd>{error.http_status}</dd></>}
      </dl>
      {error.raw_response && (
        <details>
          <summary>查看上游原始响应</summary>
          <pre>{JSON.stringify(error.raw_response, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

function DecisionTrace({ response }: { response: AnalysisResponse | null }) {
  if (!response?.decision_trace.length) {
    return <ol className="workflow-list">{workflowSteps.map((step) => <li key={step}>{step}</li>)}</ol>;
  }
  return (
    <ol className="decision-trace">
      {response.decision_trace.map((step, index) => (
        <li className={`is-${step.status}`} key={`${step.stage}-${index}`}>
          <div>
            <span>{decisionStatusLabels[step.status]}</span>
            {step.external_call && <em>{"\u5df2\u8c03\u7528\u5916\u90e8 API"}</em>}
          </div>
          <strong>{step.title}</strong>
          <p>{step.detail}</p>
          {step.evidence.length > 0 && (
            <ul>{step.evidence.map((item) => <li key={item}>{item}</li>)}</ul>
          )}
        </li>
      ))}
    </ol>
  );
}

function RequirementCoverage({ response }: { response: AnalysisResponse }) {
  const plan = response.plan;
  if (!plan) return null;
  return (
    <div className="coverage-block">
      <p>
        <strong>{"\u53ef\u884c\u6027\uff1a"}</strong>
        <span className={`feasibility is-${plan.feasibility}`}>
          {plan.feasibility === "supported" ? "\u53ef\u6267\u884c" : "\u65e0\u6cd5\u5b8c\u6574\u5b9e\u73b0"}
        </span>
      </p>
      {plan.requirements.length > 0 && (
        <div className="coverage-table-wrap">
          <table className="coverage-table">
            <thead><tr>
              <th><span className="result-column-title">{"\u7528\u6237\u8981\u6c42"}<TermHelp label="用户要求" description="从自然语言问题中拆分出的单项数据需求。" /></span></th>
              <th><span className="result-column-title">{"\u5b9e\u73b0\u65b9\u5f0f"}<TermHelp label="实现方式" description="用于满足该项要求的数据接口或确定性本地计算。" /></span></th>
              <th><span className="result-column-title">{"\u80fd\u529b\u8bc1\u636e"}<TermHelp label="能力证据" description="证明当前数据源能够提供所需字段或操作的依据。" /></span></th>
              <th><span className="result-column-title">{"\u72b6\u6001"}<TermHelp label="状态" description="该项要求是否已被当前查询计划完整覆盖。" /></span></th>
            </tr></thead>
            <tbody>{plan.requirements.map((item) => (
              <tr key={`${item.requirement}-${item.evidence}`}>
                <td>{item.requirement}</td>
                <td>{item.implementation ?? "\u2014"}</td>
                <td>{item.evidence}</td>
                <td>{item.status === "covered" ? "\u5df2\u8986\u76d6" : "\u4e0d\u652f\u6301"}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      {plan.limitations.length > 0 && (
        <ul className="plan-limitations">
          {plan.limitations.map((item) => <li key={item}>{item}</li>)}
        </ul>
      )}
    </div>
  );
}

function ResultTable({ result, query }: { result: QueryResult; query?: DataQuery }) {
  const [searchText, setSearchText] = useState("");
  const [sortColumn, setSortColumn] = useState<string | null>(null);
  const [sortDirection, setSortDirection] = useState<SortDirection>("default");
  const [resultPage, setResultPage] = useState(1);
  const uniqueStockCount = useMemo(() => {
    const codes = new Set<string>();
    for (const row of result.rows) {
      const code = String(row.ts_code ?? "");
      if (code) codes.add(code);
    }
    return codes.size;
  }, [result.rows]);
  const processedRows = useMemo(() => {
    const normalizedSearch = searchText.trim().toLocaleLowerCase();
    const filteredRows = normalizedSearch
      ? result.rows.filter((row) => [row.ts_code, row.name].some(
          (value) => String(value ?? "").toLocaleLowerCase().includes(normalizedSearch),
        ))
      : result.rows;
    if (!sortColumn || sortDirection === "default") return filteredRows;
    const multiplier = sortDirection === "ascending" ? 1 : -1;
    return filteredRows
      .map((row, index) => ({ row, index }))
      .sort((left, right) => (
        compareResultValues(left.row[sortColumn], right.row[sortColumn]) * multiplier
        || left.index - right.index
      ))
      .map(({ row }) => row);
  }, [result.rows, searchText, sortColumn, sortDirection]);

  useEffect(() => {
    setResultPage(1);
  }, [processedRows.length]);

  if (result.error) return <ErrorCard error={result.error} />;
  const totalPages = Math.max(1, Math.ceil(processedRows.length / RESULT_PAGE_SIZE));
  const safePage = Math.min(resultPage, totalPages);
  const visibleRows = processedRows.slice((safePage - 1) * RESULT_PAGE_SIZE, safePage * RESULT_PAGE_SIZE);

  function updateSort(column: string) {
    if (sortColumn !== column) {
      setSortColumn(column);
      setSortDirection("ascending");
      return;
    }
    const direction = nextSortDirection(sortDirection);
    setSortDirection(direction);
    if (direction === "default") setSortColumn(null);
  }
  return (
    <div className="result-block">
      <div className="result-heading">
        <h3>{result.provider} · {result.operation}</h3>
        <span>共 {result.row_count.toLocaleString()} 行</span>
      </div>
      {result.row_count > 0 && Object.keys(result.summary).length > 0 && (
        <dl className="summary-grid">
          {Object.entries(result.summary).map(([label, value]) => (
            <div key={label}><dt>{label}</dt><dd>{value.toLocaleString()}</dd></div>
          ))}
        </dl>
      )}
      {result.row_count > 1 && uniqueStockCount > 1 && (
        <div className="result-tools">
          <label>
            <span>搜索结果</span>
            <input
              type="search"
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              placeholder="股票代码或名称"
            />
          </label>
          <span>找到 {processedRows.length.toLocaleString()} 行</span>
        </div>
      )}
      {result.row_count === 1 ? (
        <dl className="record-grid">
          {result.columns.map((column) => (
            <div key={column}>
              <dt>
                <span>{resultColumnMetadata[column]?.label ?? column}</span>
                <ColumnHelp column={column} />
              </dt>
              <dd>{formatResultValue(result.operation, column, result.rows[0][column], result.rows[0])}</dd>
              {resultColumnMetadata[column] && <small>{column}</small>}
            </div>
          ))}
        </dl>
      ) : visibleRows.length > 0 ? (
        <>
          <div className="table-scroll">
            <table>
              <thead><tr>{result.columns.map((column) => {
                const isActive = sortColumn === column && sortDirection !== "default";
                const sortLabel = isActive
                  ? sortDirection === "ascending" ? "升序" : "降序"
                  : "未排序";
                return (
                  <th key={column} aria-sort={isActive ? sortDirection : "none"}>
                    <div className="result-column-controls">
                      <button
                        type="button"
                        className="result-sort-button"
                        onClick={() => updateSort(column)}
                        aria-label={`${resultColumnMetadata[column]?.label ?? column}，${sortLabel}`}
                      >
                        <span>{resultColumnMetadata[column]?.label ?? column}</span>
                        {resultColumnMetadata[column] && <small>{column}</small>}
                        <i aria-hidden="true">{isActive ? sortDirection === "ascending" ? "↑" : "↓" : "↕"}</i>
                      </button>
                      <ColumnHelp column={column} />
                    </div>
                  </th>
                );
              })}</tr></thead>
              <tbody>
                {visibleRows.map((row, index) => (
                  <tr key={`${result.query_id}-${index}`}>
                    {result.columns.map((column) => (
                      <td key={column}>{formatResultValue(result.operation, column, row[column], row)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {processedRows.length > RESULT_PAGE_SIZE && (
            <nav className="result-pagination" aria-label="结果分页">
              <button
                type="button"
                disabled={safePage === 1}
                onClick={() => setResultPage((page) => Math.max(1, page - 1))}
              >
                上一页
              </button>
              <span>第 {safePage} 页，共 {totalPages} 页（{processedRows.length.toLocaleString()} 行）</span>
              <button
                type="button"
                disabled={safePage === totalPages}
                onClick={() => setResultPage((page) => Math.min(totalPages, page + 1))}
              >
                下一页
              </button>
            </nav>
          )}
        </>
      ) : result.row_count > 0 ? (
        <div className="empty-result" role="status">
          <strong>没有匹配的股票</strong>
          <p>请尝试输入完整或部分股票代码、股票名称。</p>
        </div>
      ) : (
        <div className="empty-result" role="status">
          <strong>{"\u672a\u67e5\u8be2\u5230\u6570\u636e"}</strong>
          <p>{"\u8bf7\u6c42\u5df2\u6210\u529f\u53d1\u9001\u5230 Tushare\uff0c\u4f46\u8fd4\u56de\u7ed3\u679c\u4e3a\u7a7a\u3002\u8fd9\u4e0d\u8868\u793a\u4e0a\u6da8\u3001\u4e0b\u8dcc\u548c\u5e73\u76d8\u6570\u91cf\u90fd\u662f 0\u3002"}</p>
          {query && (
            <dl>
              <dt>{"\u5b9e\u9645\u67e5\u8be2"}</dt>
              <dd><code>{result.provider}.{query.operation}</code></dd>
              <dt>{"\u67e5\u8be2\u53c2\u6570"}</dt>
              <dd><code>{JSON.stringify(query.params)}</code></dd>
            </dl>
          )}
          <p>{"\u53ef\u80fd\u539f\u56e0\uff1a\u622a\u56fe\u4e2d\u7684\u80a1\u7968\u4ee3\u7801\u6216\u5b8c\u6574\u65e5\u671f\u4e0d\u6e05\u6670\uff0c\u65e5\u671f\u4e0d\u662f\u4ea4\u6613\u65e5\uff0c\u6216\u95ee\u9898\u6ca1\u6709\u8bf4\u660e\u8981\u67e5\u8be2\u7684\u6307\u6807\u3002"}</p>
          <p>{"\u5efa\u8bae\u63d0\u95ee\uff1a\u201c\u8bc6\u522b\u622a\u56fe\u4e2d\u7684\u80a1\u7968\u4ee3\u7801\u548c\u65e5\u671f\uff0c\u67e5\u8be2\u5f53\u65e5\u5f00\u76d8\u4ef7\u3001\u6536\u76d8\u4ef7\u548c\u6da8\u8dcc\u5e45\u3002\u201d"}</p>
        </div>
      )}
    </div>
  );
}

function ReferenceDataPage() {
  const [activeView, setActiveView] = useState<ReferenceView>("stocks");
  const [stockSearch, setStockSearch] = useState("");
  const [exchangeFilter, setExchangeFilter] = useState<StockExchange>("SSE");
  const [industryFilter, setIndustryFilter] = useState("");
  const [stockPage, setStockPage] = useState(1);
  const [calendarExchange, setCalendarExchange] = useState<StockExchange>("SSE");
  const [stockResponse, setStockResponse] = useState<StockListResponse | null>(null);
  const [availableIndustries, setAvailableIndustries] = useState<string[]>([]);
  const [stockServiceError, setStockServiceError] = useState<ServiceError | null>(null);
  const [stockLocalError, setStockLocalError] = useState("");
  const [isStockLoading, setIsStockLoading] = useState(false);

  useEffect(() => {
    if (activeView !== "stocks") return undefined;
    const controller = new AbortController();
    setIsStockLoading(true);
    setStockResponse(null);
    setStockServiceError(null);
    setStockLocalError("");
    fetchStocks({
      page: stockPage,
      page_size: STOCK_PAGE_SIZE,
      search: stockSearch.trim(),
      exchange: exchangeFilter,
      industry: industryFilter,
    }, controller.signal)
      .then((response) => {
        setStockResponse(response);
        setAvailableIndustries(response.available_industries);
      })
      .catch((error: unknown) => {
        if (error instanceof Error && error.name === "AbortError") return;
        if (error instanceof StockListRequestError) {
          setStockServiceError(error.serviceError);
          return;
        }
        setStockLocalError(error instanceof Error ? error.message : "股票列表请求失败。");
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsStockLoading(false);
      });
    return () => controller.abort();
  }, [activeView, exchangeFilter, industryFilter, stockPage, stockSearch]);

  function updateStockSearch(value: string) {
    setStockSearch(value);
    setStockPage(1);
  }

  function updateExchangeFilter(value: StockExchange) {
    setExchangeFilter(value);
    setStockPage(1);
  }

  function updateIndustryFilter(value: string) {
    setIndustryFilter(value);
    setStockPage(1);
  }

  return (
    <div className="reference-page" data-feedback-id="reference-page">
      <nav className="reference-tabs" aria-label="基础信息分类" role="tablist">
        {([
          ["stocks", "股票列表"],
          ["calendar", "交易日历"],
        ] as const).map(([view, label]) => (
          <button
            type="button"
            role="tab"
            aria-selected={activeView === view}
            key={view}
            onClick={() => setActiveView(view)}
          >
            {label}
          </button>
        ))}
      </nav>

      {activeView === "stocks" && (
        <section className="reference-panel" aria-labelledby="stock-list-heading">
          <div className="reference-view-heading">
            <div>
              <h2 id="stock-list-heading">股票列表</h2>
            </div>
            <span>{stockResponse ? `共 ${stockResponse.total.toLocaleString()} 只` : "正在读取股票目录"}</span>
          </div>
          <div className="stock-filters">
            <label className="stock-search-field">
              <span>搜索</span>
              <input
                type="search"
                value={stockSearch}
                onChange={(event) => updateStockSearch(event.target.value)}
                placeholder="股票代码、名称或行业"
              />
            </label>
            <label>
              <span>市场</span>
              <select
                value={exchangeFilter}
                onChange={(event) => updateExchangeFilter(event.target.value as StockExchange)}
              >
                <option value="SSE">上海</option>
                <option value="SZSE">深圳</option>
                <option value="BSE">北京</option>
              </select>
            </label>
            <label>
              <span>行业</span>
              <select value={industryFilter} onChange={(event) => updateIndustryFilter(event.target.value)}>
                <option value="">全部行业</option>
                {availableIndustries.map((industry) => <option key={industry}>{industry}</option>)}
              </select>
            </label>
          </div>
          {isStockLoading && <p className="stock-status" role="status">正在从 Tushare 读取股票列表…</p>}
          {stockLocalError && <div className="error-card" role="alert"><strong>本地错误</strong><p>{stockLocalError}</p></div>}
          {stockServiceError && <ErrorCard error={stockServiceError} />}
          {stockResponse && stockResponse.items.length > 0 && <>
            <div className="stock-table-scroll">
              <table className="stock-table">
                <thead>
                  <tr>
                    <th><span className="result-column-title">股票代码<TermHelp label="股票代码" description="证券在 Tushare 中使用的交易所限定代码。" /></span></th>
                    <th><span className="result-column-title">名称<TermHelp label="名称" description="证券当前公开使用的简称。" /></span></th>
                    <th><span className="result-column-title">市场<TermHelp label="市场" description="证券挂牌交易的市场板块。" /></span></th>
                    <th><span className="result-column-title">地区<TermHelp label="地区" description="上市公司在数据源中登记的所属地区。" /></span></th>
                    <th><span className="result-column-title">行业<TermHelp label="行业" description="上市公司在数据源中登记的行业分类。" /></span></th>
                    <th><span className="result-column-title">上市日期<TermHelp label="上市日期" description="证券首次挂牌交易的日期。" /></span></th>
                  </tr>
                </thead>
                <tbody>
                  {stockResponse.items.map((stock) => (
                    <tr key={stock.code}>
                      <td className="stock-code">{stock.code}</td>
                      <td><strong>{stock.name}</strong></td>
                      <td>{exchangeLabels[stock.exchange]}{stock.board ? ` · ${stock.board}` : ""}</td>
                      <td>{stock.area ?? "—"}</td>
                      <td>{stock.industry ?? "—"}</td>
                      <td>{stock.listed_on}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <nav className="stock-pagination" aria-label="股票列表分页">
              <button
                type="button"
                disabled={stockResponse.page === 1}
                onClick={() => setStockPage((page) => page - 1)}
              >
                上一页
              </button>
              <span>第 {stockResponse.page} 页，共 {stockResponse.total_pages} 页</span>
              <button
                type="button"
                disabled={stockResponse.page === stockResponse.total_pages}
                onClick={() => setStockPage((page) => page + 1)}
              >
                下一页
              </button>
            </nav>
          </>}
          {stockResponse?.items.length === 0 && <p className="empty-state">没有符合当前条件的股票。</p>}
        </section>
      )}

      {activeView === "calendar" && (
        <section className="reference-panel" aria-labelledby="calendar-heading">
          <div className="calendar-header">
            <div>
              <label className="calendar-exchange-field">
                <span>市场</span>
                <select
                  value={calendarExchange}
                  onChange={(event) => setCalendarExchange(event.target.value as StockExchange)}
                >
                  <option value="SSE">上海</option>
                  <option value="SZSE">深圳</option>
                  <option value="BSE">北京</option>
                </select>
              </label>
              <h2 id="calendar-heading">2026年7月</h2>
            </div>
            <div className="calendar-legend" aria-label="日历图例">
              <span><i className="open-day-dot" />交易日</span>
              <span><i className="closed-day-dot" />休市日</span>
              <span><i className="today-dot" />今天</span>
            </div>
          </div>
          <div className="market-calendar" aria-label="2026年7月 A股交易日历">
            {calendarWeekdays.map((weekday) => (
              <div className="calendar-weekday" key={weekday}>{weekday}</div>
            ))}
            {calendarDays.map((day, index) => {
              const isWeekend = index % 7 >= 5;
              const isToday = day === 19;
              const dayClassName = [
                "calendar-day",
                day == null ? "is-empty" : isWeekend ? "is-closed" : "is-open",
                isToday ? "is-today" : "",
              ].filter(Boolean).join(" ");
              return (
                <div
                  className={dayClassName}
                  key={`${index}-${day ?? "empty"}`}
                  aria-label={day == null ? undefined : `7月${day}日，${isWeekend ? "休市日" : "交易日"}${isToday ? "，今天" : ""}`}
                >
                  {day != null && <><strong>{day}</strong><span>{isWeekend ? "休" : "开"}</span></>}
                </div>
              );
            })}
          </div>
        </section>
      )}

    </div>
  );
}

export default function App() {
  const [activePage, setActivePage] = useState<PageView>("analysis");
  const [prompt, setPrompt] = useState("");
  const [promptHistory, setPromptHistory] = useState<string[]>([]);
  const [response, setResponse] = useState<AnalysisResponse | null>(null);
  const [localError, setLocalError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [taskProgress, setTaskProgress] = useState<AnalysisTaskProgress | null>(null);
  const [isImageReading, setIsImageReading] = useState(false);
  const [analysisImage, setAnalysisImage] = useState<AnalysisImage | null>(null);
  const [analysisImageName, setAnalysisImageName] = useState("");
  const imageInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    try {
      const storedHistory: unknown = JSON.parse(
        window.localStorage.getItem(PROMPT_HISTORY_STORAGE_KEY) ?? "[]",
      );
      if (!Array.isArray(storedHistory)) return;
      setPromptHistory(
        storedHistory
          .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
          .map((item) => item.trim())
          .slice(0, MAX_PROMPT_HISTORY_ITEMS),
      );
    } catch (error) {
      console.warn("Unable to read prompt history from local storage.", error);
    }
  }, []);

  async function loadAnalysisImage(file: File) {
    setIsImageReading(true);
    setLocalError("");
    try {
      setAnalysisImage(await readAnalysisImage(file));
      setAnalysisImageName(file.name || "screenshot");
    } catch (error) {
      setAnalysisImage(null);
      setAnalysisImageName("");
      setLocalError(
        error instanceof Error
          ? error.message
          : "\u622a\u56fe\u4e0a\u4f20\u5931\u8d25\u3002",
      );
    } finally {
      setIsImageReading(false);
    }
  }

  function handleImageChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void loadAnalysisImage(file);
  }

  function handlePromptPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const imageItem = Array.from(event.clipboardData.items).find(
      (item) => item.kind === "file" && SUPPORTED_ANALYSIS_IMAGE_TYPES.has(item.type),
    );
    const file = imageItem?.getAsFile();
    if (!file) return;
    event.preventDefault();
    void loadAnalysisImage(file);
  }

  function removeAnalysisImage() {
    setAnalysisImage(null);
    setAnalysisImageName("");
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const submittedPrompt = prompt.trim();
    if (!submittedPrompt || isLoading || isImageReading) return;
    const nextPromptHistory = [
      submittedPrompt,
      ...promptHistory.filter((item) => item !== submittedPrompt),
    ].slice(0, MAX_PROMPT_HISTORY_ITEMS);
    setPromptHistory(nextPromptHistory);
    try {
      window.localStorage.setItem(
        PROMPT_HISTORY_STORAGE_KEY,
        JSON.stringify(nextPromptHistory),
      );
    } catch (error) {
      console.warn("Unable to save prompt history to local storage.", error);
    }
    setIsLoading(true);
    setTaskProgress(null);
    setLocalError("");
    setResponse(null);
    try {
      setResponse(
        await submitAnalysis(
          {
            prompt: submittedPrompt,
            ...(analysisImage ? { image: analysisImage } : {}),
          },
          setTaskProgress,
        ),
      );
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "本地请求失败。");
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <main className="app-shell" data-feedback-id="app-shell">
      <UiFeedbackController />
      <header className="hero" data-feedback-id="hero">
        <p className="eyebrow">数据世界</p>
      </header>

      <nav className="page-tabs" aria-label="主要功能" role="tablist" data-feedback-id="page-tabs">
        <button
          type="button"
          role="tab"
          aria-selected={activePage === "analysis"}
          onClick={() => setActivePage("analysis")}
        >
          数据分析
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activePage === "reference"}
          onClick={() => setActivePage("reference")}
        >
          基础信息
        </button>
      </nav>

      {activePage === "analysis" ? <>
      <section className="request-panel" aria-labelledby="request-heading" data-feedback-id="request-panel">
        <div className="section-heading"><span>01</span><h2 id="request-heading">数据请求</h2></div>
        <form onSubmit={handleSubmit}>
          <label htmlFor="analysis-prompt">描述你需要的数据</label>
          <textarea
            id="analysis-prompt"
            rows={6}
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            onPaste={handlePromptPaste}
            placeholder="例如：北京时间2026年7月17日有多少只A股上涨，多少只下跌？"
          />
          <div className="screenshot-control">
            <input
              ref={imageInputRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              onChange={handleImageChange}
              aria-label="Select screenshot"
            />
            <button
              type="button"
              className="screenshot-select-button"
              disabled={isImageReading || isLoading}
              onClick={() => imageInputRef.current?.click()}
            >
              {analysisImage
                ? "\u66f4\u6362\u622a\u56fe"
                : "\u6dfb\u52a0\u622a\u56fe"}
            </button>
            <span>
              {isImageReading
                ? "\u6b63\u5728\u8bfb\u53d6\u622a\u56fe\u2026"
                : "\u652f\u6301 PNG\u3001JPEG\u3001WebP\uff0c\u6700\u5927 10 MiB\uff1b\u4e5f\u53ef\u76f4\u63a5\u7c98\u8d34\u622a\u56fe\u3002"}
            </span>
          </div>
          {analysisImage && (
            <div className="screenshot-preview">
              <img
                src={`data:${analysisImage.media_type};base64,${analysisImage.base64_data}`}
                alt="Selected screenshot preview"
              />
              <div>
                <strong>{analysisImageName}</strong>
                <span>{"\u622a\u56fe\u5c06\u5148\u7531 GLM-5V-Turbo \u8bc6\u522b\uff0c\u518d\u4ea4\u7ed9 DeepSeek \u89c4\u5212\u67e5\u8be2\u3002"}</span>
              </div>
              <button type="button" onClick={removeAnalysisImage} disabled={isLoading}>
                {"\u5220\u9664"}
              </button>
            </div>
          )}
          <button
            type="submit"
            disabled={!prompt.trim() || isLoading || isImageReading}
          >
            {isLoading
              ? taskProgress?.totalItems
                ? `\u6b63\u5728\u5206\u6790 ${taskProgress.completedItems}/${taskProgress.totalItems}\u2026`
                : "\u6b63\u5728\u521b\u5efa\u5206\u6790\u4efb\u52a1\u2026"
              : "\u5f00\u59cb\u5206\u6790"}
          </button>
          {isLoading && taskProgress && (
            <p className="task-progress" role="status">
              {taskProgress.totalItems > 0
                ? `\u5df2\u5904\u7406 ${taskProgress.completedItems} / ${taskProgress.totalItems} \u652f\u80a1\u7968\uff0c\u53ef\u4ee5\u7ee7\u7eed\u7b49\u5f85\u3002`
                : "\u4efb\u52a1\u5df2\u5165\u961f\uff0c\u6b63\u5728\u7b49\u5f85\u540e\u53f0\u6267\u884c\u3002"}
            </p>
          )}
        </form>
      </section>

      <section className="results-panel" aria-labelledby="results-heading" data-feedback-id="results-panel">
        <div className="section-heading"><span>02</span><h2 id="results-heading">数据与错误</h2></div>
        {localError && <div className="error-card" role="alert"><strong>本地错误</strong><p>{localError}</p></div>}
        {response?.error && <ErrorCard error={response.error} />}
        {response?.plan?.feasibility === "unsupported"
          && !response.error
          && response.results.length === 0 && (
            <div className="error-card" role="alert">
              <strong>当前请求无法完整处理</strong>
              {response.plan.limitations.length > 0 ? (
                response.plan.limitations.map((limitation) => (
                  <p key={limitation}>{limitation}</p>
                ))
              ) : (
                <p>当前数据源或分析能力无法完整满足这项请求。</p>
              )}
            </div>
          )}
        {response?.results.map((result) => (
          <ResultTable
            result={result}
            query={response.plan?.queries.find(
              (query) => query.query_id === result.query_id,
            )}
            key={result.query_id}
          />
        ))}
        {!localError && !response && <p className="empty-output">数据源查询结果将在这里显示。</p>}
      </section>

      <section className="details-stack" aria-label="查询与执行详情" data-feedback-id="execution-details">
        <details className="collapsible-panel">
          <summary><span>03</span><strong>查询与执行详情</strong></summary>
          <div className="plan-content">
            {response?.plan ? (
              <>
              <h2>{response.plan.interpretation}</h2>
              <p>规划器：{response.planner} · 数据源：{response.data_provider}</p>
              <RequirementCoverage response={response} />
              {response.plan.queries.map((query) => (
                <div className="query-card" key={query.query_id}>
                  <strong>{query.operation}</strong><p>{query.purpose}</p>
                  <code>{JSON.stringify(query.params, null, 2)}</code>
                  <code>{JSON.stringify({ fields: query.fields, filters: query.filters, aggregations: query.aggregations }, null, 2)}</code>
                </div>
              ))}
              </>
            ) : (
              <p className="empty-output">完成分析后，可在这里查看经过校验的接口、参数和执行记录。</p>
            )}
            <DecisionTrace response={response} />
          </div>
        </details>
      </section>
      </> : <ReferenceDataPage />}
    </main>
  );
}
