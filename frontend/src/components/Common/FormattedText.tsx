import type { ReactNode } from "react"

// Renders the little Markdown the assistant writes: paragraphs, line breaks, "- " bullets, and
// **bold**. Everything is plain text in React elements, never HTML, so nothing in the text can
// inject markup. Anything else (headings, links, tables) shows as typed.

const BOLD = /\*\*(.+?)\*\*/g
const BULLET = /^\s*[-*]\s+/

function inline(text: string): ReactNode[] {
  const parts: ReactNode[] = []
  let last = 0
  for (const match of text.matchAll(BOLD)) {
    const start = match.index ?? 0
    if (start > last) parts.push(text.slice(last, start))
    parts.push(<strong key={start}>{match[1]}</strong>)
    last = start + match[0].length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

function block(lines: string[], key: number): ReactNode {
  if (lines.every((line) => BULLET.test(line))) {
    return (
      <ul key={key} className="ml-5 list-disc">
        {lines.map((line, index) => (
          // Lines have no id of their own; their order is the text's.
          <li key={index}>{inline(line.replace(BULLET, ""))}</li>
        ))}
      </ul>
    )
  }
  return (
    <p key={key}>
      {lines.map((line, index) => (
        <span key={index}>
          {index > 0 && <br />}
          {inline(line.trimEnd())}
        </span>
      ))}
    </p>
  )
}

export function FormattedText({
  text,
  className,
  ...props
}: { text: string; className?: string } & React.ComponentProps<"div">) {
  const blocks = text
    .split(/\n\s*\n/)
    .map((chunk) => chunk.split("\n").filter((line) => line.trim() !== ""))
    .filter((lines) => lines.length > 0)
  return (
    <div className={`flex flex-col gap-2 ${className ?? ""}`} {...props}>
      {blocks.map(block)}
    </div>
  )
}
