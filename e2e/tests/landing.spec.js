const { test, expect } = require("@playwright/test");

test.describe("landing page (index.html)", () => {
  test("renders headline and links to demo and dashboard", async ({ page }) => {
    await page.goto("/index.html");
    await expect(page).toHaveTitle(/CodeAutopsy/);
    await expect(page.locator("main#main")).toBeVisible();
    await expect(page.locator('a[href="demo.html"]').first()).toBeVisible();
    await expect(page.locator('a[href="app.html"]').first()).toBeVisible();
  });

  test("theme toggle flips the dark class on <html>", async ({ page }) => {
    await page.goto("/index.html");
    const html = page.locator("html");
    const initiallyDark = (await html.getAttribute("class"))?.includes("dark") ?? false;

    await page.locator("#theme").click();
    await expect
      .poll(async () => (await html.getAttribute("class"))?.includes("dark") ?? false)
      .toBe(!initiallyDark);

    await page.locator("#theme").click();
    await expect
      .poll(async () => (await html.getAttribute("class"))?.includes("dark") ?? false)
      .toBe(initiallyDark);
  });
});
