/**
 * Deterministic E2E test fixtures for the A-Share Lab frontend.
 *
 * All data below is SYNTHETIC — it does NOT originate from real Tushare or
 * DeepSeek responses. Field names and semantics conform to the official
 * Tushare stock-data API documentation, but the values are fabricated for
 * automated testing only.
 */

import type {
  AnalysisResponse,
  DecisionTraceStep,
  QueryResult,
  ServiceError,
} from "../src/contracts";

/* ------------------------------------------------------------------ */
/*  Shared synthetic helpers                                           */
/* ------------------------------------------------------------------ */

let fixtureCounter = 0;
function nextRequestId(): string {
  fixtureCounter += 1;
  return `e2e-req-${String(fixtureCounter).padStart(4, "0")}`;
}

/* ------------------------------------------------------------------ */
/*  Synthetic ServiceError fixtures                                     */
/* ------------------------------------------------------------------ */

export const tushareErrorFixture: ServiceError = {
  source: "tushare",
  code: 40203,
  message:
    "您的当前积分不支持调用该接口。部分高级接口需要更高积分。",
  http_status: 200,
  raw_response: {
    code: 40203,
    msg: "您的当前积分不支持调用该接口。部分高级接口需要更高积分。",
  },
};

export const systemErrorFixture: ServiceError = {
  source: "system",
  code: null,
  message: "本地请求失败。",
  http_status: null,
  raw_response: null,
};

/* ------------------------------------------------------------------ */
/*  Synthetic analysis trace steps                                      */
/* ------------------------------------------------------------------ */

const successTraceSteps: DecisionTraceStep[] = [
  {
    stage: "requirements",
    status: "success",
    title: "识别用户需求",
    detail: "已将自然语言转换为 1 项结构化数据请求。",
    evidence: [
      "需求：查询2026年7月17日A股涨跌分布",
      "目标市场：A_SHARE",
    ],
    external_call: false,
  },
  {
    stage: "capability",
    status: "success",
    title: "映射到数据源接口",
    detail: "已将需求映射到 daily 接口。",
    evidence: [
      "daily 接口支持 trade_date、ts_code、pct_chg 字段",
      "支持按交易日期过滤",
    ],
    external_call: false,
  },
  {
    stage: "planning",
    status: "success",
    title: "生成查询计划",
    detail: "查询计划已通过 A-share 市场边界校验。",
    evidence: [
      "操作：daily",
      "参数：trade_date=20260717",
      "计划可行性：supported",
    ],
    external_call: false,
  },
  {
    stage: "validation",
    status: "success",
    title: "校验查询计划",
    detail: "查询计划已通过 A-share 市场边界和参数校验。",
    evidence: [
      "市场：A_SHARE ✓",
      "证券后缀：.SH / .SZ / .BJ ✓",
      "日期格式：YYYYMMDD ✓",
    ],
    external_call: false,
  },
  {
    stage: "execution",
    status: "success",
    title: "执行数据查询",
    detail: "已从 Tushare 获取 3 行结果。",
    evidence: [
      "数据源：tushare",
      "返回 3 行",
      "列：ts_code, trade_date, pct_chg, change",
    ],
    external_call: true,
  },
  {
    stage: "result",
    status: "success",
    title: "汇总本地计算结果",
    detail: "已计算上涨、下跌和平盘数量。",
    evidence: [
      "上涨：1",
      "下跌：1",
      "平盘：1",
    ],
    external_call: false,
  },
];

/* ------------------------------------------------------------------ */
/*  Synthetic AnalysisResponse fixtures                                 */
/* ------------------------------------------------------------------ */

/** Multi-row daily-market success result (smoke-test scenario). */
export const successWithMultiRowFixture: AnalysisResponse = {
  request_id: nextRequestId(),
  planner: "deepseek",
  data_provider: "tushare",
  status: "success",
  plan: {
    market: "A_SHARE",
    interpretation:
      "查询2026年7月17日所有A股的日线行情，计算上涨、下跌和平盘数量。",
    feasibility: "supported",
    requirements: [
      {
        requirement: "获取2026-07-17所有A股日线行情",
        status: "covered",
        implementation: "daily",
        evidence: "daily 接口支持 trade_date 过滤和 pct_chg 字段。",
      },
      {
        requirement: "统计上涨、下跌、平盘数量",
        status: "covered",
        implementation: "本地 conditional_count 聚合",
        evidence:
          "condition 支持 pct_chg > 0（上涨），pct_chg < 0（下跌），pct_chg = 0（平盘）。",
      },
    ],
    limitations: [],
    queries: [
      {
        query_id: "e2e-q1",
        operation: "daily",
        params: {
          trade_date: "20260717",
        },
        fields: ["ts_code", "trade_date", "pct_chg", "change", "close"],
        purpose: "获取所有A股的日线行情以计算涨跌分布",
        transform: null,
        filters: [],
        aggregations: [
          { label: "上涨", field: "pct_chg", operator: "gt", value: 0 },
          { label: "下跌", field: "pct_chg", operator: "lt", value: 0 },
          { label: "平盘", field: "pct_chg", operator: "eq", value: 0 },
        ],
      },
    ],
  },
  results: [
    {
      query_id: "e2e-q1",
      provider: "tushare",
      operation: "daily",
      status: "success",
      columns: ["ts_code", "trade_date", "pct_chg", "change", "close"],
      rows: [
        {
          ts_code: "000001.SZ",
          trade_date: "20260717",
          pct_chg: 2.35,
          change: 0.28,
          close: 12.18,
        },
        {
          ts_code: "600519.SH",
          trade_date: "20260717",
          pct_chg: -1.42,
          change: -24.50,
          close: 1701.00,
        },
        {
          ts_code: "000002.SZ",
          trade_date: "20260717",
          pct_chg: 0.0,
          change: 0.0,
          close: 14.50,
        },
      ],
      row_count: 3,
      summary: { 上涨: 1, 下跌: 1, 平盘: 1 },
      error: null,
    } satisfies QueryResult,
  ],
  decision_trace: successTraceSteps,
  error: null,
};

/** Single-row result fixture (e.g. stock basic info). */
export const successWithSingleRowFixture: AnalysisResponse = {
  request_id: nextRequestId(),
  planner: "deepseek",
  data_provider: "tushare",
  status: "success",
  plan: {
    market: "A_SHARE",
    interpretation: "查询当前正常上市的A股列表。",
    feasibility: "supported",
    requirements: [
      {
        requirement: "获取正常上市A股列表",
        status: "covered",
        implementation: "stock_basic",
        evidence:
          "stock_basic 支持 list_status='L' 过滤和 exchange 过滤。",
      },
    ],
    limitations: [],
    queries: [
      {
        query_id: "e2e-q2",
        operation: "stock_basic",
        params: { list_status: "L" },
        fields: [
          "ts_code",
          "symbol",
          "name",
          "area",
          "industry",
          "market",
          "list_date",
        ],
        purpose: "获取当前正常上市的A股基础信息",
        transform: null,
        filters: [],
        aggregations: [],
      },
    ],
  },
  results: [
    {
      query_id: "e2e-q2",
      provider: "tushare",
      operation: "stock_basic",
      status: "success",
      columns: [
        "ts_code",
        "symbol",
        "name",
        "area",
        "industry",
        "market",
        "list_date",
      ],
      rows: [
        {
          ts_code: "000001.SZ",
          symbol: "000001",
          name: "平安银行",
          area: "深圳",
          industry: "银行",
          market: "主板",
          list_date: "19910403",
        },
      ],
      row_count: 1,
      summary: {},
      error: null,
    } satisfies QueryResult,
  ],
  decision_trace: [
    {
      stage: "requirements",
      status: "success",
      title: "识别用户需求",
      detail: "已将自然语言转换为 1 项结构化数据请求。",
      evidence: ["需求：查询A股上市列表"],
      external_call: false,
    },
    {
      stage: "execution",
      status: "success",
      title: "执行数据查询",
      detail: "已从 Tushare 获取 1 行结果。",
      evidence: ["数据源：tushare", "返回 1 行"],
      external_call: true,
    },
    {
      stage: "result",
      status: "success",
      title: "汇总本地计算结果",
      detail: "结果已准备就绪。",
      evidence: [],
      external_call: false,
    },
  ],
  error: null,
};

/** Multi-query partial-success fixture. */
export const partialSuccessFixture: AnalysisResponse = {
  request_id: nextRequestId(),
  planner: "deepseek",
  data_provider: "tushare",
  status: "partial_success",
  plan: {
    market: "A_SHARE",
    interpretation:
      "同时查询平安银行的日线行情和财务指标，其中一个接口因权限不足失败。",
    feasibility: "partial",
    requirements: [
      {
        requirement: "获取平安银行日线行情",
        status: "covered",
        implementation: "daily",
        evidence: "daily 接口支持 ts_code 和 trade_date 过滤。",
      },
      {
        requirement: "获取平安银行财务指标",
        status: "covered",
        implementation: "fina_indicator",
        evidence: "fina_indicator 接口支持 ts_code 和 end_date 过滤。",
      },
    ],
    limitations: [
      "fina_indicator 接口因积分不足调用失败，仅返回 daily 结果。",
    ],
    queries: [
      {
        query_id: "e2e-q3",
        operation: "daily",
        params: { ts_code: "000001.SZ", trade_date: "20260717" },
        fields: ["ts_code", "trade_date", "pct_chg", "close"],
        purpose: "获取平安银行日线行情",
        transform: null,
        filters: [],
        aggregations: [],
      },
      {
        query_id: "e2e-q4",
        operation: "fina_indicator",
        params: { ts_code: "000001.SZ", end_date: "20251231" },
        fields: ["ts_code", "end_date", "roe", "grossprofit_margin"],
        purpose: "获取平安银行财务指标",
        transform: null,
        filters: [],
        aggregations: [],
      },
    ],
  },
  results: [
    {
      query_id: "e2e-q3",
      provider: "tushare",
      operation: "daily",
      status: "success",
      columns: ["ts_code", "trade_date", "pct_chg", "close"],
      rows: [
        {
          ts_code: "000001.SZ",
          trade_date: "20260717",
          pct_chg: 2.35,
          close: 12.18,
        },
      ],
      row_count: 1,
      summary: {},
      error: null,
    } satisfies QueryResult,
    {
      query_id: "e2e-q4",
      provider: "tushare",
      operation: "fina_indicator",
      status: "error",
      columns: [],
      rows: [],
      row_count: 0,
      summary: {},
      error: {
        source: "tushare",
        code: 40203,
        message:
          "您的当前积分不支持调用该接口。部分高级接口需要更高积分。",
        http_status: 200,
        raw_response: {
          code: 40203,
          msg:
            "您的当前积分不支持调用该接口。部分高级接口需要更高积分。",
        },
      },
    } satisfies QueryResult,
  ],
  decision_trace: [
    {
      stage: "requirements",
      status: "success",
      title: "识别用户需求",
      detail: "已将自然语言转换为 2 项结构化数据请求。",
      evidence: ["需求：日线行情", "需求：财务指标"],
      external_call: false,
    },
    {
      stage: "execution",
      status: "success",
      title: "执行 daily 查询",
      detail: "已从 Tushare 获取 1 行 daily 结果。",
      evidence: ["数据源：tushare", "返回 1 行"],
      external_call: true,
    },
    {
      stage: "execution",
      status: "error",
      title: "执行 fina_indicator 查询失败",
      detail:
        "Tushare 返回错误 40203：积分不足，无法调用 fina_indicator 接口。",
      evidence: ["错误码：40203"],
      external_call: true,
    },
  ],
  error: null,
};

/** Upstream planning error fixture (e.g. DeepSeek failure). */
export const planningErrorFixture: AnalysisResponse = {
  request_id: nextRequestId(),
  planner: "deepseek",
  data_provider: "tushare",
  status: "error",
  plan: null,
  results: [],
  decision_trace: [
    {
      stage: "planning",
      status: "error",
      title: "DeepSeek 规划失败",
      detail:
        "DeepSeek API 返回认证错误：无效的 API Key。请检查 DEEPSEEK_API_KEY 环境变量。",
      evidence: ["错误源：deepseek", "HTTP 状态：401"],
      external_call: true,
    },
  ],
  error: {
    source: "deepseek",
    code: 401,
    message:
      "DeepSeek API 返回认证错误：无效的 API Key。请检查 DEEPSEEK_API_KEY 环境变量。",
    http_status: 401,
    raw_response: {
      error: {
        message: "Invalid API key provided.",
        type: "invalid_request_error",
        code: "invalid_api_key",
      },
    },
  },
};

export const unsupportedAnalysisFixture: AnalysisResponse = {
  request_id: nextRequestId(),
  planner: "deepseek",
  data_provider: "tushare",
  status: "error",
  plan: {
    market: "A_SHARE",
    interpretation: "Compare healthcare stocks by the approved retail-ratio proxy.",
    feasibility: "unsupported",
    requirements: [
      {
        requirement: "Build and compare the two healthcare cohorts.",
        status: "unsupported",
        implementation: null,
        evidence: "The current executor cannot dynamically fan out the industry universe.",
      },
    ],
    limitations: [
      "The current executor cannot dynamically fan out the full healthcare universe.",
    ],
    queries: [],
  },
  results: [],
  decision_trace: [],
  error: null,
};

/** Empty result fixture (API returned successfully but no rows). */
export const emptyResultFixture: AnalysisResponse = {
  request_id: nextRequestId(),
  planner: "deepseek",
  data_provider: "tushare",
  status: "success",
  plan: {
    market: "A_SHARE",
    interpretation: "查询未来日期2026-12-31的A股日线行情（该日期尚无数据）。",
    feasibility: "supported",
    requirements: [
      {
        requirement: "获取2026-12-31日线行情",
        status: "covered",
        implementation: "daily",
        evidence: "daily 接口支持 trade_date 过滤。",
      },
    ],
    limitations: [],
    queries: [
      {
        query_id: "e2e-q5",
        operation: "daily",
        params: { trade_date: "20261231" },
        fields: ["ts_code", "trade_date", "pct_chg", "close"],
        purpose: "获取2026-12-31日线行情",
        transform: null,
        filters: [],
        aggregations: [],
      },
    ],
  },
  results: [
    {
      query_id: "e2e-q5",
      provider: "tushare",
      operation: "daily",
      status: "success",
      columns: [],
      rows: [],
      row_count: 0,
      summary: {},
      error: null,
    } satisfies QueryResult,
  ],
  decision_trace: [
    {
      stage: "result",
      status: "warning",
      title: "查询结果为空",
      detail: "请求已成功发送到 Tushare，但返回结果为空。",
      evidence: ["返回 0 行"],
      external_call: true,
    },
  ],
  error: null,
};

/** Single-stock multi-row fixture (e.g. trade_cal results all for one stock). */
export const successWithSingleStockManyRowsFixture: AnalysisResponse = {
  request_id: nextRequestId(),
  planner: "deepseek",
  data_provider: "tushare",
  status: "success",
  plan: {
    market: "A_SHARE",
    interpretation: "查询平安银行2026年7月的交易日期。",
    feasibility: "supported",
    requirements: [
      {
        requirement: "获取平安银行2026年7月交易日历",
        status: "covered",
        implementation: "trade_cal",
        evidence: "trade_cal 接口支持 exchange 和 cal_date 过滤。",
      },
    ],
    limitations: [],
    queries: [
      {
        query_id: "e2e-q6",
        operation: "trade_cal",
        params: { exchange: "SZSE", start_date: "20260701", end_date: "20260731" },
        fields: ["exchange", "cal_date", "is_open", "pretrade_date"],
        purpose: "获取深圳市场2026年7月交易日历",
        transform: null,
        filters: [],
        aggregations: [],
      },
    ],
  },
  results: [
    {
      query_id: "e2e-q6",
      provider: "tushare",
      operation: "trade_cal",
      status: "success",
      columns: ["exchange", "cal_date", "is_open", "pretrade_date"],
      rows: [
        { exchange: "SZSE", cal_date: "20260701", is_open: 1, pretrade_date: "20260630" },
        { exchange: "SZSE", cal_date: "20260702", is_open: 1, pretrade_date: "20260701" },
        { exchange: "SZSE", cal_date: "20260703", is_open: 1, pretrade_date: "20260702" },
        { exchange: "SZSE", cal_date: "20260706", is_open: 1, pretrade_date: "20260703" },
        { exchange: "SZSE", cal_date: "20260707", is_open: 1, pretrade_date: "20260706" },
        { exchange: "SZSE", cal_date: "20260708", is_open: 1, pretrade_date: "20260707" },
        { exchange: "SZSE", cal_date: "20260709", is_open: 1, pretrade_date: "20260708" },
        { exchange: "SZSE", cal_date: "20260710", is_open: 1, pretrade_date: "20260709" },
        { exchange: "SZSE", cal_date: "20260713", is_open: 1, pretrade_date: "20260710" },
        { exchange: "SZSE", cal_date: "20260714", is_open: 1, pretrade_date: "20260713" },
        { exchange: "SZSE", cal_date: "20260715", is_open: 1, pretrade_date: "20260714" },
        { exchange: "SZSE", cal_date: "20260716", is_open: 1, pretrade_date: "20260715" },
        { exchange: "SZSE", cal_date: "20260717", is_open: 1, pretrade_date: "20260716" },
      ],
      row_count: 13,
      summary: {},
      error: null,
    } satisfies QueryResult,
  ],
  decision_trace: [
    {
      stage: "requirements",
      status: "success",
      title: "识别用户需求",
      detail: "已将自然语言转换为 1 项结构化数据请求。",
      evidence: ["需求：交易日历"],
      external_call: false,
    },
    {
      stage: "execution",
      status: "success",
      title: "执行数据查询",
      detail: "已从 Tushare 获取 13 行结果。",
      evidence: ["数据源：tushare", "返回 13 行"],
      external_call: true,
    },
  ],
  error: null,
};
