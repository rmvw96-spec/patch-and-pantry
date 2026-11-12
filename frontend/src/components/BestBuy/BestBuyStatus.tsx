import { Printer, Star } from "lucide-react"

import type { BestBuyResult, BestBuyRow } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
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
import { isFinished, money, RUN_BADGE, useRun } from "@/hooks/useRun"

function cups(value: number | null | undefined): string {
  return value == null ? "-" : value.toFixed(2)
}

function percent(share: number | null | undefined): string {
  return share == null ? "-" : `${Math.round(share * 100)}%`
}

function TradeOffTable({ rows }: { rows: BestBuyRow[] }) {
  return (
    <Table data-testid="best-buy-table">
      <TableHeader>
        <TableRow>
          <TableHead>Item</TableHead>
          <TableHead>Where</TableHead>
          <TableHead>Price</TableHead>
          <TableHead>Cups bought</TableHead>
          <TableHead>Usable</TableHead>
          <TableHead>Spoils</TableHead>
          <TableHead>Per usable cup</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row, index) => (
          <TableRow
            // Rows have no id of their own; their order is the table's.
            key={index}
            data-testid={row.marked ? "best-buy-marked" : "best-buy-row"}
            className={row.marked ? "font-medium" : undefined}
          >
            <TableCell>
              {row.marked && (
                <Star className="mr-1 inline size-4" aria-label="Best buy" />
              )}
              {row.item_name}
            </TableCell>
            <TableCell>
              {row.label}
              {(row.notes ?? []).length > 0 && (
                <p className="text-muted-foreground text-xs font-normal">
                  {(row.notes ?? []).join(" ")}
                </p>
              )}
            </TableCell>
            <TableCell>
              {row.price_cents == null
                ? "-"
                : `${money(row.price_cents)} for ${row.quantity} ${row.unit}`}
            </TableCell>
            <TableCell>{cups(row.cups_bought)}</TableCell>
            <TableCell>{cups(row.usable_cups)}</TableCell>
            <TableCell>{percent(row.spoiled_share)}</TableCell>
            <TableCell data-testid="best-buy-cost">
              {money(row.cost_per_usable_cup_cents)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function ShoppingList({ result }: { result: BestBuyResult }) {
  const groups = result.shopping_list ?? []
  return (
    <section
      className="flex flex-col gap-4 print:text-black"
      data-testid="shopping-list"
      aria-labelledby="shopping-list-title"
    >
      <div className="flex items-center justify-between">
        <h2 id="shopping-list-title" className="text-lg font-semibold">
          Shopping list
        </h2>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="print:hidden"
          onClick={() => window.print()}
        >
          <Printer className="mr-2" />
          Print
        </Button>
      </div>
      {groups.map((group) => (
        <div key={group.label} data-testid="shopping-group">
          <h3 className="font-medium">{group.label}</h3>
          <ul className="ml-5 list-disc">
            {(group.lines ?? []).map((line) => (
              <li key={line.item_name}>
                {line.item_name}
                {line.purchase && `: ${line.purchase}`}
                {line.price_cents != null && ` (${money(line.price_cents)})`}
                {line.note && (
                  <span className="text-muted-foreground"> ({line.note})</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  )
}

const BestBuyStatus = ({ runId }: { runId: string }) => {
  const { data: run, isError } = useRun(runId)

  if (isError) {
    return (
      <p className="text-destructive" data-testid="run-load-error">
        Could not load this comparison. Refresh the page to try again.
      </p>
    )
  }
  if (!run) {
    return null
  }
  const result = run.result?.kind === "best_buy" ? run.result : null

  return (
    <div className="flex flex-col gap-6">
      <Card className="print:hidden">
        <CardHeader>
          <CardTitle className="flex items-center gap-3">
            Best buy
            <span aria-live="polite">
              <Badge variant={RUN_BADGE[run.status]} data-testid="run-status">
                {run.status}
              </Badge>
            </span>
          </CardTitle>
          {result && (
            <CardDescription>
              Prices from the 30 days before {result.on}; the cheapest per
              usable cup is starred
            </CardDescription>
          )}
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {!isFinished(run) && (
            <p className="text-muted-foreground">
              Comparing prices. This updates automatically.
            </p>
          )}
          {result && (
            <>
              {result.explanation && (
                <p data-testid="best-buy-explanation">{result.explanation}</p>
              )}
              {result.explanation_error && (
                <p className="text-muted-foreground text-sm">
                  {result.explanation_error}
                </p>
              )}
              <TradeOffTable rows={result.rows} />
            </>
          )}
          {run.status === "failed" && (
            <p className="text-destructive" data-testid="run-error">
              {run.error_message ?? "The comparison failed."}
            </p>
          )}
        </CardContent>
      </Card>
      {result && <ShoppingList result={result} />}
    </div>
  )
}

export default BestBuyStatus
