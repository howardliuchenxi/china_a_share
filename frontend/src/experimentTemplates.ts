export interface ExperimentTemplate {
  apiName: string;
  prompt: string;
}

export interface ExperimentGroup {
  id: string;
  label: string;
  description: string;
  templates: ExperimentTemplate[];
}

export const experimentGroups: ExperimentGroup[] = [
  {
    id: "market-basics",
    label: "01 · 基础接口冒烟测试",
    description: "股票基础信息、交易日历、行情、估值和涨跌停价格。",
    templates: [
      { apiName: "stock_basic", prompt: "查询目前正常上市的A股列表，包括股票代码、名称、行业和上市日期。" },
      { apiName: "trade_cal", prompt: "查询2026年7月上海证券交易所的交易日历。" },
      { apiName: "daily", prompt: "查询平安银行000001.SZ从2026年7月1日到7月17日的日线行情。" },
      { apiName: "daily_basic", prompt: "查询平安银行000001.SZ在2026年7月17日的换手率、市盈率、市净率和总市值。" },
      { apiName: "adj_factor", prompt: "查询平安银行000001.SZ从2026年7月1日到7月17日的复权因子。" },
      { apiName: "stk_limit", prompt: "查询2026年7月17日所有A股的涨停价和跌停价。" },
    ],
  },
  {
    id: "capital-activity",
    label: "02 · 资金和交易行为",
    description: "资金流向、龙虎榜、涨跌停、大宗交易和融资融券。",
    templates: [
      { apiName: "moneyflow", prompt: "查询平安银行000001.SZ在2026年7月17日的大单、中单和小单资金流向。" },
      { apiName: "top_list", prompt: "查询2026年7月17日的A股龙虎榜每日明细。" },
      { apiName: "top_inst", prompt: "查询2026年7月17日龙虎榜中的机构交易明细。" },
      { apiName: "limit_list_d", prompt: "查询2026年7月17日A股涨停、跌停和炸板股票列表。" },
      { apiName: "block_trade", prompt: "查询2026年7月17日的A股大宗交易记录。" },
      { apiName: "margin", prompt: "查询2026年7月17日融资融券交易汇总。" },
      { apiName: "margin_detail", prompt: "查询平安银行000001.SZ在2026年7月17日的融资融券明细。" },
    ],
  },
  {
    id: "financial-statements",
    label: "03 · 财务报表",
    description: "财务报表、财务指标、业绩预告、业绩快报和分红。",
    templates: [
      { apiName: "income", prompt: "查询贵州茅台600519.SH在2025年12月31日报告期的利润表。" },
      { apiName: "balancesheet", prompt: "查询贵州茅台600519.SH在2025年12月31日报告期的资产负债表。" },
      { apiName: "cashflow", prompt: "查询贵州茅台600519.SH在2025年12月31日报告期的现金流量表。" },
      { apiName: "fina_indicator", prompt: "查询贵州茅台600519.SH在2025年12月31日报告期的ROE、毛利率、净利率和每股收益。" },
      { apiName: "forecast", prompt: "查询贵州茅台600519.SH最近的业绩预告。" },
      { apiName: "express", prompt: "查询贵州茅台600519.SH最近的业绩快报。" },
      { apiName: "dividend", prompt: "查询贵州茅台600519.SH从2024年到2026年的分红送股记录。" },
    ],
  },
  {
    id: "company-events",
    label: "04 · 公司行为和股东数据",
    description: "股票回购、股东数据、股权质押、限售股解禁和停复牌。",
    templates: [
      { apiName: "repurchase", prompt: "查询贵州茅台600519.SH最近的股票回购记录。" },
      { apiName: "stk_holdernumber", prompt: "查询贵州茅台600519.SH最近的股东人数变化。" },
      { apiName: "top10_holders", prompt: "查询贵州茅台600519.SH最近一期前十大股东。" },
      { apiName: "top10_floatholders", prompt: "查询贵州茅台600519.SH最近一期前十大流通股东。" },
      { apiName: "pledge_stat", prompt: "查询贵州茅台600519.SH的股权质押统计。" },
      { apiName: "share_float", prompt: "查询贵州茅台600519.SH的限售股解禁记录。" },
      { apiName: "suspend_d", prompt: "查询2026年7月17日的停牌和复牌股票。" },
    ],
  },
];
