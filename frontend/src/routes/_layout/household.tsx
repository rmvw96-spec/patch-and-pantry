import { createFileRoute } from "@tanstack/react-router"

import HouseholdForm from "@/components/Household/HouseholdForm"

export const Route = createFileRoute("/_layout/household")({
  component: Household,
  head: () => ({
    meta: [
      {
        title: "Household - Patch & Pantry",
      },
    ],
  }),
})

function Household() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Household</h1>
        <p className="text-muted-foreground">
          Your weekly produce budget and the people you buy for
        </p>
      </div>
      <HouseholdForm />
    </div>
  )
}
