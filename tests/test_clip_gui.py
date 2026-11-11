"""動画の再生・ラベル付けと手動保存要求のHTTP確認。"""

from support import ready_clip, request, submit

from poop_detector.clip_labels import ClipLabel
from poop_detector.clip_reviews import Assessment


def test_video_range_and_labels(gui):
    from poop_detector.clips import ClipStore

    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = clips.request()
    (store.root / "clips" / (clip + ".mp4")).write_bytes(b"0123456789abcdef")
    clips.update(clip, status="ready", path=f"clips/{clip}.mp4", duration=10)
    assert b"<video" in request(server, "GET", "/clips?id=" + clip)[1]
    assert request(server, "GET", "/clip-video?id=" + clip, headers={"Range": "bytes=2-5"}) == (
        206,
        b"2345",
    )
    assert (
        request(server, "GET", "/clip-video?id=" + clip, headers={"Range": "bytes=100-"})[0] == 416
    )
    assert (
        submit(
            server,
            "/clips/label",
            {"id": clip, "label": "feces", "start": "2", "end": "4", "activity": "excreting"},
        )[0]
        == 303
    )
    assert clips.get(clip)["actual_start_offset"] == 2
    assert (
        submit(server, "/clips/label", {"id": clip, "label": "urine", "start": "-1", "end": "4"})[0]
        == 400
    )
    assert request(server, "GET", "/clip-video?id=../../.env")[0] == 404
    status, data = request(
        server, "GET", "/clip-video?id=" + clip, headers={"Range": "bytes=2-5"}, auth=False
    )
    assert status == 403
    assert b"2345" not in data


def test_manual_request_requires_running_camera(gui):
    from poop_detector.clips import ClipStore

    server, store, _ = gui
    assert submit(server, "/clips/request", {})[0] == 409
    clips = ClipStore(store.root)
    clips.heartbeat(True)
    assert submit(server, "/clips/request", {})[0] == 303
    assert clips.list()[0]["source"] == "manual"


def test_codex_draft_is_visible_without_overwriting_human_label(gui):
    from poop_detector.clips import ClipStore

    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = clips.request()
    clips.update(clip, status="ready", duration=10)
    clips.assessments.save(
        clip,
        Assessment(
            label="feces",
            activity="excreting",
            start_seconds=2,
            end_seconds=4,
            reason="<新しい便が見えます>",
        ),
    )
    _, body = request(server, "GET", "/clips?id=" + clip)
    page = body.decode()
    assert "Codex候補 ／ 排便" in page
    assert "&lt;新しい便が見えます&gt;" in page
    assert 'name="label" value="feces" checked' in page
    assert clips.get(clip)["label"] is None

    clips.labels.save(clip, ClipLabel("none", activity="feeding"))
    _, body = request(server, "GET", "/clips?id=" + clip)
    assert 'name="label" value="none" checked' in body.decode()
    assert clips.assessments.get(clip)["label"] == "feces"


def test_clip_links_keep_addressable_details_for_partial_navigation(gui):
    from poop_detector.clips import ClipStore

    server, store, _ = gui
    clips = ClipStore(store.root)
    old = clips.request()
    new = clips.request()
    status, body = request(server, "GET", "/clips?id=" + old)
    assert status == 200
    assert b'id="clip-list"' in body
    assert b'id="clip-detail"' in body
    assert b'src="/assets/clips.js"' in body
    assert f'class="active" href="/clips?id={old}"'.encode() in body
    assert f'href="/clips?id={new}"'.encode() in body
    assert request(server, "GET", "/assets/clips.js")[0] == 200


def test_video_confirmation_converts_offsets_and_reuses_existing_record(gui):
    from poop_detector.clips import ClipStore

    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = ready_clip(clips)
    fields = {"id": clip, "label": "feces", "start": "5.25", "end": "11", "record_id": "new"}
    assert submit(server, "/clips/confirm", fields)[0] == 303
    assert submit(server, "/clips/confirm", fields)[0] == 303

    (record,) = store.records()
    assert record["actual_start"] == "2026-11-09T06:10:05.250000+00:00"
    assert record["actual_end"] == "2026-11-09T06:10:11+00:00"
    assert clips.get(clip)["label"] == "feces"

    other = ready_clip(clips)
    _, page = request(server, "GET", "/clips?id=" + other)
    assert record["id"].encode() in page
    assert (
        submit(
            server,
            "/clips/confirm",
            {
                "id": other,
                "label": "feces",
                "record_id": record["id"],
            },
        )[0]
        == 303
    )
    assert store.records() == [record]

    _, page = request(server, "GET", "/records")
    for clip_id in (clip, other):
        assert f'href="/clips?id={clip_id}"'.encode() in page


def test_video_confirmation_preserves_unknown_time_and_can_use_original_event(gui):
    from poop_detector.clips import ClipStore

    server, store, event_id = gui
    clips = ClipStore(store.root)
    clip = ready_clip(clips, event_id=event_id)
    assert (
        submit(
            server,
            "/clips/confirm",
            {
                "id": clip,
                "label": "urine",
                "record_id": event_id,
            },
        )[0]
        == 303
    )
    (record,) = store.records()
    assert record["id"] == event_id
    assert record["excretion_type"] == "urine"
    assert record["actual_start"] is None
    assert record["actual_end"] is None

    another = ready_clip(clips)
    assert (
        submit(
            server,
            "/clips/confirm",
            {
                "id": another,
                "label": "feces",
                "record_id": "new",
            },
        )[0]
        == 303
    )
    video_record = next(row for row in store.records() if row["id"] != event_id)
    assert video_record["actual_start"] is None
    assert video_record["timestamp"] == "2026-11-09T06:10:00+00:00"


def test_invalid_confirmation_leaves_video_label_and_records_intact(gui):
    from poop_detector.clips import ClipStore

    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = ready_clip(clips)
    for fields in (
        {"label": "none", "record_id": "new"},
        {"label": "feces", "record_id": "missing"},
        {"label": "feces", "record_id": "new", "start": "12", "end": "3"},
    ):
        assert submit(server, "/clips/confirm", {"id": clip, **fields})[0] == 400
        assert clips.get(clip)["label"] is None
        assert store.records() == []
