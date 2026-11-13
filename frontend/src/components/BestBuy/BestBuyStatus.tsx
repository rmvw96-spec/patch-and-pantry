import { Printer, Star } from "lucide-react"

import type { BestBuyResult, BestBuyRow } from "@/client"
import { FormattedText } from "@/components/Common/FormattedText"
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

function amount(value: number | null | undefined): string {
  return value == null ? "-" : value.toFixed(2)
}

/** A cost per usable cup shows as money alone; per piece it names the piece (spec P1-11). */
function perMeasure(cents: number | null | undefined, measure: string): string {
  if (cents == null) return "-"
  return measure === "cup" ? money(cents) : `${money(cents)} per ${measure}`
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
          <TableHead>Bought</TableHead>
          <TableHead>Usable</TableHead>
          <TableHead>Spoils</TableHead>
          <TableHead>Per usable cup or piece</TableHead>
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
            <TableCell>
              {amount(row.amount_bought)}
              {row.amount_bought != null && ` ${row.measure ?? "cup"}s`}
            </TableCell>
            <TableCell>{amount(row.usable_amount)}</TableCell>
            <TableCell>{percent(row.spoiled_share)}</TableCell>
            <TableCell data-testid="best-buy-cost">
              {perMeasure(row.cost_per_usable_cents, row.measure ?? "cup")}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function TableNotes({ result }: { result: BestBuyResult }) {
  const items = (result.items ?? []).filter(
    (item) => item.storage || (item.notes ?? []).length > 0,
  )
  const average = result.national_average
  if (items.length === 0 && !average) return null
  return (
    <div
      className="text-muted-foreground flex flex-col gap-2 text-sm"
      data-testid="best-buy-notes"
    >
      {items.length > 0 && (
        <ul className="flex flex-col gap-1">
          {items.map((item) => (
            <li key={item.item_name} data-testid="best-buy-item-note">
              <span className="text-foreground font-medium">
                {item.item_name}:
              </span>{" "}
              {[item.storage, ...(item.notes ?? [])].filter(Boolean).join(" ")}
            </li>
          ))}
        </ul>
      )}
      {average && (
        <p data-testid="best-buy-national-average">
          {average.note} Source:{" "}
          <a
            href={average.url}
            target="_blank"
            rel="noreferrer"
            className="underline"
          >
            USDA ERS Fruit and Vegetable Prices
          </a>
          .
        </p>
      )}
    </div>
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
              usable cup (or piece, for items counted by the piece) is starred
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
                <FormattedText
                  text={result.explanation}
                  data-testid="best-buy-explanation"
                />
              )}
              {result.explanation_error && (
                <p className="text-muted-foreground text-sm">
                  {result.explanation_error}
                </p>
              )}
              <TradeOffTable rows={result.rows} />
              <TableNotes result={result} />
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
