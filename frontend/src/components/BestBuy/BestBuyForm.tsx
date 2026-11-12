import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Calculator, Plus, X } from "lucide-react"
import { useState } from "react"

import {
  CatalogService,
  type GroceryAnalysisRunPublic,
  GroceryAnalysisRunsService,
  HouseholdService,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { newIdempotencyKey } from "@/lib/idempotency"
import { handleError } from "@/utils"

const fieldClass =
  "border-input bg-transparent dark:bg-input/30 w-full rounded-md border px-3 py-2 text-base shadow-xs md:text-sm"

export interface NeedRow {
  catalogItemId: string
  name: string
  cupsPerWeek: number
}

interface BestBuyFormProps {
  needs: NeedRow[]
  onNeedsChange: (needs: NeedRow[]) => void
  onSubmitted: (run: GroceryAnalysisRunPublic) => void
}

const BestBuyForm = ({
  needs,
  onNeedsChange,
  onSubmitted,
}: BestBuyFormProps) => {
  const { showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()
  const [itemId, setItemId] = useState("")
  const [cups, setCups] = useState("")

  const catalog = useQuery({
    queryKey: ["catalog"],
    queryFn: async () => (await CatalogService.readCatalog()).data,
  })
  const settings = useQuery({
    queryKey: ["item-settings"],
    queryFn: async () => (await HouseholdService.readItemSettings()).data,
  })
  const freezes = new Set(
    (settings.data ?? [])
      .filter((s) => s.freeze_extras)
      .map((s) => s.catalog_item_id),
  )

  const setFreeze = useMutation({
    mutationFn: async ({ id, on }: { id: string; on: boolean }) =>
      (
        await HouseholdService.updateItemSetting({
          path: { catalog_item_id: id },
          body: { freeze_extras: on },
        })
      ).data,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["item-settings"] }),
    onError: handleError.bind(showErrorToast),
  })

  const run = useMutation({
    mutationFn: async () =>
      (
        await GroceryAnalysisRunsService.createGroceryAnalysisRun({
          body: {
            kind: "best_buy",
            needs: needs.map((n) => ({
              catalog_item_id: n.catalogItemId,
              cups_per_week: n.cupsPerWeek,
            })),
          },
          headers: { "idempotency-key": newIdempotencyKey() },
        })
      ).data,
    onSuccess: (created) => onSubmitted(created),
    onError: handleError.bind(showErrorToast),
  })

  const items = (catalog.data ?? []).filter(
    (item) => !needs.some((n) => n.catalogItemId === item.id),
  )
  const cupsNumber = Number(cups)
  const canAdd = itemId !== "" && cupsNumber > 0 && cupsNumber <= 1000

  function add() {
    const item = catalog.data?.find((i) => i.id === itemId)
    if (!item || !canAdd) return
    onNeedsChange([
      ...needs,
      { catalogItemId: item.id, name: item.name, cupsPerWeek: cupsNumber },
    ])
    setItemId("")
    setCups("")
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-[1fr_10rem_auto] sm:items-end">
        <div className="flex flex-col gap-2">
          <Label htmlFor="need-item">Item</Label>
          <select
            id="need-item"
            className={fieldClass}
            value={itemId}
            onChange={(e) => setItemId(e.target.value)}
          >
            <option value="">Choose an item</option>
            {items.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="need-cups">Cups per week</Label>
          <Input
            id="need-cups"
            inputMode="decimal"
            value={cups}
            onChange={(e) => setCups(e.target.value)}
          />
        </div>
        <Button
          type="button"
          variant="outline"
          disabled={!canAdd}
          onClick={add}
        >
          <Plus className="mr-2" />
          Add item
        </Button>
      </div>

      {needs.length > 0 && (
        <ul className="flex flex-col gap-2" data-testid="best-buy-needs">
          {needs.map((need) => (
            <li
              key={need.catalogItemId}
              className="flex flex-wrap items-center gap-4"
            >
              <span className="min-w-40 font-medium">{need.name}</span>
              <span>{need.cupsPerWeek} cups a week</span>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={freezes.has(need.catalogItemId)}
                  onChange={(e) =>
                    setFreeze.mutate({
                      id: need.catalogItemId,
                      on: e.target.checked,
                    })
                  }
                />
                Cook and freeze extras
              </label>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Remove ${need.name}`}
                onClick={() =>
                  onNeedsChange(
                    needs.filter((n) => n.catalogItemId !== need.catalogItemId),
                  )
                }
              >
                <X />
              </Button>
            </li>
          ))}
        </ul>
      )}

      <div>
        <LoadingButton
          type="button"
          loading={run.isPending}
          disabled={needs.length === 0}
          onClick={() => run.mutate()}
        >
          <Calculator className="mr-2" />
          Compare prices
        </LoadingButton>
      </div>
    </div>
  )
}

export default BestBuyForm
