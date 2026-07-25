const { test, expect } = require("@playwright/test");
const { mockApi, seedSession, PROVENANCE_API } = require("../fixtures");

// Prognosis is priced client-side (flagCrashRate) from whatever the dashboard
// already loaded into the in-memory `_dash` — so the dashboard must be
// visited first, exactly like a real user clicking "Prognosis" from it.
const dashboardFixture = {
  decisions: [
    { decision_id: "dec_1", file_path: "a.py", risk_flags: ["assumed_valid_input"], created_at: "2026-07-01T00:00:00Z" },
    { decision_id: "dec_2", file_path: "b.py", risk_flags: ["assumed_valid_input"], created_at: "2026-07-02T00:00:00Z" },
    { decision_id: "dec_3", file_path: "c.py", risk_flags: ["rare_flag"], created_at: "2026-07-03T00:00:00Z" },
  ],
  incidents: [{ incident_id: "inc_1", decision_id: "dec_1", file_path: "a.py", created_at: "2026-07-01T01:00:00Z" }],
  org_id: "org_test",
  resolved_incident_count: 0,
};

test.describe("prognosis", () => {
  test("prices a risk flag with enough samples, and defers one without", async ({ page }) => {
    await seedSession(page);
    await mockApi(page, PROVENANCE_API, [{ method: "GET", path: "/v1/dashboard", respond: { body: dashboardFixture } }]);

    await page.goto("/app.html#/dashboard");
    await page.getByRole("link", { name: "Prognosis →" }).click();

    await expect(page.locator("h1")).toHaveText("Prognosis");
    await expect(page.locator("#main")).toContainText("assumed_valid_input");
    await expect(page.locator("#main")).toContainText("50%"); // 1 of 2 decisions crashed
    await expect(page.locator("#main")).toContainText("rare_flag");
    await expect(page.locator("#main")).toContainText("not priced yet");
  });
});
