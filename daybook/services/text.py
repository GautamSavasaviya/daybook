"""Small text helpers for messages built by services (toasts, notifications)."""

MAX_SUBJECT = 40  # D-054: titles start with the subject and stay short


def short(text: str, limit: int = MAX_SUBJECT) -> str:
    """Shorten `text` for a toast or notification title, ending in an ellipsis if cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
