import { expect, test } from "@playwright/test"

import { savePrices } from "./utils/prices"

test("Price book page is accessible from the sidebar", async ({ page }) => {
  await page.goto("/")
  await page.getByRole("link", { name: "Price book" }).click()
  await expect(page).toHaveURL(/\/price-book/)
  await expect(page.getByRole("heading", { name: "Price book" })).toBeVisible()
})

test.describe("Editing prices", () => {
  test.slow() // saving prices runs a real background job first

  test("A price can be edited and deleted", async ({ page }) => {
    const store = await savePrices(page, "onions 3 lb bag 2.99")
    await page.goto("/price-book")
    const row = page.getByTestId("price-row").filter({ hasText: store })
    await expect(row).toContainText("$2.99")
    await expect(row.getByTestId("price-unit-price")).toHaveText("$1.00/lb")
    await expect(row.getByTestId("price-cost-per-cup")).toHaveText("$0.39")

    await row.getByRole("button", { name: "Edit onions 3 lb bag 2.99" }).click()
    const edit = page.getByTestId("price-edit-row")
    // The price is the total paid; the page shows what one unit costs as you type.
    await edit.getByLabel("Total paid").fill("3.49")
    await expect(edit.getByTestId("edit-unit-price")).toHaveText(
      "Total paid. $1.16/lb",
    )
    await edit.getByRole("button", { name: "Save" }).click()

    const edited = page.getByTestId("price-row").filter({ hasText: store })
    await expect(edited.getByTestId("price-amount")).toHaveText("$3.49")
    await expect(edited.getByTestId("price-unit-price")).toHaveText("$1.16/lb")
    // 3.49 for 3 lb: the cost per cup is recomputed by the tool, not the page
    await expect(edited.getByTestId("price-cost-per-cup")).toHaveText("$0.46")

    await edited
      .getByRole("button", { name: "Delete onions 3 lb bag 2.99" })
      .click()
    await expect(
      page.getByTestId("price-row").filter({ hasText: store }),
    ).toHaveCount(0)
  })
})
