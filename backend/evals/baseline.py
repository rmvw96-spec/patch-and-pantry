"""The baseline the nightly evals are checked against (spec P1-9, D14 and D15).

The baseline sorts every case and reader, and every line in it, into three tiers from a reviewed
run:

- **stable**: right in every run. A drop here is a regression.
- **flaky**: right in some runs. Tracked, never failed on.
- **broken**: wrong in every run. The quality bar: these should move up.

The check (``check``) compares a night's results with the baseline, and with the night before
for D14's "one run fewer two nights in a row". It sorts what it finds into regressions (which
fail the workflow once it is enforced), things to watch, things fixed or improving, and new
cases or lines the baseline does not know. The baseline changes only by a reviewed PR.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from evals.cases import Case
from evals.results import CaseResult, Miss, Results

Tier = Literal["stable", "flaky", "broken"]
Kind = Literal["regression", "watch", "better", "new"]

# D14: an extraction case's share of fields may fall this far below the baseline before it is a
# regression. To be set from the report-only weeks.
FIELD_DROP = 0.03


def tier(right: int, runs: int) -> Tier:
    if right == runs:
        return "stable"
    return "broken" if right == 0 else "flaky"


class Lines(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable: tuple[str, ...] = ()
    flaky: dict[str, int] = {}  # line -> runs it was wrong in
    broken: tuple[str, ...] = ()

    def tier_of(self, text: str) -> Tier | None:
        if text in self.stable:
            return "stable"
        if text in self.flaky:
            return "flaky"
        if text in self.broken:
            return "broken"
        return None


class Entry(BaseModel):
    """One case with one reader, as the reviewed run left it."""

    model_config = ConfigDict(frozen=True)

    tier: Tier
    group: str
    passed: int
    runs: int
    fields: float
    output_tokens_per_run: int
    lines: Lines


class MadeFrom(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    repeat: int
    commit: str | None = None
    started: datetime | None = None


class Baseline(BaseModel):
    model_config = ConfigDict(frozen=True)

    made_from: MadeFrom
    cases: dict[str, Entry]

    def count(self, which: Tier) -> int:
        return sum(e.tier == which for e in self.cases.values())


def make_baseline(results: Results, cases: Iterable[Case]) -> Baseline:
    texts = {c.name: [line.text for line in c.lines] for c in cases}
    entries = {}
    for key, r in results.cases.items():
        if r.case not in texts:
            raise ValueError(
                f"{r.case} is not in the case files: rerun on the current cases"
            )
        wrong = {m.text: m.wrong for m in r.misses}
        flaky = {t: wrong[t] for t in texts[r.case] if 0 < wrong.get(t, 0) < r.runs}
        entries[key] = Entry(
            tier=tier(r.passed, r.runs),
            group=r.group,
            passed=r.passed,
            runs=r.runs,
            fields=round(r.fields, 4),
            output_tokens_per_run=r.output_tokens_per_run,
            lines=Lines(
                stable=tuple(t for t in texts[r.case] if t not in wrong),
                flaky=flaky,
                broken=tuple(t for t in texts[r.case] if wrong.get(t) == r.runs),
            ),
        )
    made_from = MadeFrom(
        model=results.model,
        repeat=results.repeat,
        commit=results.commit,
        started=results.started,
    )
    return Baseline(made_from=made_from, cases=entries)


@dataclass(frozen=True)
class Finding:
    kind: Kind
    key: str  # "case [reader]"
    what: str
    line: Miss | None = None  # tonight's miss, for a line that got worse

    @property
    def text(self) -> str | None:
        return self.line.text if self.line else None


@dataclass(frozen=True)
class Check:
    findings: tuple[Finding, ...]
    baseline: Baseline
    results: Results
    previous: Results | None

    def of(self, kind: Kind) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    @property
    def regressed(self) -> bool:
        return bool(self.of("regression"))


def check(
    baseline: Baseline, results: Results, previous: Results | None = None
) -> Check:
    found: list[Finding] = []
    for key, now in results.cases.items():
        entry = baseline.cases.get(key)
        if entry is None:
            found.append(Finding("new", key, "not in the baseline"))
            continue
        before = previous.cases.get(key) if previous else None
        found += _case(key, entry, now, before)
        found += _lines(key, entry, now, before)
    return Check(tuple(found), baseline, results, previous)


def _strict(key: str) -> bool:
    """Rules first is how the app reads lists, and it is held to the stricter bar (D14)."""
    return key.endswith("[rules_first]")


def _case(
    key: str, entry: Entry, now: CaseResult, before: CaseResult | None
) -> list[Finding]:
    found = []
    runs = f"{now.passed} of {now.runs} runs"
    for error in now.errors:
        found.append(Finding("regression", key, f"a run failed: {error}"))
    if entry.group == "extraction" and now.fields < entry.fields - FIELD_DROP:
        found.append(
            Finding(
                "regression",
                key,
                f"fields {now.fields:.0%}, down from {entry.fields:.0%} in the baseline",
            )
        )
    share = now.passed / now.runs
    if entry.tier == "stable" and share < 1:
        dipped_before = before is not None and before.passed < before.runs
        if _strict(key) or now.runs - now.passed >= 2 or dipped_before:
            why = (
                " (rules first)"
                if _strict(key)
                else " (two nights in a row)"
                if dipped_before and now.runs - now.passed < 2
                else ""
            )
            found.append(Finding("regression", key, f"stable, passed {runs}{why}"))
        else:
            found.append(
                Finding("watch", key, f"stable, passed {runs}: one run dipped")
            )
    elif entry.tier == "flaky":
        was = entry.passed / entry.runs
        if share == 1:
            found.append(Finding("better", key, f"flaky, passed every run ({runs})"))
        elif share < was:
            found.append(
                Finding(
                    "watch",
                    key,
                    f"flaky, passed {runs} (baseline {entry.passed} of {entry.runs})",
                )
            )
    elif entry.tier == "broken" and now.passed:
        found.append(Finding("better", key, f"broken, passed {runs}"))
    return found


def _lines(
    key: str, entry: Entry, now: CaseResult, before: CaseResult | None
) -> list[Finding]:
    found = []
    missed = {m.text: m for m in now.misses}
    missed_before = {m.text for m in before.misses} if before else set()
    for text, miss in missed.items():
        was = entry.lines.tier_of(text)
        if was is None:
            found.append(Finding("new", key, "a line not in the baseline", miss))
        elif was == "stable":
            twice = text in missed_before
            if _strict(key) or miss.wrong >= 2 or twice:
                found.append(Finding("regression", key, "a stable line is wrong", miss))
            else:
                found.append(
                    Finding("watch", key, "a stable line was wrong in one run", miss)
                )
        elif (
            was == "flaky"
            and miss.wrong / now.runs > entry.lines.flaky[text] / entry.runs
        ):
            found.append(
                Finding("watch", key, "a flaky line was wrong more often", miss)
            )
    for text in entry.lines.broken:
        if text not in missed:
            found.append(Finding("better", key, f"broken line now right: {text}"))
    for text in entry.lines.flaky:
        if text not in missed:
            found.append(Finding("better", key, f"flaky line right every run: {text}"))
    return found


def markdown(result: Check, enforced: bool, repo: str | None = None) -> str:
    """The run's summary, for the workflow's summary page and the artifact."""
    r, b = result.results, result.baseline
    regressions = result.of("regression")
    if not regressions:
        verdict = "No regressions against the baseline."
    elif enforced:
        verdict = f"**{len(regressions)} regressions**: the workflow fails."
    else:
        verdict = f"**{len(regressions)} regressions** (report only for now, D14: the workflow does not fail)."
    when = f"{r.started:%Y-%m-%d %H:%M} UTC" if r.started else "unknown time"
    out = [
        "# Golden evals: typed lists",
        "",
        verdict,
        "",
        f"- Model `{r.model}`, {r.repeat} runs a case, {when}.",
        f"- Code: {_commit(repo, r.commit)}"
        + (_since(repo, result.previous.commit, r.commit) if result.previous else ""),
        f"- Baseline from {_commit(repo, b.made_from.commit)} ({b.made_from.repeat} runs a case).",
        "",
        "| Baseline tier | Cases and readers | Still there tonight |",
        "| --- | --- | --- |",
    ]
    for which in ("stable", "flaky", "broken"):
        keys = [k for k, e in b.cases.items() if e.tier == which and k in r.cases]
        same = sum(tier(r.cases[k].passed, r.cases[k].runs) == which for k in keys)
        out.append(f"| {which} | {len(keys)} | {same} |")
    titles = {
        "regression": "Regressions",
        "watch": "Watch",
        "better": "Fixed or improving",
        "new": "Not in the baseline",
    }
    kinds: tuple[Kind, ...] = ("regression", "watch", "better", "new")
    for kind in kinds:
        items = result.of(kind)
        if not items:
            continue
        out += ["", f"## {titles[kind]} ({len(items)})", ""]
        for f in items:
            out.append(f"- `{f.key}`: {_md(f.what)}")
            if f.line:
                got = "; ".join(f.line.got)
                out.append(
                    f"  - `{_code(f.line.text)}`: expected {_md(f.line.expected)}, got {_md(got)}"
                    f" ({f.line.wrong} of {r.cases[f.key].runs} runs)"
                )
    out += [
        "",
        "The run's artifact holds the results, the full report, and what changed since the last"
        " nightly run.",
    ]
    return "\n".join(out) + "\n"


def _commit(repo: str | None, sha: str | None) -> str:
    if not sha:
        return "unknown commit"
    return (
        f"[`{sha[:7]}`](https://github.com/{repo}/commit/{sha})"
        if repo
        else f"`{sha[:7]}`"
    )


def _since(repo: str | None, old: str | None, new: str | None) -> str:
    if not (repo and old and new) or old == new:
        return " (no commits since the last nightly run)" if old and old == new else ""
    return f" ([commits since the last nightly run](https://github.com/{repo}/compare/{old}...{new}))"


def _md(text: str) -> str:
    """Text from the model or a list, shown literally in Markdown."""
    return "".join("\\" + c if c in "\\`*_[]<>|#" else c for c in text)


def _code(text: str) -> str:
    return text.replace("`", "'")
