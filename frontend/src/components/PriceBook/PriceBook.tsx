import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Pencil, Trash2 } from "lucide-react"
import { useState } from "react"

import { type PricePublic, PricesService, VendorsService } from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import useCustomToast from "@/hooks/useCustomToast"
import { money, parseCents, unitPrice } from "@/hooks/useRun"
import { handleError } from "@/utils"

const selectClass =
  "border-input bg-transparent dark:bg-input/30 rounded-md border px-2 py-1 text-sm"

interface Draft {
  price: string
  quantity: string
  unit: string
  observedOn: string
  vendorId: string
}

function amount(price: PricePublic): string {
  return `${price.quantity}${price.size ? ` ${price.size}` : ""} ${price.unit}`
}

function EditRow({
  price,
  onDone,
}: {
  price: PricePublic
  onDone: () => void
}) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()
  const vendors = useQuery({
    queryKey: ["vendors"],
    queryFn: async () => (await VendorsService.readVendors()).data,
  })
  const [draft, setDraft] = useState<Draft>({
    price: (price.price_cents / 100).toFixed(2),
    quantity: String(price.quantity),
    unit: price.unit,
    observedOn: price.observed_on,
    vendorId: price.vendor_id ?? "",
  })
  const cents = parseCents(draft.price)
  const quantity = Number(draft.quantity)
  const valid = cents !== null && quantity > 0 && draft.vendorId !== ""

  const update = useMutation({
    mutationFn: async () =>
      (
        await PricesService.updateOnePrice({
          path: { id: price.id },
          body: {
            vendor_id: draft.vendorId,
            observed_on: draft.observedOn,
            quantity,
            unit: draft.unit,
            size: price.size ?? null,
            price_cents: cents ?? 0,
          },
        })
      ).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prices"] })
      showSuccessToast("Price updated")
      onDone()
    },
    onError: handleError.bind(showErrorToast),
  })

  return (
    <TableRow data-testid="price-edit-row">
      <TableCell>
        <Input
          type="date"
          aria-label="Date"
          value={draft.observedOn}
          onChange={(e) => setDraft({ ...draft, observedOn: e.target.value })}
        />
      </TableCell>
      <TableCell>
        <select
          className={selectClass}
          aria-label="Store"
          value={draft.vendorId}
          onChange={(e) => setDraft({ ...draft, vendorId: e.target.value })}
        >
          {(vendors.data ?? []).map((vendor) => (
            <option key={vendor.id} value={vendor.id}>
              {vendor.name}
            </option>
          ))}
        </select>
      </TableCell>
      <TableCell>{price.item_name}</TableCell>
      <TableCell>{price.raw_text}</TableCell>
      <TableCell>
        <div className="flex gap-1">
          <Input
            className="w-20"
            inputMode="decimal"
            aria-label="Quantity"
            value={draft.quantity}
            onChange={(e) => setDraft({ ...draft, quantity: e.target.value })}
          />
          <Input
            className="w-20"
            aria-label="Unit"
            value={draft.unit}
            onChange={(e) => setDraft({ ...draft, unit: e.target.value })}
          />
        </div>
      </TableCell>
      <TableCell colSpan={2}>
        {/* The total paid for the amount; the price of one unit is worked out from it. */}
        <Input
          className="w-24"
          inputMode="decimal"
          aria-label="Total paid"
          value={draft.price}
          onChange={(e) => setDraft({ ...draft, price: e.target.value })}
        />
        <p
          className="text-muted-foreground mt-1 text-xs"
          data-testid="edit-unit-price"
        >
          {cents !== null && quantity > 0
            ? `Total paid. ${unitPrice(cents, quantity, draft.unit)}`
            : "Total paid for the amount"}
        </p>
      </TableCell>
      <TableCell colSpan={2}>
        <div className="flex gap-2">
          <Button
            size="sm"
            disabled={!valid || update.isPending}
            onClick={() => update.mutate()}
          >
            Save
          </Button>
          <Button size="sm" variant="outline" onClick={onDone}>
            Cancel
          </Button>
        </div>
      </TableCell>
    </TableRow>
  )
}

const PriceBook = () => {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<string | null>(null)
  const prices = useQuery({
    queryKey: ["prices"],
    queryFn: async () => (await PricesService.readPrices()).data,
  })

  const remove = useMutation({
    mutationFn: async (id: string) =>
      (await PricesService.deletePrice({ path: { id } })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prices"] })
      showSuccessToast("Price deleted")
    },
    onError: handleError.bind(showErrorToast),
  })

  if (prices.isError) {
    return (
      <p className="text-destructive">
        Could not load your prices. Refresh the page to try again.
      </p>
    )
  }
  if (!prices.data) {
    return null
  }
  if (prices.data.length === 0) {
    return (
      <p className="text-muted-foreground" data-testid="price-book-empty">
        No prices yet. Type a list on the Prices page to add some.
      </p>
    )
  }

  return (
    <Table data-testid="price-book">
      <TableHeader>
        <TableRow>
          <TableHead>Date</TableHead>
          <TableHead>Store</TableHead>
          <TableHead>Item</TableHead>
          <TableHead>As typed</TableHead>
          <TableHead>Amount</TableHead>
          <TableHead>Total paid</TableHead>
          <TableHead>Unit price</TableHead>
          <TableHead>Per cup</TableHead>
          <TableHead>
            <span className="sr-only">Actions</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {prices.data.map((price) =>
          editing === price.id ? (
            <EditRow
              key={price.id}
              price={price}
              onDone={() => setEditing(null)}
            />
          ) : (
            <TableRow key={price.id} data-testid="price-row">
              <TableCell>{price.observed_on}</TableCell>
              <TableCell>{price.vendor_name ?? "-"}</TableCell>
              <TableCell>{price.item_name}</TableCell>
              <TableCell>{price.raw_text}</TableCell>
              <TableCell>
                {amount(price)}
                {price.weight_source && (
                  <p className="text-muted-foreground text-xs">
                    {price.weight_source} weight
                  </p>
                )}
              </TableCell>
              <TableCell data-testid="price-amount">
                {money(price.price_cents)}
              </TableCell>
              <TableCell data-testid="price-unit-price">
                {unitPrice(price.price_cents, price.quantity, price.unit) ??
                  "-"}
              </TableCell>
              <TableCell data-testid="price-cost-per-cup">
                {price.cost_per_cup_cents == null
                  ? (price.note ?? "-")
                  : money(price.cost_per_cup_cents)}
              </TableCell>
              <TableCell>
                <div className="flex gap-1">
                  <Button
                    size="icon"
                    variant="ghost"
                    aria-label={`Edit ${price.raw_text}`}
                    onClick={() => setEditing(price.id)}
                  >
                    <Pencil />
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    aria-label={`Delete ${price.raw_text}`}
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(price.id)}
                  >
                    <Trash2 />
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          ),
        )}
      </TableBody>
    </Table>
  )
}

export default PriceBook
