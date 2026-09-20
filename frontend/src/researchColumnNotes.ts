/**
 * Fallback hover explanations for research-viewer columns when a workbook
 * predates per-column notes. Derived study columns are explained by the
 * workbook's own column notes; unknown columns fall back to a pointer to the
 * methodology section instead of a guessed definition.
 */
const FALLBACK_COLUMN_NOTES: Record<string, string> = {
  ts_code: "证券代码：沪市 .SH、深市 .SZ、北市 .BJ 后缀；港股为 HK 前缀代码。",
  code: "证券代码。",
  symbol: "证券代码或交易代码。",
  name: "证券简称，来自交易所证券主档；可能有 ST 等特别处理标记。",
  industry: "所属行业，按供应商行业分类。",
  trade_date: "交易日，格式 YYYY-MM-DD。",
  ann_date: "公告日期，格式 YYYY-MM-DD。",
  end_date: "报告期或区间截止日，格式 YYYY-MM-DD。",
  signal_date: "信号触发的交易日。",
  complete: "是否拥有完整的前向观察窗口：窗口未走完或数据缺失时为否，不应计入概率统计。",
  is_first: "是否为同一证券重叠时间窗内保留的首个信号：重叠信号按一个事件统计。",
};

const UNKNOWN_COLUMN_NOTE =
  "本研究派生列：完整定义见页面“研究口径”部分或下载 Excel 中的列说明/Methodology 页。";

/** Hover explanation for one viewer column, preferring workbook column notes. */
export function columnNote(
  column: string,
  columnNotes: Record<string, string> | undefined,
): string {
  const recorded = columnNotes?.[column];
  if (recorded && recorded.trim()) return recorded.trim();
  return FALLBACK_COLUMN_NOTES[column.trim().toLowerCase()] ?? UNKNOWN_COLUMN_NOTE;
}
