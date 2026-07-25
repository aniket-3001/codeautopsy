const { test, expect } = require("@playwright/test");
const { mockApi, seedSession, PROVENANCE_API } = require("../fixtures");

const me = { org: { id: "org_test", name: "Test Org" }, user: { email: "org@example.com" } };

test.describe("settings", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
  });

  test("lists existing keys and shows org/user info", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      { method: "GET", path: "/v1/me", respond: { body: me } },
      {
        method: "GET",
        path: "/v1/keys",
        respond: { body: [{ id: "key_1", prefix: "ab12", created_at: "2026-07-01T00:00:00Z", last_used_at: null }] },
      },
    ]);

    await page.goto("/app.html#/settings");
    await expect(page.locator("#main")).toContainText("Test Org");
    await expect(page.locator("#main")).toContainText("org@example.com");
    await expect(page.locator("#key-list")).toContainText("ca_live_ab12");
    await expect(page.locator("#key-list")).toContainText("never used");
  });

  test("creating a new key reveals it once", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      { method: "GET", path: "/v1/me", respond: { body: me } },
      { method: "GET", path: "/v1/keys", respond: { body: [] } },
      { method: "POST", path: "/v1/keys", respond: { status: 201, body: { key: "ca_live_freshsecret" } } },
    ]);

    await page.goto("/app.html#/settings");
    await expect(page.locator("#key-created")).toHaveClass(/hidden/);

    await page.locator("#btn-new-key").click();
    await expect(page.locator("#key-created")).not.toHaveClass(/hidden/);
    await expect(page.locator("#key-created-value")).toHaveText("ca_live_freshsecret");
  });

  test("revoking a key deletes it and refreshes the list", async ({ page }) => {
    let keysCallCount = 0;
    await mockApi(page, PROVENANCE_API, [
      { method: "GET", path: "/v1/me", respond: { body: me } },
      {
        method: "GET",
        path: "/v1/keys",
        respond: () => {
          keysCallCount += 1;
          const list = keysCallCount === 1 ? [{ id: "key_1", prefix: "ab12", created_at: "2026-07-01T00:00:00Z" }] : [];
          return { body: list };
        },
      },
      { method: "DELETE", path: "/v1/keys/key_1", respond: { status: 204, body: {} } },
    ]);

    await page.goto("/app.html#/settings");
    await expect(page.locator("#key-list")).toContainText("ca_live_ab12");

    await page.locator(".btn-revoke").click();
    await expect(page.locator("#key-list")).toContainText("No keys yet.");
  });
});

test.describe("integrate", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
  });

  test("reveals a key and fills the code snippets with it", async ({ page }) => {
    await mockApi(page, PROVENANCE_API, [
      { method: "GET", path: "/v1/keys", respond: { body: [] } },
      { method: "POST", path: "/v1/keys", respond: { status: 201, body: { key: "ca_live_integratesecret" } } },
    ]);

    await page.goto("/app.html#/integrate");
    await expect(page.locator("#snip-record")).not.toContainText("ca_live_integratesecret");

    await page.locator("#btn-reveal").click();
    await expect(page.locator("#key-state")).toContainText("ca_live_integratesecret");
    await expect(page.locator("#snip-record")).toContainText("ca_live_integratesecret");
    await expect(page.locator("#snip-enrich")).toContainText("ca_live_integratesecret");
  });
});
