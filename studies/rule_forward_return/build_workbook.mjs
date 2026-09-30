// Build the two-sheet Excel deliverable from validated US rule-study outputs.

import fs from "node:fs/promises";
import { createReadStream } from "node:fs";
import readline from "node:readline";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";


process.on("uncaughtException", (error) => {
  const localFrames = String(error?.stack ?? "")
    .split("\n")
    .filter((line) => line.includes("build_workbook.mjs"));
  console.error(JSON.stringify({
    name: error?.name ?? "Error",
    message: error?.message ?? String(error),
    localFrames,
  }));
  process.exit(1);
});


const [summaryPath, detailPath, outputPath, previewDir] = process.argv.slice(2);
if (!summaryPath || !detailPath || !outputPath || !previewDir) {
  throw new Error(
    "Usage: node build_workbook.mjs <summary.json> <detail.csv> <output.xlsx> <preview-dir>",
  );
}

const fontFamily = "Arial";
const summarySheetName = "\u7ed3\u679c\u6458\u8981";
const detailSheetName = "\u7ed3\u679c\u660e\u7ec6";
const ruleLabels = {
  gapdown2: "\u5f00\u76d8\u4f4e\u5f00\u2264-2%",
  mom20_top10: "20\u65e5\u52a8\u91cf\u00b7\u6c60\u5185\u524d10%",
  rev5_bot10: "5\u65e5\u53cd\u8f6c\u00b7\u6c60\u5185\u540e10%\u4e70\u5165",
  volspike2: "\u91cf\u6bd4\u22652",
};
const scenarioLabels = {
  baseline: "\u57fa\u7ebf",
  exclude_gap8: "\u5254\u9664\u7edd\u5bf9\u7f3a\u53e3>8%",
  cap_1pct: "\u805a\u5408\u5355\u7968\u4e0a\u96501%",
  cap_2pct: "\u805a\u5408\u5355\u7968\u4e0a\u96502%",
  cap_3pct: "\u805a\u5408\u5355\u7968\u4e0a\u96503%",
};
const ruleOrder = ["gapdown2", "mom20_top10", "rev5_bot10", "volspike2"];
const scenarioOrder = ["baseline", "exclude_gap8", "cap_1pct", "cap_2pct", "cap_3pct"];
const palette = ["#1F4E78", "#C55A11", "#548235", "#7030A0"];


function parseCsvLine(line) {
  const values = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (character === '"') {
      if (quoted && line[index + 1] === '"') {
        value += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === "," && !quoted) {
      values.push(value);
      value = "";
    } else {
      value += character;
    }
  }
  values.push(value);
  return values;
}


function numeric(value) {
  if (value === "" || value === undefined || value === null || value === "nan") {
    return null;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    throw new Error(`Expected numeric value, received: ${value}`);
  }
  return parsed;
}


function dateValue(value) {
  return value ? new Date(`${value.slice(0, 10)}T00:00:00Z`) : null;
}


function booleanValue(value) {
  return String(value).toLowerCase() === "true";
}


function excelColumnName(columnNumber) {
  let number = columnNumber;
  let result = "";
  while (number > 0) {
    const remainder = (number - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    number = Math.floor((number - 1) / 26);
  }
  return result;
}


function sectionHeader(range) {
  range.format.fill = "#1F4E78";
  range.format.font = { name: fontFamily, size: 10, bold: true, color: "#FFFFFF" };
  range.format.verticalAlignment = "center";
  range.format.horizontalAlignment = "center";
  range.format.wrapText = true;
}


function lightBorders(range) {
  range.format.borders = {
    insideHorizontal: { style: "thin", color: "#D9E2F3" },
    bottom: { style: "thin", color: "#AAB7C4" },
  };
}


const summary = JSON.parse(await fs.readFile(summaryPath, "utf8"));
const workbook = Workbook.create();
const summarySheet = workbook.worksheets.add(summarySheetName);
const detailSheet = workbook.worksheets.add(detailSheetName);
summarySheet.showGridLines = false;
detailSheet.showGridLines = false;
summarySheet.tabColor = "#1F4E78";
detailSheet.tabColor = "#5B9BD5";

summarySheet.getRange("A2").values = [[
  "\u7f8e\u80a1\u89c4\u5219\u524d\u5411\u6536\u76ca\u4e0e\u7ec4\u5408\u5316\u590d\u9a8c",
]];
summarySheet.getRange("A2").format.font = {
  name: fontFamily,
  size: 16,
  bold: true,
  color: "#1F2937",
};
summarySheet.getRange("A3").values = [[
  `\u6570\u636e\u81f3 ${summary.as_of_date} \u00b7 ${summary.universe} \u53ea\u7f8e\u80a1 \u00b7 ${summary.trading_days} \u4e2a\u4ea4\u6613\u65e5 \u00b7 ${summary.source}`,
]];
summarySheet.getRange("A3").format.font = {
  name: fontFamily,
  size: 10,
  italic: true,
  color: "#5B6573",
};
summarySheet.getRange("A3:K3").format.borders = {
  bottom: { style: "thin", color: "#AAB7C4" },
};
summarySheet.mergeCells("A5:K5");
summarySheet.getRange("A5").values = [[
  "\u7ed3\u8bba\uff1a4\u6761\u89c4\u5219\u7ec4\u5408\u5747\u672a\u540c\u65f6\u901a\u8fc7\u6708\u5ea6CI\u4e0e16/24\u6b63\u8d85\u989d\u6708\u7a33\u5b9a\u6027\u7ebf\uff1b1%\u20133%\u5355\u7968\u4e0a\u9650\u964d\u4f4e\u56de\u64a4\uff0c\u4f46\u540c\u65f6\u524a\u5f31\u7d2f\u8ba1\u8d85\u989d\u3002",
]];
summarySheet.getRange("A5:K5").format.fill = "#FDE9E7";
summarySheet.getRange("A5").format.font = {
  name: fontFamily,
  size: 11,
  bold: true,
  color: "#9C0006",
};

const p1Headers = [
  "\u89c4\u5219",
  "\u6301\u4ed3\u4e8b\u4ef6",
  "\u6708\u6570",
  "\u7d2f\u8ba1\u51c0\u6536\u76ca",
  "\u6c60\u57fa\u51c6",
  "\u7d2f\u8ba1\u51c0\u8d85\u989d",
  "\u6700\u5927\u56de\u64a4",
  "\u6708\u5ea6\u80dc\u7387",
  "\u6b63\u8d85\u989d\u6708",
  "CI\u4e0b\u754c",
  "\u5224\u5b9a",
];
summarySheet.getRange("A8:K8").values = [p1Headers];
sectionHeader(summarySheet.getRange("A8:K8"));
const p1ByRule = Object.fromEntries(summary.portfolio_summary.map((row) => [row.rule, row]));
summarySheet.getRange("A9:K12").values = ruleOrder.map((rule) => {
  const row = p1ByRule[rule];
  return [
    ruleLabels[rule],
    null,
    row.months,
    row.total_net_return,
    row.total_benchmark_return,
    row.total_excess_return,
    row.max_drawdown,
    row.monthly_win_rate,
    `${row.positive_excess_months}/${row.months}`,
    row.ci_low,
    row.verdict,
  ];
});

ruleOrder.forEach((rule, index) => {
  summarySheet.getRange(`B${9 + index}`).values = [[summary.detail_counts[rule]]];
});
summarySheet.getRange("B9:C12").format.numberFormat = "#,##0";
summarySheet.getRange("D9:H12").format.numberFormat = "0.00%";
summarySheet.getRange("J9:J12").format.numberFormat = "0.00%";
summarySheet.getRange("A9:K12").format.font = { name: fontFamily, size: 10, color: "#1F2937" };
lightBorders(summarySheet.getRange("A8:K12"));
summarySheet.getRange("F9:J12").conditionalFormats.add("cellIs", {
  operator: "lessThan",
  formula: 0,
  format: { fill: "#FDE9E7", font: { color: "#9C0006" } },
});
summarySheet.getRange("F9:J12").conditionalFormats.add("cellIs", {
  operator: "greaterThan",
  formula: 0,
  format: { fill: "#E2F0D9", font: { color: "#006100" } },
});

const eventHeaders = [
  "\u65b9\u6848",
  "\u4e8b\u4ef6\u6570",
  "\u5254\u9664",
  "\u5e73\u5747\u6536\u76ca",
  "\u8d85\u989d",
  "OOS\u8d85\u989d",
  "CI\u4e0b\u754c",
  "null95",
  "\u80dc\u7387",
  "p5",
  "\u6700\u5dee",
  "\u5224\u5b9a",
];
summarySheet.getRange("A16:L16").values = [eventHeaders];
sectionHeader(summarySheet.getRange("A16:L16"));
summarySheet.getRange("A17:L18").values = summary.event_summary.map((row) => [
  scenarioLabels[row.scenario],
  row.n,
  row.removed_events,
  row.mean_ret,
  row.mean_excess,
  row.oos_excess,
  row.ci_low,
  row.null95,
  row.win_rate,
  row.p5_return,
  row.worst_return,
  row.verdict,
]);
summarySheet.getRange("B17:C18").format.numberFormat = "#,##0";
summarySheet.getRange("D17:K18").format.numberFormat = "0.00%";
lightBorders(summarySheet.getRange("A16:L18"));

const controlHeaders = [
  "\u65b9\u6848",
  "\u6301\u4ed3\u4e8b\u4ef6",
  "\u7d2f\u8ba1\u51c0\u6536\u76ca",
  "\u7d2f\u8ba1\u51c0\u8d85\u989d",
  "\u6700\u5927\u56de\u64a4",
  "\u6708\u5ea6\u80dc\u7387",
  "\u6b63\u8d85\u989d\u6708",
  "CI\u4e0b\u754c",
  "\u8fc7\u6ee4\u4fe1\u53f7",
  "\u4e0a\u9650\u672a\u5efa\u4ed3",
  "\u5224\u5b9a",
];
summarySheet.getRange("A22:K22").values = [controlHeaders];
sectionHeader(summarySheet.getRange("A22:K22"));
const controlByScenario = Object.fromEntries(summary.control_summary.map((row) => [row.scenario, row]));
summarySheet.getRange("A23:K27").values = scenarioOrder.map((scenario) => {
  const row = controlByScenario[scenario];
  return [
    scenarioLabels[scenario],
    row.positions,
    row.total_net_return,
    row.total_excess_return,
    row.max_drawdown,
    row.monthly_win_rate,
    `${row.positive_excess_months}/${row.months}`,
    row.ci_low,
    row.filtered_signals,
    row.capped_out_signals,
    row.verdict,
  ];
});
summarySheet.getRange("B23:B27").format.numberFormat = "#,##0";
summarySheet.getRange("C23:F27").format.numberFormat = "0.00%";
summarySheet.getRange("H23:H27").format.numberFormat = "0.00%";
summarySheet.getRange("I23:J27").format.numberFormat = "#,##0";
lightBorders(summarySheet.getRange("A22:K27"));

summarySheet.getRange("A31:H31").values = [[
  "\u65b9\u6848",
  "\u5206\u6bb5",
  "\u4e8b\u4ef6\u6570",
  "\u5e73\u5747\u6536\u76ca",
  "\u5e73\u5747\u8d85\u989d",
  "\u80dc\u7387",
  "p5",
  "\u6700\u5dee",
]];
sectionHeader(summarySheet.getRange("A31:H31"));
summarySheet.getRange("A32:H37").values = summary.annual_events.map((row) => [
  scenarioLabels[row.scenario],
  row.period,
  row.event_count,
  row.mean_return,
  row.mean_excess,
  row.win_rate,
  row.p5_return,
  row.worst_return,
]);
summarySheet.getRange("C32:C37").format.numberFormat = "#,##0";
summarySheet.getRange("D32:H37").format.numberFormat = "0.00%";
lightBorders(summarySheet.getRange("A31:H37"));

summarySheet.getRange("A41:G41").values = [[
  "\u65b9\u6848",
  "\u5206\u6bb5",
  "\u6708\u6570",
  "\u51c0\u6536\u76ca",
  "\u6c60\u57fa\u51c6",
  "\u8d85\u989d",
  "\u6b63\u8d85\u989d\u6708",
]];
sectionHeader(summarySheet.getRange("A41:G41"));
const yearlyRows = [];
for (const scenario of scenarioOrder) {
  for (const row of summary.control_yearly.filter((item) => item.scenario === scenario)) {
    yearlyRows.push([
      scenarioLabels[scenario],
      row.period,
      row.months,
      row.net_return,
      row.benchmark_return,
      row.excess_return,
      `${row.positive_excess_months}/${row.months}`,
    ]);
  }
}
summarySheet.getRange(`A42:G${41 + yearlyRows.length}`).values = yearlyRows;
summarySheet.getRange(`C42:C${41 + yearlyRows.length}`).format.numberFormat = "#,##0";
summarySheet.getRange(`D42:F${41 + yearlyRows.length}`).format.numberFormat = "0.00%";
lightBorders(summarySheet.getRange(`A41:G${41 + yearlyRows.length}`));

const p1MonthlyByMonth = new Map();
for (const row of summary.portfolio_monthly) {
  if (!p1MonthlyByMonth.has(row.month)) {
    p1MonthlyByMonth.set(row.month, {});
  }
  p1MonthlyByMonth.get(row.month)[row.rule] = row;
}
const p1Months = [...p1MonthlyByMonth.keys()].sort();
summarySheet.getRange("M23:Q23").values = [[
  "\u6708\u4efd",
  ...ruleOrder.map((rule) => ruleLabels[rule]),
]];
sectionHeader(summarySheet.getRange("M23:Q23"));
summarySheet.getRange(`M24:Q${23 + p1Months.length}`).values = p1Months.map((month) => [
  month,
  ...ruleOrder.map((rule) => p1MonthlyByMonth.get(month)[rule].strategy_nav),
]);
summarySheet.getRange(`N24:Q${23 + p1Months.length}`).format.numberFormat = "0.0000";
lightBorders(summarySheet.getRange(`M23:Q${23 + p1Months.length}`));

const navChart = summarySheet.charts.add(
  "line",
  summarySheet.getRange(`M23:Q${23 + p1Months.length}`),
);
navChart.title = "\u6708\u672b\u7d2f\u8ba1\u51c0\u503c\uff08\u5df2\u626310bp\uff09";
navChart.titleTextStyle.typeface = fontFamily;
navChart.titleTextStyle.fontSize = 12;
navChart.legend = { position: "top", textStyle: { typeface: fontFamily, fontSize: 10 } };
navChart.xAxis = { axisType: "textAxis", textStyle: { typeface: fontFamily, fontSize: 9 } };
navChart.yAxis = {
  numberFormatCode: "0.00",
  numberFormatSourceLinked: false,
  textStyle: { typeface: fontFamily, fontSize: 9 },
};
navChart.setPosition("M2", "W20");
navChart.series.items.forEach((series, index) => {
  const color = palette[index % palette.length];
  series.line = { fill: color, style: "solid", width: 2 };
});

const controlMonthlyByMonth = new Map();
for (const row of summary.control_monthly) {
  if (!controlMonthlyByMonth.has(row.month)) {
    controlMonthlyByMonth.set(row.month, {});
  }
  controlMonthlyByMonth.get(row.month)[row.scenario] = row;
}
const controlMonths = [...controlMonthlyByMonth.keys()].sort();
summarySheet.getRange("M51:R51").values = [[
  "\u6708\u4efd",
  ...scenarioOrder.map((scenario) => scenarioLabels[scenario]),
]];
sectionHeader(summarySheet.getRange("M51:R51"));
summarySheet.getRange(`M52:R${51 + controlMonths.length}`).values = controlMonths.map((month) => [
  month,
  ...scenarioOrder.map((scenario) => controlMonthlyByMonth.get(month)[scenario].excess_return),
]);
summarySheet.getRange(`N52:R${51 + controlMonths.length}`).format.numberFormat = "0.00%";
lightBorders(summarySheet.getRange(`M51:R${51 + controlMonths.length}`));

summarySheet.mergeCells("A59:K59");
summarySheet.getRange("A59").values = [["\u65b9\u6cd5\u3001\u53e3\u5f84\u4e0e\u9650\u5236"]];
summarySheet.getRange("A59:K59").format.fill = "#D9EAF7";
summarySheet.getRange("A59").format.font = {
  name: fontFamily,
  size: 11,
  bold: true,
  color: "#1F4E78",
};
const notes = [
  "1. \u6c60\uff1a\u6eda\u52a820\u65e5\u6210\u4ea4\u989d\u4e2d\u4f4d\u6570\u22652,000\u4e07\u7f8e\u5143\uff0c\u4e14\u4fe1\u53f7\u65e5\u6536\u76d8\u22655\u7f8e\u5143\u3002",
  "2. \u6267\u884c\uff1aT\u65e5\u6536\u76d8\u5224\u5b9a\uff0cT+1\u5f00\u76d8\u4e70\u5165\uff0cT+5\u6536\u76d8\u5356\u51fa\uff1b5\u4e2a\u8f6e\u6362\u8896\u5957\uff0c\u6bcf\u65e5\u65b0\u8896\u5957\u536020%\uff0c\u8896\u5957\u5185\u7b49\u6743\u3002",
  "3. \u6210\u672c\uff1a\u5b8c\u6574\u5f80\u8fd410bp\uff0c\u4e70\u5356\u54045bp\uff1b\u6c60\u57fa\u51c6\u4e0d\u6263\u6210\u672c\u3002",
  "4. \u7ec4\u5408\u5224\u5b9a\uff1a\u6708\u5ea6\u8d85\u989d\u5747\u503c bootstrap 95%CI\u4e0b\u754c>0\uff0c\u4e1424\u4e2a\u6708\u4e2d\u81f3\u5c1116\u4e2a\u6708\u8d85\u989d\u4e3a\u6b63\u3002",
  "5. \u6df1\u7f3a\u53e3\u8fc7\u6ee4\u4ec5\u4f5c\u5bf9\u7167\uff0c\u4e0d\u66ff\u6362\u4e3b\u53e3\u5f84\uff1b\u5355\u7968\u4e0a\u9650\u6309\u5165\u573a\u65f6\u5168\u7ec4\u5408\u540c\u7968\u655e\u53e3\u8ba1\u7b97\uff0c\u5269\u4f59\u8d44\u91d1\u7559\u73b0\u91d1\u3002",
  `6. \u6570\u636e\u6e90\uff1aMassive \u7f8e\u80a1\u524d\u590d\u6743\u65e5\u7ebf\uff0c\u63d0\u53d6\u65e5 ${summary.source_extraction_date}\uff1b\u672c\u5730\u9762\u677f\u7f3a\u5c11\u8bc1\u5238\u540d\u79f0\uff0c\u660e\u7ec6\u4ec5\u4fdd\u7559\u53ef\u5ba1\u8ba1\u4ee3\u7801\u3002`,
  "7. \u5386\u53f2\u56de\u6d4b\u4e0d\u4ee3\u8868\u672a\u6765\u7ed3\u679c\uff1b\u672a\u5efa\u6a21\u5e02\u573a\u51b2\u51fb\u3001\u989d\u5916\u6ed1\u70b9\u4e0e\u7a0e\u8d39\u3002",
];
notes.forEach((note, index) => {
  const row = 60 + index;
  summarySheet.mergeCells(`A${row}:K${row}`);
  summarySheet.getRange(`A${row}`).values = [[note]];
});
summarySheet.getRange("A60:K66").format.font = { name: fontFamily, size: 10, color: "#334155" };
summarySheet.getRange("A60:A66").format.wrapText = true;

summarySheet.getRange("A1:W76").format.verticalAlignment = "center";
summarySheet.getRange("A1:W76").format.font.name = fontFamily;
summarySheet.getRange("A5:K5").format.rowHeight = 32;
summarySheet.getRange("A8:K8").format.rowHeight = 30;
summarySheet.getRange("A16:L16").format.rowHeight = 30;
summarySheet.getRange("A22:K22").format.rowHeight = 34;
summarySheet.getRange("A31:H31").format.rowHeight = 28;
summarySheet.getRange("A41:G41").format.rowHeight = 28;
summarySheet.getRange("M23:Q23").format.rowHeight = 34;
summarySheet.getRange("M51:R51").format.rowHeight = 34;
summarySheet.getRange("A60:A66").format.rowHeight = 24;
const summaryWidths = [22, 12, 10, 14, 12, 14, 12, 12, 12, 12, 12, 12];
summaryWidths.forEach((width, index) => {
  const column = excelColumnName(index + 1);
  summarySheet.getRange(`${column}:${column}`).format.columnWidth = width;
});
for (let index = 13; index <= 18; index += 1) {
  const column = excelColumnName(index);
  summarySheet.getRange(`${column}:${column}`).format.columnWidth = index === 13 ? 12 : 18;
}

const detailHeaders = [
  "\u89c4\u5219",
  "\u4fe1\u53f7\u65e5",
  "\u4e70\u5165\u65e5",
  "\u5356\u51fa\u65e5",
  "\u8bc1\u5238\u4ee3\u7801",
  "\u8bc1\u5238\u540d\u79f0\uff08\u6570\u636e\u6e90\u672a\u63d0\u4f9b\uff09",
  "\u89c4\u5219\u539f\u59cb\u503c\uff08\u7f3a\u53e3/\u6536\u76ca/\u91cf\u6bd4\uff09",
  "20\u65e5\u4e2d\u4f4d\u6210\u4ea4\u989d\uff08USD\uff09",
  "\u6bdb\u6536\u76ca",
  "\u51c0\u6536\u76ca\uff0810bp\uff09",
  "\u6c60\u57fa\u51c6",
  "\u6bdb\u8d85\u989d",
  "\u6743\u91cd/\u8fc7\u6ee4\u5ba1\u8ba1",
];
detailSheet.getRange("A1:M1").values = [detailHeaders];
sectionHeader(detailSheet.getRange("A1:M1"));
detailSheet.getRange("A1:M1").format.rowHeight = 42;
const detailWidths = [
  24, 12, 12, 12, 12, 18, 22, 19, 12, 14, 12, 12, 68,
];
detailWidths.forEach((width, index) => {
  const column = excelColumnName(index + 1);
  detailSheet.getRange(`${column}:${column}`).format.columnWidth = width;
});
detailSheet.freezePanes.freezeRows(1);
detailSheet.freezePanes.freezeColumns(6);

function detailValues(row) {
  const rawValue = {
    gapdown2: numeric(row.open_gap),
    mom20_top10: numeric(row.ret_20d),
    rev5_bot10: numeric(row.ret_5d),
    volspike2: numeric(row.vol_ratio),
  }[row.rule];
  const audit = [
    `baseline=${row.entry_weight ? `${(Number(row.entry_weight) * 100).toFixed(3)}%` : "n/a"}`,
    `gap>8%=${booleanValue(row.deep_gap_excluded) ? "yes" : "no"}`,
    `cap1=${row.cap_1pct_aggregate_weight ? `${(Number(row.cap_1pct_aggregate_weight) * 100).toFixed(3)}%` : "n/a"}`,
    `cap2=${row.cap_2pct_aggregate_weight ? `${(Number(row.cap_2pct_aggregate_weight) * 100).toFixed(3)}%` : "n/a"}`,
    `cap3=${row.cap_3pct_aggregate_weight ? `${(Number(row.cap_3pct_aggregate_weight) * 100).toFixed(3)}%` : "n/a"}`,
  ].join("; ");
  return [
    ruleLabels[row.rule] ?? row.rule,
    dateValue(row.signal_date),
    dateValue(row.entry_date),
    dateValue(row.exit_date),
    row.symbol,
    row.security_name,
    rawValue,
    numeric(row.adv20),
    numeric(row.gross_return),
    numeric(row.net_return),
    numeric(row.benchmark_return),
    numeric(row.excess_return),
    audit,
  ];
}

// Render both final layouts before loading the full 69,458-row detail table.
// The full-detail write uses the same columns, formats, widths, and header.
const sampleStream = readline.createInterface({
  input: createReadStream(detailPath, { encoding: "utf8" }),
  crlfDelay: Infinity,
});
let sampleHeaders = null;
const sampleRows = [];
for await (const line of sampleStream) {
  if (!sampleHeaders) {
    sampleHeaders = parseCsvLine(line);
    continue;
  }
  if (!line) {
    continue;
  }
  const values = parseCsvLine(line);
  const row = Object.fromEntries(
    sampleHeaders.map((header, index) => [header, values[index] ?? ""]),
  );
  sampleRows.push(detailValues(row));
  if (sampleRows.length >= 19) {
    break;
  }
}
detailSheet.getRange("A2:M20").values = sampleRows;
detailSheet.getRange("B2:D20").format.numberFormat = "yyyy-mm-dd";
detailSheet.getRange("G2:G20").format.numberFormat = "0.0000";
detailSheet.getRange("H2:H20").format.numberFormat = '"$"#,##0';
detailSheet.getRange("I2:L20").format.numberFormat = "0.00%";

await fs.mkdir(previewDir, { recursive: true });
const summaryPreview = await workbook.render({
  sheetName: summarySheetName,
  range: "A1:W76",
  scale: 1,
  format: "png",
});
await fs.writeFile(
  `${previewDir}/summary.png`,
  new Uint8Array(await summaryPreview.arrayBuffer()),
);
const detailPreview = await workbook.render({
  sheetName: detailSheetName,
  range: "A1:M20",
  scale: 1,
  format: "png",
});
await fs.writeFile(
  `${previewDir}/detail.png`,
  new Uint8Array(await detailPreview.arrayBuffer()),
);

const stream = readline.createInterface({
  input: createReadStream(detailPath, { encoding: "utf8" }),
  crlfDelay: Infinity,
});
let sourceHeaders = null;
let rowNumber = 2;
let chunkStartRow = rowNumber;
let chunk = [];
const chunkSize = 1500;

async function writeDetailChunk() {
  if (chunk.length === 0) {
    return;
  }
  const endRow = chunkStartRow + chunk.length - 1;
  detailSheet.getRange(`A${chunkStartRow}:M${endRow}`).values = chunk;
  rowNumber = endRow + 1;
  chunkStartRow = rowNumber;
  chunk = [];
}

for await (const line of stream) {
  if (!sourceHeaders) {
    sourceHeaders = parseCsvLine(line);
    continue;
  }
  if (!line) {
    continue;
  }
  const values = parseCsvLine(line);
  const row = Object.fromEntries(
    sourceHeaders.map((header, index) => [header, values[index] ?? ""]),
  );
  chunk.push(detailValues(row));
  if (chunk.length >= chunkSize) {
    await writeDetailChunk();
  }
}
await writeDetailChunk();
const actualDetailRows = rowNumber - 2;
if (actualDetailRows !== summary.detail_rows) {
  throw new Error(
    `Detail row reconciliation failed: expected ${summary.detail_rows}, wrote ${actualDetailRows}.`,
  );
}

const detailLastRow = actualDetailRows + 1;
detailSheet.getRange(`B2:D${detailLastRow}`).format.numberFormat = "yyyy-mm-dd";
detailSheet.getRange(`G2:G${detailLastRow}`).format.numberFormat = "0.0000";
detailSheet.getRange(`H2:H${detailLastRow}`).format.numberFormat = '"$"#,##0';
detailSheet.getRange(`I2:L${detailLastRow}`).format.numberFormat = "0.00%";
detailSheet.getRange(`L2:L${detailLastRow}`).conditionalFormats.add("cellIs", {
  operator: "lessThan",
  formula: 0,
  format: { fill: "#FDE9E7", font: { color: "#9C0006" } },
});
detailSheet.getRange(`L2:L${detailLastRow}`).conditionalFormats.add("cellIs", {
  operator: "greaterThan",
  formula: 0,
  format: { fill: "#E2F0D9", font: { color: "#006100" } },
});
const detailTable = detailSheet.tables.add(
  `A1:M${detailLastRow}`,
  true,
  "RulePortfolioEventsTable",
);
detailTable.style = "TableStyleMedium2";
detailTable.showBandedColumns = false;
detailTable.showFilterButton = true;

workbook.recalculate();
const summaryInspection = await workbook.inspect({
  kind: "table",
  range: `${summarySheetName}!A1:R27`,
  include: "values,formulas",
  tableMaxRows: 27,
  tableMaxCols: 18,
  maxChars: 18000,
});
console.log(summaryInspection.ndjson);
const detailTopInspection = await workbook.inspect({
  kind: "table",
  range: `${detailSheetName}!A1:M5`,
  include: "values,formulas",
  tableMaxRows: 5,
  tableMaxCols: 13,
  maxChars: 15000,
});
console.log(detailTopInspection.ndjson);
const detailBottomInspection = await workbook.inspect({
  kind: "table",
  range: `${detailSheetName}!A${detailLastRow - 2}:M${detailLastRow}`,
  include: "values,formulas",
  tableMaxRows: 3,
  tableMaxCols: 13,
  maxChars: 10000,
});
console.log(detailBottomInspection.ndjson);
const errorInspection = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
console.log(errorInspection.ndjson);

await fs.mkdir(outputPath.slice(0, outputPath.lastIndexOf("/")), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
const savedWorkbook = await SpreadsheetFile.importXlsx(await FileBlob.load(outputPath));
const savedInspection = await savedWorkbook.inspect({
  kind: "sheet,table,drawing",
  include: "id,name,range",
  maxChars: 10000,
});
console.log(savedInspection.ndjson);
console.log(
  JSON.stringify({
    outputPath,
    detailRows: actualDetailRows,
    expectedDetailRows: summary.detail_rows,
    sheets: [summarySheetName, detailSheetName],
  }),
);
