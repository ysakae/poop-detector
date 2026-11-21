"""AI の回答の Markdown を、許した要素だけの HTML にする。

GUI で `Markup`（エスケープを外した HTML）を作ってよいのは、このモジュールだけにする。
回答は判定器の CLI が返した外からの文字なので、二重に守る。

1. markdown-it-py は生の HTML を受け付けない設定（`html: False`）で変換する。
   `<script>` などは文字としてエスケープされる。
2. 変換した HTML を nh3 に通し、下の表のタグだけを残す。リンクは https の絶対 URL だけ、
   画像・style・class・title は捨てる。CSP はインラインの style を止めない
   （style-src に 'unsafe-inline' がある）ので、style を落とすのはここが唯一の守りになる。
3. https のリンクの後ろに、行き先のドメインを文字で添える（表示の文字と行き先が違う
   リンクで、別のサイトへ誘わないため）。リンクの文字にドメインが入っていれば添えない。
4. 変換後が MAX_HTML_LENGTH を超えたら None を返し、画面は文字のまま出す
   （空のセルの多い表は、短い本文でも大きな HTML になる）。
"""

import re
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from markupsafe import Markup
from nh3 import Cleaner

# AI が相談の答えに使う書き方。画像（img）は外の宛先を読みに行くので入れない。
ALLOWED_TAGS = frozenset(
    {
        "p", "br", "strong", "em", "code", "pre", "blockquote", "hr",
        "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "thead", "tbody", "tr", "th", "td", "a",
    }
)  # fmt: skip
# "*" を空にして、nh3 が既定ですべてのタグに許す title・lang も捨てる。
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {"*": set(), "a": {"href"}, "ol": {"start"}}
# 回答の本文は6000文字まで（prompts.ANSWER_SCHEMA）。ふつうの回答は数十KBに収まる。
MAX_HTML_LENGTH = 100_000


def _https_host(href: str) -> str | None:
    if not href.startswith("https://"):
        return None
    try:
        return urlsplit(href).hostname
    except ValueError:
        return None


def _names_host(label: str, host: str) -> bool:
    """リンクの文字が行き先のドメインをそのまま示しているか（大文字・小文字は区別しない）。

    bank-evil.example のように、ドメインを含む別の名前は示したことにしない。
    """
    pattern = rf"(?<![\w.-]){re.escape(host)}(?![\w-])"
    return re.search(pattern, label.lower()) is not None


def _with_host_notes(children: list[Token]) -> list[Token]:
    """リンクの閉じの後ろに「（ドメイン）」の文字を足した、新しい並びを返す。"""
    result: list[Token] = []
    host: str | None = None
    label = ""
    for child in children:
        result.append(child)
        if child.type == "link_open":
            host = _https_host(str(child.attrGet("href") or ""))
            label = ""
        elif child.type == "link_close":
            if host and not _names_host(label, host):
                note = Token("text", "", 0)
                note.content = f"（{host}）"
                result.append(note)
            host = None
        elif host is not None and child.type in ("text", "code_inline"):
            label += child.content
    return result


def _note_link_hosts(state: StateCore) -> None:
    for token in state.tokens:
        if token.type == "inline" and token.children:
            token.children = _with_host_notes(token.children)


# 段落の中の改行も改行として出す（AI は1行ずつ区切って書くことが多い）。
_MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")
_MARKDOWN.core.ruler.push("link_hosts", _note_link_hosts)


def _https_only(_tag: str, attribute: str, value: str) -> str | None:
    """リンク先は https の絶対 URL だけを残す（相対 URL で GUI の中の操作へ誘わない）。"""
    if attribute == "href":
        return value if value.startswith("https://") else None
    return value


_CLEANER = Cleaner(
    tags=set(ALLOWED_TAGS),
    attributes=ALLOWED_ATTRIBUTES,
    attribute_filter=_https_only,
    url_schemes={"https"},
    link_rel="noopener noreferrer",
    strip_comments=True,
)


def answer_html(text: str) -> Markup | None:
    """AI の回答（Markdown）を、表示してよい HTML にする。大きすぎれば None。"""
    html = _CLEANER.clean(_MARKDOWN.render(text))
    if len(html) > MAX_HTML_LENGTH:
        return None
    # nh3 が許したタグと属性だけを通した結果なので、エスケープを外して出してよい。
    return Markup(html)  # noqa: S704
