import { expect, test } from "@playwright/test"

test("Household page is accessible from the sidebar", async ({ page }) => {
  await page.goto("/")
  await page.getByRole("link", { name: "Household" }).click()
  await expect(page).toHaveURL(/\/household/)
  await expect(page.getByRole("heading", { name: "Household" })).toBeVisible()
})

test("The budget and people are saved", async ({ page }) => {
  await page.goto("/household")
  await expect(page.getByLabel("Person 1 calorie level")).toBeVisible()

  await page.getByLabel("Weekly produce budget (dollars)").fill("45.50")
  await page.getByRole("button", { name: "Add person" }).click()
  await page.getByLabel("Person 2 calorie level").selectOption("1600")
  await page.getByRole("button", { name: "Save household" }).click()
  await expect(page.getByText("Household saved")).toBeVisible()

  await page.reload()
  await expect(page.getByLabel("Weekly produce budget (dollars)")).toHaveValue(
    "45.50",
  )
  await expect(page.getByLabel("Person 2 calorie level")).toHaveValue("1600")
})

test("A budget that is not an amount is refused", async ({ page }) => {
  await page.goto("/household")
  await page.getByLabel("Weekly produce budget (dollars)").fill("lots")
  await expect(
    page.getByText("Type an amount such as 45 or 45.50"),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Save household" }),
  ).toBeDisabled()
})
