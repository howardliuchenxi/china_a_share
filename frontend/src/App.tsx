import {
  ChangeEvent,
  ClipboardEvent,
  FormEvent,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import { submitAnalysis } from "./api";
import { UiFeedbackController } from "./UiFeedbackController";
import { DiscoveryPage } from "./DiscoveryPage";
import type {
  AnalysisImage,
  AnalysisResponse,
  AnalysisTaskProgress,
  DataQuery,
  QueryResult,
  ServiceError,
} from "./contracts";

type PageView = "analysis" | "reference" | "discovery";

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
import { DATA_DICTIONARY_ENTRIES, resultColumnMetadata } from "./dataDictionary";

const errorSourceLabels: Record<string, string> = {
  tushare: "Tushare",
  deepseek: "DeepSeek",
  glm: "GLM",
  system: "系统",
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
const A_SHARE_CODE_PATTERN = /^(?<symbol>\d{6})\.(?:SH|SZ|BJ)$/;
const EASTMONEY_CAPITAL_FLOW_BASE_URL = "https://data.eastmoney.com/zjlx";

const LEGACY_SUMMARY_METADATA: Record<
  string,
  { label: string; description: string; percentage?: "value" | "ratio" }
> = {
  "Event count": {
    label: "\u6709\u6548\u4e8b\u4ef6\u6837\u672c\u6570",
    description: "\u6ee1\u8db3\u524d\u7f6e\u4e8b\u4ef6\u6761\u4ef6\uff0c\u4e14\u5728\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u6709\u53ef\u7528\u4ef7\u683c\u6570\u636e\u7684\u6837\u672c\u6570\u3002",
  },
  "Positive event count": {
    label: "\u540e\u7eed\u6536\u76ca\u4e3a\u6b63\u7684\u6837\u672c\u6570",
    description: "\u6709\u6548\u6837\u672c\u4e2d\uff0c\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u4ef7\u683c\u9ad8\u4e8e\u4e8b\u4ef6\u53d1\u751f\u65f6\u4ef7\u683c\u7684\u6837\u672c\u6570\u3002",
  },
  "Positive event ratio": {
    label: "\u540e\u7eed\u6536\u76ca\u4e3a\u6b63\u7684\u6bd4\u4f8b",
    description: "\u540e\u7eed\u6536\u76ca\u4e3a\u6b63\u7684\u6837\u672c\u6570 \u00f7 \u6709\u6548\u4e8b\u4ef6\u6837\u672c\u6570\u3002",
    percentage: "ratio",
  },
  "Average return (%)": {
    label: "\u4e8b\u4ef6\u540e\u5e73\u5747\u6536\u76ca\u7387",
    description: "\u6240\u6709\u6709\u6548\u4e8b\u4ef6\u6837\u672c\uff0c\u4ece\u4e8b\u4ef6\u53d1\u751f\u65f6\u70b9\u5230\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u7684\u6536\u76ca\u7387\u7b97\u672f\u5e73\u5747\u503c\u3002",
    percentage: "value",
  },
  "Minimum return (%)": {
    label: "\u4e8b\u4ef6\u540e\u6700\u4f4e\u6536\u76ca\u7387",
    description: "\u6240\u6709\u6709\u6548\u4e8b\u4ef6\u6837\u672c\u4e2d\uff0c\u4ece\u4e8b\u4ef6\u53d1\u751f\u65f6\u70b9\u5230\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u7684\u6700\u4f4e\u6536\u76ca\u7387\u3002",
    percentage: "value",
  },
  "Maximum return (%)": {
    label: "\u4e8b\u4ef6\u540e\u6700\u9ad8\u6536\u76ca\u7387",
    description: "\u6240\u6709\u6709\u6548\u4e8b\u4ef6\u6837\u672c\u4e2d\uff0c\u4ece\u4e8b\u4ef6\u53d1\u751f\u65f6\u70b9\u5230\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u7684\u6700\u9ad8\u6536\u76ca\u7387\u3002",
    percentage: "value",
  },
};

const SUMMARY_FUNCTION_LABELS = {
  count: "\u8ba1\u6570",
  sum: "\u6c42\u548c",
  mean: "\u5e73\u5747\u503c",
  min: "\u6700\u5c0f\u503c",
  max: "\u6700\u5927\u503c",
} as const;

const SUMMARY_OUTPUT_DESCRIPTIONS: Record<string, string> = {
  event_count: "\u6ee1\u8db3\u4e8b\u4ef6\u6761\u4ef6\uff0c\u4e14\u5728\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u6709\u53ef\u7528\u4ef7\u683c\u6570\u636e\u7684\u6837\u672c\u6570\u3002",
  positive_event_count: "\u6709\u6548\u6837\u672c\u4e2d\uff0c\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u6536\u76ca\u4e3a\u6b63\u7684\u6837\u672c\u6570\u3002",
  positive_event_ratio: "\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u6536\u76ca\u4e3a\u6b63\u7684\u6837\u672c\u5360\u5168\u90e8\u6709\u6548\u6837\u672c\u7684\u6bd4\u4f8b\u3002",
  average_return_pct: "\u6240\u6709\u6709\u6548\u6837\u672c\u4ece\u4e8b\u4ef6\u53d1\u751f\u5230\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u7684\u5e73\u5747\u6536\u76ca\u7387\u3002",
  minimum_return_pct: "\u6240\u6709\u6709\u6548\u6837\u672c\u4ece\u4e8b\u4ef6\u53d1\u751f\u5230\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u7684\u6700\u4f4e\u6536\u76ca\u7387\u3002",
  maximum_return_pct: "\u6240\u6709\u6709\u6548\u6837\u672c\u4ece\u4e8b\u4ef6\u53d1\u751f\u5230\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9\u7684\u6700\u9ad8\u6536\u76ca\u7387\u3002",
};

const CALCULATION_OPERATION_LABELS: Record<string, string> = {
  latest_by_group: "\u6309\u5206\u7ec4\u53d6\u6700\u65b0\u8bb0\u5f55",
  derive: "\u8ba1\u7b97\u6d3e\u751f\u5b57\u6bb5",
  drop_missing: "\u6392\u9664\u7f3a\u5931\u6837\u672c",
  filter: "\u6309\u6761\u4ef6\u7b5b\u9009",
  sort: "\u6392\u5e8f",
  limit: "\u9650\u5236\u7ed3\u679c\u6570\u91cf",
  quantile_filter: "\u6309\u5206\u4f4d\u6570\u7b5b\u9009",
  aggregate: "\u5206\u7ec4\u6c47\u603b",
  rolling_mean: "\u8ba1\u7b97\u6eda\u52a8\u5e73\u5747",
  rolling_sum: "\u8ba1\u7b97\u6eda\u52a8\u6c42\u548c",
  shift: "\u6309\u987a\u5e8f\u504f\u79fb\u53d6\u503c",
  match_at_offset: "\u5339\u914d\u76ee\u6807\u89c2\u5bdf\u65f6\u70b9",
  match_source: "\u5339\u914d\u5916\u90e8\u4e8b\u4ef6\u6570\u636e",
  exists_in_source: "\u68c0\u67e5\u5916\u90e8\u6570\u636e\u662f\u5426\u5b58\u5728",
  join_fields: "\u5173\u8054\u5916\u90e8\u5b57\u6bb5",
  compare_fields: "\u6bd4\u8f83\u4e24\u4e2a\u5b57\u6bb5",
  compare_scalar: "\u4e0e\u56fa\u5b9a\u503c\u6bd4\u8f83",
  conditional_count: "\u7edf\u8ba1\u6ee1\u8db3\u6761\u4ef6\u7684\u8bb0\u5f55",
};

function calculationStepDescription(
  step: NonNullable<QueryResult["summary_metadata"]>[string]["calculation_steps"][number],
): string {
  const operation = CALCULATION_OPERATION_LABELS[step.operation] ?? step.operation;
  const inputs = (step.input_fields ?? []).map(
    (field) => resultColumnMetadata[field]?.label ?? field,
  );
  const outputs = step.output_fields ?? [];
  const fieldDescription = [
    inputs.length > 0 ? `\u8f93\u5165\uff1a${inputs.join("\u3001")}` : "",
    outputs.length > 0 ? `\u8f93\u51fa\uff1a${outputs.join("\u3001")}` : "",
  ].filter(Boolean).join("\uff1b");
  const parameters = Object.keys(step.parameters ?? {}).length > 0
    ? `\uff1b\u53c2\u6570\uff1a${JSON.stringify(step.parameters)}`
    : "";
  return `${operation}${fieldDescription ? `\uff08${fieldDescription}\uff09` : ""}${parameters}`;
}

function summaryDescription(
  label: string,
  metadata: NonNullable<QueryResult["summary_metadata"]>[string] | undefined,
): string | undefined {
  const legacyDescription = LEGACY_SUMMARY_METADATA[label]?.description;
  if (legacyDescription) return legacyDescription;
  if (!metadata) return undefined;
  const outputDescription = SUMMARY_OUTPUT_DESCRIPTIONS[metadata.output_field];
  if (outputDescription) return outputDescription;
  const sourceLabel = resultColumnMetadata[metadata.source_field]?.label
    ?? metadata.source_field;
  return `${label}\uff1a\u5bf9\u201c${sourceLabel}\u201d\u5b57\u6bb5\u6267\u884c${SUMMARY_FUNCTION_LABELS[metadata.function]}\u7edf\u8ba1\u3002`;
}

function formatSummaryValue(
  label: string,
  value: number | null,
  metadata: NonNullable<QueryResult["summary_metadata"]>[string] | undefined,
): string {
  if (value === null) return "\u4e0d\u53ef\u8ba1\u7b97";
  const valueFormat = metadata?.value_format;
  const legacyPercentage = LEGACY_SUMMARY_METADATA[label]?.percentage;
  if (valueFormat === "ratio" || (!valueFormat && legacyPercentage === "ratio")) {
    return `${(value * 100).toFixed(2)}%`;
  }
  if (
    valueFormat === "percentage_points"
    || (!valueFormat && legacyPercentage === "value")
  ) {
    return `${value.toFixed(2)}%`;
  }
  return value.toLocaleString("zh-CN");
}

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
  const anchorRef = useRef<HTMLSpanElement>(null);
  const tooltipRef = useRef<HTMLSpanElement>(null);
  const tooltipId = useId();
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0, placeAbove: false });

  useLayoutEffect(() => {
    if (!isVisible) return;

    const updatePosition = () => {
      const anchor = anchorRef.current?.getBoundingClientRect();
      const tooltip = tooltipRef.current?.getBoundingClientRect();
      if (!anchor || !tooltip) return;

      const viewportPadding = 12;
      const tooltipGap = 8;
      const idealLeft = anchor.left + anchor.width / 2;
      const halfWidth = tooltip.width / 2;
      const left = Math.min(
        window.innerWidth - viewportPadding - halfWidth,
        Math.max(viewportPadding + halfWidth, idealLeft),
      );
      const placeAbove = anchor.bottom + tooltipGap + tooltip.height > window.innerHeight - viewportPadding
        && anchor.top - tooltipGap - tooltip.height >= viewportPadding;

      setPosition({
        left,
        top: placeAbove ? anchor.top - tooltipGap : anchor.bottom + tooltipGap,
        placeAbove,
      });
    };

    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [isVisible]);

  return (
    <span
      ref={anchorRef}
      className="term-help"
      tabIndex={0}
      aria-label={`${label}：${description}`}
      aria-describedby={isVisible ? tooltipId : undefined}
      onMouseEnter={() => setIsVisible(true)}
      onMouseLeave={() => setIsVisible(false)}
      onFocus={() => setIsVisible(true)}
      onBlur={() => setIsVisible(false)}
    >
      <span aria-hidden="true">!</span>
      {isVisible && createPortal(
        <span
          ref={tooltipRef}
          id={tooltipId}
          className="term-help-content"
          role="tooltip"
          style={{
            left: position.left,
            top: position.top,
            transform: position.placeAbove
              ? "translate(-50%, -100%)"
              : "translateX(-50%)",
          }}
        >
          {description}
        </span>,
        document.body,
      )}
    </span>
  );
}

function ColumnHelp({
  column,
  calculation,
}: {
  column: string;
  calculation?: NonNullable<QueryResult["column_metadata"]>[string];
}) {
  const metadata = resultColumnMetadata[column];
  let description = calculation
    ? `\u8be5\u5b57\u6bb5\u7531\u7cfb\u7edf\u786e\u5b9a\u6027\u8ba1\u7b97\u751f\u6210\u3002\u8ba1\u7b97\u516c\u5f0f\uff1a${calculation.formula}\u3002`
    : metadata?.description
      ?? `数据源返回的 ${column} 字段，具体统计口径遵循当前接口定义。`;
  if (calculation) {
    const sources = calculation.source_fields.map(
      (field) => resultColumnMetadata[field]?.label ?? field,
    );
    description += ` \u539f\u59cb\u5b57\u6bb5\uff1a${sources.join("\u3001") || "\u65e0"}\u3002`;
    if (calculation.calculation_steps.length > 0) {
      description += ` \u8ba1\u7b97\u6b65\u9aa4\uff1a${calculation.calculation_steps.map(
        calculationStepDescription,
      ).join("\uff1b")}\u3002`;
    }
  } else if (metadata?.formula && metadata.formula !== "-") {
    description += ` 计算公式：${metadata.formula}`;
  }
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

function eastmoneyVerificationUrl(
  column: string,
  value: unknown,
  row?: Record<string, unknown>,
): string | null {
  if (column !== "ts_code" && column !== "name") return null;
  const code = String(column === "ts_code" ? value : row?.ts_code ?? "");
  const match = A_SHARE_CODE_PATTERN.exec(code);
  if (!match?.groups?.symbol) return null;
  return `${EASTMONEY_CAPITAL_FLOW_BASE_URL}/${match.groups.symbol}.html`;
}

function formatResultValue(
  operation: string,
  column: string,
  value: unknown,
  row?: Record<string, unknown>,
): React.ReactNode {
  if (value == null || value === "") {
    if (row?.calculation_status === "partial_missing_ratio") {
      const missingHolders = Array.isArray(row.missing_ratio_holders)
        ? row.missing_ratio_holders.join("、")
        : "部分披露股东";
      if (column === "cr10_float_registered") {
        return (
          <span className="data-status-warn" title={`${missingHolders}的持股比例缺失`}>
            无法完整计算：比例缺失
          </span>
        );
      }
      if (column === "non_top10_float_ratio") {
        return (
          <span className="data-status-warn" title="完整CR10数据缺失">
            无法完整计算：CR10缺失
          </span>
        );
      }
      if (column === "omnibus_float_ratio") {
        return (
          <span className="data-status-warn" title="代理账户持股比例缺失">
            无法完整计算：代理缺失
          </span>
        );
      }
    }
    return <span className="data-missing-dash" title="该数据字段未披露或暂无数据">—</span>;
  }
  if (Array.isArray(value)) return value.length > 0 ? value.join("、") : "无";
  const verificationUrl = eastmoneyVerificationUrl(column, value, row);
  if (verificationUrl) {
    const label = String(value);
    return (
      <a
        className="result-verification-link"
        href={verificationUrl}
        target="_blank"
        rel="noopener noreferrer"
        title="在东方财富查看资金流向数据"
        aria-label={`${label}，在东方财富查看资金流向数据（新窗口）`}
      >
        {label}<span aria-hidden="true">↗</span>
      </a>
    );
  }
  if (column === "calculation_status") {
    if (value === "complete") {
      return <span className="data-status-badge complete">完整计算</span>;
    }
    if (value === "partial_missing_ratio") {
      return <span className="data-status-badge partial">部分披露</span>;
    }
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

function formatPlanDisclosure(disclosure: string): string {
  if (disclosure.includes("non_top10_float_ratio")) {
    return "\u672c\u7ed3\u679c\u4f7f\u7528\u201c\u975e\u524d\u5341\u5927\u6d41\u901a\u80a1\u4e1c\u6301\u80a1\u6bd4\u4f8b\u201d\u4f5c\u4e3a\u6301\u80a1\u5206\u6563\u5ea6\u4ee3\u7406\u3002\u5b83\u5305\u542b\u6563\u6237\u53ca\u672a\u8fdb\u5165\u524d\u5341\u7684\u673a\u6784\uff0c\u4e0d\u7b49\u4e8e\u771f\u5b9e\u4e2a\u4eba\u6295\u8d44\u8005\u6301\u80a1\u6bd4\u4f8b\u3002";
  }
  const inferredYear = disclosure.match(
    /^The omitted year was resolved to (\d{4})/,
  );
  if (inferredYear) {
    return `\u95ee\u9898\u672a\u6307\u5b9a\u5e74\u4efd\uff0c\u5df2\u6309\u4e0a\u6d77\u65f6\u533a\u89e3\u6790\u4e3a ${inferredYear[1]} \u5e74\u3002`;
  }
  return disclosure;
}

function PlanDisclosure({
  response,
  onSelectOption,
  disabled,
}: {
  response: AnalysisResponse;
  onSelectOption: (prompt: string) => void;
  disabled: boolean;
}) {
  const limitations = response.plan?.limitations ?? [];
  const clarificationOptions = response.plan?.clarification_options ?? [];
  if (
    !response.plan
    || (limitations.length === 0 && clarificationOptions.length === 0)
  ) {
    return null;
  }
  return (
    <aside className="disclosure-card" role="note">
      <strong>{"\u53e3\u5f84\u8bf4\u660e"}</strong>
      {response.plan.feasibility === "supported" && limitations.length > 0 && (
        <ul>
          {limitations.map((item) => (
            <li key={item}>{formatPlanDisclosure(item)}</li>
          ))}
        </ul>
      )}
      {clarificationOptions.length > 0 && (
        <div className="clarification-options">
          <p>请选择一个明确口径重新分析：</p>
          {clarificationOptions.map((option) => (
            <button
              type="button"
              key={option}
              disabled={disabled}
              onClick={() => onSelectOption(option)}
            >
              {option}
            </button>
          ))}
        </div>
      )}
    </aside>
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

function ResultTable({
  result,
  query,
  isRawChild,
  rawResults,
  rawQueries,
}: {
  result: QueryResult;
  query?: DataQuery;
  isRawChild?: boolean;
  rawResults?: QueryResult[];
  rawQueries?: DataQuery[];
}) {
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

  if (result.error) {
    return (
      <div className="result-block">
        <div className="result-heading">
          <h3>{result.provider} · {result.operation}</h3>
          <span>共 {result.row_count.toLocaleString()} 行</span>
        </div>
        <ErrorCard error={result.error} />
      </div>
    );
  }
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
      {Object.keys(result.summary).length > 0 && (
        <dl className="summary-grid">
          {Object.entries(result.summary).map(([label, value]) => (
            <div key={label}>
              <dt>
                <span>{LEGACY_SUMMARY_METADATA[label]?.label ?? label}</span>
                {summaryDescription(label, result.summary_metadata?.[label]) && (
                  <TermHelp
                    label={LEGACY_SUMMARY_METADATA[label]?.label ?? label}
                    description={summaryDescription(label, result.summary_metadata?.[label])!}
                  />
                )}
              </dt>
              <dd>{formatSummaryValue(label, value, result.summary_metadata?.[label])}</dd>
              {summaryDescription(label, result.summary_metadata?.[label]) && (
                <p className="summary-description">
                  {summaryDescription(label, result.summary_metadata?.[label])}
                </p>
              )}
              {result.summary_metadata?.[label]?.formula && (
                <details className="summary-explanation">
                  <summary>{"\u67e5\u770b\u5b8c\u6574\u53e3\u5f84"}</summary>
                  <p>
                    <strong>{"\u8ba1\u7b97\u516c\u5f0f\uff1a"}</strong>
                    <code>{result.summary_metadata[label].formula}</code>
                  </p>
                  <p>
                    <strong>{"\u539f\u59cb\u5b57\u6bb5\uff1a"}</strong>
                    {result.summary_metadata[label].source_fields.map(
                      (field) => resultColumnMetadata[field]?.label ?? field,
                    ).join("\u3001") || "\u65e0"}
                  </p>
                  <p>
                    <strong>{"\u6837\u672c\u53e3\u5f84\uff1a"}</strong>
                    {result.summary_metadata[label].initial_sample_count ?? "\u672a\u77e5"}
                    {" \u6761\u539f\u59cb\u8bb0\u5f55\uff0c"}
                    {result.summary_metadata[label].valid_sample_count ?? "\u672a\u77e5"}
                    {" \u6761\u6709\u6548\u6837\u672c"}
                  </p>
                  {result.summary_metadata[label].calculation_steps.length > 0 && (
                    <ol>
                      {result.summary_metadata[label].calculation_steps.map((step, index) => (
                        <li key={`${step.operation}-${index}`}>
                          {calculationStepDescription(step)}
                        </li>
                      ))}
                    </ol>
                  )}
                </details>
              )}
            </div>
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
                <ColumnHelp column={column} calculation={result.column_metadata?.[column]} />
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
                      <ColumnHelp column={column} calculation={result.column_metadata?.[column]} />
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
          <p>查询已成功执行，但数据源读取或本地筛选后没有保留任何记录。</p>
          {query && (
            <dl>
              <dt>{"\u5b9e\u9645\u67e5\u8be2"}</dt>
              <dd><code>{result.provider}.{query.operation}</code></dd>
              <dt>{"\u67e5\u8be2\u53c2\u6570"}</dt>
              <dd><code>{JSON.stringify(query.params)}</code></dd>
            </dl>
          )}
          <p>可能原因：指定日期没有可用数据、指标值为空，或分类与筛选条件没有匹配数据源中的标签。</p>
          <p>请查看查询详情中的日期、字段和过滤条件；对于行业或板块，可尝试数据源使用的更具体分类名称。</p>
        </div>
      )}
      {!isRawChild && (
        <p className="table-note table-provenance" style={{ color: "#657169", fontSize: "0.74rem", borderTop: "1px dashed #d8dcd4", paddingTop: "10px", marginTop: "14px" }}>
          {"\u6570\u636e\u6e90\u8bf4\u660e\uff1a\u672c\u8868\u683c\u6570\u636e\u7531 "} <strong>{result.provider}</strong> {" \u7684 "} <code>{result.operation}</code> {" \u63a5\u53e3\u63d0\u4f9b\u652f\u6301\uff0c\u5df2\u901a\u8fc7\u7f25\u5b58\u4e0e\u7b7e\u540d\u6821\u9a8c\u3002"}
        </p>
      )}
      {rawResults && rawResults.length > 1 && !isRawChild && (
        <details className="raw-results-panel" style={{ marginTop: "16px", borderTop: "1px dashed #c0cfc6", paddingTop: "12px" }}>
          <summary style={{ cursor: "pointer", color: "#3d7459", fontWeight: "bold", fontSize: "0.95em" }}>
            {"\u67e5\u770b "} {rawResults.length} {" \u4e2a\u539f\u59cb\u5206\u6b65\u67e5\u8be2\u7ed3\u679c"}
          </summary>
          <div className="raw-results-content" style={{ padding: "12px 0 0 12px", borderLeft: "2px solid #3d7459", marginTop: "12px" }}>
            {rawResults.map((rawRes) => (
              <ResultTable
                result={rawRes}
                query={rawQueries?.find((q) => q.query_id === rawRes.query_id)}
                key={rawRes.query_id}
                isRawChild={true}
              />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

interface GroupedResult {
  isGrouped: boolean;
  key: string;
  singleResult?: QueryResult;
  singleQuery?: DataQuery;
  results?: QueryResult[];
  queries?: DataQuery[];
  columns?: string[];
  provider?: string;
  operation?: string;
}

function areColumnsCompatible(cols1: string[], cols2: string[]): boolean {
  if (cols1.length !== cols2.length) return false;
  return cols1.every((col, i) => col === cols2[i]);
}

function groupResults(results: QueryResult[], queries: DataQuery[]): GroupedResult[] {
  const grouped: GroupedResult[] = [];
  for (let i = 0; i < results.length; i++) {
    const res = results[i];
    const query = queries.find((q) => q.query_id === res.query_id);
    if (res.status !== "success" || res.error || !res.columns || res.columns.length === 0) {
      grouped.push({
        isGrouped: false,
        key: res.query_id,
        singleResult: res,
        singleQuery: query,
      });
      continue;
    }
    const lastGroup = grouped[grouped.length - 1];
    if (
      lastGroup &&
      lastGroup.isGrouped &&
      lastGroup.provider === res.provider &&
      lastGroup.operation === res.operation &&
      areColumnsCompatible(lastGroup.columns || [], res.columns)
    ) {
      lastGroup.results!.push(res);
      if (query) lastGroup.queries!.push(query);
      continue;
    } else if (
      lastGroup &&
      !lastGroup.isGrouped &&
      lastGroup.singleResult &&
      lastGroup.singleResult.status === "success" &&
      !lastGroup.singleResult.error &&
      lastGroup.singleResult.provider === res.provider &&
      lastGroup.singleResult.operation === res.operation &&
      areColumnsCompatible(lastGroup.singleResult.columns, res.columns)
    ) {
      const prevResult = lastGroup.singleResult;
      const prevQuery = lastGroup.singleQuery;
      grouped[grouped.length - 1] = {
        isGrouped: true,
        key: `group-${prevResult.query_id}-${res.query_id}`,
        results: [prevResult, res],
        queries: prevQuery ? [prevQuery, query].filter((q): q is DataQuery => q !== undefined) : (query ? [query] : []),
        columns: res.columns,
        provider: res.provider,
        operation: res.operation,
      };
      continue;
    }
    grouped.push({
      isGrouped: false,
      key: res.query_id,
      singleResult: res,
      singleQuery: query,
    });
  }
  return grouped;
}

function ReferenceDataPage() {
  const [dictionaryPage, setDictionaryPage] = useState(1);
  const [dictionarySearch, setDictionarySearch] = useState("");
  const DICTIONARY_PAGE_SIZE = 50;

  function updateDictionarySearch(value: string) {
    setDictionarySearch(value);
    setDictionaryPage(1);
  }

  const filteredDictionary = useMemo(() => {
    if (!dictionarySearch.trim()) return DATA_DICTIONARY_ENTRIES;
    const lowerSearch = dictionarySearch.toLowerCase();
    return DATA_DICTIONARY_ENTRIES.filter(entry => 
      entry.label.toLowerCase().includes(lowerSearch) || 
      entry.field.toLowerCase().includes(lowerSearch) ||
      entry.description.toLowerCase().includes(lowerSearch)
    );
  }, [dictionarySearch]);

  const dictionaryPageCount = Math.ceil(filteredDictionary.length / DICTIONARY_PAGE_SIZE);
  const paginatedDictionary = useMemo(() => {
    const start = (dictionaryPage - 1) * DICTIONARY_PAGE_SIZE;
    return filteredDictionary.slice(start, start + DICTIONARY_PAGE_SIZE);
  }, [filteredDictionary, dictionaryPage]);

  return (
    <div className="reference-page" data-feedback-id="reference-page">
      <section className="reference-panel" aria-labelledby="dictionary-heading">
          <div className="reference-view-heading">
            <h2 id="dictionary-heading">数据字典</h2>
            <span>{dictionarySearch.trim() ? `搜索结果 ${filteredDictionary.length} 个字段` : `共 ${DATA_DICTIONARY_ENTRIES.length} 个字段`}</span>
          </div>
          <div className="stock-filters">
            <label className="stock-search-field">
              <span>搜索</span>
              <input
                type="search"
                value={dictionarySearch}
                onChange={(event) => updateDictionarySearch(event.target.value)}
                placeholder="字段名称或标识符"
              />
            </label>
          </div>
          <div className="stock-table-scroll">
            <table className="stock-table">
              <thead>
                <tr>
                  <th>字段名称</th>
                  <th>标识符</th>
                  <th>含义</th>
                  <th>计算公式</th>
                  <th>数据来源</th>
                </tr>
              </thead>
              <tbody>
                {paginatedDictionary.map((entry) => (
                  <tr key={entry.field}>
                    <td><strong>{entry.label}</strong></td>
                    <td><code>{entry.field}</code></td>
                    <td>{entry.description}</td>
                    <td>{entry.formula}</td>
                    <td>{entry.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {dictionaryPageCount > 1 && (
            <nav className="stock-pagination" aria-label="数据字典分页">
              <button
                type="button"
                disabled={dictionaryPage === 1}
                onClick={() => setDictionaryPage((page) => page - 1)}
              >
                上一页
              </button>
              <span>{dictionaryPage} / {dictionaryPageCount}</span>
              <button
                type="button"
                disabled={dictionaryPage === dictionaryPageCount}
                onClick={() => setDictionaryPage((page) => page + 1)}
              >
                下一页
              </button>
            </nav>
          )}
          {filteredDictionary.length === 0 && <p className="empty-state">没有符合当前搜索条件的字段。</p>}
      </section>
    </div>
  );
}

export default function App() {
  const [activePage, setActivePage] = useState<PageView>(() => {
    const params = new URLSearchParams(window.location.search);
    return (params.get("page") as PageView) || "analysis";
  });
  const [prompt, setPrompt] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    return params.get("prompt") || "";
  });
  const [promptHistory, setPromptHistory] = useState<string[]>([]);
  const [response, setResponse] = useState<AnalysisResponse | null>(null);
  const [localError, setLocalError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [taskProgress, setTaskProgress] = useState<AnalysisTaskProgress | null>(null);
  const [isImageReading, setIsImageReading] = useState(false);
  const [analysisImage, setAnalysisImage] = useState<AnalysisImage | null>(null);
  const [analysisImageName, setAnalysisImageName] = useState("");
  const [plannerName, setPlannerName] = useState("Vertex AI Claude");
  const [savedPrompts, setSavedPrompts] = useState<string[]>(() => {
    try {
      const stored = window.localStorage.getItem("china_a_share_saved_prompts");
      return stored ? JSON.parse(stored) : [];
    } catch {
      return [];
    }
  });

  function toggleSavePrompt(text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;
    setSavedPrompts((prev) => {
      let next;
      if (prev.includes(trimmed)) {
        next = prev.filter((p) => p !== trimmed);
      } else {
        next = [trimmed, ...prev];
      }
      try {
        window.localStorage.setItem("china_a_share_saved_prompts", JSON.stringify(next));
      } catch (e) {
        console.warn("Failed to save prompt to local storage", e);
      }
      return next;
    });
  }
  const imageInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    params.set("page", activePage);
    if (prompt) {
      params.set("prompt", prompt);
    } else {
      params.delete("prompt");
    }
    window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
  }, [activePage, prompt]);

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

  useEffect(() => {
    fetch("/api/health")
      .then((resp) => resp.json())
      .then((data) => {
        if (data.planner) setPlannerName(data.planner);
      })
      .catch(() => {});
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

  async function runAnalysis(rawPrompt: string) {
    const submittedPrompt = rawPrompt.trim();
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

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void runAnalysis(prompt);
  }

  function handleClarificationOption(option: string) {
    setPrompt(option);
    void runAnalysis(option);
  }

  return (
    <main className="app-shell" data-feedback-id="app-shell">
      <UiFeedbackController />
      <header className="hero" data-feedback-id="hero">
        <p className="eyebrow">数据世界</p>
        <h1>{activePage === "reference" ? "整理 A股基础信息。" : activePage === "discovery" ? "自动挖掘量化策略与因子。" : "用自然语言探索 A股数据。"}</h1>
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
          aria-selected={activePage === "discovery"}
          onClick={() => setActivePage("discovery")}
        >
          策略挖掘
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
          <div className="prompt-controls" style={{ display: "flex", gap: "12px", marginBottom: "10px", alignItems: "center", flexWrap: "wrap" }}>
            <div className="prompt-history" style={{ flex: "1", minWidth: "180px" }}>
              <select
                id="prompt-history"
                value=""
                disabled={promptHistory.length === 0}
                onChange={(event) => {
                  if (!event.target.value) return;
                  setPrompt(event.target.value);
                  setResponse(null);
                  setLocalError("");
                }}
              >
                <option value="">
                  {promptHistory.length === 0 ? "暂无历史输入" : "选择之前输入的问题"}
                </option>
                {promptHistory.map((item, index) => (
                  <option value={item} key={`${index}-${item}`}>{item}</option>
                ))}
              </select>
            </div>

            <div className="prompt-saved" style={{ flex: "1", minWidth: "180px" }}>
              <select
                id="prompt-saved"
                value=""
                disabled={savedPrompts.length === 0}
                onChange={(event) => {
                  if (!event.target.value) return;
                  setPrompt(event.target.value);
                  setResponse(null);
                  setLocalError("");
                }}
                style={{ width: "100%", height: "100%", padding: "9px 12px" }}
              >
                <option value="">
                  {savedPrompts.length === 0 ? "暂无已收藏提问" : "选择收藏的问题 (Saved)"}
                </option>
                {savedPrompts.map((item, index) => (
                  <option value={item} key={`${index}-${item}`}>{item}</option>
                ))}
              </select>
            </div>

            <button
              type="button"
              className="save-prompt-button"
              disabled={!prompt.trim()}
              onClick={() => toggleSavePrompt(prompt)}
              style={{
                background: prompt.trim() ? (savedPrompts.includes(prompt.trim()) ? "#ffeeba" : "#fafaf5") : "#f2f3ed",
                color: prompt.trim() ? (savedPrompts.includes(prompt.trim()) ? "#856404" : "#2e3531") : "#a0aba3",
                border: "1px solid #c0cfc6",
                borderRadius: "4px",
                padding: "8px 12px",
                fontSize: "0.76rem",
                cursor: prompt.trim() ? "pointer" : "default",
                fontWeight: "bold",
                display: "flex",
                alignItems: "center",
                gap: "4px",
              }}
            >
              <span>{savedPrompts.includes(prompt.trim()) ? "★ 已收藏" : "☆ 收藏提问"}</span>
            </button>
          </div>
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
                <span>{"截图将先由 GLM-5V-Turbo 识别，再交给 " + plannerName + " 规划查询。"}</span>
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
        {(response?.request_id || taskProgress?.taskId) && (
          <p className="request-trace" role="status">
            <strong>{"\u8ffd\u8e2a ID"}</strong>
            <code>{response?.request_id ?? taskProgress?.taskId}</code>
            <span>{"\u53cd\u9988\u95ee\u9898\u65f6\u53ea\u9700\u63d0\u4f9b\u8fd9\u4e2a ID\u3002"}</span>
          </p>
        )}
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
        {response && (
          <PlanDisclosure
            response={response}
            disabled={isLoading || isImageReading}
            onSelectOption={handleClarificationOption}
          />
        )}
        {(() => {
          if (!response?.results) return null;
          const grouped = groupResults(response.results, response.plan?.queries || []);
          return grouped.map((group) => {
            if (group.isGrouped) {
              const combinedRows = group.results!.flatMap((r) => r.rows);
              const combinedRowCount = group.results!.reduce((sum, r) => sum + r.row_count, 0);
              const combinedSummary: Record<string, number | null> = {};
              const combinedSummaryMetadata: NonNullable<QueryResult["summary_metadata"]> = {};
              const combinedColumnMetadata: NonNullable<QueryResult["column_metadata"]> = {};
              for (const r of group.results!) {
                for (const [key, val] of Object.entries(r.summary)) {
                  if (val === null) {
                    if (!(key in combinedSummary)) {
                      combinedSummary[key] = null;
                    }
                  } else {
                    combinedSummary[key] = (combinedSummary[key] || 0) + val;
                  }
                }
                Object.assign(combinedSummaryMetadata, r.summary_metadata);
                Object.assign(combinedColumnMetadata, r.column_metadata);
              }
              const virtualResult: QueryResult = {
                query_id: group.key,
                provider: group.provider!,
                operation: group.operation!,
                status: "success",
                columns: group.columns!,
                rows: combinedRows,
                row_count: combinedRowCount,
                summary: combinedSummary,
                summary_metadata: combinedSummaryMetadata,
                column_metadata: combinedColumnMetadata,
                error: null,
              };
              return (
                <ResultTable
                  result={virtualResult}
                  key={group.key}
                  rawResults={group.results}
                  rawQueries={group.queries}
                />
              );
            } else {
              return (
                <ResultTable
                  result={group.singleResult!}
                  query={group.singleQuery}
                  key={group.key}
                />
              );
            }
          });
        })()}
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
              {response.cache_metrics && (
                <p>缓存指标：命中 {response.cache_metrics.hit || 0} 次，穿透 {response.cache_metrics.miss || 0} 次，绕过 {response.cache_metrics.bypass || 0} 次</p>
              )}
              <RequirementCoverage response={response} />
              {response.plan.queries.map((query) => (
                <div className="query-card" key={query.query_id}>
                  <strong>{query.operation}</strong><p>{query.purpose}</p>
                  <code>{JSON.stringify(query.params)}</code>
                  <code>{JSON.stringify({ fields: query.fields, filters: query.filters, aggregations: query.aggregations })}</code>
                </div>
              ))}
              {response.plan.execution_plan?.nodes.map((node) => (
                <div className="query-card" key={node.node_id}>
                  <strong>{node.node_id} · {node.kind}</strong>
                  <p>{node.query?.purpose ?? node.step?.operation}</p>
                  <code>{JSON.stringify({
                    inputs: node.input_result_ids,
                    query: node.query,
                    step: node.step,
                    fanout_input_field: node.fanout_input_field,
                    fanout_param: node.fanout_param,
                  })}</code>
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
      </> : activePage === "discovery" ? <DiscoveryPage onApplyFormula={(formula) => {
        setPrompt(`筛选今日全部A股中严格满足以下条件的股票，不要改变运算符或阈值，并返回股票代码、名称及公式涉及字段：${formula}`);
        setActivePage("analysis");
      }} /> : <ReferenceDataPage />}
    </main>
  );
}
