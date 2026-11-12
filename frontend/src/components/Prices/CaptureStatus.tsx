import { useQuery } from "@tanstack/react-query"

import {
  type DraftPrice,
  type GroceryAnalysisRunPublic,
  GroceryAnalysisRunsService,
  type PriceCaptureResult,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

type RunState = GroceryAnalysisRunPublic["status"]

const FINISHED: RunState[] = ["succeeded", "failed"]
const POLL_INTERVAL_MS = 1000

const BADGE_VARIANT: Record<RunState, "default" | "secondary" | "destructive"> =
  {
    queued: "secondary",
    running: "secondary",
    succeeded: "default",
    failed: "destructive",
  }

function isFinished(run: GroceryAnalysisRunPublic | undefined): boolean {
  return run !== undefined && FINISHED.includes(run.status)
}

function money(cents: number | null | undefined): string {
  return cents == null ? "-" : `$${(cents / 100).toFixed(2)}`
}

function amount(draft: DraftPrice): string {
  const size = draft.size ? ` ${draft.size}` : ""
  return `${draft.quantity}${size} ${draft.unit}`
}

function Drafts({ result }: { result: PriceCaptureResult }) {
  return (
    <div className="flex flex-col gap-6">
      {result.drafts.length > 0 && (
        <Table data-testid="capture-drafts">
          <TableHeader>
            <TableRow>
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
                <TableCell>{draft.raw_text}</TableCell>
                <TableCell>
                  {draft.item_name}
                  {draft.match_note && (
                    <p className="text-muted-foreground text-xs">
                      {draft.match_note}
                    </p>
                  )}
                </TableCell>
                <TableCell>{amount(draft)}</TableCell>
                <TableCell>{money(draft.price_cents)}</TableCell>
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
        <div className="flex flex-col gap-2">
          <h3 className="font-medium">Questions</h3>
          <ul className="flex flex-col gap-2" data-testid="capture-questions">
            {result.questions.map((question) => (
              <li key={question.raw_text} data-testid="capture-question">
                <span className="text-muted-foreground">
                  {question.raw_text}:
                </span>{" "}
                {question.message}
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
    </div>
  )
}

const CaptureStatus = ({ runId }: { runId: string }) => {
  const { data: run, isError } = useQuery({
    queryKey: ["grocery-analysis-run", runId],
    queryFn: async () =>
      (
        await GroceryAnalysisRunsService.readGroceryAnalysisRun({
          path: { id: runId },
        })
      ).data,
    // Poll while the job is queued or running; stop as soon as it reaches a final state.
    refetchInterval: (query) =>
      isFinished(query.state.data) ? false : POLL_INTERVAL_MS,
  })

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

  const text = run.input.kind === "price_capture" ? run.input.text : ""

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-3">
          What was read
          {/* aria-live announces status changes to screen readers as polling updates them */}
          <span aria-live="polite">
            <Badge variant={BADGE_VARIANT[run.status]} data-testid="run-status">
              {run.status}
            </Badge>
          </span>
        </CardTitle>
        <CardDescription
          className="whitespace-pre-line"
          data-testid="run-input"
        >
          {text}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!isFinished(run) && (
          <p className="text-muted-foreground">
            Reading the list. This updates automatically.
          </p>
        )}

        {run.status === "succeeded" &&
          run.result &&
          run.result.kind === "price_capture" && <Drafts result={run.result} />}

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
