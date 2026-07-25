// Shared helpers for mocking the CodeAutopsy backend and seeding an
// authenticated session, so tests exercise the real docs/*.html files
// unmodified against a fake network instead of a live Cloud Run backend.

const PROVENANCE_API = "https://codeautopsy-provenance-3bczbiamba-uc.a.run.app";
const SAMPLE_APP_API = "https://codeautopsy-sample-app-3bczbiamba-uc.a.run.app";

/**
 * Installs a route mock for one API origin.
 * `routes` is an array of { method, path, respond } where `path` is an exact
 * pathname string or a RegExp, and `respond` is either a plain
 * { status?, body? } object or a function(route, request, url) => same
 * shape (or undefined if it fulfills the route itself, e.g. to simulate a
 * network failure via route.abort()).
 */
async function mockApi(page, base, routes) {
  await page.route(`${base}/**`, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const method = req.method();
    const match = routes.find(
      (r) => r.method === method && (typeof r.path === "string" ? r.path === url.pathname : r.path.test(url.pathname))
    );
    if (!match) {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: `unmocked in test: ${method} ${url.pathname}` }),
      });
      return;
    }
    const result = typeof match.respond === "function" ? await match.respond(route, req, url) : match.respond;
    if (result === undefined) return; // handler fulfilled/aborted the route itself
    await route.fulfill({
      status: result.status || 200,
      contentType: "application/json",
      body: JSON.stringify(result.body !== undefined ? result.body : {}),
    });
  });
}

async function seedSession(page, { token = "test-token", email = "org@example.com" } = {}) {
  await page.addInitScript(
    ([t, e]) => {
      localStorage.setItem("ca_token", t);
      localStorage.setItem("ca_email", e);
    },
    [token, email]
  );
}

module.exports = { PROVENANCE_API, SAMPLE_APP_API, mockApi, seedSession };
