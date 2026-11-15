"""HTML pages for golden-eval results: one run (``report``), or what changed between two (``diff``).

Each page is one self-contained file (the styles are inline; only the fonts load from Google
Fonts, and the page reads fine without them). Built from the results file alone (``results.py``).
"""

from collections.abc import Iterable
from html import escape as e

from evals.results import CaseResult, Miss, Results

GROUPS = ("extraction", "clarification", "behavior")
READERS = ("model", "rules_first")
READER_NAMES = {"model": "Model on every line", "rules_first": "Rules first"}
GROUP_RULES = {
    "extraction": "Extraction: at least 95% of fields right in every run",
    "clarification": "Clarification: every field right in every run",
    "behavior": "Behavior: every field right in every run",
}
# A change in output tokens smaller than this is noise between runs, not news.
TOKEN_CHANGE = 0.2

STYLE = """
:root{
  --bg:#f6f7f4; --surface:#ffffff; --fg:#1d2420; --muted:#5d6a63; --line:#dde3dd; --accent:#2f6b4f;
  --pass:#2f7a46; --pass-bg:#e3f2e7; --part:#9a6a12; --part-bg:#fbefd5; --fail:#b23a2e; --fail-bg:#fbe3df;
  --font-body:"Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --font-mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#141916; --surface:#1b211e; --fg:#e4ebe6; --muted:#9aa89f; --line:#2c3530; --accent:#7cc4a0;
  --pass:#7fd39a; --pass-bg:#1f3a28; --part:#e7c06f; --part-bg:#3a3020; --fail:#f09a8f; --fail-bg:#3f2421; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#141916; --surface:#1b211e; --fg:#e4ebe6; --muted:#9aa89f; --line:#2c3530; --accent:#7cc4a0;
  --pass:#7fd39a; --pass-bg:#1f3a28; --part:#e7c06f; --part-bg:#3a3020; --fail:#f09a8f; --fail-bg:#3f2421; color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font-body);font-size:15px;line-height:1.5}
.wrap{max-width:1040px;margin:0 auto;padding:32px 16px 64px;display:grid;gap:36px}
header{display:grid;gap:8px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-size:30px;line-height:1.15;margin:0;text-wrap:balance;font-weight:700}
h2{font-size:20px;margin:0;text-wrap:balance}
h3{font-size:16px;margin:0;font-family:var(--font-mono);font-weight:500}
p{margin:0;max-width:68ch}
.meta{color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px;display:grid;gap:2px}
.stat b{font-size:24px;font-variant-numeric:tabular-nums}
.stat span{color:var(--muted);font-size:13px}
section{display:grid;gap:14px}
.tablewrap{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:760px;font-size:14px}
table.narrow{min-width:480px}
th,td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
thead th{font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:600;white-space:nowrap}
thead tr.top th{border-bottom:none;padding-bottom:0;color:var(--fg)}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.dim{color:var(--muted)}
tr.grp th{background:var(--bg);font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--accent);font-weight:600}
.case{display:block;font-family:var(--font-mono);font-size:13px;font-weight:500}
.why{display:block;color:var(--muted);font-size:13px;font-weight:400;max-width:52ch}
.sep{border-left:1px solid var(--line)}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}
.chip.pass{color:var(--pass);background:var(--pass-bg)} .chip.part{color:var(--part);background:var(--part-bg)} .chip.fail{color:var(--fail);background:var(--fail-bg)}
.up{color:var(--pass);font-weight:600} .down{color:var(--fail);font-weight:600}
.findings{display:grid;gap:16px}
.finding{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:16px;gap:10px}
.reader{display:grid;gap:8px;border-top:1px solid var(--line);padding-top:10px}
.rhead{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.rname{font-weight:600;font-size:14px}
ul{list-style:none;margin:0;padding:0;display:grid;gap:12px}
li{display:grid;gap:3px;min-width:0}
code.line{font-family:var(--font-mono);font-size:13px;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:2px 6px;justify-self:start;max-width:100%;overflow-wrap:anywhere}
.runs{font-size:12px;color:var(--fail);font-weight:600}
.runs.fixed{color:var(--pass)}
.exp,.got{display:grid;grid-template-columns:72px 1fr;gap:8px;font-size:14px;min-width:0}
.exp span:last-child,.got span:last-child{overflow-wrap:anywhere}
.lbl{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em;padding-top:2px}
.got span:last-child{color:var(--fail)}
.got.was span:last-child{color:var(--muted);text-decoration:line-through}
.notes ul{list-style:disc;gap:8px} .notes li{display:list-item;margin-left:18px;max-width:72ch}
.empty{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:16px}
code{font-family:var(--font-mono);font-size:.9em}
"""

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Public+Sans:'
    'wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">'
)


def page(title: str, body: str) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{e(title)}</title>\n{FONTS}\n<style>{STYLE}</style>\n</head>\n"
        f'<body>\n<div class="wrap">\n{body}\n</div>\n</body>\n</html>\n'
    )


def describe(results: Results) -> str:
    """The model, the date, and the runs, for a page's header."""
    when = f"{results.started:%-d %b %Y} · " if results.started else ""
    names = {r.case for r in results.cases.values()}
    lines = sum({r.case: r.lines for r in results.cases.values()}.values())
    return (
        f"{e(results.model)} · {when}every case run {results.repeat} times with each reader"
        f" · {len(names)} cases, {lines} lines"
    )


def chip(passed: int, runs: int) -> str:
    kind = "pass" if passed == runs else ("part" if passed else "fail")
    return f'<span class="chip {kind}">{passed}/{runs}</span>'


def ordered(results: Results) -> list[str]:
    """Case names in group order, then in the order the runner wrote them."""
    names: dict[str, str] = {}
    for r in results.cases.values():
        names.setdefault(r.case, r.group)
    return sorted(
        names, key=lambda n: GROUPS.index(names[n]) if names[n] in GROUPS else 9
    )


def miss_item(miss: Miss, runs: int) -> str:
    got = "".join(
        f'<div class="got"><span class="lbl">got</span><span>{e(g)}</span></div>'
        for g in miss.got
    )
    return (
        f'<li><code class="line">{e(miss.text)}</code>'
        f'<span class="runs">{miss.wrong} of {runs} runs wrong</span>'
        f'<div class="exp"><span class="lbl">expected</span><span>{e(miss.expected)}</span></div>'
        f"{got}</li>"
    )


def render_report(
    results: Results, title: str = "Typed list evals", notes: Iterable[str] = ()
) -> str:
    rows = []
    for group in GROUPS:
        names = [n for n in ordered(results) if _group(results, n) == group]
        if not names:
            continue
        rows.append(
            f'<tr class="grp"><th colspan="10" scope="colgroup">{e(GROUP_RULES[group])}</th></tr>'
        )
        for name in names:
            first = next(r for r in results.cases.values() if r.case == name)
            cells = "".join(
                _cells(results.cases.get(f"{name} [{reader}]")) for reader in READERS
            )
            rows.append(
                f'<tr><th scope="row"><span class="case">{e(name)}</span>'
                f'<span class="why">{e(first.why)}</span></th>'
                f'<td class="num dim">{first.lines}</td>{cells}</tr>'
            )

    findings = []
    for name in ordered(results):
        blocks = []
        for reader in READERS:
            r = results.cases.get(f"{name} [{reader}]")
            if r is None or not (r.misses or r.errors):
                continue
            status = (
                '<span class="chip pass">case passed</span>'
                if r.passed == r.runs
                else '<span class="chip fail">case failed</span>'
            )
            errors = "".join(
                f'<li><span class="runs">run failed</span><span>{e(x)}</span></li>'
                for x in r.errors
            )
            items = "".join(miss_item(m, r.runs) for m in r.misses)
            blocks.append(
                f'<div class="reader"><div class="rhead"><span class="rname">'
                f"{READER_NAMES[reader]}</span>{status}</div><ul>{errors}{items}</ul></div>"
            )
        if blocks:
            why = next(r.why for r in results.cases.values() if r.case == name)
            findings.append(
                f'<section class="finding"><h3>{e(name)}</h3><p class="why">{e(why)}</p>'
                f"{''.join(blocks)}</section>"
            )

    stats = [
        _stat(*_passing(results, None), "case and reader pairs passed every run"),
        _stat(
            *_passing(results, "rules_first"), "rules first (how the app reads lists)"
        ),
        _stat(*_passing(results, "model"), "model reading every line"),
        f'<div class="stat"><b>{_tokens(results):,}</b><span>output tokens, all runs</span></div>',
    ]
    if results.seconds is not None:
        stats.append(
            f'<div class="stat"><b>{results.seconds / 60:.1f} min</b><span>wall time</span></div>'
        )
    note_list = "".join(f"<li>{e(n)}</li>" for n in notes)
    body = f"""<header>
  <span class="eyebrow">Patch &amp; Pantry · spec P1-9 · golden evals</span>
  <h1>{e(title)}</h1>
  <p class="meta">{describe(results)}, through the real capture code and the loaded catalog</p>
</header>
<div class="stats">{"".join(stats)}</div>
<section>
  <h2>Scores by case</h2>
  <p class="meta">"Passed" is how many runs met the case's rule. "Fields" is the share of scored
  fields that were right over all runs. Seconds per run include waits after rate limits.</p>
  <div class="tablewrap"><table>
    <thead>
      <tr class="top"><th></th><th></th><th colspan="4" class="sep">Model on every line</th><th colspan="4" class="sep">Rules first</th></tr>
      <tr><th scope="col">Case</th><th scope="col" class="num">Lines</th>{_heads() * 2}</tr>
    </thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
</section>
<section>
  <h2>Every line that missed</h2>
  <p class="meta">Includes misses inside cases that still passed at 95%. "Expected" is the closest
  acceptable answer in the case file; "got" is each distinct wrong result. Prices are totals for
  the amount bought.</p>
  <div class="findings">{"".join(findings) or '<p class="empty">No line missed.</p>'}</div>
</section>"""
    if note_list:
        body += f'\n<section class="notes"><h2>Notes on this run</h2><ul>{note_list}</ul></section>'
    return page(title, body)


def render_diff(
    old: Results, new: Results, title: str = "Typed list evals: what changed"
) -> str:
    """Only what changed from ``old`` to ``new``: results, then lines, then cases added or gone."""
    keys = [k for k in new.cases if k in old.cases]
    lines = {k: _line_changes(old.cases[k], new.cases[k]) for k in keys}
    changed = [k for k in keys if lines[k] or _changed(old.cases[k], new.cases[k])]
    worse = [k for k in changed if _direction(old.cases[k], new.cases[k]) < 0]
    better = [k for k in changed if _direction(old.cases[k], new.cases[k]) > 0]
    added = [k for k in new.cases if k not in old.cases]
    gone = [k for k in old.cases if k not in new.cases]

    rows = []
    for k in changed:
        o, n = old.cases[k], new.cases[k]
        tokens = (
            f"{o.output_tokens_per_run:,} → {n.output_tokens_per_run:,}"
            if _tokens_changed(o, n)
            else ""
        )
        rows.append(
            f'<tr><th scope="row"><span class="case">{e(n.case)}</span>'
            f'<span class="why">{READER_NAMES.get(n.reader, e(n.reader))}</span></th>'
            f'<td class="num">{chip(o.passed, o.runs)} → {chip(n.passed, n.runs)}</td>'
            f'<td class="num">{o.fields:.0%} → {n.fields:.0%} {_delta(n.fields - o.fields)}</td>'
            f'<td class="num dim">{tokens}</td></tr>'
        )

    findings = []
    for k in keys:
        n, items = new.cases[k], lines[k]
        if items:
            findings.append(
                f'<section class="finding"><h3>{e(n.case)}</h3>'
                f'<p class="why">{READER_NAMES.get(n.reader, e(n.reader))}</p>'
                f"<ul>{''.join(items)}</ul></section>"
            )

    setup = []
    if old.model != new.model:
        setup.append(f"The model changed: {e(old.model)} → {e(new.model)}.")
    if old.repeat != new.repeat:
        setup.append(f"Runs per case changed: {old.repeat} → {new.repeat}.")
    if added:
        setup.append(
            "New cases: " + ", ".join(f"<code>{e(k)}</code>" for k in added) + "."
        )
    if gone:
        setup.append(
            "Cases no longer run: "
            + ", ".join(f"<code>{e(k)}</code>" for k in gone)
            + "."
        )

    old_pass, total_old = _passing(old, None)
    new_pass, total_new = _passing(new, None)
    stats = (
        _stat(f"{old_pass} → {new_pass}", f"of {total_new}", "pairs passing every run")
        + _stat(str(len(worse)), "", "pairs worse")
        + _stat(str(len(better)), "", "pairs better")
        + _stat(str(sum(map(len, lines.values()))), "", "lines changed")
    )
    nothing = not (changed or findings or setup)
    body = f"""<header>
  <span class="eyebrow">Patch &amp; Pantry · spec P1-9 · golden evals</span>
  <h1>{e(title)}</h1>
  <p class="meta"><b>Before:</b> {describe(old)}</p>
  <p class="meta"><b>After:</b> {describe(new)}</p>
</header>"""
    if nothing:
        body += '\n<p class="empty">Nothing changed: every case and every line scored as before.</p>'
        return page(title, body)
    body += f'\n<div class="stats">{stats}</div>'
    if setup:
        body += f'\n<section class="notes"><h2>What was run</h2><ul>{"".join(f"<li>{s}</li>" for s in setup)}</ul></section>'
    if rows:
        body += f"""
<section>
  <h2>Cases whose result changed</h2>
  <p class="meta">Only cases that passed a different number of runs, scored a different share of
  fields, missed different lines, or used at least {TOKEN_CHANGE:.0%} more or fewer output tokens.</p>
  <div class="tablewrap"><table class="narrow">
    <thead><tr><th scope="col">Case</th><th scope="col" class="num">Passed</th>
    <th scope="col" class="num">Fields</th><th scope="col" class="num">Out tok / run</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
</section>"""
    if findings:
        body += f"""
<section>
  <h2>Lines that changed</h2>
  <p class="meta">Newly wrong, now right, or wrong in a different way or a different number of runs.</p>
  <div class="findings">{"".join(findings)}</div>
</section>"""
    return page(title, body)


def _group(results: Results, name: str) -> str:
    return next(r.group for r in results.cases.values() if r.case == name)


def _heads() -> str:
    return (
        '<th scope="col" class="num sep">Passed</th><th scope="col" class="num">Fields</th>'
        '<th scope="col" class="num">s/run</th><th scope="col" class="num">Out tok</th>'
    )


def _cells(r: CaseResult | None) -> str:
    if r is None:
        return '<td class="num dim sep">not run</td><td></td><td></td><td></td>'
    return (
        f'<td class="num sep">{chip(r.passed, r.runs)}</td>'
        f'<td class="num">{r.fields:.0%}</td>'
        f'<td class="num dim">{r.seconds_per_run:.1f}</td>'
        f'<td class="num dim">{r.output_tokens_per_run:,}</td>'
    )


def _passing(results: Results, reader: str | None) -> tuple[int, int]:
    rs = [r for r in results.cases.values() if reader is None or r.reader == reader]
    return sum(r.passed == r.runs for r in rs), len(rs)


def _stat(value: object, of: object, label: str) -> str:
    shown = f"{value} of {of}" if isinstance(of, int) else f"{value} {of}".strip()
    return f'<div class="stat"><b>{shown}</b><span>{label}</span></div>'


def _tokens(results: Results) -> int:
    return sum(r.output_tokens_per_run * r.runs for r in results.cases.values())


def _tokens_changed(o: CaseResult, n: CaseResult) -> bool:
    base = max(o.output_tokens_per_run, 1)
    return abs(n.output_tokens_per_run - o.output_tokens_per_run) / base >= TOKEN_CHANGE


def _changed(o: CaseResult, n: CaseResult) -> bool:
    return (
        o.passed / o.runs != n.passed / n.runs
        or round(o.fields, 3) != round(n.fields, 3)
        or o.errors != n.errors
        or _tokens_changed(o, n)
    )


def _direction(o: CaseResult, n: CaseResult) -> int:
    """Positive when the case got better, negative when worse, by pass rate then fields."""
    if n.pass_rate != o.pass_rate:
        return 1 if n.pass_rate > o.pass_rate else -1
    if round(n.fields, 3) != round(o.fields, 3):
        return 1 if n.fields > o.fields else -1
    return 0


def _delta(d: float) -> str:
    if round(d, 3) == 0:
        return ""
    return f'<span class="{"up" if d > 0 else "down"}">({d:+.0%})</span>'


def _line_changes(o: CaseResult, n: CaseResult) -> list[str]:
    before = {m.text: m for m in o.misses}
    after = {m.text: m for m in n.misses}
    items = []
    for text, m in after.items():
        was = before.get(text)
        if was is None:
            items.append(_change(m, None, n.runs, "newly wrong"))
        elif (was.wrong / o.runs, was.got, was.expected) != (
            m.wrong / n.runs,
            m.got,
            m.expected,
        ):
            items.append(
                _change(m, was, n.runs, f"was {was.wrong} of {o.runs} runs wrong")
            )
    for text, was in before.items():
        if text not in after:
            items.append(
                f'<li><code class="line">{e(text)}</code>'
                f'<span class="runs fixed">now right (was {was.wrong} of {o.runs} runs wrong)</span>'
                f'<div class="exp"><span class="lbl">expected</span><span>{e(was.expected)}</span></div>'
                + "".join(
                    f'<div class="got was"><span class="lbl">was</span><span>{e(g)}</span></div>'
                    for g in was.got
                )
                + "</li>"
            )
    return items


def _change(m: Miss, was: Miss | None, runs: int, label: str) -> str:
    previously = (
        "".join(
            f'<div class="got was"><span class="lbl">was</span><span>{e(g)}</span></div>'
            for g in was.got
            if g not in m.got
        )
        if was
        else ""
    )
    return (
        f'<li><code class="line">{e(m.text)}</code>'
        f'<span class="runs">{m.wrong} of {runs} runs wrong · {e(label)}</span>'
        f'<div class="exp"><span class="lbl">expected</span><span>{e(m.expected)}</span></div>'
        + "".join(
            f'<div class="got"><span class="lbl">got</span><span>{e(g)}</span></div>'
            for g in m.got
        )
        + previously
        + "</li>"
    )
