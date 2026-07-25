const { test, expect } = require("@playwright/test");
const { mockApi, seedSession, PROVENANCE_API } = require("../fixtures");

test.describe("leaderboard", () => {
  test("ranks tools/models by crash rate", async ({ page }) => {
    await seedSession(page);
    await mockApi(page, PROVENANCE_API, [
      {
        method: "GET",
        path: "/v1/leaderboard",
        respond: {
          body: {
            total_decisions: 12,
            total_incidents: 4,
            scores: [
              {
                tool: "claude-code",
                model: "claude-sonnet-5",
                crash_rate: 0.25,
                decisions: 8,
                crashed_decisions: 2,
                incidents_caused: 2,
                worst_flag: "assumed_valid_input",
                worst_flag_rate: 0.5,
              },
              {
                tool: "cursor",
                model: "gpt-5",
                crash_rate: 0.5,
                decisions: 4,
                crashed_decisions: 2,
                incidents_caused: 2,
                worst_flag: null,
                worst_flag_rate: null,
              },
            ],
          },
        },
      },
    ]);

    await page.goto("/app.html#/leaderboard");
    await expect(page.locator("#main")).toContainText("claude-code");
    await expect(page.locator("#main")).toContainText("claude-sonnet-5");
    await expect(page.locator("#main")).toContainText("cursor");
    await expect(page.locator("#main")).toContainText("25%");
    await expect(page.locator("#main")).toContainText("50%");
  });

  test("shows an empty state with no decisions recorded", async ({ page }) => {
    await seedSession(page);
    await mockApi(page, PROVENANCE_API, [
      { method: "GET", path: "/v1/leaderboard", respond: { body: { total_decisions: 0, total_incidents: 0, scores: [] } } },
    ]);

    await page.goto("/app.html#/leaderboard");
    await expect(page.locator("#main")).toContainText("No decisions recorded yet");
  });
});
