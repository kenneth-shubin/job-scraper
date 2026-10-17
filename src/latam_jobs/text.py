"""Plain-text cleanup shared by source parsers."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from latam_jobs.models import DESCRIPTION_LIMIT


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append(" ")


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = " ".join(parser.parts)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:DESCRIPTION_LIMIT]
