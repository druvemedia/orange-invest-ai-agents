"""No model-facing prompt may reveal an evaluation answer.

Every prompt the code sends as instructions (each agent's ``instructions``, ``SHARED_RULES`` and the single-agent
baseline's ``INSTRUCTIONS``) is checked against every evaluation label and every case's change records. A Reasoning
prompt example once read "Pool size cut 40 to 8 by CHG-1", which is EVAL-01's answer; live run 4 used it.
"""

import json
import re
from pathlib import Path

import pytest

import app.agents  # noqa: F401  (registers every agent class)
from app.agents.base import SHARED_RULES, LLMAgent
from app.baseline.single_agent import INSTRUCTIONS as BASELINE_INSTRUCTIONS
from app.evaluation.dataset import EVAL_DIR, load_labels
from app.evaluation.metrics import matches

LEAKED_EXAMPLE = 'e.g. "Pool size cut 40 to 8 by CHG-1", not "Pool saturation".'


def _subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def model_prompts() -> dict[str, str]:
    prompts = {sub.__name__: sub.instructions for sub in _subclasses(LLMAgent) if isinstance(sub.instructions, str)}
    prompts["SHARED_RULES"] = SHARED_RULES
    prompts["baseline INSTRUCTIONS"] = BASELINE_INSTRUCTIONS
    return prompts


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]


def change_records() -> list[dict]:
    return [json.loads(line) for path in Path(EVAL_DIR, "cases").glob("*/changes.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def leaks(text: str) -> list[str]:
    """Every way ``text`` reveals an evaluation answer or a case-specific detail."""
    found = []
    labels = load_labels()
    lowered = text.lower()
    for case_id, label in labels.items():
        if case_id.lower() in lowered:
            found.append(f"names case {case_id}")
        for answer in [label.root_cause, *label.acceptable_alternatives]:
            if answer is None:
                continue
            found += [f"{case_id} answer matched by: {s!r}" for s in sentences(text) if matches(s, answer.match_all)]
            words = re.findall(r"[a-z0-9]+", answer.summary.lower())
            found += [f"{case_id} answer phrase {' '.join(words[i:i + 4])!r}" for i in range(len(words) - 3)
                      if " ".join(words[i:i + 4]) in " ".join(re.findall(r"[a-z0-9]+", lowered))]
    for change in change_records():
        if str(change["change_id"]).lower() in lowered:
            found.append(f"names change {change['change_id']}")
        d = change.get("details") or {}
        if "old" in d and "new" in d and re.search(rf"\b{re.escape(str(d['old']))}\s*(?:to|→|->)\s*{re.escape(str(d['new']))}\b",
                                                   text, re.IGNORECASE):
            found.append(f"reveals {change['change_id']}'s change {d['old']} -> {d['new']}")
    return found


def test_the_checker_finds_the_leak_live_run_4_used():
    found = leaks(LEAKED_EXAMPLE)

    assert any("EVAL-01 answer matched" in f for f in found)
    assert any("40 -> 8" in f for f in found)


def test_prompts_are_discovered_from_the_code():
    names = set(model_prompts())

    assert {"OrchestratorAgent", "ObservabilityAgent", "DependencyAgent", "ReasoningAgent", "CriticAgent",
            "ReportingAgent", "SHARED_RULES", "baseline INSTRUCTIONS"} <= names


@pytest.mark.parametrize("name", sorted(model_prompts()))
def test_no_model_facing_prompt_reveals_an_evaluation_answer(name):
    assert leaks(model_prompts()[name]) == []
