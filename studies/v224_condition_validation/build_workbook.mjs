import fs from "node:fs/promises";
import path from "node:path";

import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const repoRoot = process.cwd();
const outputDir = path.join(repoRoot, "studies/v224_condition_validation/outputs");
const artifactDir = path.join(repoRoot, "artifacts");
const artifactPath = path.join(artifactDir, "a_share_v224_effect_validation.xlsx");

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += char;
    }
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field.replace(/\r$/, ""));
    rows.push(row);
  }
  return rows;
}

function csvObjects(text) {
  const [headers, ...rows] = parseCsv(text);
  return rows.filter((row) => row.some((value) => value !== "")).map((row) =>
    Object.fromEntries(headers.map((header, index) => [header, row[index] ?? ""])),
  );
}

function numeric(value) {
  if (value === "" || value === undefined || value === null) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : value;
}

function excelColumn(index) {
  let value = index + 1;
  let label = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    label = String.fromCharCode(65 + remainder) + label;
    value = Math.floor((value - 1) / 26);
  }
  return label;
}

function detailHeader(name) {
  const direct = {
    ts_code: "\u80a1\u7968\u4ee3\u7801",
    name: "\u80a1\u7968\u7b80\u79f0",
    trade_date: "\u4fe1\u53f7\u65e5",
    session_rank: "\u4ea4\u6613\u65e5\u5e8f\u53f7",
    industry_code: "\u540c\u82b1\u987a\u4e8c\u7ea7\u884c\u4e1a\u4ee3\u7801",
    industry_name: "\u540c\u82b1\u987a\u4e8c\u7ea7\u884c\u4e1a",
    l1_pass: "L1\u5f53\u65e5\u6210\u7acb",
    l2_pass: "L2\u5f53\u65e5\u6210\u7acb",
    qf: "\u590d\u6743\u6536\u76d8qf",
    qf_open: "\u590d\u6743\u5f00\u76d8",
    ma5: "MA5",
    ma20: "MA20",
    ma60: "MA60",
    prev_ma5: "\u524d\u4e00\u65e5MA5",
    prev_ma20: "\u524d\u4e00\u65e5MA20",
    prev_ma60: "\u524d\u4e00\u65e5MA60",
    amount: "\u5f53\u65e5\u6210\u4ea4\u989d(\u5343\u5143)",
    prev5_amount_mean: "\u4e0d\u542b\u5f53\u65e5\u524d5\u65e5\u5e73\u5747\u6210\u4ea4\u989d",
    turnover_rate: "\u6362\u624b\u7387(%)",
    market_cap_billion: "\u603b\u5e02\u503c(\u4ebf\u5143)",
    pe: "\u52a8\u6001\u5e02\u76c8\u7387",
    version: "\u6a21\u578b\u7248\u672c",
    dwell6: "L6\u505c\u7559\u4ea4\u6613\u65e5",
    amount_ratio: "\u6210\u4ea4\u989d/\u524d5\u65e5\u5e73\u5747",
    r60: "MA60\u65e5\u6bd4",
    is_incremental: "\u662fv2.24\u65b0\u589e\u4fe1\u53f7",
    keep_first_10d: "10\u65e5keep-first\u4fdd\u7559",
  };
  if (direct[name]) return direct[name];
  let match = name.match(/^future_close_(\d+)$/);
  if (match) return `N${match[1]}\u672a\u6765\u590d\u6743\u6536\u76d8`;
  match = name.match(/^research_return_(\d+)$/);
  if (match) return `N${match[1]}\u4fe1\u53f7\u6536\u76d8\u6536\u76ca`;
  match = name.match(/^tradable_return_(\d+)$/);
  if (match) return `N${match[1]}\u6b21\u65e5\u5f00\u76d8\u6536\u76ca`;
  match = name.match(/^benchmark_return_(\d+)$/);
  if (match) return `N${match[1]}\u7b49\u6743\u57fa\u51c6\u6536\u76ca`;
  match = name.match(/^benchmark_tradable_return_(\d+)$/);
  if (match) return `N${match[1]}\u7b49\u6743\u57fa\u51c6\u6b21\u65e5\u5f00\u76d8\u6536\u76ca`;
  match = name.match(/^excess_return_(\d+)$/);
  if (match) return `N${match[1]}\u4fe1\u53f7\u6536\u76d8\u8d85\u989d`;
  match = name.match(/^tradable_excess_return_(\d+)$/);
  if (match) return `N${match[1]}\u6b21\u65e5\u5f00\u76d8\u8d85\u989d`;
  return name;
}

const [summaryText, comparisonText, yearlyText, eventsText, qaText] = await Promise.all([
  fs.readFile(path.join(outputDir, "summary.csv"), "utf8"),
  fs.readFile(path.join(outputDir, "comparison.csv"), "utf8"),
  fs.readFile(path.join(outputDir, "yearly.csv"), "utf8"),
  fs.readFile(path.join(outputDir, "events.csv"), "utf8"),
  fs.readFile(path.join(outputDir, "qa.json"), "utf8"),
]);

const summaryRows = csvObjects(summaryText);
const comparisonRows = csvObjects(comparisonText);
const yearlyRows = csvObjects(yearlyText);
const eventCsvRows = parseCsv(eventsText);
const qa = JSON.parse(qaText);
const [eventHeaders, ...eventRows] = eventCsvRows;

const keySummary = summaryRows.filter(
  (row) => row.sample_type === "keep_first_10d" && [1, 3, 5, 10].includes(Number(row.horizon)),
);
const canonicalN10 = keySummary.find(
  (row) => row.group === "v2.24" && row.entry_basis === "signal_close" && row.horizon === "10",
);
const canonicalEvents = eventRows.filter((row) => row[eventHeaders.indexOf("version")] === "v2.24").length;
const counterfactualEvents = eventRows.length - canonicalEvents;
const incrementalEvents = eventRows.filter(
  (row) => row[eventHeaders.indexOf("is_incremental")] === "True",
).length;

const workbook = Workbook.create();
const summarySheet = workbook.worksheets.add("\u7ed3\u679c\u6458\u8981");
const detailSheet = workbook.worksheets.add("\u7ed3\u679c\u660e\u7ec6");

summarySheet.showGridLines = false;
summarySheet.tabColor = "#1F4E78";
summarySheet.getRange("A1:K1").merge();
summarySheet.getRange("A1").values = [["A\u80a1 v2.24 \u6761\u4ef6\u6548\u679c\u9a8c\u8bc1"]];
summarySheet.getRange("A2:K2").merge();
summarySheet.getRange("A2").values = [[
  `\u6837\u672c\u671f ${qa.analysis_start_date} - ${qa.analysis_end_date} | \u4fe1\u53f7\u540e N=1-10 \u4e2a\u6709\u884c\u60c5\u4ea4\u6613\u65e5 | \u6570\u636e\u63d0\u53d6 ${qa.source_manifest.extracted_at.slice(0, 10)}`,
]];
summarySheet.getRange("A1:K2").format.fill = "#1F4E78";
summarySheet.getRange("A1:K2").format.font = { color: "#FFFFFF", bold: true };
summarySheet.getRange("A1").format.font = { color: "#FFFFFF", bold: true, size: 18 };
summarySheet.getRange("A1:K2").format.verticalAlignment = "center";
summarySheet.getRange("A1:K2").format.rowHeight = 28;

summarySheet.getRange("A4:K4").merge();
summarySheet.getRange("A4").values = [["\u6838\u5fc3\u7ed3\u8bba"]];
summarySheet.getRange("A4:K4").format.fill = "#D9EAF7";
summarySheet.getRange("A4:K4").format.font = { bold: true, color: "#1F4E78" };

summarySheet.getRange("A5:B8").values = [
  ["v2.24 L7\u4e8b\u4ef6\u6570", canonicalEvents],
  ["\u53cd\u4e8b\u5b9e\u4e8b\u4ef6\u6570", counterfactualEvents],
  ["v2.24\u7279\u6709\u4e8b\u4ef6\u6570", incrementalEvents],
  ["\u4e8b\u4ef6\u6269\u5f20\u500d\u6570", null],
];
summarySheet.getRange("B8").formulas = [["=B5/B6"]];
summarySheet.getRange("D5:E8").values = [
  ["N10\u4e0a\u6da8\u6982\u7387", numeric(canonicalN10.hit_rate)],
  ["N10\u5e73\u5747\u6da8\u5e45", numeric(canonicalN10.mean_return)],
  ["N10\u4e2d\u4f4d\u6570", numeric(canonicalN10.median_return)],
  ["N10\u5e73\u5747\u8d85\u989d", numeric(canonicalN10.mean_excess_return)],
];
summarySheet.getRange("G5:K8").merge();
summarySheet.getRange("G5").values = [[
  "\u7ed3\u8bba\uff1av2.24 \u663e\u8457\u6269\u5927\u4e86\u4fe1\u53f7\u6570\u91cf\uff0c\u4f46\u672a\u8bc1\u660e\u4fe1\u53f7\u8d28\u91cf\u4f18\u4e8e\u53cd\u4e8b\u5b9e\u53e3\u5f84\u3002N10 \u5e73\u5747\u6da8\u5e45\u4e3a\u6b63\uff0c\u4f46\u4e0a\u6da8\u6982\u7387\u4f4e\u4e8e 50%\u3001\u4e2d\u4f4d\u6570\u548c\u7b49\u6743\u8d85\u989d\u4e3a\u8d1f\uff0c5% \u622a\u5c3e\u5747\u503c\u4e5f\u4e3a\u8d1f\uff0c\u8bf4\u660e\u7edd\u5bf9\u5747\u503c\u53d7\u5c11\u6570\u5927\u6da8\u6837\u672c\u9a71\u52a8\u3002",
]];
summarySheet.getRange("G5:K8").format.wrapText = true;
summarySheet.getRange("G5:K8").format.fill = "#FFF2CC";
summarySheet.getRange("A5:K8").format.borders = { preset: "all", style: "thin", color: "#D9E1F2" };
summarySheet.getRange("B8").format.numberFormat = "0.00x";
summarySheet.getRange("E5:E8").format.numberFormat = "0.00%";

summarySheet.getRange("A10:K10").merge();
summarySheet.getRange("A10").values = [["\u4fe1\u53f7\u540e\u6536\u76ca\u6982\u89c8\uff0810\u65e5 keep-first\uff09"]];
summarySheet.getRange("A10:K10").format.fill = "#D9EAF7";
summarySheet.getRange("A10:K10").format.font = { bold: true, color: "#1F4E78" };
const summaryHeaders = [
  "\u7ec4\u522b", "\u5165\u573a\u53e3\u5f84", "N", "\u6837\u672c\u6570", "\u4e0a\u6da8\u6982\u7387", "\u5e73\u5747\u6da8\u5e45",
  "\u4e2d\u4f4d\u6570", "5%\u622a\u5c3e\u5747\u503c", "\u5e73\u5747\u8d85\u989d", "\u5e73\u5747\u6da8\u5e45CI\u4e0b\u9650", "\u5e73\u5747\u6da8\u5e45CI\u4e0a\u9650",
];
summarySheet.getRange("A11:K11").values = [summaryHeaders];
const summaryMatrix = keySummary.map((row) => [
  row.group === "retroactive_counterfactual" ? "\u53cd\u4e8b\u5b9e" : row.group === "v2.24_incremental" ? "v2.24\u7279\u6709" : row.group,
  row.entry_basis === "signal_close" ? "\u4fe1\u53f7\u65e5\u6536\u76d8" : "\u6b21\u65e5\u5f00\u76d8",
  numeric(row.horizon), numeric(row.event_count), numeric(row.hit_rate), numeric(row.mean_return),
  numeric(row.median_return), numeric(row.trimmed_mean_5pct), numeric(row.mean_excess_return),
  numeric(row.mean_return_ci_low), numeric(row.mean_return_ci_high),
]);
summarySheet.getRangeByIndexes(11, 0, summaryMatrix.length, 11).values = summaryMatrix;
summarySheet.getRange(`E12:K${11 + summaryMatrix.length}`).format.numberFormat = "0.00%";
summarySheet.getRange(`A11:K${11 + summaryMatrix.length}`).format.borders = { preset: "all", style: "thin", color: "#D9E1F2" };

const comparisonTitleRow = 13 + summaryMatrix.length;
summarySheet.getRange(`A${comparisonTitleRow}:K${comparisonTitleRow}`).merge();
summarySheet.getRange(`A${comparisonTitleRow}`).values = [["v2.24 \u51cf\u53bb\u53cd\u4e8b\u5b9e\u7684\u8d28\u91cf\u5dee\u5f02"]];
summarySheet.getRange(`A${comparisonTitleRow}:K${comparisonTitleRow}`).format.fill = "#D9EAF7";
summarySheet.getRange(`A${comparisonTitleRow}:K${comparisonTitleRow}`).format.font = { bold: true, color: "#1F4E78" };
const comparisonHeaderRow = comparisonTitleRow + 1;
summarySheet.getRange(`A${comparisonHeaderRow}:I${comparisonHeaderRow}`).values = [[
  "\u5165\u573a\u53e3\u5f84", "N", "v2.24\u6837\u672c", "\u53cd\u4e8b\u5b9e\u6837\u672c", "\u4e0a\u6da8\u6982\u7387\u5dee",
  "\u4e0a\u6da8\u6982\u7387\u5deeCI", "\u5e73\u5747\u6da8\u5e45\u5dee", "\u5e73\u5747\u6da8\u5e45\u5deeCI\u4e0b\u9650", "\u5e73\u5747\u6da8\u5e45\u5deeCI\u4e0a\u9650",
]];
const comparisonSelected = comparisonRows.filter(
  (row) => row.comparison === "v2.24_minus_counterfactual" && [1, 3, 5, 10].includes(Number(row.horizon)),
);
const comparisonMatrix = comparisonSelected.map((row) => [
  row.entry_basis === "signal_close" ? "\u4fe1\u53f7\u65e5\u6536\u76d8" : "\u6b21\u65e5\u5f00\u76d8",
  numeric(row.horizon), numeric(row.left_event_count), numeric(row.right_event_count), numeric(row.hit_rate_difference),
  `${(numeric(row.hit_rate_difference_ci_low) * 100).toFixed(2)}% ~ ${(numeric(row.hit_rate_difference_ci_high) * 100).toFixed(2)}%`,
  numeric(row.mean_return_difference), numeric(row.mean_return_difference_ci_low), numeric(row.mean_return_difference_ci_high),
]);
summarySheet.getRangeByIndexes(comparisonHeaderRow, 0, comparisonMatrix.length, 9).values = comparisonMatrix;
summarySheet.getRange(`E${comparisonHeaderRow + 1}:E${comparisonHeaderRow + comparisonMatrix.length}`).format.numberFormat = "0.00%";
summarySheet.getRange(`G${comparisonHeaderRow + 1}:I${comparisonHeaderRow + comparisonMatrix.length}`).format.numberFormat = "0.00%";
summarySheet.getRange(`A${comparisonHeaderRow}:I${comparisonHeaderRow + comparisonMatrix.length}`).format.borders = { preset: "all", style: "thin", color: "#D9E1F2" };

const yearlyTitleRow = comparisonHeaderRow + comparisonMatrix.length + 2;
summarySheet.getRange(`A${yearlyTitleRow}:G${yearlyTitleRow}`).merge();
summarySheet.getRange(`A${yearlyTitleRow}`).values = [["\u5e74\u5ea6\u7a33\u5b9a\u6027\uff08\u4fe1\u53f7\u65e5\u6536\u76d8\u53e3\u5f84\uff09"]];
summarySheet.getRange(`A${yearlyTitleRow}:G${yearlyTitleRow}`).format.fill = "#D9EAF7";
summarySheet.getRange(`A${yearlyTitleRow}:G${yearlyTitleRow}`).format.font = { bold: true, color: "#1F4E78" };
const yearlyHeaderRow = yearlyTitleRow + 1;
summarySheet.getRange(`A${yearlyHeaderRow}:G${yearlyHeaderRow}`).values = [[
  "\u7248\u672c", "\u5e74\u4efd", "N", "\u6837\u672c\u6570", "\u4e0a\u6da8\u6982\u7387", "\u5e73\u5747\u6da8\u5e45", "\u4e2d\u4f4d\u6570",
]];
const yearlyMatrix = yearlyRows.map((row) => [
  row.version === "retroactive_counterfactual" ? "\u53cd\u4e8b\u5b9e" : row.version,
  numeric(row.signal_year), numeric(row.horizon), numeric(row.event_count), numeric(row.hit_rate), numeric(row.mean_return), numeric(row.median_return),
]);
summarySheet.getRangeByIndexes(yearlyHeaderRow, 0, yearlyMatrix.length, 7).values = yearlyMatrix;
summarySheet.getRange(`E${yearlyHeaderRow + 1}:G${yearlyHeaderRow + yearlyMatrix.length}`).format.numberFormat = "0.00%";
summarySheet.getRange(`A${yearlyHeaderRow}:G${yearlyHeaderRow + yearlyMatrix.length}`).format.borders = { preset: "all", style: "thin", color: "#D9E1F2" };

const qaTitleRow = yearlyHeaderRow + yearlyMatrix.length + 2;
summarySheet.getRange(`A${qaTitleRow}:K${qaTitleRow}`).merge();
summarySheet.getRange(`A${qaTitleRow}`).values = [["\u6570\u636e\u8d28\u91cf\u3001\u53e3\u5f84\u4e0e\u9650\u5236"]];
summarySheet.getRange(`A${qaTitleRow}:K${qaTitleRow}`).format.fill = "#D9EAF7";
summarySheet.getRange(`A${qaTitleRow}:K${qaTitleRow}`).format.font = { bold: true, color: "#1F4E78" };
const qaRows = [
  ["\u5206\u6790\u9762\u677f", `${qa.panel_rows.toLocaleString()} \u884c\uff0c${qa.panel_security_count.toLocaleString()} \u53ea\u80a1\u7968\uff0c\u91cd\u590d\u952e ${qa.duplicate_panel_keys}`],
  ["\u6570\u636e\u5b8c\u6574\u6027", `daily_basic \u6708\u5ea6\u6700\u4f4e ${(qa.minimum_monthly_daily_basic_complete_rate * 100).toFixed(2)}%\uff1b\u884c\u4e1a\u6620\u5c04\u6708\u5ea6\u6700\u4f4e ${(qa.minimum_monthly_industry_mapping_rate * 100).toFixed(2)}%`],
  ["\u53bb\u91cd\u89c4\u5219", "\u540c\u80a1\u4fe1\u53f7\u540e 10 \u4e2a\u6709\u884c\u60c5\u4ea4\u6613\u65e5\u5185 keep-first\uff0c\u539f\u59cb\u53ca\u53bb\u91cd\u53e3\u5f84\u5747\u5df2\u8ba1\u7b97"],
  ["\u6536\u76ca\u53e3\u5f84", "\u7814\u7a76\u53e3\u5f84=qf(t+N)/qf(t)-1\uff1b\u53ef\u4ea4\u6613\u53e3\u5f84=\u6b21\u65e5\u590d\u6743\u5f00\u76d8\u5230 t+N \u590d\u6743\u6536\u76d8"],
  ["\u7f6e\u4fe1\u533a\u95f4", "\u6309\u4fe1\u53f7\u65e5\u805a\u7c7b bootstrap\uff0c1,000 \u6b21\uff0c\u56fa\u5b9a\u968f\u673a\u79cd\u5b50"],
  ["\u53cd\u4e8b\u5b9e", "\u4ec5\u5728 L3-L6 \u6bcf\u65e5\u91cd\u65b0\u65bd\u52a0 L1/L2 \u95e8\u69db\uff1b\u4e0d\u58f0\u79f0\u5b8c\u6574\u8fd8\u539f v2.23"],
  ["\u91cd\u5927\u9650\u5236", "THS \u884c\u4e1a\u6210\u5458\u8868\u65e0\u751f\u6548\u65e5\uff0c\u5386\u53f2\u671f\u4f7f\u7528\u63d0\u53d6\u65e5\u6210\u5458\u6620\u5c04\uff0cL2 \u5b58\u5728\u65f6\u70b9\u524d\u89c6\u98ce\u9669"],
  ["\u6267\u884c\u5c42\u9650\u5236", "L10 \u5356\u70b9/\u6301\u6709\u671f\u672a\u5b9a\uff0c\u672c\u8868\u9a8c\u8bc1\u7684\u662f L7 \u4fe1\u53f7\uff0c\u4e0d\u662f\u5b8c\u6574\u7ec4\u5408\u7b56\u7565\u6536\u76ca"],
  ["\u62bd\u67e5-\u6b63\u6536\u76ca", `${qa.spot_checks.positive_return.ts_code} ${qa.spot_checks.positive_return.trade_date}\uff0cN10 ${(qa.spot_checks.positive_return.reported_return_10 * 100).toFixed(2)}%\uff0c\u91cd\u7b97\u5dee ${qa.spot_checks.positive_return.absolute_difference}`],
  ["\u62bd\u67e5-\u8d1f\u6536\u76ca", `${qa.spot_checks.negative_return.ts_code} ${qa.spot_checks.negative_return.trade_date}\uff0cN10 ${(qa.spot_checks.negative_return.reported_return_10 * 100).toFixed(2)}%\uff0c\u91cd\u7b97\u5dee ${qa.spot_checks.negative_return.absolute_difference}`],
  ["\u62bd\u67e5-\u6210\u4ea4\u989d\u8fb9\u754c", `${qa.spot_checks.amount_boundary.ts_code} ${qa.spot_checks.amount_boundary.trade_date}\uff0c\u6bd4\u503c ${qa.spot_checks.amount_boundary.reported_ratio.toFixed(6)}`],
];
summarySheet.getRangeByIndexes(qaTitleRow, 0, qaRows.length, 2).values = qaRows;
summarySheet.getRange(`A${qaTitleRow + 1}:K${qaTitleRow + qaRows.length}`).format.wrapText = true;
summarySheet.getRange(`A${qaTitleRow + 1}:K${qaTitleRow + qaRows.length}`).format.borders = { preset: "all", style: "thin", color: "#E7E6E6" };
summarySheet.getRange(`A${qaTitleRow + 1}:A${qaTitleRow + qaRows.length}`).format.font = { bold: true, color: "#404040" };
summarySheet.getRange(`B${qaTitleRow + 1}:K${qaTitleRow + qaRows.length}`).merge(true);

const usedSummary = summarySheet.getUsedRange();
usedSummary.format.font = { name: "Aptos", size: 10 };
summarySheet.getRange("A1:K2").format.font = { name: "Aptos", color: "#FFFFFF", bold: true };
summarySheet.getRange("A1").format.font = { name: "Aptos", color: "#FFFFFF", bold: true, size: 18 };
summarySheet.getRange("A:K").format.columnWidth = 14;
summarySheet.getRange("A:A").format.columnWidth = 22;
summarySheet.getRange("B:B").format.columnWidth = 16;
summarySheet.getRange("G:K").format.columnWidth = 16;
summarySheet.freezePanes.freezeRows(11);

detailSheet.showGridLines = false;
detailSheet.tabColor = "#70AD47";
const detailHeaders = eventHeaders.map(detailHeader);
const textColumns = new Set(["ts_code", "name", "trade_date", "industry_code", "industry_name", "version"]);
const booleanColumns = new Set(["l1_pass", "l2_pass", "is_incremental", "keep_first_10d"]);
const typedRows = eventRows.filter((row) => row.some((value) => value !== "")).map((row) =>
  row.map((value, index) => {
    const name = eventHeaders[index];
    if (textColumns.has(name)) return value;
    if (booleanColumns.has(name)) return value === "True" || value === "true";
    return numeric(value);
  }),
);
detailSheet.getRangeByIndexes(0, 0, 1, detailHeaders.length).values = [detailHeaders];
detailSheet.getRangeByIndexes(1, 0, typedRows.length, detailHeaders.length).values = typedRows;
const detailLastColumn = excelColumn(detailHeaders.length - 1);
const detailLastRow = typedRows.length + 1;
detailSheet.tables.add(`A1:${detailLastColumn}${detailLastRow}`, true, "ResultDetailTable");
detailSheet.getRange(`A1:${detailLastColumn}1`).format.fill = "#1F4E78";
detailSheet.getRange(`A1:${detailLastColumn}1`).format.font = { bold: true, color: "#FFFFFF", size: 9 };
detailSheet.getRange(`A1:${detailLastColumn}${detailLastRow}`).format.verticalAlignment = "center";
detailSheet.getRange(`A1:${detailLastColumn}${detailLastRow}`).format.font = { name: "Aptos", size: 9 };
detailSheet.getRange(`A1:${detailLastColumn}1`).format.font = { name: "Aptos", bold: true, color: "#FFFFFF", size: 9 };
detailSheet.getRange(`A1:${detailLastColumn}1`).format.wrapText = true;
detailSheet.getRange(`A1:${detailLastColumn}1`).format.rowHeight = 42;
detailSheet.freezePanes.freezeRows(1);
detailSheet.freezePanes.freezeColumns(4);

eventHeaders.forEach((name, index) => {
  const column = excelColumn(index);
  if (name.includes("return_")) {
    detailSheet.getRange(`${column}2:${column}${detailLastRow}`).format.numberFormat = "0.00%";
    detailSheet.getRange(`${column}2:${column}${detailLastRow}`).conditionalFormats.add("cellIs", {
      operator: "greaterThan",
      formula: 0,
      format: { fill: "#E2F0D9", font: { color: "#006100" } },
    });
    detailSheet.getRange(`${column}2:${column}${detailLastRow}`).conditionalFormats.add("cellIs", {
      operator: "lessThan",
      formula: 0,
      format: { fill: "#FCE4D6", font: { color: "#9C0006" } },
    });
  } else if (["turnover_rate", "market_cap_billion", "pe", "amount_ratio", "r60"].includes(name)) {
    detailSheet.getRange(`${column}2:${column}${detailLastRow}`).format.numberFormat = "0.0000";
  } else if (["qf", "qf_open", "ma5", "ma20", "ma60", "prev_ma5", "prev_ma20", "prev_ma60", "future_close_1", "future_close_2", "future_close_3", "future_close_4", "future_close_5", "future_close_6", "future_close_7", "future_close_8", "future_close_9", "future_close_10"].includes(name)) {
    detailSheet.getRange(`${column}2:${column}${detailLastRow}`).format.numberFormat = "0.0000";
  }
});

detailSheet.getRange("A:A").format.columnWidth = 13;
detailSheet.getRange("B:B").format.columnWidth = 12;
detailSheet.getRange("C:C").format.columnWidth = 12;
detailSheet.getRange("D:F").format.columnWidth = 15;
detailSheet.getRange(`G:${detailLastColumn}`).format.columnWidth = 13;

await workbook.recalculate();
await fs.mkdir(artifactDir, { recursive: true });
const exported = await SpreadsheetFile.exportXlsx(workbook);
await exported.save(artifactPath);
const savedWorkbook = await SpreadsheetFile.importXlsx(await FileBlob.load(artifactPath));
savedWorkbook.worksheets.getItem("\u7ed3\u679c\u6458\u8981");
savedWorkbook.worksheets.getItem("\u7ed3\u679c\u660e\u7ec6");

const summaryPreview = await savedWorkbook.render({
  sheetName: "\u7ed3\u679c\u6458\u8981",
  range: `A1:K${qaTitleRow + qaRows.length}`,
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, "summary_preview.png"), new Uint8Array(await summaryPreview.arrayBuffer()));
const detailPreview = await savedWorkbook.render({
  sheetName: "\u7ed3\u679c\u660e\u7ec6",
  range: "A1:R24",
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, "detail_preview.png"), new Uint8Array(await detailPreview.arrayBuffer()));
const detailReturnsPreview = await savedWorkbook.render({
  sheetName: "\u7ed3\u679c\u660e\u7ec6",
  range: "AA1:AT24",
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, "detail_returns_preview.png"), new Uint8Array(await detailReturnsPreview.arrayBuffer()));

const inspection = await savedWorkbook.inspect({
  kind: "sheet,table,region,formula",
  maxChars: 12000,
  tableMaxRows: 5,
  tableMaxCols: 12,
  options: { maxResults: 100 },
});
const inspectionText = inspection.ndjson ?? String(inspection);
if (/#REF!|#DIV\/0!|#VALUE!|#NAME\?|#N\/A/.test(inspectionText)) {
  throw new Error("Workbook validation found a formula error.");
}
await fs.writeFile(path.join(outputDir, "workbook_inspection.txt"), inspectionText, "utf8");

console.log(JSON.stringify({ artifactPath, detailRows: typedRows.length, detailColumns: detailHeaders.length }));
