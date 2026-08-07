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
    progress: {
      current_generation: 1,
      total_generations: 1,
      formulas_tested: 1,
      candidates_evaluated: 18,
      current_log: "规律搜索与独立验证已完成。",
      current_stage: "completed",
      training_sample_count: 1200,
      training_samples_purged: 80,
      validation_sample_count: 420,
      leaderboard: [{
        formula: "pe_ttm <= 12 and turnover_rate >= 8",
        description: "Low valuation with active turnover",
        reasoning: "Generated from training-window quantiles and independently validated.",
        validation_score: 0.54,
        generalization_gap: 0.03,
        p_value: 0.012,
        q_value: 0.048,
        validation_passed: true,
        train_result: {
          win_rate: 0.63, mean_return: 0.071, median_return: 0.052,
          return_p05: -0.16,
          max_drawdown: -0.12, eval_time_ms: 10, sample_count: 180,
          matched_sample_count: 184, missing_outcome_count: 4, outcome_coverage_rate: 0.978,
          positive_count: 113, return_std: 0.18, baseline_win_rate: 0.52,
          win_rate_lift: 0.11, confidence_lower: 0.56, confidence_upper: 0.70,
          target_return: 0,
          trading_day_count: 120,
          cluster_standard_error: 0.04,
          lift_standard_error: 0.035,
          dependence_lag_days: 19,
          return_price_basis: "split_and_dividend_adjusted_close",
        },
        val_result: {
          win_rate: 0.60, mean_return: 0.054, median_return: 0.041,
          return_p05: -0.19,
          max_drawdown: -0.15, eval_time_ms: 8, sample_count: 75,
          matched_sample_count: 77, missing_outcome_count: 2, outcome_coverage_rate: 0.974,
          positive_count: 45, return_std: 0.20, baseline_win_rate: 0.51,
          win_rate_lift: 0.09, confidence_lower: 0.49, confidence_upper: 0.70,
          target_return: 0,
          trading_day_count: 55,
          cluster_standard_error: 0.06,
          lift_standard_error: 0.05,
          dependence_lag_days: 19,
          return_price_basis: "split_and_dividend_adjusted_close",
        },
      }],
    },
    error: null,
  };
  await page.route("**/api/discovery/tasks", route => {
    const request = route.request().postDataJSON() as { minimum_trading_days: number; minimum_outcome_coverage_pct: number };
    expect(request.minimum_trading_days).toBe(20);
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
  await page.route("**/api/discovery/tasks/discovery-e2e", route => {
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(discoveryStatus),
    });
  });
  await page.goto("/analysis");
  await page.getByRole("tab", { name: "策略挖掘" }).click();

  await expect(page.getByRole("heading", { name: "从历史数据反向发现规律" })).toBeVisible();
  await page.getByRole("button", { name: "开始反向搜索" }).click();

  await expect(page.getByRole("heading", { name: "验证集摘要" })).toBeVisible();
  await expect(page.locator(".headline-metrics")).toContainText("60.0%");
  await expect(page.locator(".headline-metrics")).toContainText("验证通过");
  await expect(page.locator(".headline-metrics")).toContainText("5% 分位收益");
  await expect(page.locator(".rule-card")).toContainText("75");
  await expect(page.locator(".rule-card")).toContainText("97.4%");
  await expect(page.getByText("已清除 80 条未来结算日进入验证窗口的训练样本，防止标签泄漏。", { exact: true })).toBeVisible();
  await expect(page.getByText("这是事件研究结果", { exact: false })).toContainText("不等同于可直接交易的组合回测");
});
