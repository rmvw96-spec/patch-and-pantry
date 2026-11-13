import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { z } from "zod"

import CaptureForm from "@/components/Prices/CaptureForm"
import CaptureStatus from "@/components/Prices/CaptureStatus"

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
      {id && <CaptureStatus key={id} runId={id} />}
    </div>
  )
}
