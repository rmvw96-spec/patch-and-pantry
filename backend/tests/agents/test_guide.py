"""The agent guide (spec P1-8): the doc and the agents' instructions say the same thing."""

import re
from pathlib import Path

import pytest

from app.agents import best_buy_agent, capture_agent
from app.agents.guide import (
    BEST_BUY_TASK,
    CAPTURE_TASK,
    SHARED_RULES,
    dieting_words,
)

REPO = Path(__file__).resolve().parents[3]
GUIDE = REPO / "docs" / "agent-guide.md"


def guide_blocks() -> dict[str, str]:
    """The fenced ``text`` block under each ``## `` heading of the guide."""
    blocks: dict[str, str] = {}
    for section in re.split(r"^## ", GUIDE.read_text(), flags=re.MULTILINE)[1:]:
        heading = section.split("\n", 1)[0].strip()
        found = re.findall(r"^```text\n(.*?)^```$", section, re.MULTILINE | re.DOTALL)
        assert len(found) <= 1, f"more than one text block under '{heading}'"
        if found:
            blocks[heading] = found[0]
    return blocks


def test_the_guide_and_the_constants_match() -> None:
    assert guide_blocks() == {
        "Rules for every agent": SHARED_RULES,
        "Prices reader": CAPTURE_TASK,
        "Best buy explainer": BEST_BUY_TASK,
    }


def test_both_agents_get_their_task_and_the_shared_rules() -> None:
    for agent, task in [
        (capture_agent, CAPTURE_TASK),
        (best_buy_agent, BEST_BUY_TASK),
    ]:
        assert agent.INSTRUCTIONS.startswith(task)
        assert agent.INSTRUCTIONS.endswith(SHARED_RULES)


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("About 200 Calories a cup.", ["Calories"]),
        ("Good for dieting.", ["dieting"]),
        ("Helps you lose weight.", ["lose weight"]),
        ("Two servings a day, one serving at lunch.", ["servings", "serving"]),
        ("A small portion.", ["portion"]),
        # Produce is sold by weight; that is not dieting talk.
        ("The 3 lb bag is cheaper by weight.", []),
        ("Dietary fiber is not mentioned; neither is preserving.", []),
    ],
)
def test_dieting_words(text: str, found: list[str]) -> None:
    assert dieting_words(text) == found


# User-facing wording outside the model: the UI and the messages code writes never say
# "serving" (design.md, display unit). Only the files that state the rule may name it.
GENERATED = REPO / "frontend" / "src" / "client"
STATES_THE_RULE = {
    REPO / "backend" / "app" / "agents" / "guide.py",
    REPO / "backend" / "app" / "agents" / "best_buy_agent.py",
}


def test_the_ui_and_messages_never_say_serving() -> None:
    files = [
        *(REPO / "frontend" / "src").rglob("*.ts*"),
        *(REPO / "backend" / "app").rglob("*.py"),
    ]
    offenders = [
        f"{path.relative_to(REPO)}:{n}"
        for path in files
        if path not in STATES_THE_RULE and not path.is_relative_to(GENERATED)
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"\bservings?\b", line, re.IGNORECASE)
    ]
    assert files, "no source files found"
    assert offenders == []
