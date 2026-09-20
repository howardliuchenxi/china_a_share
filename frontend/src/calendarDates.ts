/**
 * Shared rendering rules for compact YYYYMMDD calendar values.
 *
 * Research datasets frequently carry calendar dates as plain integers such
 * as 20260917. Rendering those as ordinary numbers splits them into
 * thousands groups (20,260,917), so every date-named column keeps its
 * calendar shape instead.
 */

const DATE_COLUMN_TOKEN_PATTERN = /date|day|time|时|日|期/i;

/**
 * Whether a column denotes calendar dates. Token matching (rather than a
 * fixed suffix list) accepts arbitrary research-agent column names such as
 * “最近大涨日” or “上市时间”; value validation in compactCalendarDate stays
 * the final gate, so a token match alone never rewrites a non-date value.
 */
export function isDateLikeColumn(column: string): boolean {
  return DATE_COLUMN_TOKEN_PATTERN.test(column.trim().toLocaleLowerCase("zh-CN"));
}

/**
 * Return the YYYY-MM-DD rendering of an eight-digit YYYYMMDD value, or null
 * when the value is not a valid calendar date.
 */
export function compactCalendarDate(value: unknown): string | null {
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
