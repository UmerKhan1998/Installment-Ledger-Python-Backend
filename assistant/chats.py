"""Planner chat history ("Old chats"): every question and answer in the planner panel is kept per user, so a chat can
be reopened, searched, continued or deleted. Follow-up context comes from the stored chat, not from the browser.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .llm.base import ProviderError
from .models import PlannerChat, PlannerMessage

MAX_CHATS = 200          # oldest chats beyond this are removed
HISTORY_TURNS = 4        # earlier turns sent as context with a follow-up
DEFAULT_QUESTION = "Explain my plan"
PERIODS = {"7d": 7, "30d": 30, "90d": 90}


def title_for(question):
    text = (question or DEFAULT_QUESTION).strip()
    return text if len(text) <= 80 else text[:77].rstrip() + "…"


def history_of(chat):
    """Last answered turns of a chat as [{q, a}] for the prompt (answers summarised to their summary)."""
    turns = chat.messages.filter(role="ai").order_by("-created_at", "-id")[:HISTORY_TURNS]
    return [{"q": m.question or DEFAULT_QUESTION, "a": (m.out or {}).get("answer", {}).get("summary", "")} for m in reversed(turns)]


def clean_page(page):
    return " ".join(str(page or "").split())[:40]


def run_chat_turn(user, question, chat=None, page=""):
    """Ask the planner inside a chat (a new one when `chat` is None) and store both turns.
    Returns (response, chat). Consent/bad-question errors are raised before anything is stored."""
    from .planner import clean_chat, run_planner

    question, _ = clean_chat(question, None)  # validate first: nothing stored for a rejected question
    history = history_of(chat) if chat else None
    if chat is None:
        chat = PlannerChat(user=user, title=title_for(question), page=clean_page(page))
    try:
        out = run_planner(user, question, history)
    except ProviderError as err:
        _store(chat, question, role="error", text=str(err), error_kind=err.kind)
        err.chat = chat
        raise
    _store(chat, question, role="ai", out=out)
    return out, chat


def _store(chat, question, role, out=None, text="", error_kind=""):
    if chat.pk is None:
        chat.save()
        prune(chat.user)
    PlannerMessage.objects.create(chat=chat, role="user", text=question or DEFAULT_QUESTION)
    PlannerMessage.objects.create(chat=chat, role=role, question=question or "", out=out, text=text, error_kind=error_kind)
    chat.save(update_fields=["updated_at"])


def prune(user):
    old = PlannerChat.objects.filter(user=user).order_by("-updated_at", "-id").values_list("id", flat=True)[MAX_CHATS:]
    PlannerChat.objects.filter(id__in=list(old)).delete()


def search(user, q="", period="", page=""):
    chats = PlannerChat.objects.filter(user=user)
    q = " ".join((q or "").split())[:100]
    if q:
        chats = chats.filter(Q(title__icontains=q) | Q(messages__text__icontains=q)).distinct()
    if period in PERIODS:
        chats = chats.filter(updated_at__gte=timezone.now() - timedelta(days=PERIODS[period]))
    if page:
        chats = chats.filter(page=page)
    return chats.order_by("-updated_at", "-id")


def chat_summary(chat):
    return {"id": chat.id, "title": chat.title, "page": chat.page, "created_at": chat.created_at.isoformat(),
            "updated_at": chat.updated_at.isoformat()}


def message_dict(m):
    data = {"id": m.id, "role": m.role, "at": m.created_at.isoformat()}
    if m.role == "user":
        data["text"] = m.text
    elif m.role == "ai":
        data.update(q=m.question, out=m.out)
    else:
        data.update(q=m.question, text=m.text, consent=m.error_kind == "consent_required")
    return data
