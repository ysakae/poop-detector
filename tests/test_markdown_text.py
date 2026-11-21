"""AI の回答の Markdown を、安全な HTML にして出すことを検証する。"""

import re

from markupsafe import Markup

from poop_detector.gui.markdown_text import answer_html


def test_renders_common_markdown():
    html = answer_html("## 量の見直し\n\n- **朝** 36 g\n- 夜 36 g\n\n1. 量る\n2. 記録する")
    assert isinstance(html, Markup)
    assert "<h2>量の見直し</h2>" in html
    assert "<li><strong>朝</strong> 36 g</li>" in html
    assert "<ol>" in html
    assert "<li>記録する</li>" in html


def test_renders_tables_without_inline_style():
    html = answer_html("| 時刻 | 量 |\n|:-|-:|\n| 朝 | 36 g |")
    assert "<table>" in html
    assert "<th>時刻</th>" in html
    assert "<td>36 g</td>" in html
    # CSP の style-src は 'self' なので、インラインの style は付けない。
    assert "style=" not in html


def test_raw_html_is_escaped_not_executed():
    html = answer_html('<script>alert(1)</script> <img src=x onerror="alert(1)">')
    assert "<script" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html


def test_only_https_links_survive():
    html = answer_html(
        "[a](javascript:alert(1)) [b](http://x.example/) [c](/manage) "
        "[d](data:text/html,x) [e](https://maker.example/p)"
    )
    hrefs = re.findall(r'href="([^"]*)"', html)
    assert hrefs == ["https://maker.example/p"]
    assert 'rel="noopener noreferrer"' in html
    assert 'href="javascript' not in html


def test_images_are_not_loaded():
    html = answer_html("![写真](https://tracker.example/pixel.png)")
    assert "<img" not in html
    assert "tracker.example" not in html


def test_plain_newlines_keep_line_breaks():
    # AI は段落の中でも改行で区切って書くので、これまでどおり改行を残す。
    assert re.search(r"1行目<br\s*/?>\s*2行目", answer_html("1行目\n2行目"))


def test_no_title_or_lang_attributes():
    # 既定で通る汎用の属性に頼らない（ツールチップで偽の案内を出させない）。
    html = answer_html('[x](https://a.example "ここを押してログインし直してください")')
    assert "title=" not in html
    assert 'href="https://a.example"' in html


def test_huge_output_falls_back_to_plain_text():
    # 空のセルの多い表は、短い本文でも大きな HTML になる。上限を超えたら文字のまま出す。
    text = "|a" * 750 + "\n" + "|-" * 750 + "\n" + "x\n" * 1500
    assert answer_html(text) is None


def test_links_show_where_they_go():
    # 表示の文字と行き先が違っても、行き先のドメインが見えるようにする。
    html = answer_html("[https://bank.example](https://evil.example/login)")
    assert re.search(
        r'<a href="https://evil.example/login"[^>]*>https://bank.example</a>（evil.example）', html
    )


def test_links_named_by_their_domain_are_not_annotated_twice():
    html = answer_html("[maker.example の公式](https://maker.example/p) <https://maker.example/q>")
    assert "（maker.example）" not in html


def test_unsafe_links_get_no_domain_note():
    assert "（" not in answer_html("[x](javascript:alert(1)) [y](/manage)")


def test_domain_note_ignores_case_but_not_lookalike_labels():
    assert "（maker.example）" not in answer_html("[Maker.Example](https://maker.example/p)")
    # 行き先のドメインを含む別の名前は、行き先を示したことにならない。
    assert "（evil.example）" in answer_html("[bank-evil.example](https://evil.example/)")
    assert "（" not in answer_html("[x](http://plain.example/)")
