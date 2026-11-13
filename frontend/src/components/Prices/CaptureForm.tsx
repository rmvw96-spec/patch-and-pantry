import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus, ScanText } from "lucide-react"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  type GroceryAnalysisRunPublic,
  GroceryAnalysisRunsService,
  VendorsService,
} from "@/client"
import { Button } from "@/components/ui/button"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { newIdempotencyKey } from "@/lib/idempotency"
import { handleError } from "@/utils"

// Matches the API's limits (app/grocery_analysis_jobs.py and app/agents/capture_agent.py).
const MAX_LIST_CHARACTERS = 20_000

const formSchema = z.object({
  vendorId: z.string().min(1, { message: "Choose a store" }),
  observedOn: z.string().min(1, { message: "Date is required" }),
  text: z
    .string()
    .trim()
    .min(1, { message: "Type at least one line" })
    .max(MAX_LIST_CHARACTERS, { message: "The list is too long" }),
})

type FormData = z.infer<typeof formSchema>

function today(): string {
  const now = new Date()
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 10)
}

const fieldClass =
  "border-input bg-transparent dark:bg-input/30 w-full rounded-md border px-3 py-2 text-base shadow-xs md:text-sm focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] outline-none"

interface CaptureFormProps {
  onSubmitted: (run: GroceryAnalysisRunPublic) => void
}

const CaptureForm = ({ onSubmitted }: CaptureFormProps) => {
  const { showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()
  const [newStore, setNewStore] = useState("")

  const vendors = useQuery({
    queryKey: ["vendors"],
    queryFn: async () => (await VendorsService.readVendors()).data,
  })

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onSubmit",
    defaultValues: { vendorId: "", observedOn: today(), text: "" },
  })

  const addStore = useMutation({
    mutationFn: async (name: string) =>
      (await VendorsService.createVendor({ body: { name } })).data,
    onSuccess: (vendor) => {
      queryClient.invalidateQueries({ queryKey: ["vendors"] })
      form.setValue("vendorId", vendor.id, { shouldValidate: true })
      setNewStore("")
    },
    onError: handleError.bind(showErrorToast),
  })

  const mutation = useMutation({
    // One idempotency key per submission: if the request is retried, the API returns the same
    // run instead of starting a duplicate job.
    mutationFn: async (data: FormData) =>
      (
        await GroceryAnalysisRunsService.createGroceryAnalysisRun({
          body: {
            kind: "price_capture",
            text: data.text,
            vendor_id: data.vendorId,
            observed_on: data.observedOn,
          },
          headers: { "idempotency-key": newIdempotencyKey() },
        })
      ).data,
    onSuccess: (run) => onSubmitted(run),
    onError: handleError.bind(showErrorToast),
  })

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit((data) => mutation.mutate(data))}
        className="flex flex-col gap-4"
      >
        <div className="grid gap-4 sm:grid-cols-[1fr_auto]">
          <FormField
            control={form.control}
            name="vendorId"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Store</FormLabel>
                <FormControl>
                  <select className={fieldClass} {...field}>
                    <option value="">Choose a store</option>
                    {(vendors.data ?? []).map((vendor) => (
                      <option key={vendor.id} value={vendor.id}>
                        {vendor.name}
                      </option>
                    ))}
                  </select>
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="observedOn"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Date</FormLabel>
                <FormControl>
                  <Input type="date" {...field} />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="new-store">Add a store</Label>
          <div className="flex gap-2">
            <Input
              id="new-store"
              placeholder="Store or market name"
              value={newStore}
              onChange={(event) => setNewStore(event.target.value)}
            />
            <Button
              type="button"
              variant="outline"
              disabled={newStore.trim() === "" || addStore.isPending}
              onClick={() => addStore.mutate(newStore.trim())}
            >
              <Plus className="mr-2" />
              Add store
            </Button>
          </div>
        </div>

        <FormField
          control={form.control}
          name="text"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Typed list</FormLabel>
              <FormControl>
                <textarea
                  rows={8}
                  className={fieldClass}
                  placeholder={
                    "onions 3 lb bag 2.99\ntomatoes 3 for $5\nkale bunch 2.50"
                  }
                  {...field}
                />
              </FormControl>
              <p className="text-muted-foreground text-sm">
                One item a line. Write the total you paid with the amount
                ("onions 3 lb bag 2.99"), or the price of one and how many
                ("tomatoes $0.29 each (3)"). For items sold by count, add small,
                medium, or large ("large oranges $1 each") and the weight is
                estimated for you; without a size, medium is used.
              </p>
              <FormMessage />
            </FormItem>
          )}
        />
        <div>
          <LoadingButton type="submit" loading={mutation.isPending}>
            <ScanText className="mr-2" />
            Read list
          </LoadingButton>
        </div>
      </form>
    </Form>
  )
}

export default CaptureForm
