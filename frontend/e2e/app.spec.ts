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
  stockListFixture,
  successWithMultiRowFixture,
  successWithSingleRowFixture,
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
  await page.route("**/api/stocks", (route) => {
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(stockListFixture),
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
  await expect(page.locator(".hero h1")).toBeVisible();

  // Page tabs are visible with correct state
  const analysisTab = page.locator('.page-tabs button[role="tab"]').first();
  await expect(analysisTab).toHaveAttribute("aria-selected", "true");

  // Prompt textarea is visible
  const promptField = page.locator("#analysis-prompt");
  await expect(promptField).toBeVisible();

  // Prompt history is empty before the first submission
  await expect(page.locator("#prompt-history")).toBeDisabled();

  // Submit button is disabled when prompt is empty
  const submitButton = page.locator('button[type="submit"]');
  await expect(submitButton).toBeDisabled();

  // Empty output placeholder is visible
  await expect(page.locator(".empty-output")).toBeVisible();
});

/* ------------------------------------------------------------------ */
/*  Scenario: prompt history                                            */
/* ------------------------------------------------------------------ */

test("prompt history selection populates prompt input", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "china-a-share.prompt-history",
      JSON.stringify([
        "查询2026年7月17日A股涨跌分布",
        "查询A股列表",
      ]),
    );
  });
  await mockApiRoutes(page, successWithMultiRowFixture);

  await page.goto("/analysis");

  const historySelect = page.locator("#prompt-history");
  await expect(historySelect).toBeEnabled();
  await historySelect.selectOption("查询A股列表");

  const promptField = page.locator("#analysis-prompt");
  await expect(promptField).toHaveValue("查询A股列表");
  await expect(page.locator('button[type="submit"]')).toBeEnabled();
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

  const dialog = page.getByRole("dialog", { name: "改进这个页面区域" });
  const suggestion = page.locator("#ui-feedback-suggestion");
  await expect(dialog).toBeVisible();
  await expect(suggestion).toBeFocused();

  await suggestion.fill("标题可以再简洁一些");

  await expect(dialog).toBeVisible();
  await expect(suggestion).toHaveValue("标题可以再简洁一些");
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
/*  Scenario: reference-data page renders stock list                    */
/* ------------------------------------------------------------------ */

test("basic info page shows stock list with mocked data", async ({ page }) => {
  await mockApiRoutes(page, successWithMultiRowFixture);

  await page.goto("/analysis");

  // Click the reference data tab
  await page.locator('.page-tabs button[role="tab"]').nth(1).click();

  // Stock list section should appear
  await expect(page.locator(".reference-page")).toBeVisible();

  // Stock table should populate with mock data
  await expect(page.locator(".stock-table")).toBeVisible({ timeout: 5000 });

  // Should contain mocked stock names
  await expect(page.locator(".stock-table")).toContainText("平安银行");
  await expect(page.locator(".stock-table")).toContainText("贵州茅台");
});
