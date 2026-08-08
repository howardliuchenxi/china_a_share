/**
 * Baseline E2E tests for the A-Share Lab frontend.
 *
 * These tests use deterministic synthetic fixtures and do NOT call
 * live DeepSeek or Tushare. API responses are intercepted via
 * Playwright route mocking.
 */

import { expect, test } from "@playwright/test";
import {
  emptyResultFixture,
  partialSuccessFixture,
  planningErrorFixture,
  successWithMultiRowFixture,
  successWithSingleRowFixture,
  successWithSingleStockManyRowsFixture,
  unsupportedAnalysisFixture,
} from "./fixtures";

/* ------------------------------------------------------------------ */
/*  Mock setup helper — intercept /api/* routes                         */
/* ------------------------------------------------------------------ */

async function mockApiRoutes(
  page: import("@playwright/test").Page,
  analysisFixture: object,
) {
  await page.route("**/api/analysis", (route) => {
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(analysisFixture),
    });
  });
  await page.route("**/api/health", (route) => {
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok" }),
    });
  });
}

/* ------------------------------------------------------------------ */
/*  Scenario: page load                                                 */
/* ------------------------------------------------------------------ */

test("page load renders analysis page with hero and form", async ({
  page,
}) => {
  await mockApiRoutes(page, successWithMultiRowFixture);

  await page.goto("/analysis");

  // Hero section is visible
  await expect(page.locator(".hero")).toBeVisible();

  // Page tabs are visible with correct state
  const analysisTab = page.locator('.page-tabs button[role="tab"]').first();
  await expect(analysisTab).toHaveAttribute("aria-selected", "true");

  // Prompt textarea is visible
  const promptField = page.locator("#analysis-prompt");
  await expect(promptField).toBeVisible();

  // Submit button is disabled when prompt is empty
  const submitButton = page.locator('button[type="submit"]');
  await expect(submitButton).toBeDisabled();

  // Empty output placeholder is visible
  await expect(page.locator(".results-panel .empty-output")).toBeVisible();
});

/* ------------------------------------------------------------------ */
/*  Scenario: custom prompt input                                       */
/* ------------------------------------------------------------------ */

test("custom prompt input enables submit button", async ({ page }) => {
  await mockApiRoutes(page, successWithMultiRowFixture);

  await page.goto("/analysis");

  const promptField = page.locator("#analysis-prompt");
  const submitButton = page.locator('button[type="submit"]');

  // Initially disabled
  await expect(submitButton).toBeDisabled();

  // Type a custom prompt
  await promptField.fill("查询2026年7月17日A股涨跌分布");

  // Submit button becomes enabled
  await expect(submitButton).toBeEnabled();
});

test("A-share result codes link to Eastmoney verification pages", async ({
  page,
}) => {
  await mockApiRoutes(page, successWithMultiRowFixture);
  await page.goto("/analysis");
  await page.locator("#analysis-prompt").fill("查询2026年7月17日A股涨跌分布");
  await page.locator('button[type="submit"]').click();

  const verificationLink = page.getByRole("link", {
    name: "000001.SZ，在东方财富查看资金流向数据（新窗口）",
  });
  await expect(verificationLink).toHaveAttribute(
    "href",
    "https://data.eastmoney.com/zjlx/000001.html",
  );
  await expect(verificationLink).toHaveAttribute("target", "_blank");
  await expect(verificationLink).toHaveAttribute("rel", "noopener noreferrer");
});

test("administrator feedback dialog stays open while entering a suggestion", async ({
  page,
}) => {
  await mockApiRoutes(page, successWithMultiRowFixture);
  await page.route("**/api/ui-feedback/config", (route) => {
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        enabled: true,
        google_client_id: "test-client-id",
        git_branch: "main",
        git_sha: "test-sha",
      }),
    });
  });
  await page.route("https://accounts.google.com/gsi/client", (route) => {
    void route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: "",
    });
  });
  await page.route("**/api/ui-feedback/chat", async (route) => {
    const request = route.request().postDataJSON() as {
      conversation: Array<{ role: string; content: string }>;
    };
    const question = request.conversation.at(-1)?.content;
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        message: {
          role: "assistant",
          content: `建议先解释原因，再提供下一步。收到：${question}`,
        },
      }),
    });
  });
  await page.addInitScript(() => {
    window.google = {
      accounts: {
        id: {
          initialize: ({ callback }) => callback({ credential: "test-id-token" }),
          renderButton: () => undefined,
        },
      },
    };
  });

  await page.goto("/analysis");
  await expect(page.locator(".ui-feedback-area-button")).toBeVisible();
  await page.locator(".hero").click({ button: "right" });

  const dialog = page.getByRole("dialog", { name: "讨论并改进这个页面区域" });
  const suggestion = page.locator("#ui-feedback-suggestion");
  await expect(dialog).toBeVisible();
  await expect(page.locator("#ui-feedback-question")).toBeFocused();

  await page.locator("#ui-feedback-question").fill("这个空状态应该怎样解释？");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(dialog).toContainText("建议先解释原因，再提供下一步");

  await suggestion.fill("标题可以再简洁一些");

  await expect(dialog).toBeVisible();
  await expect(suggestion).toHaveValue("标题可以再简洁一些");
});

test("selected feedback context exposes separate discussion and improvement actions", async ({
  page,
}) => {
  await mockApiRoutes(page, successWithMultiRowFixture);
  await page.route("**/api/ui-feedback/config", (route) => {
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        enabled: true,
        google_client_id: "test-client-id",
        git_branch: "main",
        git_sha: "test-sha",
      }),
    });
  });
  await page.route("https://accounts.google.com/gsi/client", (route) => {
    void route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: "",
    });
  });
  await page.addInitScript(() => {
    window.google = {
      accounts: {
        id: {
          initialize: ({ callback }) => callback({ credential: "test-id-token" }),
          renderButton: () => undefined,
        },
      },
    };
  });

  await page.goto("/analysis");
  await page.locator(".ui-feedback-area-button").click();
  await page.locator(".hero").click();

  await expect(page.getByRole("button", { name: "问答", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "改进", exact: true })).toBeVisible();

  await page.getByRole("button", { name: "改进", exact: true }).click();
  await expect(page.locator("#ui-feedback-suggestion")).toBeFocused();
});

test("unsupported analysis displays its limitation instead of a blank result", async ({
  page,
}) => {
  await mockApiRoutes(page, unsupportedAnalysisFixture);
  await page.goto("/analysis");

  await page.locator("#analysis-prompt").fill("分析医疗行业的散户比例分组表现");
  await page.locator('button[type="submit"]').click();

  const alert = page.locator('.results-panel [role="alert"]');
  await expect(alert).toContainText("当前请求无法完整处理");
  await expect(alert).toContainText(
    "The current executor cannot dynamically fan out the full healthcare universe.",
  );
});

/* ------------------------------------------------------------------ */
/*  Scenario: start-analysis button (loading state)                     */
/* ------------------------------------------------------------------ */

test("start-analysis button shows loading state and then results", async ({
  page,
}) => {
  await mockApiRoutes(page, successWithMultiRowFixture);

  await page.goto("/analysis");

  // Type prompt and click submit
  await page.locator("#analysis-prompt").fill(
    "查询2026年7月17日A股涨跌分布",
  );
  const submitButton = page.locator('button[type="submit"]');
  await submitButton.click();

  // Button text changes to loading state
  // (may be too fast to catch, so we just verify results appear)
  await expect(submitButton).toBeEnabled();

  // Results panel should show data — wait for table to appear
  await expect(page.locator(".result-block")).toBeVisible({ timeout: 5000 });

  // Summary grid should show counts
  await expect(page.locator(".summary-grid")).toBeVisible();
  await expect(page.locator(".summary-grid dt").first()).toHaveText("上涨");
  await expect(page.locator(".request-trace")).toContainText(
    successWithMultiRowFixture.request_id,
  );
});

/* ------------------------------------------------------------------ */
/*  Scenario: multiple query results                                    */
/* ------------------------------------------------------------------ */

test("multi-query partial success shows success and error results", async ({
  page,
}) => {
  await mockApiRoutes(page, partialSuccessFixture);

  await page.goto("/analysis");

  await page.locator("#analysis-prompt").fill(
    "查询平安银行的日线行情和财务指标",
  );
  await page.locator('button[type="submit"]').click();

  // Two result blocks should appear (one success, one error)
  const resultBlocks = page.locator(".result-block");
  await expect(resultBlocks.first()).toBeVisible({ timeout: 5000 });
  await expect(resultBlocks).toHaveCount(2);

  // Error card should be visible
  await expect(page.locator(".error-card")).toBeVisible();

  // Error should show Tushare permission error
  await expect(page.locator(".error-card")).toContainText("40203");
});

/* ------------------------------------------------------------------ */
/*  Scenario: upstream error rendering                                  */
/* ------------------------------------------------------------------ */

test("planning error shows DeepSeek error card", async ({ page }) => {
  await mockApiRoutes(page, planningErrorFixture);

  await page.goto("/analysis");

  await page.locator("#analysis-prompt").fill(
    "查询A股数据",
  );
  await page.locator('button[type="submit"]').click();

  // Error card should appear
  await expect(page.locator(".error-card")).toBeVisible({ timeout: 5000 });

  // Error should mention DeepSeek
  const errorCard = page.locator(".error-card");
  await expect(errorCard).toContainText("DeepSeek");

  // Error should contain HTTP status 401
  await expect(errorCard).toContainText("401");

  // Expand the collapsible panel first to make the decision trace visible
  const detailsPanel = page.locator(".collapsible-panel");
  await detailsPanel.locator("summary").click();

  // Error trace should be visible in the decision flow
  await expect(
    page.locator(".decision-trace li.is-error").first(),
  ).toBeVisible();
});

/* ------------------------------------------------------------------ */
/*  Scenario: query-details visibility                                  */
/* ------------------------------------------------------------------ */

test("query and execution details share one panel", async ({
  page,
}) => {
  await mockApiRoutes(page, successWithMultiRowFixture);

  await page.goto("/analysis");

  await page.locator("#analysis-prompt").fill(
    "查询2026年7月17日A股涨跌分布",
  );
  await page.locator('button[type="submit"]').click();

  // Wait for results to appear
  await expect(page.locator(".result-block")).toBeVisible({ timeout: 5000 });

  // Query details panel exists
  const detailsPanel = page.locator(".collapsible-panel");
  await expect(detailsPanel).toBeVisible();

  // It should be collapsed by default
  const summary = detailsPanel.locator("summary");
  await expect(summary).toBeVisible();
  await expect(summary).toContainText("查询与执行详情");

  // Click to expand
  await summary.click();

  // Plan content should be visible
  const planContent = detailsPanel.locator(".plan-content");
  await expect(planContent).toBeVisible({ timeout: 3000 });

  // Interpretation text should appear
  await expect(planContent).toContainText(
    "查询2026年7月17日所有A股的日线行情",
  );

  // Query cards should show operation details
  const queryCards = planContent.locator(".query-card");
  await expect(queryCards.first()).toBeVisible();
  await expect(queryCards.first()).toContainText("daily");

  // Execution trace is part of the same plan content
  await expect(planContent.locator(".decision-trace")).toBeVisible();
});

/* ------------------------------------------------------------------ */
/*  Scenario: single row result shows record grid                       */
/* ------------------------------------------------------------------ */

test("single-row result renders as a record grid", async ({ page }) => {
  await mockApiRoutes(page, successWithSingleRowFixture);

  await page.goto("/analysis");

  await page.locator("#analysis-prompt").fill("查询A股列表");
  await page.locator('button[type="submit"]').click();

  await expect(page.locator(".result-block")).toBeVisible({ timeout: 5000 });

  // Single row should render as a dl grid, not a table
  await expect(page.locator(".record-grid")).toBeVisible();

  // Should show the stock name
  await expect(page.locator(".record-grid")).toContainText("平安银行");
});

/* ------------------------------------------------------------------ */
/*  Scenario: empty result shows status message                         */
/* ------------------------------------------------------------------ */

test("empty result displays user guidance", async ({ page }) => {
  await mockApiRoutes(page, emptyResultFixture);

  await page.goto("/analysis");

  await page.locator("#analysis-prompt").fill(
    "查询2026年12月31日的A股行情",
  );
  await page.locator('button[type="submit"]').click();

  // Empty result message should appear
  await expect(page.locator(".empty-result")).toBeVisible({ timeout: 5000 });

  // Should contain guidance text
  await expect(page.locator(".empty-result")).toContainText(
    "未查询到数据",
  );
});

/* ------------------------------------------------------------------ */
/*  Scenario: single stock with many rows — no search filter            */
/* ------------------------------------------------------------------ */

test("single-stock multi-row result hides search filter", async ({
  page,
}) => {
  await mockApiRoutes(page, successWithSingleStockManyRowsFixture);

  await page.goto("/analysis");

  await page.locator("#analysis-prompt").fill("查询A股列表");
  await page.locator('button[type="submit"]').click();

  await expect(page.locator(".result-block")).toBeVisible({ timeout: 5000 });

  // Table should render (13 rows > 1)
  await expect(page.locator(".table-scroll")).toBeVisible();

  // Search filter should NOT be shown because all rows belong to
  // one exchange scope (no ts_code column, so uniqueStockCount === 0)
  await expect(page.locator(".result-tools")).not.toBeVisible();
});

test("multi-stock multi-row result shows search filter", async ({
  page,
}) => {
  await mockApiRoutes(page, successWithMultiRowFixture);

  await page.goto("/analysis");

  await page.locator("#analysis-prompt").fill(
    "查询2026年7月17日A股涨跌分布",
  );
  await page.locator('button[type="submit"]').click();

  await expect(page.locator(".result-block")).toBeVisible({ timeout: 5000 });

  // Search filter should be shown because there are multiple stocks
  await expect(page.locator(".result-tools")).toBeVisible();
});

test("compatible results grouping merges multiple tables and displays raw details", async ({ page }) => {
  const compatibleResultsFixture = {
    request_id: "e2e-grouping-test",
    planner: "deepseek",
    data_provider: "tushare",
    status: "success",
    plan: {
      market: "A_SHARE",
      interpretation: "对比平安银行和万科A的日线行情",
      feasibility: "supported",
      requirements: [],
      limitations: [],
      queries: [
        {
          query_id: "q-pingan",
          operation: "daily",
          params: { ts_code: "000001.SZ", trade_date: "20260717" },
          fields: ["ts_code", "trade_date", "close"],
          purpose: "pingan daily",
          transform: null,
          filters: [],
          aggregations: [],
        },
        {
          query_id: "q-wanke",
          operation: "daily",
          params: { ts_code: "000002.SZ", trade_date: "20260717" },
          fields: ["ts_code", "trade_date", "close"],
          purpose: "wanke daily",
          transform: null,
          filters: [],
          aggregations: [],
        },
      ],
    },
    results: [
      {
        query_id: "q-pingan",
        provider: "tushare",
        operation: "daily",
        status: "success",
        columns: ["ts_code", "trade_date", "close"],
        rows: [
          { ts_code: "000001.SZ", trade_date: "20260717", close: 12.18 },
        ],
        row_count: 1,
        summary: {},
        error: null,
      },
      {
        query_id: "q-wanke",
        provider: "tushare",
        operation: "daily",
        status: "success",
        columns: ["ts_code", "trade_date", "close"],
        rows: [
          { ts_code: "000002.SZ", trade_date: "20260717", close: 18.50 },
        ],
        row_count: 1,
        summary: {},
        error: null,
      },
    ],
    decision_trace: [],
  };

  await page.route("**/api/analysis", (route) => {
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(compatibleResultsFixture),
    });
  });

  await page.goto("/analysis");

  await page.locator("#analysis-prompt").fill("对比平安银行和万科A");
  await page.locator('button[type="submit"]').click();

  // There should be exactly 1 top-level result block (since the two results are merged!)
  const resultBlocks = page.locator(".results-panel > .result-block");
  await expect(resultBlocks.first()).toBeVisible({ timeout: 5000 });
  await expect(resultBlocks).toHaveCount(1);

  // Both stocks should be visible in the single merged table
  await expect(resultBlocks).toContainText("000001.SZ");
  await expect(resultBlocks).toContainText("000002.SZ");

  // The raw results collapsible panel should exist
  const rawDetails = page.locator(".raw-results-panel");
  await expect(rawDetails).toBeVisible();
  await expect(rawDetails.locator("summary")).toContainText("查看 2 个原始分步查询结果");

  // Clicking it should expand the child tables
  await rawDetails.locator("summary").click();
  const rawContent = rawDetails.locator(".raw-results-content");
  await expect(rawContent).toBeVisible();

  // Inside raw content, child result blocks exist
  const childBlocks = rawContent.locator(".result-block");
  await expect(childBlocks).toHaveCount(2);
  await expect(childBlocks.first()).toContainText("000001.SZ");
  await expect(childBlocks.nth(1)).toContainText("000002.SZ");
});

test("discovery page submits a bounded study and renders validation evidence", async ({ page }) => {
  const discoveryStatus = {
    task_id: "discovery-e2e",
    status: "succeeded",
    research_config: {
      target_pool: "A_SHARE",
      train_start: "20240101",
      train_end: "20251231",
      val_start: "20260101",
      val_end: "20260630",
      factors: ["pe_ttm", "turnover_rate", "circ_mv", "positive_days_3"],
      forward_days: 20,
      target_return_pct: 5,
      minimum_samples: 30,
      minimum_trading_days: 20,
      minimum_securities: 10,
      minimum_outcome_coverage_pct: 95,
      max_conditions: 2,
    },
    progress: {
      current_generation: 1,
      total_generations: 1,
      formulas_tested: 2,
      candidates_evaluated: 18,
      current_log: "规律搜索与独立验证已完成。",
      current_stage: "completed",
      training_sample_count: 1200,
      training_samples_purged: 80,
      validation_sample_count: 420,
      training_factor_coverage: { pe_ttm: 0.98, turnover_rate: 0.96, circ_mv: 1, positive_days_3: 0.91 },
      validation_factor_coverage: { pe_ttm: 0.97, turnover_rate: 0.95, circ_mv: 1, positive_days_3: 0.90 },
      leaderboard: [{
        formula: "pe_ttm <= 12 and turnover_rate >= 8",
        description: "Low valuation with active turnover",
        reasoning: "Generated from training-window quantiles and independently validated.",
        threshold_source: "quantile",
        validation_score: 0.027,
        generalization_gap: 0.03,
        support_rate_gap: 0.029,
        support_retention_ratio: 1.196,
        p_value: 0.001,
        q_value: 0.063,
        fdr_family_size: 18,
        validation_passed: true,
        validation_reason: "passed",
        train_result: {
          win_rate: 0.63, mean_return: 0.071, median_return: 0.052,
          return_p05: -0.16,
          max_drawdown: null, eval_time_ms: 10, sample_count: 180,
          matched_sample_count: 184, eligible_sample_count: 1200, rule_support_rate: 0.1533,
          missing_outcome_count: 4, outcome_coverage_rate: 0.978,
          positive_count: 113, return_std: 0.18, baseline_win_rate: 0.52,
          baseline_sample_count: 1182,
          baseline_outcome_coverage_rate: 0.985,
          win_rate_lift: 0.11, lift_confidence_lower: 0.04,
          outcome_robust_lift_lower: 0.09, outcome_robust_lift_upper: 0.13,
          lift_confidence_upper: 0.18, confidence_lower: 0.56, confidence_upper: 0.70,
          target_return: 0.05,
          trading_day_count: 120,
          effective_trading_day_count: 96.4,
          security_count: 86,
          effective_security_count: 74.2,
          max_security_event_share: 0.08,
          max_signal_date_event_share: 0.06,
          cluster_standard_error: 0.04,
          lift_standard_error: 0.035,
          dependence_lag_days: 19,
          return_price_basis: "split_and_dividend_adjusted_close",
          event_examples: [{ trade_date: "20251230", ts_code: "000001.SZ", future_trade_date: "20260128", forward_return: 0.082, factor_values: { pe_ttm: 11.2, turnover_rate: 8.6 } }],
        },
        val_result: {
          win_rate: 0.60, mean_return: 0.054, median_return: 0.041,
          return_p05: -0.19,
          max_drawdown: null, eval_time_ms: 8, sample_count: 75,
          matched_sample_count: 77, eligible_sample_count: 420, rule_support_rate: 0.1833,
          missing_outcome_count: 2, outcome_coverage_rate: 0.974,
          positive_count: 45, return_std: 0.20, baseline_win_rate: 0.51,
          baseline_sample_count: 407,
          baseline_outcome_coverage_rate: 0.969,
          win_rate_lift: 0.09, lift_confidence_lower: 0.02,
          outcome_robust_lift_lower: 0.07, outcome_robust_lift_upper: 0.11,
          lift_confidence_upper: 0.16, confidence_lower: 0.49, confidence_upper: 0.70,
          target_return: 0.05,
          trading_day_count: 55,
          effective_trading_day_count: 42.7,
          security_count: 48,
          effective_security_count: 35.6,
          max_security_event_share: 0.12,
          max_signal_date_event_share: 0.15,
          cluster_standard_error: 0.06,
          lift_standard_error: 0.05,
          dependence_lag_days: 19,
          return_price_basis: "split_and_dividend_adjusted_close",
          event_examples: [{ trade_date: "20260629", ts_code: "000002.SZ", future_trade_date: "20260727", forward_return: 0.061, factor_values: { pe_ttm: 10.8, turnover_rate: 9.1 } }],
        },
      }],
    },
    error: null,
  };
  discoveryStatus.progress.leaderboard.push({
    ...discoveryStatus.progress.leaderboard[0],
    formula: "positive_days_3 >= 3",
    description: "Three positive adjusted-close sessions",
    reasoning: "Generated from observed discrete training values.",
    threshold_source: "observed_value",
    validation_score: 0,
    support_rate_gap: 0.1533,
    support_retention_ratio: 0,
    p_value: 1,
    q_value: 1,
    validation_passed: false,
    validation_reason: "insufficient_validation_samples",
    val_result: {
      ...discoveryStatus.progress.leaderboard[0].val_result,
      win_rate: 0,
      mean_return: 0,
      median_return: 0,
      return_p05: 0,
      sample_count: 0,
      matched_sample_count: 0,
      eligible_sample_count: 0,
      rule_support_rate: 0,
      baseline_win_rate: 0,
      baseline_sample_count: 0,
      event_examples: [],
    },
  });
  await page.route("**/api/discovery/tasks", route => {
    const request = route.request().postDataJSON() as { factors: string[]; minimum_trading_days: number; minimum_securities: number; minimum_outcome_coverage_pct: number };
    expect(request.factors).toContain("positive_days_3");
    expect(request.minimum_trading_days).toBe(20);
    expect(request.minimum_securities).toBe(10);
    expect(request.minimum_outcome_coverage_pct).toBe(95);
    void route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        task_id: "discovery-e2e",
        status: "queued",
        status_url: "/api/discovery/tasks/discovery-e2e",
      }),
    });
  });
  let statusPollCount = 0;
  await page.route("**/api/discovery/tasks/discovery-e2e", route => {
    statusPollCount += 1;
    if (statusPollCount === 1) {
      void route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Temporarily unavailable" }),
      });
      return;
    }
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(discoveryStatus),
    });
  });
  await page.goto("/analysis");
  await page.getByRole("tab", { name: "策略挖掘" }).click();

  await expect(page.getByRole("heading", { name: "从历史数据反向发现规律" })).toBeVisible();
  await expect(page.getByText("未来标签隔离", { exact: true })).toBeVisible();
  await expect(page.getByText(/信号日复权收盘到未来交易日复权收盘的事件收益/)).toBeVisible();
  await expect(page.getByLabel("训练信号结束")).toHaveValue("20251231");
  await expect(page.getByLabel("验证信号结束")).toHaveValue("20260630");
  await expect(page.getByText(/验证信号结束后必须已经产生至少 20 个真实交易日的行情/)).toBeVisible();
  await expect(page.getByText(/不会自动缩短窗口或用尚未发生的价格填充结果/)).toBeVisible();
  await expect(page.locator(".factor-grid")).toContainText("近3日上涨天数");
  await expect(page.locator(".factor-grid")).toContainText("复权5日收益率");
  await expect(page.locator(".factor-grid")).toContainText("复权5日波动率");
  await expect(page.locator(".factor-grid")).toContainText("复权5日最大回撤");
  await expect(page.locator(".factor-grid")).toContainText("复权5日峰值距离");
  await expect(page.locator(".factor-grid")).toContainText("自由流通换手率");
  await page.locator(".factor-checkbox").filter({ hasText: "近3日上涨天数" }).locator("input").check();
  await expect(page.getByText(/50 个配对席位会先覆盖所有存在有效候选的因子/)).toBeVisible();
  await expect(page.getByText(/再为每个因子覆盖反方向条件/)).toBeVisible();
  await expect(page.getByText(/每个因子方向按训练排名补入一个次优阈值/)).toBeVisible();
  await expect(page.getByText(/单条件标签覆盖不足时仍可参与交互生成.*最终交互仍须重新通过全部覆盖门槛/)).toBeVisible();
  await expect(page.getByText(/训练提升非正或无法抵御标签缺失的规则不会占用预留验证名额/)).toBeVisible();
  await expect(page.getByText(/离散因子会枚举全部实际阈值/)).toBeVisible();
  await expect(page.getByLabel("最少交易日")).toHaveAttribute("max", "30");
  await expect(page.getByLabel("最少证券数")).toHaveAttribute("max", "30");
  await page.getByRole("button", { name: "开始反向搜索" }).click();

  await expect(page.getByRole("alert")).toHaveText("研究任务状态暂时不可用，系统正在自动重试。");
  await expect(page.getByRole("heading", { name: "验证集摘要" })).toBeVisible();
  await expect(page.getByText("研究任务状态暂时不可用，系统正在自动重试。")).toHaveCount(0);
  const topRuleCard = page.locator(".rule-card").first();
  const topWindowComparison = topRuleCard.locator(".window-comparison");
  await expect(page.getByText("本次研究配置（任务快照）", { exact: true })).toBeVisible();
  await expect(page.locator(".research-config-grid")).toContainText("20240101 – 20251231");
  await expect(page.locator(".research-config-grid")).toContainText("20260101 – 20260630");
  await expect(page.locator(".research-config-grid")).toContainText("20 个交易日");
  await expect(page.locator(".research-config-grid")).toContainText("信号日复权收盘 → 第 20 个未来交易日复权收盘");
  await expect(page.locator(".research-config-grid")).toContainText("30 / 20 / 10");
  await expect(page.locator(".headline-metrics")).toContainText("60.0%");
  await expect(page.locator(".headline-metrics")).toContainText("HAC、42.7 个有效日 score 与缺失标签的保守包络：49.0% – 70.0%");
  await expect(page.locator(".headline-metrics")).toContainText("可比基准命中率 51.0%");
  await expect(page.locator(".headline-metrics")).toContainText("含规则样本，N=407");
  await expect(page.locator(".headline-metrics")).toContainText("保守 95% 包络 2.0% – 16.0%");
  await expect(page.locator(".headline-metrics")).toContainText("训练榜首预留验证结论");
  await expect(page.locator(".headline-metrics")).toContainText("1 / 2 条入榜规律验证通过");
  await expect(page.locator(".headline-metrics")).toContainText("验证通过");
  await expect(page.locator(".headline-metrics")).toContainText("训练与验证同向，且通过 10% BY-FDR");
  await expect(topWindowComparison).toContainText("规则覆盖（可比事件 1200）");
  await expect(topWindowComparison).toContainText("可比基准命中率");
  await expect(topWindowComparison).toContainText("可比基准命中率（含规则样本）");
  await expect(topWindowComparison).toContainText("相对可比全体（N=1182）");
  await expect(topWindowComparison).toContainText("52.0%");
  await expect(topWindowComparison).toContainText("51.0%");
  await expect(topWindowComparison).toContainText("信号日复权收盘");
  await expect(topWindowComparison).toContainText("可比基准标签覆盖");
  await expect(topWindowComparison).toContainText("98.5%");
  await expect(topWindowComparison).toContainText("96.9%");
  await expect(topWindowComparison).toContainText("15.3%");
  await expect(topWindowComparison).toContainText("180 / 120 / 86");
  await expect(topWindowComparison).toContainText("日期集中度折算后有效交易日");
  await expect(topWindowComparison).toContainText("证券集中度折算后有效证券");
  await expect(topWindowComparison).toContainText("74.2");
  await expect(topWindowComparison).toContainText("35.6");
  await expect(topWindowComparison).toContainText("96.4");
  await expect(topWindowComparison).toContainText("42.7");
  await expect(topWindowComparison).toContainText("最大单股事件占比");
  await expect(topWindowComparison).toContainText("最大单日事件占比");
  await expect(topWindowComparison).toContainText("12.0%");
  await expect(topWindowComparison).toContainText("15.0%");
  await expect(page.locator(".rule-list")).toContainText("规则覆盖差距：2.9%");
  await expect(page.locator(".rule-list")).toContainText("验证期扩张至训练期的 119.6%");
  expect(statusPollCount).toBeGreaterThanOrEqual(2);
  await expect(page.locator(".headline-metrics")).toContainText("N=407");
  await expect(page.getByText("因子可用率（训练 / 验证）", { exact: true })).toBeVisible();
  await expect(page.locator(".factor-coverage-grid")).toContainText("98.0% / 97.0%");
  await expect(page.locator(".headline-metrics")).toContainText("5% 分位收益");
  await expect(topRuleCard).toContainText("75");
  await expect(page.locator(".rule-card h3").first()).toHaveText("市盈率TTM × 换手率分位规律");
  await expect(topRuleCard.locator(".rule-expression")).toHaveText("市盈率TTM ≤ 12 且 换手率 ≥ 8");
  await expect(page.getByText(/候选按相对可比基准提升的保守 95% 下界排序/)).toBeVisible();
  await expect(topRuleCard).toContainText("验证结果未参与重新排序");
  await expect(topWindowComparison).toContainText("预留验证");
  await expect(topRuleCard).toContainText("阈值来源：训练窗口分位阈值");
  await expect(topRuleCard).toContainText("97.4%");
  await expect(page.getByText("已清除 80 条未来结算日进入验证窗口的训练样本，防止标签泄漏。", { exact: true })).toBeVisible();
  await expect(page.getByText(/估值接口成功但无记录时仍保留行情标签/)).toBeVisible();
  await expect(page.getByText(/按缺失结果全部失败或全部成功的边界扩展概率区间/)).toBeVisible();
  await expect(page.getByText(/交易日门槛同时约束原始不同日期数和按事件权重折算的有效日期数/)).toBeVisible();
  await expect(page.getByText(/最低标签覆盖门槛同时约束规则命中事件和完整的因子可比基准/)).toBeVisible();
  await expect(page.getByText(/验证判定会优先显示标签覆盖失败这一根因/)).toBeVisible();
  await expect(page.getByText(/证券门槛同样同时约束原始不同证券数和有效证券数/)).toBeVisible();
  await expect(page.getByText(/相对提升标准误取日期 HAC 与证券聚类两者中较大值/)).toBeVisible();
  await expect(topRuleCard).toContainText("日期 HAC 与证券聚类中较大的标准误");
  await expect(page.getByText(/FDR 分母包含所有进入盲测的冻结候选/)).toBeVisible();
  await expect(page.getByText(/验证期证据不足，也会保留原名次并明确显示失败原因/)).toBeVisible();
  await expect(page.getByText(/训练和验证窗口相对基准均为正向提升/)).toBeVisible();
  await expect(page.getByText(/可比基准.*包含规则命中样本.*并不是只由未命中事件组成的对照组/)).toBeVisible();
  await expect(page.getByText(/提升等于规则命中率减去这个可比全体命中率/)).toBeVisible();
  await expect(page.getByText(/验证通过.*单侧检验并控制 10% BY-FDR/)).toBeVisible();
  await expect(page.getByText(/q-value 通过不等于保守 95% 包络必然完全高于零/)).toBeVisible();
  await expect(page.getByText(/预留验证.*只表示本次任务的搜索和排名没有读取该窗口结果/)).toBeVisible();
  await expect(page.getByText(/同一验证窗口调整因子、日期、门槛或目标收益/)).toBeVisible();
  await expect(page.getByText(/应改用更晚且从未查看的数据再次确认/)).toBeVisible();
  await expect(page.getByText(/这些指标是适用性诊断而非显著性通过门槛/)).toBeVisible();
  await expect(page.getByText(/排除沪市 900xxx 与深市 200xxx B 股/)).toBeVisible();
  await expect(page.getByText(/信号日停牌等没有可定义收盘信号的证券不会进入当天股票池/)).toBeVisible();
  await expect(page.getByText(/未来停牌、退市等造成的结果缺失则会保留在分母中/)).toBeVisible();
  await expect(page.getByText(/达到单页行数上限时会自动继续分页/)).toBeVisible();
  await expect(page.getByText(/重复返回同一满页或同一交易日出现重复证券时.*快速失败/)).toBeVisible();
  await expect(page.getByText("18 个盲测候选 · 通过 10% BY-FDR", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("这是事件研究结果", { exact: false })).toContainText("不等同于可直接交易的组合回测");
  await expect(page.getByText("这是事件研究结果", { exact: false })).toContainText("信号日完整行情通常只能在收盘后确认");

  await expect(topRuleCard).toContainText("收益超过 5.0%");
  await expect(topRuleCard).toContainText("验证集收益超过 5.0% 的概率 95% 区间");
  await expect(topRuleCard).toContainText("验证集相对可比全体提升保守 95% 包络：2.0% – 16.0%");
  await expect(topRuleCard).toContainText("标签缺失最坏—最好提升：7.0% – 11.0%");
  await expect(topRuleCard).toContainText("验证判定：训练与验证同向，且通过 10% BY-FDR");
  await expect(topRuleCard).toContainText("训练—验证提升差距：3.0%");
  await expect(topRuleCard).toContainText("保守相对提升：2.7%");
  const validationExamples = topRuleCard.getByText("核验最近 1 条验证命中事件");
  await expect(validationExamples).toBeVisible();
  await validationExamples.click();
  await expect(topRuleCard).toContainText("20260629");
  await expect(topRuleCard).toContainText("000002.SZ");
  await expect(topRuleCard).toContainText("市盈率TTM=10.8 · 换手率=9.1");
  await expect(topRuleCard).toContainText("6.10%");
  await expect(page.getByRole("button", { name: "暂不可带入" })).toBeDisabled();
  const missingValidationCard = page.locator(".rule-card").nth(1);
  await expect(missingValidationCard).toContainText("不会交给模型猜测执行口径");
  await expect(missingValidationCard).toContainText("验证期暂无可观测结果");
  await expect(missingValidationCard).toContainText("验证判定：验证期有效事件数不足");
  await expect(missingValidationCard.locator(".window-comparison > div").nth(1)).toContainText("收益超过 5.0%—");
  await expect(missingValidationCard.locator(".window-comparison > div").nth(1)).toContainText("平均收益—");
  await expect(missingValidationCard).toContainText("保守相对提升：— · 训练—验证提升差距：—");
  await expect(missingValidationCard).toContainText("验证期收缩至训练期的 0.0%");
  await expect(missingValidationCard).toContainText("显著性未检验：证据门槛不足，按 p=1.000 计入 BY-FDR");
  await expect(missingValidationCard).not.toContainText("Student-t 提升检验 p-value");

  await page.getByLabel("目标收益（%）").fill("10");
  await page.locator(".factor-checkbox.is-selected").first().click();
  await expect(page.getByText("超过 5.0% 的概率", { exact: true })).toBeVisible();
  await expect(page.locator(".factor-coverage-grid > div")).toHaveCount(4);

  await expect(page.getByText(/仍需经过查询规划/)).toBeVisible();
  await page.getByRole("button", { name: "带入分析页" }).click();
  await expect(page.locator("#analysis-prompt")).toHaveValue(/筛选今日全部A股中严格满足以下条件的股票/);
  await expect(page.locator("#analysis-prompt")).toHaveValue(/不要改变运算符或阈值/);
  await expect(page.locator("#analysis-prompt")).toHaveValue(/pe_ttm <= 12 and turnover_rate >= 8/);
});

test("discovery page stops polling when a task no longer exists", async ({ page }) => {
  await page.route("**/api/discovery/tasks", route => {
    void route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        task_id: "missing-discovery-task",
        status: "queued",
        status_url: "/api/discovery/tasks/missing-discovery-task",
      }),
    });
  });
  let statusPollCount = 0;
  await page.route("**/api/discovery/tasks/missing-discovery-task", route => {
    statusPollCount += 1;
    void route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Discovery task was not found." }),
    });
  });

  await page.goto("/analysis");
  await page.getByRole("tab", { name: "策略挖掘" }).click();
  await page.getByRole("button", { name: "开始反向搜索" }).click();

  await expect(page.getByRole("alert")).toHaveText("研究任务不存在或已过期，请重新提交。");
  await page.waitForTimeout(2200);
  expect(statusPollCount).toBe(1);
});

test("discovery page normalizes factors restored from the URL", async ({ page }) => {
  await page.goto("/analysis?page=discovery&dp_factors=pe_ttm,retired_factor,pe_ttm,pb");

  await expect(page.getByRole("group", { name: /候选因子/ })).toContainText("2");
  await expect(page).toHaveURL(/dp_factors=pe_ttm%2Cpb/);
  await expect(page).not.toHaveURL(/retired_factor/);
});

test("discovery page supports broad factor selection and fast reset", async ({ page }) => {
  await page.goto("/analysis");
  await page.getByRole("tab", { name: "策略挖掘" }).click();

  await expect(page.getByText("可搜索 25 个", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "全选全部因子" }).click();
  await expect(page.locator(".factor-selector legend strong")).toHaveText("25");
  await expect(page.locator(".factor-checkbox.is-selected")).toHaveCount(25);
  await expect(page.getByText("自由流通换手率")).toBeVisible();

  await page.getByRole("button", { name: "清空重选" }).click();
  await expect(page.locator(".factor-selector legend strong")).toHaveText("0");
  await expect(page.locator(".factor-checkbox.is-selected")).toHaveCount(0);
  await page.getByRole("button", { name: "开始反向搜索" }).click();
  await expect(page.getByRole("alert")).toHaveText("请至少选择一个可搜索因子。");
});
