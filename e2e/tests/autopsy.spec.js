const { test, expect } = require("@playwright/test");
const { mockApi, seedSession, PROVENANCE_API } = require("../fixtures");

test.describe("Live Autopsy playground", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
  });

  test("seeding a decision then simulating a crash resolves back to it", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      { method: "POST", path: "/v1/keys", respond: { status: 201, body: { key: "ca_live_playgroundkey" } } },
      { method: "POST", path: "/v1/provenance", respond: { status: 201, body: { decision_id: "dec_playground" } } },
      {
        method: "POST",
        path: "/v1/resolve",
        respond: {
          body: {
            resolved: true,
            record: {
              decision_id: "dec_playground",
              reasoning_summary: "assuming discount_code is always a clean integer string — skipped validation to hit the deadline",
              risk_flags: ["assumed_valid_input"],
            },
          },
        },
      },
    ]);

    await page.goto("/app.html#/autopsy");
    await expect(page.locator("#step2")).toHaveClass(/opacity-40/);
    await expect(page.locator("#step2")).toHaveClass(/pointer-events-none/);

    await page.locator("#btn-seed").click();
    await expect(page.locator("#seed-msg")).toBeVisible();
    await expect(page.locator("#seed-msg")).toContainText("indexed dec_");
    await expect(page.locator("#step2")).not.toHaveClass(/opacity-40/);
    await expect(page.locator("#step2")).not.toHaveClass(/pointer-events-none/);

    await page.locator("#btn-crash").click();
    await expect(page.locator("#step3")).toContainText("cause of death identified");
    await expect(page.locator("#step3")).toContainText("assumed_valid_input");
  });

  test("a crash with no matching decision reports unresolved", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      { method: "POST", path: "/v1/keys", respond: { status: 201, body: { key: "ca_live_playgroundkey" } } },
      { method: "POST", path: "/v1/provenance", respond: { status: 201, body: { decision_id: "dec_playground" } } },
      { method: "POST", path: "/v1/resolve", respond: { body: { resolved: false } } },
    ]);

    await page.goto("/app.html#/autopsy");
    await page.locator("#btn-seed").click();
    await expect(page.locator("#step2")).not.toHaveClass(/pointer-events-none/);
    await page.locator("#btn-crash").click();
    await expect(page.locator("#step3")).toContainText("no matching decision found");
  });
});
