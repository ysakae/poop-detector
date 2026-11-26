"""人が動画のラベルで便の状態を直せるようにする（#294 の残り）を検証する。"""

import pytest
from support import ready_clip

from poop_detector.gui.clip_pages import clip_list_item, label_form
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clips import ClipStore


def test_label_keeps_the_condition_only_for_stool(tmp_path):
    clips = ClipStore(tmp_path)
    clip_id = ready_clip(clips)
    clips.labels.save(clip_id, ClipLabel("feces", stool_condition="diarrhea"))
    assert clips.get(clip_id)["stool_condition"] == "diarrhea"
    clips.labels.save(clip_id, ClipLabel("urine", stool_condition="diarrhea"))
    assert clips.get(clip_id)["stool_condition"] is None
    clips.labels.save(clip_id, ClipLabel("both", stool_condition=""))
    assert clips.get(clip_id)["stool_condition"] is None
    with pytest.raises(ValueError, match="うんちの状態"):
        clips.labels.save(clip_id, ClipLabel("feces", stool_condition="liquid"))


def test_label_form_uses_the_assessment_as_a_draft(tmp_path):
    clips = ClipStore(tmp_path)
    clip_id = ready_clip(clips)
    clips.assessments.save(
        clip_id, Assessment("feces", "excreting", 2.0, 5.0, "根拠", "tray", stool_condition="soft")
    )
    row = clips.get(clip_id)
    assert label_form(row, clips.assessments.get(clip_id))["stool_condition"] == "soft"
    clips.labels.save(clip_id, ClipLabel("feces", stool_condition="normal"))
    row = clips.get(clip_id)
    assert label_form(row, clips.assessments.get(clip_id))["stool_condition"] == "normal"


def test_clip_list_shows_the_human_condition(tmp_path):
    clips = ClipStore(tmp_path)
    clip_id = ready_clip(clips)
    clips.labels.save(clip_id, ClipLabel("feces", stool_condition="diarrhea"))
    [row] = clips.list(include_assessments=True)
    assert clip_list_item(row, None)["label"] == "うんち（下痢らしい）"


def test_clip_page_has_the_condition_field_and_saves_it(gui):
    from support import exchange, login, submit

    server, _, _ = gui
    clips = server.app.clips
    clip_id = ready_clip(clips)
    clips.assessments.save(
        clip_id,
        Assessment("feces", "excreting", 2.0, 5.0, "根拠", "tray", stool_condition="diarrhea"),
    )
    cookie = login(server, role="member")
    _, _, body = exchange(server, "GET", f"/clips?id={clip_id}", headers=cookie, auth=False)
    html = body.decode()
    assert 'name="stool_condition"' in html
    assert '<option value="diarrhea" selected>' in html
    assert "うんちの状態：下痢らしい" in html
    status, _ = submit(
        server,
        "/clips/label",
        {"id": clip_id, "label": "feces", "activity": "excreting", "stool_condition": "soft"},
        page=f"/clips?id={clip_id}",
    )
    assert status == 303
    assert clips.get(clip_id)["stool_condition"] == "soft"


def test_stool_condition_is_validated_before_it_is_discarded():
    from poop_detector.video.clip_labels import _validated_stool_condition

    assert _validated_stool_condition(ClipLabel("urine", stool_condition="soft")) is None
    assert _validated_stool_condition(ClipLabel("both", stool_condition="soft")) == "soft"
    with pytest.raises(ValueError, match="うんちの状態"):
        _validated_stool_condition(ClipLabel("urine", stool_condition="invalid"))
