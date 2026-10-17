"""Short location labels. Structured country lists stay complete for eligibility."""

from __future__ import annotations

_LIST_LIMIT = 3


def country_list_label(names: list[str], *, zone: str = "") -> str:
    unique = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if len(unique) > _LIST_LIMIT:
        summary = f"{len(unique)} countries"
    else:
        summary = ", ".join(unique)
    prefix = zone.strip()
    if prefix and summary:
        return f"{prefix}: {summary}"
    return summary or prefix


def location_label(location_text: str, countries: tuple[str, ...] = ()) -> str:
    text = location_text.strip()
    count = len(dict.fromkeys(countries))
    if count <= _LIST_LIMIT:
        return text or "—"
    zone = ""
    if ":" in text:
        head, _tail = text.split(":", 1)
        if head.strip() and "," not in head:
            zone = head.strip()
    summary = f"{count} countries"
    if zone:
        return f"{zone}: {summary}"
    return summary
