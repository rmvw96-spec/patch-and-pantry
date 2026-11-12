import { expect, type Page } from "@playwright/test"

// Helpers for the food planner's pages. They need the backend in demo mode (SIM_MODEL=demo) with
// the committed reference data loaded (python -m app.seed all); CI does both.

export const RUN_TIMEOUT_MS = 30_000

export function uniqueName(prefix: string): string {
  return `${prefix} ${Math.random().toString(36).slice(2, 8)}`
}

/** Add a store on the Prices page; it becomes the chosen store. */
export async function addStore(page: Page, name = uniqueName("Test store")) {
  await page.getByLabel("Add a store").fill(name)
  await page.getByRole("button", { name: "Add store" }).click()
  await expect(
    page.getByLabel("Store", { exact: true }).locator("option:checked"),
  ).toHaveText(name)
  return name
}

/** Read a typed list at a new store and wait for the run to finish. */
export async function readList(page: Page, text: string, store?: string) {
  await page.goto("/prices")
  const name = await addStore(page, store)
  await page.getByLabel("Typed list").fill(text)
  await page.getByRole("button", { name: "Read list" }).click()
  await expect(page).toHaveURL(/\/prices\?id=/)
  // Wait for a final state first, so a failed run fails fast with a clear message.
  const status = page.getByTestId("run-status")
  await expect(status).toHaveText(/succeeded|failed/, {
    timeout: RUN_TIMEOUT_MS,
  })
  await expect(status).toHaveText("succeeded")
  return name
}

/** Read a list and save every draft, returning the store's name. */
export async function savePrices(page: Page, text: string): Promise<string> {
  const store = await readList(page, text)
  await page.getByRole("button", { name: /^Save \d+ prices?$/ }).click()
  await expect(page.getByTestId("capture-saved")).toBeVisible()
  return store
}
