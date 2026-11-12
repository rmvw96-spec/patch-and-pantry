import { useQuery } from "@tanstack/react-query"

import {
  type GroceryAnalysisRunPublic,
  GroceryAnalysisRunsService,
} from "@/client"

type RunState = GroceryAnalysisRunPublic["status"]

const FINISHED: RunState[] = ["succeeded", "failed"]
const POLL_INTERVAL_MS = 1000

export function isFinished(run: GroceryAnalysisRunPublic | undefined): boolean {
  return run !== undefined && FINISHED.includes(run.status)
}

export const RUN_BADGE: Record<
  RunState,
  "default" | "secondary" | "destructive"
> = {
  queued: "secondary",
  running: "secondary",
  succeeded: "default",
  failed: "destructive",
}

/** A grocery analysis run, polled while it is queued or running. */
export function useRun(runId: string) {
  return useQuery({
    queryKey: ["grocery-analysis-run", runId],
    queryFn: async () =>
      (
        await GroceryAnalysisRunsService.readGroceryAnalysisRun({
          path: { id: runId },
        })
      ).data,
    // Poll while the job is queued or running; stop as soon as it reaches a final state.
    refetchInterval: (query) =>
      isFinished(query.state.data) ? false : POLL_INTERVAL_MS,
  })
}

/** Cents as dollars for display ("$2.99"); "-" when there is no amount. */
export function money(cents: number | null | undefined): string {
  return cents == null ? "-" : `$${(cents / 100).toFixed(2)}`
}

/** A typed price ("2.99" or "$2.99") as integer cents; null if it is not a plain price. */
export function parseCents(text: string): number | null {
  const match = text.trim().match(/^\$?(\d+(?:\.\d{1,2})?|\.\d{1,2})$/)
  return match ? Math.round(Number(match[1]) * 100) : null
}

/** The price of one unit, from a total and the amount it bought: "$0.29 each", "$2.49/lb". */
export function unitPrice(
  totalCents: number,
  quantity: number,
  unit: string,
): string | null {
  if (!(quantity > 0)) return null
  const per = money(totalCents / quantity)
  return unit === "each" ? `${per} each` : `${per}/${unit}`
}
