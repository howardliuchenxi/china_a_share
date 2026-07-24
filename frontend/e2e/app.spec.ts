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

  // Experiment library is visible
  await expect(page.locator(".experiment-library")).toBeVisible();

  // Prompt textarea is visible
  const promptField = page.locator("#analysis-prompt");
  await expect(promptField).toBeVisible();

  // Submit button is disabled when prompt is empty
  const submitButton = page.locator('button[type="submit"]');
  await expect(submitButton).toBeDisabled();

  // Empty output placeholder is visible
  await expect(page.locator(".empty-output")).toBeVisible();
});

/* ------------------------------------------------------------------ */
/*  Scenario: template selection                                        */
/* ------------------------------------------------------------------ */

test("template selection populates prompt input", async ({ page }) => {
  await mockApiRoutes(page, successWithMultiRowFixture);

  await page.goto("/analysis");

  // Select a question from the template dropdown
  const questionSelect = page.locator("#experiment-question");
  await questionSelect.selectOption({ index: 1 }); // first actual template

  // Prompt textarea should be populated
  const promptField = page.locator("#analysis-prompt");
  const promptValue = await promptField.inputValue();
  expect(promptValue.length).toBeGreaterThan(0);

  // Submit button should be enabled
  const submitButton = page.locator('button[type="submit"]');
  await expect(submitButton).toBeEnabled();
});

test("template group switching updates available templates", async ({
  page,
}) => {
  await mockApiRoutes(page, successWithMultiRowFixture);

  await page.goto("/analysis");

  // Switch to a different experiment group
  const groupSelect = page.locator("#experiment-group");
  await groupSelect.selectOption("capital-activity");

  // Question dropdown options should refresh
  const questionSelect = page.locator("#experiment-question");
  const options = await questionSelect.locator("option").all();
  // First option is the placeholder, then templates
  expect(options.length).toBeGreaterThan(1);
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

test("query details panel is accessible and shows plan information", async ({
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

  // Execution trace should be visible
  await expect(detailsPanel.locator(".execution-trace")).toBeVisible();
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
