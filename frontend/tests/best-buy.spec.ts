import { expect, test } from "@playwright/test"

import { RUN_TIMEOUT_MS, savePrices } from "./utils/prices"

test("Best buy page is accessible from the sidebar", async ({ page }) => {
  await page.goto("/")
  await page.getByRole("link", { name: "Best buy" }).click()
  await expect(page).toHaveURL(/\/best-buy/)
  await expect(page.getByRole("heading", { name: "Best buy" })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Compare prices" }),
  ).toBeDisabled()
})

test.describe("Comparing prices", () => {
  test.slow() // saves a price and runs a comparison: two real background jobs

  test("The table, explanation, and shopping list are shown", async ({
    page,
  }) => {
    await savePrices(page, "onions 3 lb bag 2.99")
    await page.goto("/best-buy")
    await page.getByLabel("Item").selectOption({ label: "Onion" })
    await page.getByLabel("Cups per week").fill("2")
    await page.getByRole("button", { name: "Add item" }).click()
    await expect(page.getByTestId("best-buy-needs")).toContainText(
      "2 cups a week",
    )

    await page.getByRole("button", { name: "Compare prices" }).click()
    await expect(page).toHaveURL(/\/best-buy\?id=/)
    await expect(page.getByTestId("run-status")).toHaveText("succeeded", {
      timeout: RUN_TIMEOUT_MS,
    })

    // The household's own price beats the ERS average (0.42) and is starred.
    const marked = page.getByTestId("best-buy-marked")
    await expect(marked).toHaveCount(1)
    await expect(marked.getByTestId("best-buy-cost")).toHaveText("$0.39")
    await expect(page.getByTestId("best-buy-table")).toContainText(
      "National average (2023)",
    )
    // Storage and the national average's source are said once, under the table.
    await expect(page.getByTestId("best-buy-item-note")).toHaveCount(1)
    await expect(page.getByTestId("best-buy-item-note")).toContainText("Onion:")
    await expect(page.getByTestId("best-buy-national-average")).toContainText(
      "USDA ERS average retail prices for 2023",
    )
    // The demo answer bolds the cost the way the live model does; it renders as bold text.
    const explanation = page.getByTestId("best-buy-explanation")
    await expect(explanation.locator("strong")).toHaveText(
      "$0.39 per usable cup",
    )
    await expect(explanation).not.toContainText("**")
    const list = page.getByTestId("shopping-list")
    await expect(list).toContainText("Onion")
    await expect(list.getByRole("button", { name: "Print" })).toBeVisible()
  })

  test("Lemons are compared per lemon, not per cup", async ({ page }) => {
    // Spec P1-11: lemons, limes, and garlic are compared per piece. A count price needs no
    // piece weight, so this runs on CI's data too.
    await savePrices(page, "Lemons 3 for $1")
    await page.goto("/best-buy")
    await page.getByLabel("Item").selectOption({ label: "Lemon" })
    await page.getByLabel("Lemons per week").fill("4")
    await page.getByRole("button", { name: "Add item" }).click()
    await expect(page.getByTestId("best-buy-needs")).toContainText(
      "4 lemons a week",
    )
    await page.getByRole("button", { name: "Compare prices" }).click()
    await expect(page.getByTestId("run-status")).toHaveText("succeeded", {
      timeout: RUN_TIMEOUT_MS,
    })

    const marked = page.getByTestId("best-buy-marked")
    await expect(marked.getByTestId("best-buy-cost")).toHaveText(
      "$0.33 per lemon",
    )
    await expect(marked).toContainText("3.00 lemons")
    await expect(
      page.getByTestId("best-buy-explanation").locator("strong"),
    ).toHaveText("$0.33 per usable lemon")
  })

  test("An item with no store price uses the national average", async ({
    page,
  }) => {
    await page.goto("/best-buy")
    await page.getByLabel("Item").selectOption({ label: "Cabbage" })
    await page.getByLabel("Cups per week").fill("2")
    await page.getByRole("button", { name: "Add item" }).click()
    await page.getByRole("button", { name: "Compare prices" }).click()
    await expect(page.getByTestId("run-status")).toHaveText("succeeded", {
      timeout: RUN_TIMEOUT_MS,
    })

    await expect(page.getByTestId("best-buy-marked")).toContainText(
      "National average",
    )
    await expect(page.getByTestId("best-buy-item-note")).toContainText(
      "no store price yet",
    )
    await expect(
      page
        .getByTestId("shopping-group")
        .filter({ hasText: "No store price yet" }),
    ).toContainText("Cabbage")
  })
})
