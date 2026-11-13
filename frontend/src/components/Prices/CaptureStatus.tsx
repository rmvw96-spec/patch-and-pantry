import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Save, Undo2, X } from "lucide-react"
import { useState } from "react"

import {
  type CaptureQuestion,
  CatalogService,
  type DraftPrice,
  HouseholdService,
  type PriceCaptureRequest,
  type PriceCaptureResult,
  type PriceEntry,
  PricesService,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import useCustomToast from "@/hooks/useCustomToast"
import { isFinished, money, RUN_BADGE, unitPrice, useRun } from "@/hooks/useRun"
import { handleError } from "@/utils"

const selectClass =
  "border-input bg-transparent dark:bg-input/30 rounded-md border px-2 py-1 text-sm"

type WeightUnit = "lb" | "oz" | "g"
type WeightSource = "weighed" | "estimated"
type Size = "small" | "medium" | "large"

interface WeightAnswer {
  amount: string
  unit: WeightUnit
  source: WeightSource
}

function amount(draft: DraftPrice, size?: Size | null): string {
  const shown = size ?? draft.size
  return `${draft.quantity}${shown ? ` ${shown}` : ""} ${draft.unit}`
}

function toEntry(
  draft: DraftPrice,
  extra: Partial<PriceEntry> = {},
): PriceEntry {
  return {
    raw_text: draft.raw_text,
    catalog_item_id: draft.catalog_item_id,
    quantity: draft.quantity,
    unit: draft.unit,
    size: draft.size ?? null,
    price_cents: draft.price_cents,
    // A misspelling the user confirms becomes their own name for the item.
    remember_text: draft.match_kind === "fuzzy" ? draft.item_text : null,
    ...extra,
  }
}

/** A line that will not be saved, and why: said before saving and after (dogfooding). */
interface Unsaved {
  rawText: string
  reason: string
}

function UnsavedList({ lines, saved }: { lines: Unsaved[]; saved: boolean }) {
  if (lines.length === 0) return null
  return (
    <div className="text-sm" data-testid="capture-unsaved">
      <p className="font-medium">{saved ? "Not saved" : "Will not be saved"}</p>
      <ul className="text-muted-foreground ml-5 list-disc">
        {lines.map((line) => (
          <li key={line.rawText}>
            {line.rawText} ({line.reason})
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * The list without the lines the user set aside (dropped or unchecked), for reading it again.
 * Without this, every reread brought dropped lines back as questions (dogfooding).
 */
export function withoutLines(text: string, leftOut: Set<string>): string {
  return text
    .split(/\r?\n/)
    .filter((line) => !leftOut.has(line))
    .join("\n")
}

function DraftPriceCell({ draft }: { draft: DraftPrice }) {
  const each = unitPrice(draft.price_cents, draft.quantity, draft.unit)
  return (
    <>
      {money(draft.price_cents)}
      {(draft.price_note || (each && draft.quantity !== 1)) && (
        <p className="text-muted-foreground text-xs">
          {draft.price_note ?? each}
        </p>
      )}
    </>
  )
}

function WeightQuestion({
  question,
  answer,
  onChange,
}: {
  question: CaptureQuestion
  answer: WeightAnswer
  onChange: (answer: WeightAnswer) => void
}) {
  const label = question.raw_text
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Input
        className="w-24"
        inputMode="decimal"
        aria-label={`Weight of one piece for ${label}`}
        value={answer.amount}
        onChange={(e) => onChange({ ...answer, amount: e.target.value })}
      />
      <select
        className={selectClass}
        aria-label={`Weight unit for ${label}`}
        value={answer.unit}
        onChange={(e) =>
          onChange({ ...answer, unit: e.target.value as WeightUnit })
        }
      >
        <option value="lb">lb</option>
        <option value="oz">oz</option>
        <option value="g">g</option>
      </select>
      <select
        className={selectClass}
        aria-label={`Weighed or estimated for ${label}`}
        value={answer.source}
        onChange={(e) =>
          onChange({ ...answer, source: e.target.value as WeightSource })
        }
      >
        <option value="estimated">estimated</option>
        <option value="weighed">weighed</option>
      </select>
    </div>
  )
}

function CaptureReview({
  input,
  result,
  onReread,
}: {
  input: PriceCaptureRequest
  result: PriceCaptureResult
  onReread?: (leftOut: Set<string>) => void
}) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()
  const [skipped, setSkipped] = useState<Record<string, boolean>>({})
  const [weights, setWeights] = useState<Record<string, WeightAnswer>>({})
  const [sizes, setSizes] = useState<Record<string, Size>>({})
  const [choices, setChoices] = useState<Record<string, string>>({})
  const [dropped, setDropped] = useState<Record<string, boolean>>({})
  const [remembered, setRemembered] = useState(false)
  const [saved, setSaved] = useState<number | null>(null)
  const [notSaved, setNotSaved] = useState<Unsaved[]>([])

  const catalog = useQuery({
    queryKey: ["catalog"],
    queryFn: async () => (await CatalogService.readCatalog()).data,
  })

  const entries: PriceEntry[] = [
    ...result.drafts
      .filter((draft) => !skipped[draft.raw_text])
      .map((draft) => toEntry(draft)),
    ...result.questions.flatMap((question) => {
      const draft = question.draft
      if (!draft || dropped[question.raw_text]) return []
      if (question.kind === "needs_size" && sizes[question.raw_text]) {
        return [toEntry(draft, { size: sizes[question.raw_text] })]
      }
      const weight = weights[question.raw_text]
      const grams = weight ? Number(weight.amount) : Number.NaN
      if (question.kind === "needs_weight" && grams > 0) {
        return [
          toEntry(draft, {
            piece_weight: {
              amount: grams,
              unit: weight.unit,
              source: weight.source,
            },
          }),
        ]
      }
      return []
    }),
  ]

  const savedLines = new Set(entries.map((entry) => entry.raw_text))
  const unsaved: Unsaved[] = [
    ...result.drafts
      .filter((draft) => skipped[draft.raw_text])
      .map((draft) => ({ rawText: draft.raw_text, reason: "unchecked" })),
    ...result.questions
      .filter((question) => !savedLines.has(question.raw_text))
      .map((question) => ({
        rawText: question.raw_text,
        reason: dropped[question.raw_text]
          ? "you dropped it"
          : "question not answered",
      })),
  ]

  // Lines the user set aside: a reread leaves them out instead of asking about them again.
  const leftOut = new Set([
    ...result.drafts
      .filter((draft) => skipped[draft.raw_text])
      .map((draft) => draft.raw_text),
    ...result.questions
      .filter((question) => dropped[question.raw_text])
      .map((question) => question.raw_text),
  ])
  const nothingLeft = withoutLines(input.text, leftOut).trim() === ""

  const save = useMutation({
    mutationFn: async () =>
      (
        await PricesService.createPrices({
          body: {
            vendor_id: input.vendor_id,
            observed_on: input.observed_on,
            entries,
          },
        })
      ).data,
    onSuccess: (prices) => {
      setNotSaved(unsaved)
      setSaved(prices.length)
      queryClient.invalidateQueries({ queryKey: ["prices"] })
      showSuccessToast(`Saved ${prices.length} prices`)
    },
    onError: handleError.bind(showErrorToast),
  })

  const remember = useMutation({
    mutationFn: async (question: CaptureQuestion) =>
      (
        await HouseholdService.createAlias({
          body: {
            text: question.item_text ?? question.raw_text,
            catalog_item_id: choices[question.raw_text],
          },
        })
      ).data,
    onSuccess: () => {
      setRemembered(true)
      showSuccessToast("Saved. Read the list again to use it.")
    },
    onError: handleError.bind(showErrorToast),
  })

  if (saved !== null) {
    return (
      <div className="flex flex-col gap-3">
        <p data-testid="capture-saved">
          Saved {saved} {saved === 1 ? "price" : "prices"} to your{" "}
          <Link to="/price-book" className="underline">
            price book
          </Link>
          .
        </p>
        <UnsavedList lines={notSaved} saved />
      </div>
    )
  }

  const items = catalog.data ?? []

  return (
    <div className="flex flex-col gap-6">
      {result.drafts.length > 0 && (
        <Table data-testid="capture-drafts">
          <TableHeader>
            <TableRow>
              <TableHead>Save</TableHead>
              <TableHead>As typed</TableHead>
              <TableHead>Item</TableHead>
              <TableHead>Amount</TableHead>
              <TableHead>Price</TableHead>
              <TableHead>Per cup</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {result.drafts.map((draft) => (
              <TableRow key={draft.raw_text} data-testid="capture-draft">
                <TableCell>
                  <input
                    type="checkbox"
                    aria-label={`Save ${draft.raw_text}`}
                    checked={!skipped[draft.raw_text]}
                    onChange={(e) =>
                      setSkipped({
                        ...skipped,
                        [draft.raw_text]: !e.target.checked,
                      })
                    }
                  />
                </TableCell>
                <TableCell>{draft.raw_text}</TableCell>
                <TableCell>
                  {draft.item_name}
                  {draft.match_note && (
                    <p className="text-muted-foreground text-xs">
                      {draft.match_note}
                    </p>
                  )}
                </TableCell>
                <TableCell>
                  {amount(draft)}
                  {draft.size_assumed && (
                    <p
                      className="text-muted-foreground text-xs"
                      data-testid="draft-size-assumed"
                    >
                      Medium assumed. Write small or large on the line to change
                      it.
                    </p>
                  )}
                </TableCell>
                <TableCell data-testid="draft-price">
                  <DraftPriceCell draft={draft} />
                </TableCell>
                <TableCell data-testid="draft-cost-per-cup">
                  {draft.cost_per_cup_cents == null
                    ? (draft.note ?? "-")
                    : money(draft.cost_per_cup_cents)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {result.questions.length > 0 && (
        <div className="flex flex-col gap-3">
          <h3 className="font-medium">Questions</h3>
          <ul className="flex flex-col gap-4" data-testid="capture-questions">
            {result.questions.map((question) => (
              <li
                key={question.raw_text}
                className="flex flex-col gap-2"
                data-testid="capture-question"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <p
                    className={dropped[question.raw_text] ? "line-through" : ""}
                  >
                    <span className="text-muted-foreground">
                      {question.raw_text}:
                    </span>{" "}
                    {question.message}
                  </p>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`${dropped[question.raw_text] ? "Keep" : "Drop"} ${question.raw_text}`}
                    onClick={() =>
                      setDropped({
                        ...dropped,
                        [question.raw_text]: !dropped[question.raw_text],
                      })
                    }
                  >
                    {dropped[question.raw_text] ? (
                      <>
                        <Undo2 className="mr-1" />
                        Keep this line
                      </>
                    ) : (
                      <>
                        <X className="mr-1" />
                        Drop this line
                      </>
                    )}
                  </Button>
                </div>
                {dropped[question.raw_text] ? null : (
                  <>
                    {question.kind === "needs_weight" && question.draft && (
                      <WeightQuestion
                        question={question}
                        answer={
                          weights[question.raw_text] ?? {
                            amount: "",
                            unit: "lb",
                            source: "estimated",
                          }
                        }
                        onChange={(answer) =>
                          setWeights({
                            ...weights,
                            [question.raw_text]: answer,
                          })
                        }
                      />
                    )}
                    {question.kind === "needs_size" && question.draft && (
                      <select
                        className={selectClass}
                        aria-label={`Size for ${question.raw_text}`}
                        value={sizes[question.raw_text] ?? ""}
                        onChange={(e) =>
                          setSizes({
                            ...sizes,
                            [question.raw_text]: e.target.value as Size,
                          })
                        }
                      >
                        <option value="">Choose a size</option>
                        {(question.options ?? []).map((size) => (
                          <option key={size} value={size}>
                            {size}
                          </option>
                        ))}
                      </select>
                    )}
                    {(question.kind === "which_item" ||
                      question.kind === "no_match") && (
                      <div className="flex flex-wrap items-center gap-2">
                        <select
                          className={selectClass}
                          aria-label={`Item for ${question.raw_text}`}
                          value={choices[question.raw_text] ?? ""}
                          onChange={(e) =>
                            setChoices({
                              ...choices,
                              [question.raw_text]: e.target.value,
                            })
                          }
                        >
                          <option value="">Choose an item</option>
                          {/* The question's candidates first, then the whole catalog. */}
                          {[
                            ...items.filter((i) =>
                              question.options?.includes(i.name),
                            ),
                            ...items.filter(
                              (i) => !question.options?.includes(i.name),
                            ),
                          ].map((item) => (
                            <option key={item.id} value={item.id}>
                              {item.name}
                            </option>
                          ))}
                        </select>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={
                            !choices[question.raw_text] || remember.isPending
                          }
                          onClick={() => remember.mutate(question)}
                        >
                          Use this item
                        </Button>
                      </div>
                    )}
                  </>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.ignored.length > 0 && (
        <p
          className="text-muted-foreground text-sm"
          data-testid="capture-ignored"
        >
          Not prices: {result.ignored.join(", ")}
        </p>
      )}

      <UnsavedList lines={unsaved} saved={false} />

      <div className="flex flex-wrap gap-2">
        <LoadingButton
          type="button"
          loading={save.isPending}
          disabled={entries.length === 0}
          onClick={() => save.mutate()}
        >
          <Save className="mr-2" />
          Save {entries.length} {entries.length === 1 ? "price" : "prices"}
        </LoadingButton>
        {remembered && onReread && !nothingLeft && (
          <Button
            type="button"
            variant="outline"
            onClick={() => onReread(leftOut)}
          >
            Read the list again
          </Button>
        )}
      </div>
      {remembered && onReread && leftOut.size > 0 && (
        <p className="text-muted-foreground text-sm">
          Reading the list again leaves out the {leftOut.size}{" "}
          {leftOut.size === 1 ? "line" : "lines"} you dropped or unchecked.
        </p>
      )}
    </div>
  )
}

const CaptureStatus = ({
  runId,
  onReread,
}: {
  runId: string
  onReread?: (input: PriceCaptureRequest) => void
}) => {
  const { data: run, isError } = useRun(runId)

  if (isError) {
    return (
      <p className="text-destructive" data-testid="run-load-error">
        Could not load this list. Refresh the page to try again.
      </p>
    )
  }

  if (!run) {
    return null
  }

  const input = run.input.kind === "price_capture" ? run.input : null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-3">
          What was read
          {/* aria-live announces status changes to screen readers as polling updates them */}
          <span aria-live="polite">
            <Badge variant={RUN_BADGE[run.status]} data-testid="run-status">
              {run.status}
            </Badge>
          </span>
        </CardTitle>
        <CardDescription
          className="whitespace-pre-line"
          data-testid="run-input"
        >
          {input?.text}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!isFinished(run) && (
          <p className="text-muted-foreground">
            Reading the list. This updates automatically.
          </p>
        )}

        {run.status === "succeeded" &&
          input &&
          run.result?.kind === "price_capture" && (
            <CaptureReview
              input={input}
              result={run.result}
              onReread={
                onReread
                  ? (leftOut) =>
                      onReread({
                        ...input,
                        text: withoutLines(input.text, leftOut),
                      })
                  : undefined
              }
            />
          )}

        {run.status === "failed" && (
          <p className="text-destructive" data-testid="run-error">
            {run.error_message ?? "The run failed."}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

export default CaptureStatus
