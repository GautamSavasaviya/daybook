"""Ticket keys and links: which project a key belongs to and where "Open ticket" goes."""

from collections.abc import Iterable
from urllib.parse import quote

from daybook.core.models import Project
from daybook.core.validation import ticket_prefix


def project_for_key(key: str, projects: Iterable[Project]) -> Project | None:
    """The active project whose key prefix matches ``PROJ`` in ``PROJ-123`` (D-018)."""
    prefix = ticket_prefix(key)
    if prefix is None:
        return None
    for project in projects:
        if not project.archived and project.key_prefix == prefix:
            return project
    return None


def ticket_url(key: str | None, own_project: Project | None, projects: Iterable[Project]) -> str:
    """The link for a ticket, or "" when no project provides a URL template.

    The task's own project wins; otherwise the project matching the key's prefix is used,
    so a ticket filed under an area still opens in the right PMS.
    """
    if not key:
        return ""
    template = own_project.url_template if own_project is not None else None
    if template is None:
        prefix = ticket_prefix(key)
        for project in projects:
            if prefix is not None and project.key_prefix == prefix and project.url_template:
                template = project.url_template
                break
    if template is None:
        return ""
    return template.replace("{key}", quote(key, safe=""))
