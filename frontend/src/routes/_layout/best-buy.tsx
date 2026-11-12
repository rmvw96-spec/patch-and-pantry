import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useState } from "react"
import { z } from "zod"

import BestBuyForm, { type NeedRow } from "@/components/BestBuy/BestBuyForm"
import BestBuyStatus from "@/components/BestBuy/BestBuyStatus"

// The current comparison lives in the URL (?id=...), so a refresh or a shared link shows it.
const searchSchema = z.object({
  id: z.string().optional().catch(undefined),
})

export const Route = createFileRoute("/_layout/best-buy")({
  component: BestBuy,
  validateSearch: searchSchema,
  head: () => ({
    meta: [
      {
        title: "Best buy - Patch & Pantry",
      },
    ],
  }),
})

function BestBuy() {
  const { id } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const [needs, setNeeds] = useState<NeedRow[]>([])

  return (
    <div className="flex flex-col gap-6">
      <div className="print:hidden">
        <h1 className="text-2xl font-bold tracking-tight">Best buy</h1>
        <p className="text-muted-foreground">
          Choose what you need this week; see the cheapest way to buy it per
          usable cup, counting what would spoil
        </p>
      </div>
      <div className="print:hidden">
        <BestBuyForm
          needs={needs}
          onNeedsChange={setNeeds}
          onSubmitted={(run) => navigate({ search: { id: run.id } })}
        />
      </div>
      {id && <BestBuyStatus key={id} runId={id} />}
    </div>
  )
}
