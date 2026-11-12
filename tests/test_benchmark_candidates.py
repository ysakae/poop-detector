"""動画の画面で選ぶ評価の候補と条件のタグ（#103 の2つ目）を検証する。

候補とタグは benchmark_candidates テーブル（スキーマ版11）に保存し、
benchmark-dataset create が評価セットの下書きに反映する。
"""

import json
from urllib.parse import urlencode

import pytest
from support import add_clip, exchange, login, ready_clip, request, submit

from poop_detector.main import main
from poop_detector.video.benchmark_candidates import CONDITION_TAGS, Candidate, parse_tags
from poop_detector.video.benchmark_sets import build
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clips import ClipStore

CREATED = "2026-11-12T00:00:00+00:00"


# --- 台帳 ---------------------------------------------------------------------------------


def test_candidates_are_saved_and_cleared(tmp_path):
    clips = ClipStore(tmp_path)
    clip = add_clip(clips, "2026-11-01T01:00:00+00:00", "feces")

    clips.benchmark.save(clip, "include", ("夜間（赤外線）", "遮蔽"))

    assert clips.benchmark.get(clip) == Candidate("include", ("夜間（赤外線）", "遮蔽"))
    assert clips.benchmark.all() == {clip: Candidate("include", ("夜間（赤外線）", "遮蔽"))}
    # 選択もタグもなくせば、行を消して既定（ラベルのある録画をすべて使う）に戻す。
    clips.benchmark.save(clip, None, ())
    assert clips.benchmark.get(clip) is None
    assert clips.benchmark.all() == {}


@pytest.mark.parametrize(
    ("label", "status"),
    [(None, "ready"), ("uncertain", "ready"), ("feces", "removed")],
)
def test_only_labeled_saved_videos_can_be_candidates(tmp_path, label, status):
    clips = ClipStore(tmp_path)
    clip = add_clip(clips, "2026-11-01T01:00:00+00:00", label)
    clips.update(clip, status=status)
    with pytest.raises(ValueError, match="ラベル"):
        clips.benchmark.save(clip, "include", ())
    with pytest.raises(ValueError, match="ラベル"):
        clips.benchmark.save("missing", "include", ())


def test_candidate_state_is_validated(tmp_path):
    clips = ClipStore(tmp_path)
    clip = add_clip(clips, "2026-11-01T01:00:00+00:00", "none")
    with pytest.raises(ValueError, match="選択"):
        clips.benchmark.save(clip, "maybe", ())


def test_tags_are_split_trimmed_and_limited():
    assert parse_tags(["夜間（赤外線）", " 遮蔽 、匂い嗅ぎ,  ", "遮蔽", ""]) == (
        "夜間（赤外線）",
        "遮蔽",
        "匂い嗅ぎ",
    )
    with pytest.raises(ValueError, match="30文字"):
        parse_tags(["あ" * 31])
    with pytest.raises(ValueError, match="10個"):
        parse_tags([f"条件{index}" for index in range(11)])
    with pytest.raises(ValueError, match="使えない文字"):
        parse_tags(["夜間\n昼間"])


# --- 評価セットの下書き ---------------------------------------------------------------------


def test_build_uses_selected_tags_and_exclusions(tmp_path):
    clips = ClipStore(tmp_path)
    night = add_clip(clips, "2026-11-01T01:00:00+00:00", "feces", interrupted=1)
    dropped = add_clip(clips, "2026-11-02T01:00:00+00:00", "none")
    plain = add_clip(clips, "2026-11-03T01:00:00+00:00", "urine")
    clips.benchmark.save(night, "include", ("夜間（赤外線）",))
    clips.benchmark.save(dropped, "exclude", ())
    base = {"clips": [{"id": night, "tags": ["メッシュ付きトレー"], "exclude": True}]}

    dataset = build(clips, name="eval", created_at=CREATED, scenes={}, base=base)

    by_id = {entry.id: entry for entry in dataset.entries}
    # GUI で選んだ値は、引き継いだ評価セットの exclude より優先し、タグは合わせる。
    assert by_id[night].tags == ("メッシュ付きトレー", "一部欠落", "夜間（赤外線）")
    assert not by_id[night].exclude
    assert by_id[dropped].exclude
    assert not by_id[plain].exclude
    assert [entry.id for entry in dataset.included] == [night, plain]


def test_build_with_selected_only_uses_only_included_videos(tmp_path):
    clips = ClipStore(tmp_path)
    chosen = add_clip(clips, "2026-11-01T01:00:00+00:00", "feces")
    add_clip(clips, "2026-11-02T01:00:00+00:00", "none")
    add_clip(clips, "2026-11-03T01:00:00+00:00", "uncertain")
    clips.benchmark.save(chosen, "include", ())

    dataset = build(clips, name="eval", created_at=CREATED, scenes={}, selected_only=True)

    assert [entry.id for entry in dataset.entries] == [chosen]
    assert dataset.uncertain == ()


def test_included_video_represents_its_visit(tmp_path):
    clips = ClipStore(tmp_path)
    # 同じ訪問の録画。既定なら排泄を確認した方を残すが、人が選んだ録画を優先する。
    enter = add_clip(clips, "2026-11-01T01:00:00+00:00", "none", duration=40)
    detected = add_clip(clips, "2026-11-01T01:00:20+00:00", "feces", duration=30)
    clips.benchmark.save(enter, "include", ())

    dataset = build(clips, name="eval", created_at=CREATED, scenes={})

    assert [entry.id for entry in dataset.entries] == [enter]
    assert dataset.duplicates == ((detected, enter),)


def test_cli_create_reads_the_selection(tmp_path, capsys):
    root = tmp_path / "data"
    clips = ClipStore(root)
    chosen = add_clip(clips, "2026-11-01T01:00:00+00:00", "feces")
    add_clip(clips, "2026-11-02T01:00:00+00:00", "none")
    clips.benchmark.save(chosen, "include", ("遮蔽",))
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {root}\n")

    code = main(
        [
            "--config",
            str(config),
            "benchmark-dataset",
            "create",
            "--name",
            "eval",
            "--selected-only",
        ]
    )

    assert code == 0
    report = json.loads((root / "benchmarks" / "datasets" / "eval-draft.json").read_text())
    assert [(entry["id"], entry["tags"]) for entry in report["clips"]] == [(chosen, ["遮蔽"])]
    assert "条件:遮蔽" in capsys.readouterr().out


# --- 動画の画面 -----------------------------------------------------------------------------


def labeled_clip(store, label="feces"):
    clips = ClipStore(store.root)
    clip = ready_clip(clips)
    clips.labels.save(clip, ClipLabel(label))
    return clips, clip


def test_clip_page_shows_the_candidate_form_for_labeled_videos(gui):
    server, store, _ = gui
    clips, clip = labeled_clip(store)
    unlabeled = ready_clip(clips)

    page = request(server, "GET", f"/clips?id={clip}")[1].decode()
    assert 'action="/clips/benchmark"' in page
    for tag in CONDITION_TAGS:
        assert f'value="{tag}"' in page
    other = request(server, "GET", f"/clips?id={unlabeled}")[1].decode()
    assert 'action="/clips/benchmark"' not in other
    assert "ラベルを保存すると、評価の候補に選べます" in other


def test_candidate_is_saved_from_the_clip_page(gui):
    server, store, _ = gui
    clips, clip = labeled_clip(store)

    status, _ = submit(
        server,
        "/clips/benchmark",
        {"id": clip, "state": "include", "tag": ["夜間（赤外線）", "遮蔽"], "other_tags": "給水"},
    )

    assert status == 303
    assert clips.benchmark.get(clip) == Candidate("include", ("夜間（赤外線）", "遮蔽", "給水"))
    page = request(server, "GET", f"/clips?id={clip}&benchmark_saved=1")[1].decode()
    assert "評価の候補を保存しました" in page
    assert 'value="遮蔽" checked' in page
    assert 'value="給水"' in page
    assert "評価に入れる" in page


def test_invalid_candidate_is_refused_with_a_message(gui):
    server, store, _ = gui
    clips, clip = labeled_clip(store)
    status, body = submit(server, "/clips/benchmark", {"id": clip, "other_tags": "あ" * 31})
    assert status == 400
    assert "30文字" in body.decode()
    assert clips.benchmark.get(clip) is None
    unlabeled = ready_clip(clips)
    assert submit(server, "/clips/benchmark", {"id": unlabeled, "state": "include"})[0] == 400


def test_tags_are_escaped_on_the_clip_page(gui):
    server, store, _ = gui
    clips, clip = labeled_clip(store)
    clips.benchmark.save(clip, "include", ("<script>x</script>",))
    page = request(server, "GET", f"/clips?id={clip}")[1].decode()
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;x&lt;/script&gt;" in page


def test_members_can_choose_candidates(gui):
    server, store, _ = gui
    clips, clip = labeled_clip(store)
    member = login(server, role="member")
    page = exchange(server, "GET", f"/clips?id={clip}", headers=member, auth=False)[2].decode()
    token = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    body = urlencode({"id": clip, "state": "exclude", "csrf": token})
    headers = {"Content-Type": "application/x-www-form-urlencoded", **member}
    assert exchange(server, "POST", "/clips/benchmark", body, headers, auth=False)[0] == 303
    assert clips.benchmark.get(clip) == Candidate("exclude", ())


def test_clip_list_marks_candidates(gui):
    server, store, _ = gui
    clips, clip = labeled_clip(store, "none")
    clips.benchmark.save(clip, "exclude", ())
    page = request(server, "GET", "/clips")[1].decode()
    assert "評価から除く" in page
