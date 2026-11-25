"""AI planner: Gemini explains the deterministic plan from finance.assessment (agents.md §7, §8, §21).

Flow: consent check -> assessment (Decimal maths) -> minimised figures with ids (no plan/goal names, no emails)
-> structured Gemini call -> validation (figure ids must be ones we sent; numbers must match a sent figure;
no links) -> audit record -> response with the figures so the UI can show what each section refers to.
"""
import hashlib
import json
import re
from decimal import Decimal

from django.conf import settings
from pydantic import BaseModel, Field, ValidationError

from finance.assessment import assess, profile_for
from finance.calculations import METHOD_VERSION

from .llm import get_provider
from .llm.base import ProviderError
from .models import AIInteraction
from .prompts import planner_v3 as prompt
from .views import settings_for

SOURCE_LABELS = {"user_input": "provided by you", "ledger_expenses": "from your Expenses", "ledger_plans": "from your Plans",
                 "calculation": "calculated by the app", "missing": "missing"}


class Section(BaseModel):
    title: str = Field(max_length=120)
    text: str = Field(max_length=1500)
    figure_ids: list[str] = Field(default_factory=list, max_length=12)


class PlannerAnswer(BaseModel):
    summary: str = Field(max_length=800)
    sections: list[Section] = Field(default_factory=list, max_length=6)
    next_steps: list[str] = Field(default_factory=list, max_length=6)
    caveats: list[str] = Field(default_factory=list, max_length=6)


class ConsentRequired(Exception):
    pass


class BadQuestion(Exception):
    pass


MAX_QUESTION = 500
MAX_HISTORY = 4


def clean_chat(question, history):
    """Validate the chat input: a one-paragraph question and up to 4 earlier {q, a} turns (each trimmed)."""
    if question is not None and not isinstance(question, str):
        raise BadQuestion("question must be text")
    question = re.sub(r"\s+", " ", question or "").strip()
    if len(question) > MAX_QUESTION:
        raise BadQuestion(f"Keep the question under {MAX_QUESTION} characters.")
    turns = []
    if history is not None and not isinstance(history, list):
        raise BadQuestion("history must be a list")
    for t in (history or [])[-MAX_HISTORY:]:
        if not isinstance(t, dict):
            raise BadQuestion("history items must be objects")
        q, a = (re.sub(r"\s+", " ", str(t.get(k) or "")).strip()[:MAX_QUESTION] for k in ("q", "a"))
        if q or a:
            turns.append({"q": q, "a": a})
    return question or None, turns


def build_figures(result):
    """{id: {label, value, currency, source}} — only aggregates, no names."""
    cur = result["currency"]
    figures = {}
    for line in result["lines"]:
        if line["amount"] is not None:
            figures[line["id"]] = {"label": line["label"], "value": line["amount"], "currency": cur,
                                   "source": SOURCE_LABELS.get(line["source"], line["source"]), "note": line["detail"]}
    for key, label in (("emergency_target", "Emergency fund target"), ("emergency_gap", "Emergency fund gap")):
        if result.get(key) is not None:
            figures[key] = {"label": label, "value": result[key], "currency": cur, "source": "calculated by the app"}
    for i, g in enumerate(result["goals"], start=1):
        base = f"goal_{i}"
        kind = g["kind"].replace("_", " ")
        figures[f"{base}_target"] = {"label": f"Goal {i} ({kind}) target", "value": g["target_amount"], "currency": g["currency"],
                                     "source": "provided by you", "note": f"due {g['target_date'][:7]}, {g['months_left']} months left"}
        figures[f"{base}_saved"] = {"label": f"Goal {i} ({kind}) saved so far", "value": g["saved_amount"], "currency": g["currency"], "source": "provided by you"}
        figures[f"{base}_monthly"] = {"label": f"Goal {i} ({kind}) monthly needed at 0% growth", "value": g["required_monthly"],
                                      "currency": g["currency"], "source": "calculated by the app"}
    for f in figures.values():
        f["display"] = f"{f['currency']} {Decimal(f['value']):,.2f}"
    return figures


NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{3,}(?:\.\d+)?)(?![\w])")


def _close(a, b):
    return abs(a - b) <= max(1.0, abs(b) * 0.005)


def validate_answer(answer: PlannerAnswer, figures):
    """Drop unknown figure ids, flag numbers that match no figure, strip links. Returns (answer_dict, warnings)."""
    warnings = []
    known_values = [float(f["value"]) for f in figures.values()]
    years = {str(y) for y in range(1990, 2101)}

    def check_numbers(text, where):
        for raw in NUMBER.findall(text):
            if raw in years:
                continue
            value = float(raw.replace(",", ""))
            if not any(_close(value, v) for v in known_values):
                warnings.append(f"{where}: the number {raw} doesn't match any figure the app calculated.")

    def clean(text):
        stripped = re.sub(r"https?://\S+", "[link removed]", text)
        if stripped != text:
            warnings.append("A link was removed (the planner has no web sources).")
        return stripped

    data = answer.model_dump()
    data["summary"] = clean(data["summary"])
    check_numbers(data["summary"], "Summary")
    for sec in data["sections"]:
        sec["text"] = clean(sec["text"])
        bad = [f for f in sec["figure_ids"] if f not in figures]
        if bad:
            warnings.append(f"Section '{sec['title']}': removed unknown figure references {bad}.")
        sec["figure_ids"] = [f for f in sec["figure_ids"] if f in figures]
        check_numbers(sec["text"], f"Section '{sec['title']}'")
    data["next_steps"] = [clean(s) for s in data["next_steps"]]
    for s in data["next_steps"]:
        check_numbers(s, "Next steps")
    return data, warnings


def run_planner(user, question=None, history=None):
    profile = profile_for(user)
    if not profile.consent_ai:
        raise ConsentRequired("Allow the AI planner to see your summarised figures first (Invest → Your numbers).")
    question, turns = clean_chat(question, history)
    result = assess(user)
    figures = build_figures(result)
    data = {
        "currency": result["currency"],
        "method_version": result["method_version"],
        "figures": figures,
        "missing": result["missing"],
        "notes": result["notes"],
        "assumptions": result["assumptions"],
        "profile": {"emergency_months_target": profile.emergency_months_target,
                    "investment_horizon_years": profile.investment_horizon_years,
                    "experience": profile.get_experience_display() if profile.experience else None,
                    "uses_ledger_data": profile.consent_use_ledger},
    }
    data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
    prefs = settings_for(user)
    model = prefs.model or settings.GEMINI_MODEL
    record = AIInteraction(user=user, kind="planner", prompt_version=prompt.PROMPT_VERSION, method_version=METHOD_VERSION,
                           model_requested=model, figure_ids=sorted(figures),
                           figures_hash=hashlib.sha256(data_json.encode()).hexdigest())
    try:
        text, meta = get_provider(prefs.provider).generate_json(
            model, prompt.SYSTEM, prompt.build_prompt(data_json, question, json.dumps(turns, ensure_ascii=False) if turns else None), PlannerAnswer, fallback_model=settings.GEMINI_FALLBACK_MODEL)
        record.model_version = meta.get("model_version") or ""
        record.used_fallback = meta.get("used_fallback", False)
        record.latency_ms, record.input_tokens, record.output_tokens = meta.get("latency_ms"), meta.get("input_tokens"), meta.get("output_tokens")
        try:
            answer = PlannerAnswer.model_validate_json(text)
        except ValidationError:
            raise ProviderError("The AI returned an answer in an unexpected format. Please try again.", "bad_output") from None
        answer_dict, warnings = validate_answer(answer, figures)
        record.ok, record.answer, record.warnings = True, answer_dict, warnings
    except ProviderError as err:
        record.ok, record.error_kind = False, err.kind
        record.save()
        raise
    record.save()
    return {
        "question": question,
        "answer": answer_dict,
        "warnings": warnings,
        "figures": figures,
        "assessment_as_of": result["as_of"],
        "method_version": METHOD_VERSION,
        "prompt_version": prompt.PROMPT_VERSION,
        "model_requested": model,
        "model_version": record.model_version or None,
        "used_fallback": record.used_fallback,
        "generated_at": record.created_at.isoformat(),
        "interaction_id": record.id,
        "disclaimer": "AI explanation of figures calculated by the app. Educational only: not financial advice, "
                      "not a recommendation of any product, and not a promise of any return.",
    }
