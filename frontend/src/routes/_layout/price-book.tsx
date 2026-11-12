import { createFileRoute } from "@tanstack/react-router"

import PriceBook from "@/components/PriceBook/PriceBook"

export const Route = createFileRoute("/_layout/price-book")({
  component: PriceBookPage,
  head: () => ({
    meta: [
      {
        title: "Price book - Patch & Pantry",
      },
    ],
  }),
})

function PriceBookPage() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Price book</h1>
        <p className="text-muted-foreground">
          The prices you saved, newest first, with each one's cost per cup
        </p>
      </div>
      <PriceBook />
    </div>
  )
}
