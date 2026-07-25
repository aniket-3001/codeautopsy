const { test, expect } = require("@playwright/test");
const { mockApi, seedSession, PROVENANCE_API } = require("../fixtures");

// Note: render()'s route guards only decide what to paint into #main — they
// don't rewrite location.hash (only explicit navigate() calls, e.g. on
// login/logout, do that). So these assertions check rendered content, not
// the URL bar, to match the app's actual behavior.
const emptyDashboard = { body: { decisions: [], incidents: [], org_id: "org_test", resolved_incident_count: 0 } };

test.describe("app.html router guards", () => {
  test("protected route renders the login form when signed out", async ({ page }) => {
    await page.goto("/app.html#/dashboard");
    await expect(page.locator("#auth-form")).toBeVisible();
    await expect(page.locator("h1")).toHaveText("Welcome back");
  });

  test("login/signup render the dashboard when already signed in", async ({ page }) => {
    await seedSession(page);
    await mockApi(page, PROVENANCE_API, [{ method: "GET", path: "/v1/dashboard", respond: emptyDashboard }]);

    await page.goto("/app.html#/login");
    await expect(page.locator("h1")).toHaveText("Dashboard");

    await page.goto("/app.html#/signup");
    await expect(page.locator("h1")).toHaveText("Dashboard");
  });

  test("root hash resolves based on session state", async ({ page }) => {
    await page.goto("/app.html");
    await expect(page.locator("#main")).toContainText("Sign up");
    await expect(page.locator("#main")).toContainText("Log in");

    await seedSession(page);
    await mockApi(page, PROVENANCE_API, [{ method: "GET", path: "/v1/dashboard", respond: emptyDashboard }]);
    await page.goto("/app.html");
    await expect(page.locator("h1")).toHaveText("Dashboard");
  });

  test("unknown route falls through to a not-found message", async ({ page }) => {
    await seedSession(page);
    await mockApi(page, PROVENANCE_API, [{ method: "GET", path: "/v1/dashboard", respond: emptyDashboard }]);
    await page.goto("/app.html#/this-route-does-not-exist");
    await expect(page.locator("#main")).toContainText(/not found/i);
  });
});
