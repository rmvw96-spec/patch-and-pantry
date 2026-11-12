import { expect, type Page, test } from "@playwright/test"

// These tests drive the full stack: browser -> API -> background job -> agent -> database.
// They need the backend in demo mode (SIM_MODEL=demo), which reads lists with a deterministic
// parser instead of an LLM, and the committed reference data loaded (python -m app.seed all),
// so "onions" matches the catalog. CI does both in .github/workflows/playwright.yml.
const DEMO_HINT =
  "The run did not succeed. Is the backend in demo mode (SIM_MODEL=demo) with reference data loaded?"

const RUN_TIMEOUT_MS = 30_000

const LIST = "Kroger\nonions 3 lb bag 2.99\nkale bunch 2.50"

async function addStore(page: Page): Promise<string> {
  const name = `Test store ${Math.random().toString(36).slice(2, 8)}`
  await page.getByLabel("Add a store").fill(name)
  await page.getByRole("button", { name: "Add store" }).click()
  // The new store is selected once it is created.
  await expect(page.getByLabel("Store", { exact: true })).toHaveValue(/.+/)
  return name
}

async function readList(page: Page, text: string) {
  await page.goto("/prices")
  await addStore(page)
  await page.getByLabel("Typed list").fill(text)
  await page.getByRole("button", { name: "Read list" }).click()
  await expect(page).toHaveURL(/\/prices\?id=/)
  // Wait for a final state first, so a failed run fails fast with a clear message.
  const status = page.getByTestId("run-status")
  await expect(status).toHaveText(/succeeded|failed/, {
    timeout: RUN_TIMEOUT_MS,
  })
  return status
}

test("Prices page is accessible and shows correct title", async ({ page }) => {
  await page.goto("/prices")
  await expect(page.getByRole("heading", { name: "Prices" })).toBeVisible()
  await expect(page.getByRole("button", { name: "Read list" })).toBeVisible()
})

test("Prices link in the sidebar opens the page", async ({ page }) => {
  await page.goto("/")
  await page.getByRole("link", { name: "Prices" }).click()
  await expect(page).toHaveURL(/\/prices/)
  await expect(page.getByRole("heading", { name: "Prices" })).toBeVisible()
})

test("An empty list and no store show validation errors", async ({ page }) => {
  await page.goto("/prices")
  await page.getByRole("button", { name: "Read list" }).click()
  await expect(page.getByText("Type at least one line")).toBeVisible()
  await expect(
    page.getByText("Choose a store", { exact: true }).last(),
  ).toBeVisible()
  await expect(page).not.toHaveURL(/\?id=/) // nothing was submitted
})

test("A new store can be added and chosen", async ({ page }) => {
  await page.goto("/prices")
  const name = await addStore(page)
  await expect(
    page.getByLabel("Store", { exact: true }).locator("option:checked"),
  ).toHaveText(name)
})

test.describe("Reading a typed list", () => {
  test.slow() // runs real background jobs, so allow extra time

  test("Drafts, questions, and set-aside lines are shown", async ({ page }) => {
    const status = await readList(page, LIST)

    await expect(status, DEMO_HINT).toHaveText("succeeded")
    const draft = page.getByTestId("capture-draft")
    await expect(draft).toHaveCount(1)
    await expect(draft).toContainText("onions 3 lb bag 2.99")
    await expect(draft).toContainText("Onion")
    // 3 lb at 2.99, ERS yield 0.9 and cup 0.3527 lb: 0.3906 a cup, computed by normalize_price
    await expect(page.getByTestId("draft-cost-per-cup")).toHaveText("$0.39")
    await expect(page.getByTestId("capture-question")).toContainText(
      "How much does one bunch of kale weigh?",
    )
    await expect(page.getByTestId("capture-ignored")).toHaveText(
      "Not prices: Kroger",
    )
  })

  test("The result survives a page refresh", async ({ page }) => {
    const status = await readList(page, LIST)
    await expect(status, DEMO_HINT).toHaveText("succeeded")

    await page.reload()

    await expect(page.getByTestId("run-status")).toHaveText("succeeded")
    await expect(page.getByTestId("draft-cost-per-cup")).toHaveText("$0.39")
    await expect(page.getByTestId("run-input")).toContainText(
      "onions 3 lb bag 2.99",
    )
  })
})
