import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Save, Undo2, X } from "lucide-react"
import { useState } from "react"

import {
  type CaptureQuestion,
  CatalogService,
  type DraftPrice,
  GroceryAnalysisRunsService,
  type PriceCaptureRequest,
  type PriceCaptureResult,
  type PriceEntry,
  PricesService,
  type QuestionAnswer,
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

const NO_WEIGHT: WeightAnswer = { amount: "", unit: "lb", source: "estimated" }

function amount(draft: DraftPrice): string {
  return `${draft.quantity}${draft.size ? ` ${draft.size}` : ""} ${draft.unit}`
}

function toEntry(draft: DraftPrice): PriceEntry {
  return {
    raw_text: draft.raw_text,
    catalog_item_id: draft.catalog_item_id,
    quantity: draft.quantity,
    unit: draft.unit,
    size: draft.size ?? null,
    price_cents: draft.price_cents,
    // The weight the user gave for one piece, when the catalog has none.
    piece_weight: draft.piece_weight ?? null,
    // A misspelling the user confirms becomes their own name for the item.
    remember_text: draft.match_kind === "fuzzy" ? draft.item_text : null,
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
  onAnswer,
  pending,
}: {
  question: CaptureQuestion
  answer: WeightAnswer
  onChange: (answer: WeightAnswer) => void
  onAnswer: () => void
  pending: boolean
}) {
  const label = question.raw_text
  const grams = Number(answer.amount)
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
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={!(grams > 0) || pending}
        onClick={onAnswer}
      >
        Use this weight
      </Button>
    </div>
  )
}

function CaptureReview({
  runId,
  input,
  result,
}: {
  runId: string
  input: PriceCaptureRequest
  result: PriceCaptureResult
}) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()
  const [skipped, setSkipped] = useState<Record<string, boolean>>({})
  const [dropped, setDropped] = useState<Record<string, boolean>>({})
  // What the user has typed or picked for a question, before sending it.
  const [weights, setWeights] = useState<Record<string, WeightAnswer>>({})
  const [choices, setChoices] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState<number | null>(null)
  // Lines already saved for this store and day: saving updated them (one price per line).
  const [updated, setUpdated] = useState(0)
  const [notSaved, setNotSaved] = useState<Unsaved[]>([])

  const catalog = useQuery({
    queryKey: ["catalog"],
    queryFn: async () => (await CatalogService.readCatalog()).data,
  })

  // An answer completes its line on the server, in code (no model call): the question becomes
  // a draft in the table, or the next question. The run comes back with the change saved.
  const answer = useMutation({
    mutationFn: async (body: QuestionAnswer) =>
      (
        await GroceryAnalysisRunsService.answerQuestion({
          path: { id: runId },
          body,
        })
      ).data,
    onSuccess: (run) => {
      queryClient.setQueryData(["grocery-analysis-run", runId], run)
    },
    onError: handleError.bind(showErrorToast),
  })

  const entries: PriceEntry[] = result.drafts
    .filter((draft) => !skipped[draft.raw_text])
    .map(toEntry)

  const unsaved: Unsaved[] = [
    ...result.drafts
      .filter((draft) => skipped[draft.raw_text])
      .map((draft) => ({ rawText: draft.raw_text, reason: "unchecked" })),
    ...result.questions.map((question) => ({
      rawText: question.raw_text,
      reason: dropped[question.raw_text]
        ? "you dropped it"
        : "question not answered",
    })),
  ]

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
      setUpdated(prices.filter((price) => price.replaced).length)
      queryClient.invalidateQueries({ queryKey: ["prices"] })
      showSuccessToast(`Saved ${prices.length} prices`)
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
          {updated > 0 &&
            ` ${updated} ${updated === 1 ? "was" : "were"} already saved for this store and day, so ${updated === 1 ? "it was" : "they were"} updated.`}
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
                  {draft.piece_weight && (
                    <p className="text-muted-foreground text-xs">
                      {draft.piece_source}
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
          <p className="text-muted-foreground text-sm">
            Answer a question and its line moves into the table above.
          </p>
          <ul className="flex flex-col gap-4" data-testid="capture-questions">
            {result.questions.map((question) => {
              const line = question.raw_text
              const send = (body: Omit<QuestionAnswer, "raw_text">) =>
                answer.mutate({ raw_text: line, ...body })
              return (
                <li
                  key={line}
                  className="flex flex-col gap-2"
                  data-testid="capture-question"
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <p className={dropped[line] ? "line-through" : ""}>
                      <span className="text-muted-foreground">{line}:</span>{" "}
                      {question.message}
                    </p>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      aria-label={`${dropped[line] ? "Keep" : "Drop"} ${line}`}
                      onClick={() =>
                        setDropped({ ...dropped, [line]: !dropped[line] })
                      }
                    >
                      {dropped[line] ? (
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
                  {dropped[line] ? null : (
                    <>
                      {question.kind === "needs_weight" && (
                        <WeightQuestion
                          question={question}
                          answer={weights[line] ?? NO_WEIGHT}
                          onChange={(value) =>
                            setWeights({ ...weights, [line]: value })
                          }
                          pending={answer.isPending}
                          onAnswer={() => {
                            const weight = weights[line] ?? NO_WEIGHT
                            send({
                              piece_weight: {
                                amount: Number(weight.amount),
                                unit: weight.unit,
                                source: weight.source,
                              },
                            })
                          }}
                        />
                      )}
                      {question.kind === "needs_size" && (
                        <select
                          className={selectClass}
                          aria-label={`Size for ${line}`}
                          value=""
                          disabled={answer.isPending}
                          onChange={(e) =>
                            send({ size: e.target.value as Size })
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
                      {question.kind === "missing_unit" && (
                        <div className="flex flex-wrap items-center gap-2">
                          <select
                            className={selectClass}
                            aria-label={`Unit for ${line}`}
                            value={choices[line] ?? ""}
                            onChange={(e) =>
                              setChoices({
                                ...choices,
                                [line]: e.target.value,
                              })
                            }
                          >
                            <option value="">Choose a unit</option>
                            {(question.options ?? []).map((unit) => (
                              <option key={unit} value={unit}>
                                {unit === "lb" ? "a pound (lb)" : `one ${unit}`}
                              </option>
                            ))}
                          </select>
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            disabled={!choices[line] || answer.isPending}
                            onClick={() => send({ unit: choices[line] })}
                          >
                            Use this unit
                          </Button>
                        </div>
                      )}
                      {(question.kind === "which_item" ||
                        question.kind === "no_match") && (
                        <div className="flex flex-wrap items-center gap-2">
                          <select
                            className={selectClass}
                            aria-label={`Item for ${line}`}
                            value={choices[line] ?? ""}
                            onChange={(e) =>
                              setChoices({
                                ...choices,
                                [line]: e.target.value,
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
                            disabled={!choices[line] || answer.isPending}
                            onClick={() =>
                              send({ catalog_item_id: choices[line] })
                            }
                          >
                            Use this item
                          </Button>
                        </div>
                      )}
                    </>
                  )}
                </li>
              )
            })}
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
      </div>
    </div>
  )
}

const CaptureStatus = ({ runId }: { runId: string }) => {
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
            <CaptureReview runId={run.id} input={input} result={run.result} />
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
