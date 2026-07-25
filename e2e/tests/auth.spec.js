const { test, expect } = require("@playwright/test");
const { mockApi, PROVENANCE_API } = require("../fixtures");

test.describe("auth flows", () => {
  test("signup success stores the session and lands on onboarding", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      { method: "POST", path: "/v1/auth/signup", respond: { status: 201, body: { access_token: "new-token" } } },
      { method: "GET", path: "/v1/keys", respond: { body: [] } },
    ]);

    await page.goto("/app.html#/signup");
    await page.locator("#in-email").fill("new-org@example.com");
    await page.locator("#in-password").fill("supersecret1");
    await page.locator("#auth-form button[type=submit]").click();

    await expect(page).toHaveURL(/#\/onboarding$/);
    expect(await page.evaluate(() => localStorage.getItem("ca_token"))).toBe("new-token");
    expect(await page.evaluate(() => localStorage.getItem("ca_email"))).toBe("new-org@example.com");
  });

  test("login success stores the session and lands on the dashboard", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      { method: "POST", path: "/v1/auth/login", respond: { body: { access_token: "existing-token" } } },
      {
        method: "GET",
        path: "/v1/dashboard",
        respond: { body: { decisions: [], incidents: [], org_id: "org_test", resolved_incident_count: 0 } },
      },
    ]);

    await page.goto("/app.html#/login");
    await page.locator("#in-email").fill("org@example.com");
    await page.locator("#in-password").fill("supersecret1");
    await page.locator("#auth-form button[type=submit]").click();

    await expect(page).toHaveURL(/#\/dashboard$/);
    expect(await page.evaluate(() => localStorage.getItem("ca_token"))).toBe("existing-token");
  });

  test("login failure surfaces the server error and keeps the user on the form", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      { method: "POST", path: "/v1/auth/login", respond: { status: 401, body: { detail: "invalid credentials" } } },
    ]);

    await page.goto("/app.html#/login");
    await page.locator("#in-email").fill("wrong@example.com");
    await page.locator("#in-password").fill("wrongpassword");
    await page.locator("#auth-form button[type=submit]").click();

    await expect(page.locator("#auth-error")).toBeVisible();
    await expect(page.locator("#auth-error")).toHaveText("invalid credentials");
    await expect(page).toHaveURL(/#\/login$/);
    expect(await page.evaluate(() => localStorage.getItem("ca_token"))).toBeNull();
  });

  test("logout clears the session and returns to the home route", async ({ page }) => {
    const { seedSession } = require("../fixtures");
    await seedSession(page);
    await mockApi(page, PROVENANCE_API, [
      {
        method: "GET",
        path: "/v1/dashboard",
        respond: { body: { decisions: [], incidents: [], org_id: "org_test", resolved_incident_count: 0 } },
      },
    ]);
    await page.goto("/app.html#/dashboard");
    await expect(page).toHaveURL(/#\/dashboard$/);

    await page.locator("#btn-logout").click();
    await expect(page).toHaveURL(/#\/home$/);
    expect(await page.evaluate(() => localStorage.getItem("ca_token"))).toBeNull();
  });
});
