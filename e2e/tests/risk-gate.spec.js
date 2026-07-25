const { test, expect } = require("@playwright/test");
const { mockApi, seedSession, PROVENANCE_API } = require("../fixtures");

test.describe("risk gate", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
    await page.goto("/app.html#/risk-gate");
  });

  test("priced verdict shows the worst flag's historical crash rate", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      {
        method: "POST",
        path: "/v1/risk-gate",
        respond: {
          body: {
            verdict: "priced",
            worst_flag: "assumed_valid_input",
            crash_rate: 0.6,
            sample_size: 5,
            flags: [{ flag: "assumed_valid_input", crash_rate: 0.6, sample_size: 5 }],
          },
        },
      },
    ]);

    await page.locator("#rg-code").fill("return int(code)  # assume it's always a clean number");
    await page.locator("#rg-score").click();

    await expect(page.locator("#rg-result")).toContainText("assumed_valid_input");
    await expect(page.locator("#rg-result")).toContainText("60%");
  });

  test("clear verdict reports no tripped risk flags", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      { method: "POST", path: "/v1/risk-gate", respond: { body: { verdict: "clear", flags: [] } } },
    ]);

    await page.locator("#rg-code").fill("return validate(code)");
    await page.locator("#rg-score").click();

    await expect(page.locator("#rg-result")).toContainText("Clear");
  });

  test("a failed request surfaces the error message", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      { method: "POST", path: "/v1/risk-gate", respond: { status: 500, body: { detail: "scoring service unavailable" } } },
    ]);

    await page.locator("#rg-code").fill("return int(code)");
    await page.locator("#rg-score").click();

    await expect(page.locator("#rg-result")).toContainText("scoring service unavailable");
  });
});
