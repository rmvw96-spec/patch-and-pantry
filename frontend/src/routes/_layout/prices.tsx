import { useMutation } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { z } from "zod"

import { GroceryAnalysisRunsService, type PriceCaptureRequest } from "@/client"
import CaptureForm from "@/components/Prices/CaptureForm"
import CaptureStatus from "@/components/Prices/CaptureStatus"
import useCustomToast from "@/hooks/useCustomToast"
import { newIdempotencyKey } from "@/lib/idempotency"
import { handleError } from "@/utils"

// The current run lives in the URL (?id=...), so a refresh or a shared link shows the same run.
const searchSchema = z.object({
  id: z.string().optional().catch(undefined),
})

export const Route = createFileRoute("/_layout/prices")({
  component: Prices,
  validateSearch: searchSchema,
  head: () => ({
    meta: [
      {
        title: "Prices - Patch & Pantry",
      },
    ],
  }),
})

function Prices() {
  const { id } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const { showErrorToast } = useCustomToast()
  // After the user chooses an item for a line, reading the same list again matches it exactly.
  const reread = useMutation({
    mutationFn: async (input: PriceCaptureRequest) =>
      (
        await GroceryAnalysisRunsService.createGroceryAnalysisRun({
          body: input,
          headers: { "idempotency-key": newIdempotencyKey() },
        })
      ).data,
    onSuccess: (run) => navigate({ search: { id: run.id } }),
    onError: handleError.bind(showErrorToast),
  })

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Prices</h1>
        <p className="text-muted-foreground">
          Type the prices you saw, one item per line, and check what was read
          before anything is saved
        </p>
      </div>
      <CaptureForm
        onSubmitted={(run) => navigate({ search: { id: run.id } })}
      />
      {id && (
        <CaptureStatus
          key={id}
          runId={id}
          onReread={(input) => reread.mutate(input)}
        />
      )}
    </div>
  )
}
