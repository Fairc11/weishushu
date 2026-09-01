"""微博正文的内部纯文本转换。"""

from __future__ import annotations

from html.parser import HTMLParser


class _WeiboPlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() == "br":
            self._parts.append("\n")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() == "br":
            self._parts.append("\n")

    def text(self) -> str:
        return "".join(self._parts)


def weibo_html_to_text(value: str) -> str:
    """移除微博正文标签，同时精确保留 `br` 形成的换行。"""
    parser = _WeiboPlainTextParser()
    parser.feed(value)
    parser.close()
    return parser.text().replace("\r\n", "\n").replace("\r", "\n")
