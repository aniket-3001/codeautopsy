const { test, expect } = require("@playwright/test");
const { mockApi, seedSession, PROVENANCE_API } = require("../fixtures");

const dashboardFixture = {
  decisions: [
    {
      decision_id: "dec_1",
      commit_sha: "abc123def456",
      file_path: "app/checkout.py",
      line_start: 40,
      line_end: 46,
      reasoning_summary: "assumed discount_code is always numeric",
      risk_flags: ["assumed_valid_input"],
      created_at: "2026-07-20T10:00:00Z",
      model: "claude-sonnet-5",
      tool: "claude-code",
    },
    {
      decision_id: "dec_2",
      commit_sha: "def456abc123",
      file_path: "app/payments.py",
      line_start: 10,
      line_end: 12,
      reasoning_summary: "skipped null check for speed",
      risk_flags: [],
      created_at: "2026-07-21T10:00:00Z",
      model: "gpt-5",
      tool: "cursor",
    },
  ],
  incidents: [
    {
      incident_id: "inc_1",
      decision_id: "dec_1",
      commit_sha: "abc123def456",
      file_path: "app/checkout.py",
      line: 42,
      exc_type: "ValueError",
      exc_message: "invalid literal for int()",
      resolved: true,
      blast_radius: 3,
      crash_trace_id: "trace-1",
      crash_span_id: "span-1",
      created_at: "2026-07-20T11:00:00Z",
    },
  ],
  org_id: "org_test",
  resolved_incident_count: 1,
};

test.describe("dashboard", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
    await mockApi(page, PROVENANCE_API, [{ method: "GET", path: "/v1/dashboard", respond: { body: dashboardFixture } }]);
  });

  test("renders stat cards and lists from the API", async ({ page }) => {
    await page.goto("/app.html#/dashboard");
    await expect(page.locator("h1")).toHaveText("Dashboard");
    await expect(page.locator("#main")).toContainText("2"); // decisions indexed
    await expect(page.locator("#decisions-list")).toContainText("assumed discount_code is always numeric");
    await expect(page.locator("#incidents-list")).toContainText("ValueError");
  });

  test("filter narrows visible decisions and incidents", async ({ page }) => {
    await page.goto("/app.html#/dashboard");
    await page.locator("#dash-filter").fill("payments");

    const checkoutDecision = page.locator("#decisions-list .filter-card", { hasText: "checkout.py" });
    const paymentsDecision = page.locator("#decisions-list .filter-card", { hasText: "payments.py" });
    await expect(checkoutDecision).toHaveClass(/hidden/);
    await expect(paymentsDecision).not.toHaveClass(/hidden/);

    const checkoutIncident = page.locator("#incidents-list .filter-card", { hasText: "checkout.py" });
    await expect(checkoutIncident).toHaveClass(/hidden/);
  });

  test("clicking an incident opens a modal with the linked decision", async ({ page }) => {
    await page.goto("/app.html#/dashboard");
    await page.locator('.incident-card[data-id="inc_1"]').click();

    const modal = page.locator("#modal-root");
    await expect(modal).toContainText("assumed discount_code is always numeric");
    await expect(modal).toContainText("assumed_valid_input");

    await page.locator("[data-modal-close]").first().click();
    await expect(modal).toBeEmpty();
  });
});
