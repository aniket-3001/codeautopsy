const { test, expect } = require("@playwright/test");
const { mockApi, seedSession, PROVENANCE_API } = require("../fixtures");

const emptyDashboard = { decisions: [], incidents: [], org_id: "org_test", resolved_incident_count: 0 };

test.describe("Auto-Heal", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
  });

  test("renders heal runs from the API", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      {
        method: "GET",
        path: "/v1/heal/runs",
        respond: {
          body: {
            runs: [
              {
                run_id: "run_1",
                trigger: "signoz-alert",
                status: "fixed",
                file_path: "app/checkout.py",
                line: 42,
                explanation: "patched the missing validation",
                lesson: "always validate discount codes",
                events: [],
                pr_url: "https://github.com/example/repo/pull/7",
              },
            ],
          },
        },
      },
    ]);

    await page.goto("/app.html#/autoheal");
    await expect(page.locator("#ah-runs")).toContainText("patched the missing validation");
    await expect(page.locator("#ah-runs")).toContainText("always validate discount codes");
    await expect(page.locator("#ah-runs a")).toHaveAttribute("href", "https://github.com/example/repo/pull/7");
  });

  test("triggering a heal run reports status and re-polls", async ({ page }) => {
    let runsCalls = 0;
    await mockApi(page, PROVENANCE_API, [
      {
        method: "GET",
        path: "/v1/heal/runs",
        respond: () => {
          runsCalls += 1;
          return { body: { runs: [] } };
        },
      },
      { method: "POST", path: "/v1/heal/trigger", respond: { body: {} } },
    ]);

    await page.goto("/app.html#/autoheal");
    await expect.poll(() => runsCalls).toBeGreaterThanOrEqual(1);

    await page.locator("#ah-trigger").click();
    await expect(page.locator("#ah-status")).toContainText("Heal run started");
    await expect.poll(() => runsCalls).toBeGreaterThanOrEqual(2);
  });

  test("the poll loop stops once the user navigates away from #/autoheal", async ({ page }) => {
    let runsCalls = 0;
    await mockApi(page, PROVENANCE_API, [
      {
        method: "GET",
        path: "/v1/heal/runs",
        respond: () => {
          runsCalls += 1;
          return { body: { runs: [] } };
        },
      },
      { method: "GET", path: "/v1/dashboard", respond: { body: emptyDashboard } },
    ]);

    await page.goto("/app.html#/autoheal");
    await expect.poll(() => runsCalls).toBeGreaterThanOrEqual(1);

    // Navigate away before the 3s re-poll fires — stopHealPoll() must cancel it.
    await page.goto("/app.html#/dashboard");
    const callsAtNavigation = runsCalls;

    // Wait past the poll interval; the count must not grow once we've left the route.
    await page.waitForTimeout(4000);
    expect(runsCalls).toBe(callsAtNavigation);
  });
});
