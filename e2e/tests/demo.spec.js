const { test, expect } = require("@playwright/test");
const { mockApi, SAMPLE_APP_API, PROVENANCE_API } = require("../fixtures");

test.describe("sandbox demo (demo.html)", () => {
  test("full crash -> confess -> crash again -> resolved loop", async ({ page }) => {
    let checkoutCalls = 0;

    await mockApi(page, SAMPLE_APP_API, [
      { method: "GET", path: "/health", respond: { body: { status: "ok", commit: "abc123def456" } } },
      {
        method: "POST",
        path: "/checkout",
        respond: () => {
          checkoutCalls += 1;
          const resolved = checkoutCalls >= 2;
          return {
            status: 500,
            body: {
              detail: {
                codeautopsy: {
                  resolved,
                  crash_trace_id: resolved ? "trace-xyz" : null,
                  crash_span_id: resolved ? "span-xyz" : null,
                },
              },
            },
          };
        },
      },
    ]);

    await mockApi(page, PROVENANCE_API, [
      { method: "POST", path: "/provenance", respond: { status: 201, body: { decision_id: "dec_demo_test" } } },
      { method: "DELETE", path: /^\/provenance\/.+/, respond: { status: 204, body: {} } },
    ]);

    await page.goto("/demo.html");

    await expect(page.locator("#live-label")).toHaveText(/live · checkout-api/);

    // Step 1: crash — unresolved, unlocks step 2.
    await page.locator("#btn-crash").click();
    await expect(page.locator("#out-crash")).toBeVisible();
    await expect(page.locator("#out-crash")).toContainText('"resolved": false');
    await expect(page.locator("#step2")).toHaveAttribute("data-locked", "false");

    // Step 2: confess as the AI agent — unlocks step 3.
    await page.locator("#btn-submit").click();
    await expect(page.locator("#out-submit")).toBeVisible();
    await expect(page.locator("#step3")).toHaveAttribute("data-locked", "false");

    // Step 3: crash again — resolves, shows the win state with a SigNoz link.
    await page.locator("#btn-crash2").click();
    await expect(page.locator("#out-crash2")).toContainText('"resolved": true');
    await expect(page.locator("#win")).toBeVisible();
    await expect(page.locator("#btn-signoz")).toHaveAttribute("href", /trace-xyz/);

    // Cleanup button fires the DELETE and reports success.
    await page.locator("#btn-reset").click();
    await expect(page.locator("#btn-reset")).toHaveText(/Done/);
  });

  test("shows an unreachable state when the sample app health check fails", async ({ page }) => {
    await page.route(`${SAMPLE_APP_API}/**`, (route) => route.abort("failed"));
    await page.goto("/demo.html");
    await expect(page.locator("#live-label")).toHaveText(/unreachable/);
  });
});
