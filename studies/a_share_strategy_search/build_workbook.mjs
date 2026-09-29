// Build the two-sheet Excel deliverable from validated study outputs.

import fs from "node:fs/promises";
import { createReadStream } from "node:fs";
import readline from "node:readline";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const [summaryPath, detailPath, qualityPath, outputPath, previewDir] = process.argv.slice(2);
if (!summaryPath || !detailPath || !qualityPath || !outputPath || !previewDir) {
  throw new Error(
    "Usage: node build_workbook.mjs <summary.csv> <detail.csv> <quality.json> <output.xlsx> <preview-dir>",
  );
}

const fontFamily = "Arial";
const summarySheetName = "\u7ed3\u679c\u6458\u8981";
const detailSheetName = "\u7ed3\u679c\u660e\u7ec6";
const familyNames = {
  value_quality_low_vol_dividend: "\u4ef7\u503c+\u9690\u542b\u8d28\u91cf+\u4f4e\u6ce2+\u80a1\u606f",
  china_ch4_inspired: "CH-4\u542f\u53d1\uff08\u89c4\u6a21+\u76c8\u5229\u6536\u76ca\u7387+\u4f4e\u6362\u624b\uff09",
  residual_reversal_abnormal_turnover: "\u5e02\u573a\u8c03\u6574\u53cd\u8f6c+\u5f02\u5e38\u6362\u624b",
  dividend_low_volatility: "\u7ea2\u5229\u4f4e\u6ce2",
  earnings_express_drift: "\u4e1a\u7ee9\u5feb\u62a5\u6f02\u79fb",
  sw_industry_index_rotation: "\u7533\u4e07\u4e00\u7ea7\u884c\u4e1a\u6307\u6570\u8f6e\u52a8",
};
const statusNames = {
  validation_passed_blind_failed: "\u9a8c\u8bc1\u901a\u8fc7\uff0c\u4f46\u76f2\u6d4b\u8dd1\u8f93\u57fa\u51c6",
  no_stable_validation_winner: "\u9a8c\u8bc1\u671f\u65e0\u7a33\u5b9a\u8d62\u5bb6",
  stable_blind_positive: "\u76f2\u6d4b\u7a33\u5065\u4e3a\u6b63",
  blind_positive_but_uncertain: "\u76f2\u6d4b\u4e3a\u6b63\uff0c\u4f46\u4e0d\u786e\u5b9a",
};
const splitNames = {
  train: "\u8bad\u7ec3",
  validation: "\u9a8c\u8bc1",
  blind: "\u76f2\u6d4b",
};
const ruleNames = {
  "F1-171": "\u76c8\u5229\u6536\u76ca\u7387\u5206\u4f4d\u226560%\uff1b60\u65e5\u6ce2\u52a8\u7387\u5206\u4f4d\u226460%\uff1bTTM\u80a1\u606f\u7387\u5206\u4f4d\u226580%",
  "F2-057": "\u603b\u5e02\u503c\u5206\u4f4d30%\u201365%\uff1b\u76c8\u5229\u6536\u76ca\u7387\u5206\u4f4d\u226560%\uff1b60\u65e5\u6362\u624b\u7387\u5206\u4f4d\u226430%",
  "F3-046": "10\u65e5\u5e02\u573a\u8c03\u6574\u6536\u76ca\u5206\u4f4d\u226415%\uff1b\u5f02\u5e38\u6362\u624b\u5206\u4f4d\u226580%",
  "F4-081": "TTM\u80a1\u606f\u7387\u5206\u4f4d\u226580%\uff1b60\u65e5\u6ce2\u52a8\u7387\u5206\u4f4d\u226440%\uff1b\u8fc7\u53bb12\u4e2a\u6708\u6bcf\u6708TTM\u80a1\u606f\u7387\u5747\u4e3a\u6b63",
  "F5-006": "\u6bcf\u62a5\u544a\u671f\u9996\u4efd\u4e1a\u7ee9\u5feb\u62a5\uff1b\u51c0\u5229\u6da6\u540c\u6bd4\u22650%\uff1b\u644a\u8584ROE\u22650%\uff1b\u644a\u8584EPS>0",
  "F6-035": "120\u65e5\u52a8\u91cf\u4e3a\u6b63\uff1b\u9009\u62e9\u52a8\u91cf\u524d5\u540d\u7533\u4e07\u4e00\u7ea7\u884c\u4e1a\u6307\u6570",
};


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


async function readSmallCsv(path) {
  const text = await fs.readFile(path, "utf8");
  const lines = text.trim().split(/\r?\n/);
  const headers = parseCsvLine(lines[0]);
  return lines.slice(1).map((line) => {
    const values = parseCsvLine(line);
    return Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]));
  });
}


function numeric(value) {
  if (value === "" || value === undefined || value === null) {
    return null;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    throw new Error(`Expected numeric value, received: ${value}`);
  }
  return parsed;
}


function dateValue(value) {
  if (!value) {
    return null;
  }
  return new Date(`${value.slice(0, 10)}T00:00:00Z`);
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


function styleSectionHeader(range) {
  range.format.fill = "#1F4E78";
  range.format.font = { name: fontFamily, size: 10, bold: true, color: "#FFFFFF" };
  range.format.verticalAlignment = "center";
  range.format.horizontalAlignment = "center";
  range.format.wrapText = true;
}


const summaryRows = await readSmallCsv(summaryPath);
const quality = JSON.parse(await fs.readFile(qualityPath, "utf8"));
const expectedDetailRows = summaryRows.reduce(
  (total, row) =>
    total + numeric(row.train_events) + numeric(row.validation_events) + numeric(row.blind_events),
  0,
);

const workbook = Workbook.create();
const summarySheet = workbook.worksheets.add(summarySheetName);
const detailSheet = workbook.worksheets.add(detailSheetName);
summarySheet.showGridLines = false;
detailSheet.showGridLines = false;
summarySheet.tabColor = "#1F4E78";
detailSheet.tabColor = "#5B9BD5";

summarySheet.getRange("A2").values = [["A\u80a1\u5e38\u89c1\u91cf\u5316\u89c4\u5219\u5386\u53f2\u9a8c\u8bc1"]];
summarySheet.getRange("A2").format.font = {
  name: fontFamily,
  size: 16,
  bold: true,
  color: "#1F2937",
};
summarySheet.getRange("A3").values = [[
  "\u8bad\u7ec3 2016\u20132021\uff0c\u9a8c\u8bc1 2022\u20132023\uff0c\u76f2\u6d4b 2024\u2013\u6700\u65b0\uff1b\u4fe1\u53f7\u65e5\u6536\u76d8\u540e\u5224\u5b9a\uff0c\u4e0b\u4e00\u53ef\u4ea4\u6613\u65e5\u5f00\u76d8\u4e70\u5165",
]];
summarySheet.getRange("A3").format.font = {
  name: fontFamily,
  size: 10,
  italic: true,
  color: "#5B6573",
};
summarySheet.getRange("A3:N3").format.borders = {
  bottom: { style: "thin", color: "#AAB7C4" },
};
summarySheet.getRange("A5:N5").format.fill = "#FDE9E7";
summarySheet.getRange("A5:N5").format.borders = {
  preset: "outside",
  style: "thin",
  color: "#C00000",
};
summarySheet.getRange("A5").values = [[
  "\u7ed3\u8bba\uff1a6\u4e2a\u7b56\u7565\u65cf\u5747\u672a\u57282024\u20132026\u76f2\u6d4b\u4e2d\u53d6\u5f97\u6b63\u4e14\u7edf\u8ba1\u7a33\u5065\u7684\u51c0\u8d85\u989d\u6536\u76ca\u3002",
]];
summarySheet.getRange("A5").format.font = {
  name: fontFamily,
  size: 11,
  bold: true,
  color: "#9C0006",
};

const summaryHeaders = [
  "\u7b56\u7565\u65cf",
  "\u6700\u4f73\u5019\u9009",
  "\u6700\u4f73\u89c4\u5219",
  "\u6301\u6709\u53ef\u4ea4\u6613\u65e5",
  "\u7ed3\u679c\u72b6\u6001",
  "\u76f2\u6d4b\u4e8b\u4ef6\u6570",
  "\u76f2\u6d4b\u5e73\u5747\u6bdb\u6536\u76ca",
  "\u76f2\u6d4b\u5e73\u5747\u51c0\u6536\u76ca\uff0820bp\uff09",
  "\u540c\u671f\u7b49\u6743\u57fa\u51c6",
  "\u76f2\u6d4b\u5e73\u5747\u51c0\u8d85\u989d",
  "\u8d85\u989d95%\u4e0b\u754c",
  "\u51c0\u6536\u76ca\u4e2d\u4f4d\u6570",
  "\u51c0\u80dc\u7387",
  "\u968f\u673a\u57fa\u7ebfp\u503c",
];
summarySheet.getRange("A7:N7").values = [summaryHeaders];
styleSectionHeader(summarySheet.getRange("A7:N7"));
const summaryValues = summaryRows.map((row) => [
  familyNames[row.family] ?? row.family,
  row.candidate_id,
  ruleNames[row.candidate_id] ?? row.rule,
  numeric(row.holding_sessions),
  statusNames[row.status] ?? row.status,
  numeric(row.blind_events),
  numeric(row.blind_avg_gross_return),
  numeric(row.blind_avg_net_return),
  numeric(row.blind_avg_benchmark_return),
  numeric(row.blind_avg_net_excess_return),
  numeric(row.blind_net_excess_lcb_95),
  numeric(row.blind_median_net_return),
  numeric(row.blind_win_rate_net),
  numeric(row.random_p_value),
]);
summarySheet.getRange("A8:N13").values = summaryValues;
summarySheet.getRange("A8:N13").format.font = { name: fontFamily, size: 10, color: "#1F2937" };
summarySheet.getRange("A8:N13").format.verticalAlignment = "center";
summarySheet.getRange("C8:C13").format.wrapText = true;
summarySheet.getRange("E8:E13").format.wrapText = true;
summarySheet.getRange("D8:D13").format.numberFormat = "0";
summarySheet.getRange("F8:F13").format.numberFormat = "#,##0";
summarySheet.getRange("G8:M13").format.numberFormat = "0.00%";
summarySheet.getRange("N8:N13").format.numberFormat = "0.000";
summarySheet.getRange("J8:K13").conditionalFormats.add("cellIs", {
  operator: "lessThan",
  formula: 0,
  format: { fill: "#FDE9E7", font: { color: "#9C0006", bold: true } },
});
summarySheet.getRange("J8:K13").conditionalFormats.add("cellIs", {
  operator: "greaterThan",
  formula: 0,
  format: { fill: "#E2F0D9", font: { color: "#006100", bold: true } },
});
summarySheet.getRange("E8:E13").conditionalFormats.add("containsText", {
  text: "\u8dd1\u8f93",
  format: { fill: "#FFF2CC", font: { color: "#9C6500", bold: true } },
});
summarySheet.getRange("E8:E13").conditionalFormats.add("containsText", {
  text: "\u65e0\u7a33\u5b9a",
  format: { fill: "#FDE9E7", font: { color: "#9C0006", bold: true } },
});

summarySheet.getRange("A16:J16").values = [[
  "\u7b56\u7565\u65cf",
  "\u6d4b\u8bd5\u5019\u9009\u6570",
  "\u53ef\u884c\u5019\u9009\u6570",
  "\u8bad\u7ec3\u4e8b\u4ef6\u6570",
  "\u8bad\u7ec3\u5e73\u5747\u51c0\u8d85\u989d",
  "\u8bad\u7ec3\u8d85\u989d95%\u4e0b\u754c",
  "\u9a8c\u8bc1\u4e8b\u4ef6\u6570",
  "\u9a8c\u8bc1\u5e73\u5747\u51c0\u8d85\u989d",
  "\u9a8c\u8bc1\u8d85\u989d95%\u4e0b\u754c",
  "\u76f2\u6d4b\u6b63\u6536\u76ca\u5e74\u5360\u6bd4",
]];
styleSectionHeader(summarySheet.getRange("A16:J16"));
summarySheet.getRange("A17:J22").values = summaryRows.map((row) => [
  familyNames[row.family] ?? row.family,
  numeric(row.tested_candidates),
  numeric(row.viable_candidates),
  numeric(row.train_events),
  numeric(row.train_avg_net_excess_return),
  numeric(row.train_net_excess_lcb_95),
  numeric(row.validation_events),
  numeric(row.validation_avg_net_excess_return),
  numeric(row.validation_net_excess_lcb_95),
  numeric(row.blind_positive_year_ratio),
]);
summarySheet.getRange("A17:J22").format.font = { name: fontFamily, size: 10, color: "#1F2937" };
summarySheet.getRange("B17:D22").format.numberFormat = "#,##0";
summarySheet.getRange("E17:F22").format.numberFormat = "0.00%";
summarySheet.getRange("G17:G22").format.numberFormat = "#,##0";
summarySheet.getRange("H17:J22").format.numberFormat = "0.00%";

summarySheet.getRange("A25:E25").values = [[
  "\u7b56\u7565\u65cf",
  "\u76f2\u6d4b\u5e73\u5747\u6bdb\u6536\u76ca",
  "\u51c0\u6536\u76ca\uff0810bp\uff09",
  "\u51c0\u6536\u76ca\uff0820bp\uff09",
  "\u51c0\u6536\u76ca\uff0850bp\uff09",
]];
styleSectionHeader(summarySheet.getRange("A25:E25"));
summarySheet.getRange("A26:E31").values = summaryRows.map((row) => {
  const grossReturn = numeric(row.blind_avg_gross_return);
  return [
    familyNames[row.family] ?? row.family,
    grossReturn,
    grossReturn - 0.001,
    grossReturn - 0.002,
    grossReturn - 0.005,
  ];
});
summarySheet.getRange("A26:E31").format.font = { name: fontFamily, size: 10, color: "#1F2937" };
summarySheet.getRange("B26:E31").format.numberFormat = "0.00%";

summarySheet.getRange("A34:D34").values = [[
  "\u6570\u636e\u8d28\u91cf\u9879",
  "\u7ed3\u679c",
  "\u72b6\u6001",
  "\u5206\u6790\u5f71\u54cd",
]];
styleSectionHeader(summarySheet.getRange("A34:D34"));
const stockQuality = quality.stock_panel;
const eventQuality = quality.earnings_express;
const industryQuality = quality.industry_indexes;
const qualityRows = [
  ["\u80a1\u7968\u65e5\u9891\u9762\u677f\u884c\u6570", stockQuality.panel_rows, "\u901a\u8fc7", "\u5168\u90e8\u65e5\u884c\u60c5\u4e0e\u4fee\u590d\u540e\u590d\u6743\u6570\u636e\u53ef\u7528"],
  ["\u8bc1\u5238\u4ee3\u7801\u6570", stockQuality.panel_codes, "\u901a\u8fc7", "\u5305\u542b\u5df2\u9000\u5e02\u8bc1\u5238\uff0c\u907f\u514d\u4ec5\u4f7f\u7528\u5f53\u524d\u6210\u5206"],
  ["\u590d\u6743\u56e0\u5b50\u8986\u76d6\u7387", stockQuality.adj_factor_coverage, "\u901a\u8fc7", "426\u4e2a\u7a7a\u54cd\u5e94\u65e5\u671f\u5df2\u7528\u7814\u7a76\u8986\u76d6\u5c42\u8865\u9f50"],
  ["\u6bcf\u65e5\u6307\u6807\u8986\u76d6\u7387", stockQuality.daily_basic_coverage, "\u53ef\u7528", "\u7f3a\u5931\u884c\u4e0d\u8fdb\u5165\u4f9d\u8d56\u4f30\u503c\u5b57\u6bb5\u7684\u7b56\u7565"],
  ["\u65e5\u9891\u4e3b\u952e\u91cd\u590d\u6570", stockQuality.duplicate_daily_keys, "\u901a\u8fc7", "\u65e0\u91cd\u590d\u80a1\u7968-\u4ea4\u6613\u65e5"],
  ["\u9996\u6b21\u4e1a\u7ee9\u5feb\u62a5\u6761\u6570", eventQuality.express_unique_first_disclosures, "\u901a\u8fc7", "\u6bcf\u80a1\u6bcf\u62a5\u544a\u671f\u53ea\u4fdd\u7559\u9996\u6b21\u516c\u544a"],
  ["\u7533\u4e07\u4e00\u7ea7\u884c\u4e1a\u6570", industryQuality.industry_codes, "\u901a\u8fc7", "\u4e0eSW2021\u4e00\u7ea7\u5206\u7c7b31\u4e2a\u6307\u6570\u4e00\u81f4"],
  ["\u660e\u7ec6\u4e0e\u6458\u8981\u4e8b\u4ef6\u6570\u5bf9\u8d26", expectedDetailRows, "\u5f85\u751f\u6210\u6821\u9a8c", "\u4e24\u5f20\u8868\u7684\u4e8b\u4ef6\u603b\u6570\u5fc5\u987b\u4e00\u81f4"],
];
summarySheet.getRange("A35:D42").values = qualityRows;
summarySheet.getRange("A35:D42").format.font = { name: fontFamily, size: 10, color: "#1F2937" };
summarySheet.getRange("B35:B36").format.numberFormat = "#,##0";
summarySheet.getRange("B37:B38").format.numberFormat = "0.00%";
summarySheet.getRange("B39:B42").format.numberFormat = "#,##0";
for (let row = 35; row <= 42; row += 1) {
  summarySheet.mergeCells(`D${row}:N${row}`);
}

summarySheet.getRange("A45:N45").values = [[
  "\u65b9\u6cd5\u3001\u53e3\u5f84\u4e0e\u9650\u5236",
  null,
  null,
  null,
  null,
  null,
  null,
  null,
  null,
  null,
  null,
  null,
  null,
  null,
]];
summarySheet.getRange("A45:N45").format.fill = "#D9EAF7";
summarySheet.getRange("A45").format.font = {
  name: fontFamily,
  size: 11,
  bold: true,
  color: "#1F4E78",
};
const methodNotes = [
  "1. \u5165\u9009\u8303\u56f4\uff1a\u4e0a\u5e02\u6ee1365\u5929\u3001\u8fc7\u53bb20\u4e2a\u53ef\u4ea4\u6613\u65e5\u65e5\u5747\u6210\u4ea4\u989d\u4e0d\u4f4e\u4e8e2,000\u4e07\u5143\u3001\u4fe1\u53f7\u65e5\u975eST\u3002",
  "2. \u6267\u884c\uff1aT\u65e5\u6536\u76d8\u540e\u5f62\u6210\u4fe1\u53f7\uff0c\u4e0b\u4e00\u53ef\u4ea4\u6613\u65e5\u5f00\u76d8\u4e70\u5165\uff0c\u6301\u6709N\u4e2a\u4e2a\u80a1\u53ef\u4ea4\u6613\u65e5\u540e\u6536\u76d8\u5356\u51fa\u3002\u6da8\u505c\u4e00\u5b57\u677f\u65e0\u6cd5\u4e70\u5165\uff1b\u8dcc\u505c\u4e00\u5b57\u677f\u5356\u51fa\u6700\u591a\u5ef6\u8fdf5\u4e2a\u53ef\u4ea4\u6613\u65e5\u3002",
  "3. \u6210\u672c\uff1a\u4e3b\u7ed3\u8bba\u6309\u53cc\u8fb920bp\u603b\u6210\u672c\u5c55\u793a\uff1b\u6458\u8981\u7684\u6210\u672c\u654f\u611f\u6027\u8868\u540c\u65f6\u63d0\u4f9b10bp\u300120bp\u548c50bp\u60c5\u666f\u3002",
  "4. \u9009\u578b\uff1a2016\u20132021\u5148\u6309\u51c0\u8d85\u989d95%\u4e0b\u754c\u5efa\u7acb\u524d20%\u5019\u9009\u6e05\u5355\uff0c2022\u20132023\u9009\u51fa\u9a8c\u8bc1\u4e0b\u754c\u6700\u9ad8\u7684\u89c4\u5219\uff1b2024\u5e74\u4ee5\u540e\u4ec5\u7528\u4e8e\u6700\u7ec8\u76f2\u6d4b\u3002",
  "5. \u57fa\u51c6\uff1a\u80a1\u7968\u7b56\u7565\u4e3a\u540c\u4fe1\u53f7\u65e5\u3001\u540c\u6301\u6709\u671f\u7684\u53ef\u6295A\u80a1\u7b49\u6743\u6536\u76ca\uff1b\u884c\u4e1a\u8f6e\u52a8\u4e3a31\u4e2a\u7533\u4e07\u4e00\u7ea7\u6307\u6570\u7b49\u6743\u6536\u76ca\u3002",
  "6. \u9650\u5236\uff1a\u9690\u542bROE=PB/PE\u4ec5\u4e3a\u65f6\u70b9\u53ef\u7528\u7684\u8d28\u91cf\u4ee3\u7406\uff0c\u4e0d\u662f\u62ab\u9732ROE\uff1bCH-4\u4e3a\u591a\u5934\u7b5b\u9009\u542f\u53d1\u7248\uff0c\u4e0d\u662f\u5b66\u672f\u56e0\u5b50\u7ec4\u5408\u7684\u5b8c\u6574\u590d\u73b0\u3002",
  "7. \u4e1a\u7ee9\u5feb\u62a5\u6f02\u79fb\u4e0d\u5305\u542b\u5206\u6790\u5e08\u9884\u671f\u5dee\uff1b\u7533\u4e07\u884c\u4e1a\u6307\u6570\u4e0d\u53ef\u76f4\u63a5\u4ea4\u6613\uff0c\u5b9e\u76d8\u9700\u53e6\u884c\u6620\u5c04ETF\u5e76\u91cd\u505a\u8ddf\u8e2a\u8bef\u5dee\u9a8c\u8bc1\u3002",
  "8. \u6570\u636e\u6e90\uff1aTushare Pro\u65e5\u884c\u60c5\u3001\u590d\u6743\u56e0\u5b50\u3001\u6bcf\u65e5\u6307\u6807\u3001\u98ce\u9669\u8b66\u793a\u548c\u4e1a\u7ee9\u5feb\u62a5\uff1b\u7533\u4e07SW2021\u4e00\u7ea7\u884c\u4e1a\u6307\u6570\u3002\u5b66\u672f\u53c2\u8003\uff1aLiu, Stambaugh and Yuan, Size and Value in China.",
];
methodNotes.forEach((note, index) => {
  const row = 46 + index;
  summarySheet.mergeCells(`A${row}:N${row}`);
  summarySheet.getRange(`A${row}`).values = [[note]];
});
summarySheet.getRange("A46:N53").format.font = { name: fontFamily, size: 10, color: "#334155" };
summarySheet.getRange("A46:A53").format.wrapText = true;
summarySheet.getRange("A55").values = [["Tushare: https://tushare.pro/document/2"]];
summarySheet.getRange("A56").values = [[
  "NBER: https://www.nber.org/system/files/working_papers/w24458/w24458.pdf",
]];
summarySheet.getRange("A55:A56").format.font = {
  name: fontFamily,
  size: 9,
  italic: true,
  color: "#5B6573",
};

summarySheet.getRange("A1:N56").format.verticalAlignment = "center";
summarySheet.getRange("A1:N56").format.font.name = fontFamily;
summarySheet.getRange("A7:N13").format.borders = {
  insideHorizontal: { style: "thin", color: "#D9E2F3" },
  bottom: { style: "thin", color: "#AAB7C4" },
};
summarySheet.getRange("A16:J22").format.borders = {
  insideHorizontal: { style: "thin", color: "#D9E2F3" },
  bottom: { style: "thin", color: "#AAB7C4" },
};
summarySheet.getRange("A25:E31").format.borders = {
  insideHorizontal: { style: "thin", color: "#D9E2F3" },
  bottom: { style: "thin", color: "#AAB7C4" },
};
summarySheet.getRange("A34:D42").format.borders = {
  insideHorizontal: { style: "thin", color: "#D9E2F3" },
  bottom: { style: "thin", color: "#AAB7C4" },
};
summarySheet.getRange("A2:A2").format.rowHeight = 24;
summarySheet.getRange("A5:N5").format.rowHeight = 30;
summarySheet.getRange("A7:N7").format.rowHeight = 34;
summarySheet.getRange("A8:N13").format.rowHeight = 46;
summarySheet.getRange("A16:J16").format.rowHeight = 34;
summarySheet.getRange("A25:E25").format.rowHeight = 28;
summarySheet.getRange("A34:D34").format.rowHeight = 28;
summarySheet.getRange("A46:A53").format.rowHeight = 22;
const summaryWidths = [
  30,
  13,
  58,
  13,
  26,
  13,
  15,
  17,
  15,
  15,
  15,
  15,
  12,
  13,
];
summaryWidths.forEach((width, index) => {
  const column = excelColumnName(index + 1);
  summarySheet.getRange(`${column}:${column}`).format.columnWidth = width;
});

const detailHeaders = [
  "\u5019\u9009ID",
  "\u6570\u636e\u5206\u6bb5",
  "\u4fe1\u53f7\u65e5",
  "\u4e70\u5165\u65e5",
  "\u5356\u51fa\u65e5",
  "\u8bc1\u5238\u4ee3\u7801",
  "\u8bc1\u5238\u540d\u79f0",
  "\u6bdb\u6536\u76ca",
  "\u51c0\u6536\u76ca20bp",
  "\u540c\u671f\u7b49\u6743\u57fa\u51c6",
  "\u51c0\u8d85\u989d20bp",
];
detailSheet.getRange("A1:K1").values = [detailHeaders];
styleSectionHeader(detailSheet.getRange("A1:K1"));

const stream = readline.createInterface({
  input: createReadStream(detailPath, { encoding: "utf8" }),
  crlfDelay: Infinity,
});
let sourceHeaders = null;
let rowNumber = 2;
let chunkStartRow = rowNumber;
let chunk = [];
const chunkSize = 3000;

async function writeDetailChunk() {
  if (chunk.length === 0) {
    return;
  }
  const endRow = chunkStartRow + chunk.length - 1;
  detailSheet.getRange(`A${chunkStartRow}:K${endRow}`).values = chunk;
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
  const displayName = row.name || row.instrument_name || "";
  chunk.push([
    row.candidate_id,
    splitNames[row.split] ?? row.split,
    dateValue(row.signal_date),
    dateValue(row.entry_date),
    dateValue(row.exit_date),
    row.ts_code,
    displayName,
    numeric(row.gross_return),
    numeric(row.net_return_20bps),
    numeric(row.benchmark_return),
    numeric(row.net_excess_return_20bps),
  ]);
  if (chunk.length >= chunkSize) {
    await writeDetailChunk();
  }
}
await writeDetailChunk();
const actualDetailRows = rowNumber - 2;
if (actualDetailRows !== expectedDetailRows) {
  throw new Error(
    `Detail row reconciliation failed: expected ${expectedDetailRows}, wrote ${actualDetailRows}.`,
  );
}
summarySheet.getRange("C42").values = [["\u901a\u8fc7"]];

const detailLastRow = actualDetailRows + 1;
detailSheet.getRange(`C2:E${detailLastRow}`).format.numberFormat = "yyyy-mm-dd";
detailSheet.getRange(`H2:K${detailLastRow}`).format.numberFormat = "0.00%";
detailSheet.getRange(`K2:K${detailLastRow}`).conditionalFormats.add("cellIs", {
  operator: "lessThan",
  formula: 0,
  format: { fill: "#FDE9E7", font: { color: "#9C0006" } },
});
detailSheet.getRange(`K2:K${detailLastRow}`).conditionalFormats.add("cellIs", {
  operator: "greaterThan",
  formula: 0,
  format: { fill: "#E2F0D9", font: { color: "#006100" } },
});
detailSheet.getRange("A1:K1").format.rowHeight = 34;
const detailWidths = [
  12,
  10,
  12,
  12,
  12,
  14,
  14,
  12,
  13,
  15,
  14,
];
detailWidths.forEach((width, index) => {
  const column = excelColumnName(index + 1);
  detailSheet.getRange(`${column}:${column}`).format.columnWidth = width;
});
detailSheet.freezePanes.freezeRows(1);
detailSheet.freezePanes.freezeColumns(2);
const detailTable = detailSheet.tables.add(
  `A1:K${detailLastRow}`,
  true,
  "StrategyEventsTable",
);
detailTable.style = "TableStyleMedium2";
detailTable.showBandedColumns = false;
detailTable.showFilterButton = true;

workbook.recalculate();
const summaryInspection = await workbook.inspect({
  kind: "table",
  range: `${summarySheetName}!A1:N22`,
  include: "values,formulas",
  tableMaxRows: 22,
  tableMaxCols: 14,
  maxChars: 14000,
});
console.log(summaryInspection.ndjson);
const detailTopInspection = await workbook.inspect({
  kind: "table",
  range: `${detailSheetName}!A1:K6`,
  include: "values,formulas",
  tableMaxRows: 6,
  tableMaxCols: 11,
  maxChars: 10000,
});
console.log(detailTopInspection.ndjson);
const detailBottomInspection = await workbook.inspect({
  kind: "table",
  range: `${detailSheetName}!A${detailLastRow - 2}:K${detailLastRow}`,
  include: "values,formulas",
  tableMaxRows: 3,
  tableMaxCols: 11,
  maxChars: 7000,
});
console.log(detailBottomInspection.ndjson);
const errorInspection = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
console.log(errorInspection.ndjson);

await fs.mkdir(previewDir, { recursive: true });
const summaryPreview = await workbook.render({
  sheetName: summarySheetName,
  range: "A1:N56",
  scale: 1,
  format: "png",
});
await fs.writeFile(
  `${previewDir}/summary.png`,
  new Uint8Array(await summaryPreview.arrayBuffer()),
);
const detailPreview = await workbook.render({
  sheetName: detailSheetName,
  range: "A1:K24",
  scale: 1,
  format: "png",
});
await fs.writeFile(
  `${previewDir}/detail.png`,
  new Uint8Array(await detailPreview.arrayBuffer()),
);

await fs.mkdir(outputPath.slice(0, outputPath.lastIndexOf("/")), { recursive: true });
try {
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(outputPath);
} catch (error) {
  console.error(
    JSON.stringify({
      stage: "xlsx_export",
      name: error?.name ?? "Error",
      message: error?.message ?? String(error),
    }),
  );
  process.exit(1);
}
console.log(
  JSON.stringify({
    outputPath,
    detailRows: actualDetailRows,
    expectedDetailRows,
    sheets: [summarySheetName, detailSheetName],
  }),
);
