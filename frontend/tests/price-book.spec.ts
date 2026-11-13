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

  test("Several prices can be selected and deleted together", async ({
    page,
  }) => {
    const store = await savePrices(
      page,
      "onions 3 lb bag 2.99\ntomatoes 2.49/lb\nonions 1.29/lb",
    )
    await page.goto("/price-book")
    const rows = page.getByTestId("price-row").filter({ hasText: store })
    await expect(rows).toHaveCount(3)
    const remove = page.getByRole("button", { name: /^Delete selected/ })
    await expect(remove).toBeDisabled()

    // Only this test's rows: other tests share the price book.
    await rows
      .filter({ hasText: "onions 3 lb bag 2.99" })
      .getByRole("checkbox")
      .check()
    await rows
      .filter({ hasText: "tomatoes 2.49/lb" })
      .getByRole("checkbox")
      .check()
    await expect(remove).toHaveText("Delete selected (2)")

    // Cancel keeps everything.
    await remove.click()
    await expect(page.getByRole("dialog")).toContainText("Delete 2 prices?")
    await page.getByRole("button", { name: "Cancel" }).click()
    await expect(rows).toHaveCount(3)

    await remove.click()
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Delete", exact: true })
      .click()
    await expect(rows).toHaveCount(1)
    await expect(rows).toContainText("onions 1.29/lb")
    await expect(remove).toHaveText("Delete selected (0)")
  })
})
