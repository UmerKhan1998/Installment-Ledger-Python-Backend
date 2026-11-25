"""AI opportunity research: Gemini searches the web (Google Search grounding), the app attaches the sources and checks
each idea against the user's own plan (agents.md §25).

Flow: consent check -> minimal request (region, currency, horizon, risk level, Shariah preference; never income,
names or emails) -> grounded Gemini call in a fixed text layout -> deterministic parse -> sources attached from
Google's grounding metadata (never from the model's text) -> ideas with no source dropped -> fit checks against the
user's assessment (emergency fund, investable amount, horizon, risk level, experience) -> ordered by fit -> audit.
"""
import hashlib
import json
import re
from datetime import date
from decimal import Decimal

from django.conf import settings

from finance.assessment import assess, profile_for

from .llm import get_provider
from .llm.base import ProviderError
from .models import AIInteraction
from .planner import ConsentRequired
from .prompts import opportunities_v1 as prompt
from .views import settings_for

RULES_VERSION = "fit-v1"

REGIONS = {"global": "Global (worldwide)", "pakistan": "Pakistan", "us": "United States", "uk": "United Kingdom",
           "uae": "United Arab Emirates", "saudi": "Saudi Arabia", "eu": "European Union"}
RISK_LEVELS = {"conservative": 2, "balanced": 3, "growth": 4}  # highest asset risk (1-5) that fits each level

# key: (label, risk 1-5, usual minimum years, liquidity, Shariah-compliant: True / False / None = depends on product)
ASSET_CLASSES = {
    "cash_savings": ("Savings account / deposit", 1, 0, "high", None),
    "money_market_fund": ("Money market fund", 1, 0, "high", None),
    "government_bills": ("Government treasury bills", 1, 0, "medium", False),
    "national_savings": ("National savings scheme", 1, 1, "medium", False),
    "government_bonds": ("Government bonds", 2, 2, "medium", False),
    "sukuk": ("Sukuk (Islamic certificates)", 2, 2, "medium", True),
    "bond_fund": ("Bond / income fund", 2, 2, "high", None),
    "pension_fund": ("Voluntary pension fund", 3, 10, "low", None),
    "gold": ("Gold", 3, 3, "medium", True),
    "equity_index_fund": ("Equity index fund", 4, 5, "high", None),
    "islamic_equity_fund": ("Shariah-compliant equity fund", 4, 5, "high", True),
    "reit": ("Real estate investment trust (REIT)", 4, 5, "medium", None),
    "real_estate": ("Property (direct)", 4, 7, "low", None),
    "individual_stocks": ("Individual shares", 5, 7, "high", None),
    "crypto": ("Crypto-assets", 5, 7, "high", None),
    "other": ("Other", 3, 3, "medium", None),
}
assert set(ASSET_CLASSES) == set(prompt.ASSET_CLASS_KEYS)

FIELDS = {"WHAT IT IS": "what", "CURRENT FACTS": "facts", "HOW TO ACCESS": "access", "LIQUIDITY": "liquidity",
          "COSTS AND TAXES": "costs", "MAIN RISKS": "risks"}
FIELD_LABELS = {"what": "What it is", "facts": "Current facts", "access": "How to access", "liquidity": "Liquidity",
                "costs": "Costs and taxes", "risks": "Main risks", "overview": "Overview"}
FIT_ORDER = {"good": 0, "caution": 1, "not_now": 2}

HEADER = re.compile(r"^\s*#{1,4}\s*\**\s*(OVERVIEW|OPPORTUNITY\s*:\s*(?P<name>.+?))\s*\**\s*$", re.I)
FIELD = re.compile(r"^\s*[-*]*\s*\**\s*(?P<label>[A-Za-z][A-Za-z ]{2,30}?)\s*\**\s*:\s*\**\s*")
URL = re.compile(r"https?://\S+|www\.\S+")


class BadRequest(Exception):
    pass


def parse_request(data, profile):
    region = data.get("region") or "global"
    risk = data.get("risk_level") or "conservative"
    if region not in REGIONS:
        raise BadRequest(f"region must be one of: {', '.join(REGIONS)}")
    if risk not in RISK_LEVELS:
        raise BadRequest(f"risk_level must be one of: {', '.join(RISK_LEVELS)}")
    horizon = data.get("horizon_years", profile.investment_horizon_years)
    if horizon in ("", None):
        horizon = None
    else:
        try:
            horizon = int(horizon)
        except (TypeError, ValueError):
            raise BadRequest("horizon_years must be a whole number of years") from None
        if not 0 <= horizon <= 50:
            raise BadRequest("horizon_years must be between 0 and 50")
    focus = re.sub(r"[\r\n\t]+", " ", str(data.get("focus") or "")).strip()[:120] or None
    return {"region": region, "risk_level": risk, "horizon_years": horizon,
            "shariah_only": bool(data.get("shariah_only")), "focus": focus}


# ---------- parsing the grounded answer ----------

def _clean(text):
    return URL.sub("[link removed]", text.replace("**", "").replace("\n", " "))


def _segments(raw: bytes, start: int, end: int, supports, cite):
    """Cut the byte range [start, end) of the answer at the start and end of every grounding support inside it and
    attach the sources of the supports that end at each cut. Returns ([{text, sources}], all sources)."""
    cuts = {}
    for s in supports:
        if s["end"] <= start or s["start"] >= end:
            continue
        cuts.setdefault(max(s["start"], start), set())  # unsourced text before a supported span stays separate
        at = min(s["end"], end)
        cuts.setdefault(at, set()).update(cite(i) for i in s["chunks"])
    pieces, pos, every = [], start, set()
    for at in sorted(cuts) + ([end] if end not in cuts else []):
        if at <= pos:
            continue
        text = _clean(raw[pos:at].decode("utf-8", errors="ignore"))
        sources = sorted(n for n in cuts.get(at, ()) if n is not None)
        every.update(sources)
        if text.strip() or sources:
            pieces.append({"text": text, "sources": sources})
        pos = at
    if pieces:
        pieces[0]["text"] = pieces[0]["text"].lstrip()
        pieces[-1]["text"] = pieces[-1]["text"].rstrip()
    return [p for p in pieces if p["text"] or p["sources"]], sorted(every)


def parse_grounded(grounding):
    """Split the answer into overview + opportunity blocks and attach sources from grounding metadata.
    Source numbers are assigned in order of first use; only web sources Google returned can be cited."""
    parts = grounding.get("parts") or []
    raw = "".join(parts).encode("utf-8")
    bases, total = [], 0
    for p in parts:
        bases.append(total)
        total += len(p.encode("utf-8"))
    supports = []
    for s in grounding.get("supports") or []:
        base = bases[s["part"]] if s.get("part", 0) < len(bases) else 0
        supports.append({"start": base + s["start"], "end": base + s["end"], "chunks": s["chunks"]})
    chunks = grounding.get("chunks") or []
    numbers, sources = {}, []

    def cite(i):
        if not isinstance(i, int) or i >= len(chunks) or not chunks[i]:
            return None
        ch = chunks[i]
        if not ch["uri"].startswith("https://") and not ch["uri"].startswith("http://"):
            return None
        if i not in numbers:
            numbers[i] = len(sources) + 1
            sources.append({"n": numbers[i], "title": ch["title"][:200], "domain": ch["domain"] or ch["title"][:80], "uri": ch["uri"]})
        return numbers[i]

    # Walk the lines, tracking byte offsets: blocks hold {key: (start, end)} byte ranges of each field's value.
    blocks, current, field = [], None, None
    pos = 0
    for line in raw.split(b"\n"):
        line_start, line_end = pos, pos + len(line)
        pos = line_end + 1
        text = line.decode("utf-8", errors="ignore")
        head = HEADER.match(text)
        if head:
            name = head.group("name")
            current = {"kind": "opportunity" if name else "overview", "name": _clean(name or "").strip()[:120], "asset_class": None, "ranges": {}}
            blocks.append(current)
            field = "overview" if not name else None
            if field:
                current["ranges"][field] = [line_end + 1, line_end + 1]
            continue
        if current is None:
            continue
        m = FIELD.match(text)
        label = m.group("label").upper().strip() if m else ""
        if m and label == "ASSET CLASS":
            key = re.sub(r"[^a-z_]", "", text[m.end():].strip().lower().replace(" ", "_"))
            current["asset_class"] = key if key in ASSET_CLASSES else "other"
            field = None
            continue
        if m and label in FIELDS:
            field = FIELDS[label]
            value_start = line_start + len(text[:m.end()].encode("utf-8"))
            current["ranges"][field] = [value_start, line_end]
            continue
        if field and text.strip():
            current["ranges"][field][1] = line_end  # continuation line

    overview, opportunities, dropped = None, [], 0
    for b in blocks:
        fields, every = [], set()
        for key, (start, end) in b["ranges"].items():
            segs, srcs = _segments(raw, start, end, supports, cite)
            if segs:
                fields.append({"key": key, "label": FIELD_LABELS[key], "segments": segs, "sources": srcs, "sourced": bool(srcs)})
                every.update(srcs)
        if b["kind"] == "overview":
            overview = fields[0] if fields else None
            continue
        if not every or not b["name"]:
            dropped += 1  # no web source backs anything in it -> not shown
            continue
        opportunities.append({"name": b["name"], "asset_class": b["asset_class"] or "other", "fields": fields, "sources": sorted(every)})
    return {"overview": overview, "opportunities": opportunities, "dropped": dropped, "sources": sources}


# ---------- deterministic fit checks ----------

def _dec(v):
    return Decimal(v) if v not in (None, "") else None


def fit(opportunity, req, ctx):
    """Check one idea against the user's plan. Returns {level, reasons:[{tone, text}]}; tone good|caution|not_now."""
    label, risk, min_years, liquidity, shariah = ASSET_CLASSES[opportunity["asset_class"]]
    cap = RISK_LEVELS[req["risk_level"]]
    if req["risk_level"] == "growth" and ctx["experience"] == "experienced":
        cap = 5
    reasons = []
    gap = ctx["emergency_gap"]
    if gap is not None and gap > 0 and risk >= 3:
        reasons.append(("not_now", f"Your emergency fund isn't complete yet ({ctx['currency']} {gap:,.2f} short); "
                                   "higher-risk options usually come after it."))
    elif gap is not None and gap > 0 and liquidity == "high" and risk == 1:
        reasons.append(("good", "Low risk and quick to withdraw, so it can also hold your emergency fund."))
    if risk > cap:
        reasons.append(("not_now", f"Riskier than the '{req['risk_level']}' level you chose."))
    if req["horizon_years"] is None:
        if min_years > 0:
            reasons.append(("caution", f"Usually held for {min_years}+ years; set your investment horizon to check this."))
    elif req["horizon_years"] < min_years:
        reasons.append(("not_now", f"Usually needs {min_years}+ years; your horizon is {req['horizon_years']}."))
    if risk >= 4 and ctx["experience"] in (None, "none"):
        reasons.append(("caution", "Prices can fall sharply; consider learning how it works before investing."))
    if liquidity == "low":
        reasons.append(("caution", "Money can be hard to get back quickly (lock-in, penalties or a slow sale)."))
    if req["shariah_only"]:
        if shariah is False:
            reasons.append(("not_now", "Interest-based, and you asked for Shariah-compliant options only."))
        elif shariah is None:
            reasons.append(("caution", "Check the specific product is certified Shariah-compliant."))
    if opportunity["asset_class"] == "crypto":
        reasons.append(("caution", "Very volatile and lightly regulated or restricted in many countries."))
    facts = next((f for f in opportunity["fields"] if f["key"] == "facts"), None)
    if facts is None or not facts["sourced"]:
        reasons.append(("caution", "No source backed the current figures; check them yourself."))
    tones = {t for t, _ in reasons}
    level = "not_now" if "not_now" in tones else "caution" if "caution" in tones else "good"
    if level == "good" and not reasons:
        reasons.append(("good", f"Within your '{req['risk_level']}' risk level and horizon."))
    return {"level": level, "risk": risk, "liquidity": liquidity, "asset_label": label,
            "reasons": [{"tone": t, "text": x} for t, x in reasons]}


def plan_context(user, profile):
    result = assess(user)
    return {"currency": result["currency"], "investable": _dec(result["investable_monthly"]),
            "emergency_gap": _dec(result["emergency_gap"]), "emergency_target": _dec(result["emergency_target"]),
            "experience": profile.experience or None, "complete": result["complete"], "as_of": result["as_of"]}


def context_notes(ctx):
    notes = []
    if not ctx["complete"] or ctx["investable"] is None:
        notes.append("Your plan is missing figures, so the fit checks can't use your investable amount yet (Invest → Your numbers).")
    elif ctx["investable"] <= 0:
        notes.append("Your plan shows nothing investable each month right now: treat these as research for later.")
    if ctx["emergency_gap"] is not None and ctx["emergency_gap"] > 0:
        notes.append(f"Emergency fund first: {ctx['currency']} {ctx['emergency_gap']:,.2f} still to build.")
    return notes


# ---------- run ----------

DISCLAIMER = ("Research ideas found on the web by AI, with their sources, checked against your plan by fixed rules. "
              "Not financial advice and not a recommendation to buy anything. Facts can be out of date: open the sources, "
              "and check a provider is licensed by your regulator before investing.")


def run_scan(user, data, today=None):
    profile = profile_for(user)
    if not profile.consent_ai:
        raise ConsentRequired("Allow the AI to use your summarised figures first (Invest → Your numbers).")
    req = parse_request(data, profile)
    ctx = plan_context(user, profile)
    sent = {"region": REGIONS[req["region"]], "investor_currency": profile.currency, "horizon_years": req["horizon_years"],
            "risk_level": req["risk_level"], "shariah_compliant_only": req["shariah_only"], "focus": req["focus"]}
    sent_json = json.dumps(sent, ensure_ascii=False, sort_keys=True)
    prefs = settings_for(user)
    model = prefs.model or settings.GEMINI_MODEL
    record = AIInteraction(user=user, kind="opportunities", prompt_version=prompt.PROMPT_VERSION, method_version=RULES_VERSION,
                           model_requested=model, figures_hash=hashlib.sha256(sent_json.encode()).hexdigest())
    try:
        grounding, meta = get_provider(prefs.provider).generate_grounded(
            model, prompt.SYSTEM, prompt.build_prompt(sent_json, (today or date.today()).isoformat()),
            fallback_model=settings.GEMINI_FALLBACK_MODEL)
        record.model_version = meta.get("model_version") or ""
        record.used_fallback = meta.get("used_fallback", False)
        record.latency_ms, record.input_tokens, record.output_tokens = meta.get("latency_ms"), meta.get("input_tokens"), meta.get("output_tokens")
        parsed = parse_grounded(grounding)
        if not parsed["opportunities"]:
            raise ProviderError("The AI's web search returned no sourced opportunities. Please try again or change the filters.", "bad_output")
    except ProviderError as err:
        record.ok, record.error_kind = False, err.kind
        record.save()
        raise
    warnings = []
    if parsed["dropped"]:
        warnings.append(f"{parsed['dropped']} idea(s) were hidden because no web source backed them.")
    for opp in parsed["opportunities"]:
        opp["fit"] = fit(opp, req, ctx)
    parsed["opportunities"].sort(key=lambda o: (FIT_ORDER[o["fit"]["level"]], o["fit"]["risk"]))
    out = {
        "request": req,
        "sent": sent,
        "overview": parsed["overview"],
        "opportunities": parsed["opportunities"],
        "sources": parsed["sources"],
        "search_queries": grounding.get("queries") or [],
        "search_entry_point_html": grounding.get("entry_html") or "",
        "plan": {"currency": ctx["currency"], "as_of": ctx["as_of"],
                 "investable_monthly": f"{ctx['investable']:.2f}" if ctx["investable"] is not None else None,
                 "emergency_gap": f"{ctx['emergency_gap']:.2f}" if ctx["emergency_gap"] is not None else None,
                 "notes": context_notes(ctx)},
        "warnings": warnings,
        "rules_version": RULES_VERSION,
        "prompt_version": prompt.PROMPT_VERSION,
        "model_requested": model,
        "model_version": record.model_version or None,
        "used_fallback": record.used_fallback,
        "disclaimer": DISCLAIMER,
    }
    record.ok, record.answer, record.warnings = True, out, warnings
    record.save()
    out["generated_at"] = record.created_at.isoformat()
    out["interaction_id"] = record.id
    return out


def last_scan(user):
    record = AIInteraction.objects.filter(user=user, kind="opportunities", ok=True).first()
    if not record or not record.answer:
        return None
    return {**record.answer, "generated_at": record.created_at.isoformat(), "interaction_id": record.id}
