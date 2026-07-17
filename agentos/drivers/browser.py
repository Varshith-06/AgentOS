"""The browser driver, humble edition: fetch a page, hand back its visible text.

A real headless-browser driver can replace this later without a single agent
changing — agents say `request_tool("browser", "open", url=...)` and nothing
else. That swap-without-rewrite is the whole point of drivers.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

from .base import ToolError
from .http import Http


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style"}

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip_depth and data.strip():
            self._chunks.append(data.strip())

    @property
    def text(self) -> str:
        return "\n".join(self._chunks)


class Browser(Http):
    name = "browser"

    async def op_open(self, url: str) -> dict[str, Any]:
        page = await self.op_get(url)
        if page["status"] != 200:
            raise ToolError(f"{url} answered {page['status']}")
        extractor = _TextExtractor()
        extractor.feed(page["body"])
        return {"url": url, "title": extractor.title.strip(), "text": extractor.text}
