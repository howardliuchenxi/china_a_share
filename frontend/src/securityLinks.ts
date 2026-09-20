const QUOTE_PAGE_BASE_URL = "https://stockpage.10jqka.com.cn";

const A_SHARE_SUFFIXED = /^(\d{6})\.(?:SH|SZ|BJ)$/i;
const A_SHARE_BARE = /^\d{6}$/;
const HK_SUFFIXED = /^0*(\d{1,5})\.HK$/i;
const HK_PREFIXED = /^HK0*(\d{1,5})$/i;

const SECURITY_CODE_COLUMN_NAMES = new Set([
  "ts_code",
  "code",
  "symbol",
  "wind_code",
  "security_code",
  "stock_code",
]);

/** Public quote-page URL for one A-share or HK identifier, else null. */
export function securityQuotePageUrl(value: string | number | boolean | null): string | null {
  if (value === null) return null;
  const text = String(value).trim().toUpperCase();
  if (!text) return null;
  const aShareSuffixed = text.match(A_SHARE_SUFFIXED);
  if (aShareSuffixed) return `${QUOTE_PAGE_BASE_URL}/${aShareSuffixed[1]}/`;
  const hk = text.match(HK_SUFFIXED) ?? text.match(HK_PREFIXED);
  if (hk) return `${QUOTE_PAGE_BASE_URL}/HK${Number(hk[1]).toString().padStart(4, "0")}/`;
  if (A_SHARE_BARE.test(text)) return `${QUOTE_PAGE_BASE_URL}/${text}/`;
  return null;
}

/**
 * Whether one result column should render as quote-page links. Exchange-suffixed
 * or HK-prefixed values are unambiguous; bare six-digit values only qualify
 * under a known security-code column name so numeric columns stay plain.
 */
export function isSecurityCodeColumn(
  column: string,
  rows: Array<Record<string, string | number | boolean | null>>,
): boolean {
  const trustedName = SECURITY_CODE_COLUMN_NAMES.has(column.trim().toLowerCase());
  for (const row of rows.slice(0, 50)) {
    const raw = row[column];
    if (raw === null || raw === undefined) continue;
    const text = String(raw).trim().toUpperCase();
    if (!text) continue;
    if (A_SHARE_SUFFIXED.test(text) || HK_PREFIXED.test(text) || HK_SUFFIXED.test(text)) {
      return true;
    }
    if (A_SHARE_BARE.test(text)) return trustedName;
  }
  return false;
}
