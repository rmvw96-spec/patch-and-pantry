import { expect, type Page, test } from "@playwright/test"

// These tests drive the full stack: browser -> API -> background job -> Prefect flow -> database.
// They need the backend in demo mode (SIM_MODEL=demo), which gives deterministic results without
// an LLM. CI sets this in .github/workflows/playwright.yml.
const DEMO_HINT =
  "Study did not succeed. Is the backend running with SIM_MODEL=demo?"

// The first study on a fresh stack waits for Prefect to start its temporary server (~10 s).
const STUDY_TIMEOUT_MS = 60_000

async function runStudy(page: Page, prompt: string) {
  await page.goto("/studies")
  await page.getByLabel("Study prompt").fill(prompt)
  await page.getByRole("button", { name: "Run study" }).click()
  await expect(page).toHaveURL(/\/studies\?id=/)
  // Wait for a final state first, so a failed study fails fast with a clear message.
  const status = page.getByTestId("study-status")
  await expect(status).toHaveText(/succeeded|failed/, {
    timeout: STUDY_TIMEOUT_MS,
  })
  return status
}

test("Studies page is accessible and shows correct title", async ({ page }) => {
  await page.goto("/studies")
  await expect(page.getByRole("heading", { name: "Studies" })).toBeVisible()
  await expect(page.getByRole("button", { name: "Run study" })).toBeVisible()
})

test("Studies link in the sidebar opens the page", async ({ page }) => {
  await page.goto("/")
  await page.getByRole("link", { name: "Studies" }).click()
  await expect(page).toHaveURL(/\/studies/)
  await expect(page.getByRole("heading", { name: "Studies" })).toBeVisible()
})

test("Empty prompt shows a validation error", async ({ page }) => {
  await page.goto("/studies")
  await page.getByRole("button", { name: "Run study" }).click()
  await expect(page.getByText("Prompt is required")).toBeVisible()
  await expect(page).not.toHaveURL(/\?id=/) // nothing was submitted
})

test.describe("Running studies", () => {
  test.slow() // studies run real background jobs, so allow extra time

  test("Feasible study shows its result", async ({ page }) => {
    const status = await runStudy(page, "Optimize case B at 70 bar")

    await expect(status, DEMO_HINT).toHaveText("succeeded")
    // sqrt(70^2 - 2000) = 53.852 bar, computed by the simulator inside Prefect
    await expect(page.getByTestId("study-objective")).toHaveText("53.852 bar")
    await expect(page.getByTestId("study-feasible")).toHaveText("Yes")
  })

  test("Infeasible study is reported as not feasible", async ({ page }) => {
    const status = await runStudy(page, "Optimize case B at 40 bar")

    await expect(status, DEMO_HINT).toHaveText("succeeded")
    await expect(page.getByTestId("study-objective")).toHaveText("0 bar")
    await expect(page.getByTestId("study-feasible")).toHaveText("No")
  })

  test("Study result survives a page refresh", async ({ page }) => {
    const status = await runStudy(page, "Optimize case A")
    await expect(status, DEMO_HINT).toHaveText("succeeded")

    await page.reload()

    await expect(page.getByTestId("study-status")).toHaveText("succeeded")
    await expect(page.getByTestId("study-objective")).toHaveText("50 bar")
    await expect(page.getByTestId("study-prompt")).toHaveText("Optimize case A")
  })
})
