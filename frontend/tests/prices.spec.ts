import { expect, test } from "@playwright/test"

import { addStore, readList, savePrices } from "./utils/prices"

// These tests drive the full stack: browser -> API -> background job -> agent -> database.
// They need the backend in demo mode (SIM_MODEL=demo), which reads lists with a deterministic
// parser instead of an LLM, and the committed reference data loaded (python -m app.seed all),
// so "onions" matches the catalog. CI does both in .github/workflows/playwright.yml.

const LIST = "Kroger\nonions 3 lb bag 2.99\nkale bunch 2.50"

test("Prices page is accessible and shows correct title", async ({ page }) => {
  await page.goto("/prices")
  await expect(page.getByRole("heading", { name: "Prices" })).toBeVisible()
  await expect(page.getByRole("button", { name: "Read list" })).toBeVisible()
})

test("Prices link in the sidebar opens the page", async ({ page }) => {
  await page.goto("/")
  await page.getByRole("link", { name: "Prices", exact: true }).click()
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
  await addStore(page)
})

test.describe("Reading a typed list", () => {
  test.slow() // runs real background jobs, so allow extra time

  test("Drafts, questions, and set-aside lines are shown", async ({ page }) => {
    await readList(page, LIST)

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
    await readList(page, LIST)

    await page.reload()

    await expect(page.getByTestId("run-status")).toHaveText("succeeded")
    await expect(page.getByTestId("draft-cost-per-cup")).toHaveText("$0.39")
    await expect(page.getByTestId("run-input")).toContainText(
      "onions 3 lb bag 2.99",
    )
  })

  test("Answering the weight question saves both lines", async ({ page }) => {
    await readList(page, LIST)
    const save = page.getByRole("button", { name: /^Save \d+ prices?$/ })
    await expect(save).toHaveText("Save 1 price") // the kale line waits for its weight

    await page.getByLabel("Weight of one piece for kale bunch 2.50").fill("0.5")
    await expect(save).toHaveText("Save 2 prices")
    await save.click()

    await expect(page.getByTestId("capture-saved")).toContainText(
      "Saved 2 prices",
    )
  })

  test("A skipped draft is not saved", async ({ page }) => {
    await readList(page, "onions 3 lb bag 2.99\ntomatoes 2.49/lb")
    await page.getByLabel("Save tomatoes 2.49/lb").uncheck()
    await page.getByRole("button", { name: "Save 1 price" }).click()
    await expect(page.getByTestId("capture-saved")).toContainText(
      "Saved 1 price ",
    )
  })

  test("Choosing an item for an unknown name matches it on the next read", async ({
    page,
  }) => {
    // A made-up name, so the alias it saves cannot collide with an earlier run of this test.
    const name = `qq${Math.random().toString(36).slice(2, 7).replace(/[0-9]/g, "x")}`
    const line = `${name} 1 lb 2.48`
    await readList(page, line)
    await expect(page.getByTestId("capture-question")).toContainText(
      `Which item is '${name}'`,
    )

    await page
      .getByLabel(`Item for ${line}`)
      .selectOption({ label: "Collards" })
    await page.getByRole("button", { name: "Use this item" }).click()
    await page.getByRole("button", { name: "Read the list again" }).click()

    await expect(page.getByTestId("run-status")).toHaveText("succeeded", {
      timeout: 30_000,
    })
    await expect(page.getByTestId("capture-draft")).toContainText("Collards")
    await expect(page.getByTestId("capture-draft")).toContainText(
      "as you confirmed before",
    )
  })

  test("A price for one with a count is saved as the total", async ({
    page,
  }) => {
    // A weight unit, so no piece weight is needed: CI loads no SR28 portions.
    await readList(page, "onions $0.99/lb (3)")
    const draft = page.getByTestId("capture-draft")
    await expect(draft).toContainText("3 lb")
    // 0.99 x 3 = 2.97, multiplied by the capture code, not the model
    await expect(draft.getByTestId("draft-price")).toContainText("$2.97")
    await expect(draft.getByTestId("draft-price")).toContainText(
      "$0.99 a lb x 3",
    )
  })

  test("A question's line can be dropped, and unsaved lines are listed", async ({
    page,
  }) => {
    await readList(
      page,
      "onions 3 lb bag 2.99\nqqzzv 1 lb 1.00\nkale bunch 2.50",
    )
    await page.getByRole("button", { name: "Drop qqzzv 1 lb 1.00" }).click()
    await expect(page.getByLabel("Item for qqzzv 1 lb 1.00")).toHaveCount(0)

    const unsaved = page.getByTestId("capture-unsaved")
    await expect(unsaved).toContainText("Will not be saved")
    await expect(unsaved).toContainText("qqzzv 1 lb 1.00 (you dropped it)")
    await expect(unsaved).toContainText(
      "kale bunch 2.50 (question not answered)",
    )

    await page.getByRole("button", { name: "Save 1 price" }).click()
    await expect(page.getByTestId("capture-saved")).toContainText(
      "Saved 1 price ",
    )
    await expect(page.getByTestId("capture-unsaved")).toContainText("Not saved")
    await expect(page.getByTestId("capture-unsaved")).toContainText(
      "qqzzv 1 lb 1.00 (you dropped it)",
    )
  })

  test("Saved prices appear in the price book", async ({ page }) => {
    const store = await savePrices(page, "onions 3 lb bag 2.99")
    await page.getByRole("link", { name: "price book", exact: true }).click()
    await expect(
      page.getByTestId("price-row").filter({ hasText: store }),
    ).toContainText("$0.39")
  })
})
