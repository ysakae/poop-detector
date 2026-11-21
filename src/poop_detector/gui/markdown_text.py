"""AI の回答の Markdown を、許した要素だけの HTML にする。

GUI で `Markup`（エスケープを外した HTML）を作ってよいのは、このモジュールだけにする。
回答は判定器の CLI が返した外からの文字なので、二重に守る。

1. markdown-it-py は生の HTML を受け付けない設定（`html: False`）で変換する。
   `<script>` などは文字としてエスケープされる。
2. 変換した HTML を nh3 に通し、下の表のタグだけを残す。リンクは https の絶対 URL だけ、
   画像・style・class・title は捨てる。CSP はインラインの style を止めない
   （style-src に 'unsafe-inline' がある）ので、style を落とすのはここが唯一の守りになる。
3. 変換後が MAX_HTML_LENGTH を超えたら None を返し、画面は文字のまま出す
   （空のセルの多い表は、短い本文でも大きな HTML になる）。
"""

from markdown_it import MarkdownIt
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

# 段落の中の改行も改行として出す（AI は1行ずつ区切って書くことが多い）。
_MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")


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
