import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus, Save, X } from "lucide-react"
import { useEffect, useState } from "react"

import { HouseholdService, type HouseholdUpdate } from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { parseCents } from "@/hooks/useRun"
import { handleError } from "@/utils"

// The Dietary Guidelines calorie levels for the USDA food patterns (app/models.py CalorieLevel).
const CALORIE_LEVELS = [
  1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400, 2600, 2800, 3000, 3200,
] as const
type CalorieLevel = (typeof CALORIE_LEVELS)[number]
const MAX_MEMBERS = 20

const selectClass =
  "border-input bg-transparent dark:bg-input/30 rounded-md border px-3 py-2 text-sm"

const HouseholdForm = () => {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()
  const household = useQuery({
    queryKey: ["household"],
    queryFn: async () => (await HouseholdService.readHousehold()).data,
  })
  const [budget, setBudget] = useState("")
  const [members, setMembers] = useState<CalorieLevel[]>([])

  // Fill the form once the household loads (it is created on first read).
  useEffect(() => {
    if (!household.data) return
    const cents = household.data.produce_budget_cents_per_week
    setBudget(cents == null ? "" : (cents / 100).toFixed(2))
    setMembers(
      household.data.members.map((m) => m.calorie_level as CalorieLevel),
    )
  }, [household.data])

  const budgetCents = budget.trim() === "" ? null : parseCents(budget)
  const budgetValid = budget.trim() === "" || budgetCents !== null

  const save = useMutation({
    mutationFn: async (body: HouseholdUpdate) =>
      (await HouseholdService.updateHousehold({ body })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["household"] })
      showSuccessToast("Household saved")
    },
    onError: handleError.bind(showErrorToast),
  })

  if (!household.data) {
    return null
  }

  return (
    <form
      className="flex max-w-xl flex-col gap-6"
      onSubmit={(event) => {
        event.preventDefault()
        if (!budgetValid || members.length === 0) return
        save.mutate({
          produce_budget_cents_per_week: budgetCents,
          members,
        })
      }}
    >
      <div className="flex flex-col gap-2">
        <Label htmlFor="budget">Weekly produce budget (dollars)</Label>
        <Input
          id="budget"
          inputMode="decimal"
          placeholder="Optional"
          value={budget}
          onChange={(e) => setBudget(e.target.value)}
        />
        {!budgetValid && (
          <p className="text-destructive text-sm">
            Type an amount such as 45 or 45.50
          </p>
        )}
      </div>

      <fieldset className="flex flex-col gap-3">
        <legend className="mb-2 font-medium">People</legend>
        <p className="text-muted-foreground text-sm">
          Each person is only a calorie level (2,000 if you are not sure). No
          names or other details are kept.
        </p>
        {members.map((level, index) => (
          // Members have no identity beyond their position in the list.
          <div key={index} className="flex items-center gap-2">
            <select
              className={selectClass}
              aria-label={`Person ${index + 1} calorie level`}
              value={level}
              onChange={(e) => {
                const next = [...members]
                next[index] = Number(e.target.value) as CalorieLevel
                setMembers(next)
              }}
            >
              {CALORIE_LEVELS.map((option) => (
                <option key={option} value={option}>
                  {option.toLocaleString()} calories
                </option>
              ))}
            </select>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={`Remove person ${index + 1}`}
              disabled={members.length === 1}
              onClick={() => setMembers(members.filter((_, i) => i !== index))}
            >
              <X />
            </Button>
          </div>
        ))}
        <div>
          <Button
            type="button"
            variant="outline"
            disabled={members.length >= MAX_MEMBERS}
            onClick={() => setMembers([...members, 2000])}
          >
            <Plus className="mr-2" />
            Add person
          </Button>
        </div>
      </fieldset>

      <div>
        <LoadingButton
          type="submit"
          loading={save.isPending}
          disabled={!budgetValid || members.length === 0}
        >
          <Save className="mr-2" />
          Save household
        </LoadingButton>
      </div>
    </form>
  )
}

export default HouseholdForm
