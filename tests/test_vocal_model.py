"""鳴き声らしさを YAMNet で確かめる試作と、評価用の短い音の保存（#295）を検証する。"""

import hashlib
import io
import wave
from datetime import datetime, timedelta

import numpy as np
import pytest

from poop_detector.config import Config, ConfigError
from poop_detector.experimental.vocal import (
    RATE,
    WINDOW_SECONDS,
    Episode,
    VocalDetector,
    VocalRecorder,
    measure,
)
from poop_detector.experimental.vocal_model import (
    DOG_CLASSES,
    MODEL_FILES,
    WINDOW,
    YamnetClassifier,
    setup_vocal_model,
)
from poop_detector.records.absences import AwayMode
from poop_detector.records.database import events_database
from poop_detector.records.vocal_episodes import VocalEpisodes

START = datetime(2026, 11, 23, 9, 0).astimezone()
SAMPLES = int(RATE * WINDOW_SECONDS)


def tone(frequency=800, amplitude=0.5, count=SAMPLES):
    t = np.arange(count) / RATE
    return (np.sin(2 * np.pi * frequency * t) * amplitude * 32767).astype(np.int16)


QUIET = (np.random.default_rng(0).normal(0, 0.002 * 32767, SAMPLES)).astype(np.int16)
BARK = tone()


# --- 区間の音をメモリーに保つ ---


def test_detector_keeps_the_samples_of_an_episode():
    detector = VocalDetector(threshold_db=15)
    now = START
    finished = []
    for samples in [QUIET] * 20 + [BARK, BARK, QUIET, BARK] + [QUIET] * 10:
        finished += detector.update(*measure(samples), now, samples=samples)
        now += timedelta(seconds=WINDOW_SECONDS)
    [episode] = finished
    # 鳴いている窓から最後の鳴いている窓まで（間の静かな窓も含む）の音。
    assert episode.samples is not None
    assert len(episode.samples) == 4 * SAMPLES


def test_detector_caps_the_samples():
    from poop_detector.experimental.vocal import MAX_CLIP_SECONDS

    detector = VocalDetector(threshold_db=15)
    now = START
    for samples in [QUIET] * 20:
        detector.update(*measure(samples), now, samples=samples)
        now += timedelta(seconds=WINDOW_SECONDS)
    for _ in range(int(MAX_CLIP_SECONDS / WINDOW_SECONDS) + 10):
        detector.update(*measure(BARK), now, samples=BARK)
        now += timedelta(seconds=WINDOW_SECONDS)
    [episode] = detector.flush(now)
    assert len(episode.samples) == int(MAX_CLIP_SECONDS * RATE)


# --- YAMNet ---


class Interpreter:
    """LiteRT の Interpreter の代わり。犬のクラスの確からしさを窓ごとに返す。"""

    def __init__(self, dog_scores, top=0):
        self.dog_scores = list(dog_scores)
        self.top = top
        self.inputs = []

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [{"index": 0, "shape": np.array([WINDOW])}]

    def get_output_details(self):
        return [{"index": 1}]

    def set_tensor(self, index, value):
        self.inputs.append(value)

    def invoke(self):
        pass

    def get_tensor(self, index):
        scores = np.zeros((1, 521), np.float32)
        scores[0, self.top] = 0.9
        scores[0, DOG_CLASSES[1]] = self.dog_scores.pop(0) if self.dog_scores else 0.0
        return scores


def test_classifier_takes_the_highest_dog_score_over_windows(tmp_path):
    (tmp_path / "yamnet_class_map.csv").write_text(
        "index,mid,display_name\n" + "".join(f"{i},/m/{i},Class{i}\n" for i in range(521))
    )
    interpreter = Interpreter([0.1, 0.8, 0.3], top=0)
    classifier = YamnetClassifier(tmp_path, interpreter=interpreter)
    score, label = classifier.classify(tone(count=int(RATE * 2)))
    assert score == pytest.approx(0.8)
    assert label == "Class0"
    # 0.975秒の窓を半分ずつずらし、-1〜1 の float32 で渡す。
    assert len(interpreter.inputs) == 3
    assert interpreter.inputs[0].dtype == np.float32
    assert np.max(np.abs(interpreter.inputs[0])) <= 1.0


def test_classifier_pads_short_audio(tmp_path):
    (tmp_path / "yamnet_class_map.csv").write_text(
        "index,mid,display_name\n" + "".join(f"{i},/m/{i},Class{i}\n" for i in range(521))
    )
    interpreter = Interpreter([0.6])
    classifier = YamnetClassifier(tmp_path, interpreter=interpreter)
    assert classifier.classify(tone(count=RATE // 2))[0] == pytest.approx(0.6)
    assert len(interpreter.inputs[0]) == WINDOW


def test_classifier_without_runtime_explains_how_to_install(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("ai_edge_litert"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ValueError, match="audio-model"):
        YamnetClassifier(tmp_path)


# --- モデルの取得 ---


def test_setup_downloads_and_verifies(tmp_path, monkeypatch):
    from poop_detector.experimental import vocal_model

    contents = {name: f"中身 {name}".encode() for name in MODEL_FILES}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    monkeypatch.setattr(vocal_model, "MODEL_FILES", hashes)
    fetched = []

    def opener(url, timeout):
        name = next(n for n in contents if url.endswith(n))
        fetched.append(name)
        return io.BytesIO(contents[name])

    directory = setup_vocal_model(tmp_path / "yamnet", opener=opener)
    assert sorted(fetched) == sorted(MODEL_FILES)
    assert all((directory / name).read_bytes() == contents[name] for name in contents)
    setup_vocal_model(tmp_path / "yamnet", opener=opener)  # 2回目は確かめるだけ
    assert len(fetched) == len(MODEL_FILES)
    (directory / "yamnet.tflite").write_bytes(b"changed")
    with pytest.raises(ValueError, match="ハッシュ"):
        setup_vocal_model(tmp_path / "yamnet", opener=opener)


def test_setup_rejects_a_wrong_download(tmp_path):
    with pytest.raises(ValueError, match="ハッシュ"):
        setup_vocal_model(tmp_path / "yamnet", opener=lambda url, timeout: io.BytesIO(b"x"))
    assert not any((tmp_path / "yamnet").glob("*.tflite"))


# --- 記録・短い音・ラベル ---


class Classifier:
    def classify(self, samples):
        return 0.83, "Dog"


class Notifier:
    def send_vocal(self, loud_seconds, minutes):
        return True


def recorder(tmp_path, *, classifier=None, keep_days=0):
    database = events_database(tmp_path)
    episodes = VocalEpisodes(database)
    config = Config(data_dir=str(tmp_path), audio_keep_days=keep_days)
    return (
        VocalRecorder(
            episodes, AwayMode(database), Notifier(), config, classifier=classifier, root=tmp_path
        ),
        episodes,
    )


def episode(start=START, samples=None):
    return Episode(start, start + timedelta(seconds=3), 2.0, -10.0, samples)


def test_recorder_saves_the_score_and_keeps_the_clip(tmp_path):
    rec, episodes = recorder(tmp_path, classifier=Classifier(), keep_days=7)
    rec.record(episode(samples=tone(count=RATE * 2)))
    [row] = episodes.list()
    assert (row.dog_score, row.top_label) == (pytest.approx(0.83), "Dog")
    clip = episodes.clip_path(tmp_path, row.id)
    with wave.open(str(clip)) as audio:
        assert (audio.getnchannels(), audio.getframerate(), audio.getnframes()) == (
            1,
            RATE,
            RATE * 2,
        )


def test_recorder_keeps_nothing_by_default_and_works_without_a_model(tmp_path):
    rec, episodes = recorder(tmp_path)
    rec.record(episode(samples=tone(count=RATE)))
    [row] = episodes.list()
    assert row.dog_score is None
    assert not episodes.clip_path(tmp_path, row.id).exists()


def test_old_clips_are_removed(tmp_path):
    import os
    import time

    rec, episodes = recorder(tmp_path, keep_days=1)
    rec.record(episode(samples=tone(count=RATE)))
    [row] = episodes.list()
    clip = episodes.clip_path(tmp_path, row.id)
    old = time.time() - 2 * 86400
    os.utime(clip, (old, old))
    rec.record(episode(start=START + timedelta(minutes=5), samples=tone(count=RATE)))
    assert not clip.exists()


def test_labels_and_evaluation(tmp_path):
    episodes = VocalEpisodes(events_database(tmp_path))
    ids = [
        episodes.add(
            START + timedelta(minutes=i),
            START + timedelta(minutes=i, seconds=3),
            2.0,
            -10.0,
            dog_score=s,
            top_label="x",
        )
        for i, s in enumerate([0.9, 0.7, 0.2, 0.1])
    ]
    for identifier, label in zip(ids, ["dog", "dog", "other", "dog"], strict=True):
        episodes.label(identifier, label, "家族")
    with pytest.raises(ValueError, match="cat"):
        episodes.label(ids[0], "cat", "家族")
    with pytest.raises(KeyError):
        episodes.label(9999, "dog", "家族")
    result = episodes.evaluation(threshold=0.5)
    # 今の判断は記録した区間をすべて鳴き声と数えるので、適合率は 3/4。
    assert result.labeled == 4
    assert result.band_precision == pytest.approx(0.75)
    # YAMNet は 0.5 以上だけを鳴き声とし、鳴き声3つのうち2つを当てた（外れは0）。
    assert result.model_precision == pytest.approx(1.0)
    assert result.model_recall == pytest.approx(2 / 3)


def test_audio_model_settings():
    config = Config()
    assert (config.audio_model, config.audio_keep_days) == (False, 0)
    with pytest.raises(ConfigError, match=r"audio\.keep_days"):
        Config(audio_keep_days=40)


# --- 画面 ---


def test_vocal_page_plays_kept_clips_labels_and_compares(gui):
    from support import exchange, login, submit

    server, store, _ = gui
    episodes = VocalEpisodes(store.database)
    now = datetime.now().astimezone()
    kept = episodes.add(
        now, now + timedelta(seconds=3), 2.0, -10.0, dog_score=0.82, top_label="Dog"
    )
    episodes.add(
        now, now + timedelta(seconds=3), 2.0, -12.0, dog_score=0.05, top_label="Television"
    )
    clip = episodes.clip_path(server.app.root, kept)
    clip.parent.mkdir(parents=True)
    with wave.open(str(clip), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(tone(count=RATE).tobytes())
    cookie = login(server, role="member")
    _, _, body = exchange(server, "GET", "/vocal", headers=cookie, auth=False)
    html = body.decode()
    assert "犬らしさ 0.82（Dog）" in html
    assert "モデル：ほかの音らしい" in html
    assert f'src="/vocal-audio?id={kept}"' in html
    assert "今の判断と鳴き声のモデルの比較" not in html
    status, headers, data = exchange(
        server, "GET", f"/vocal-audio?id={kept}", headers=cookie, auth=False
    )
    assert status == 200
    assert headers["content-type"] == "audio/wav"
    assert data[:4] == b"RIFF"
    for bad in ("x", "-1", "9999"):
        status, _, _ = exchange(server, "GET", f"/vocal-audio?id={bad}", headers=cookie, auth=False)
        assert status == 404
    status, _ = submit(server, "/vocal/label", {"id": str(kept), "label": "dog"}, page="/vocal")
    assert status == 303
    _, _, body = exchange(server, "GET", "/vocal", headers=cookie, auth=False)
    assert "今の判断と鳴き声のモデルの比較" in body.decode()
    status, _ = submit(server, "/vocal/label", {"id": "abc", "label": "dog"}, page="/vocal")
    assert status == 400
