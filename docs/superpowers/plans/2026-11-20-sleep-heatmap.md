# 睡眠時間の記録と見える化 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 監視中の犬検出と画素の動きから1分ごとの睡眠の状態を `events.sqlite3` に記録し、GUI の `/sleep` に日付 × 時刻の草の画面・日ごとの合計・日中の合計・いつもとの差を出す。

**Architecture:** 判定は純粋な状態機械（`features/sleep.py`）と、画像の計算（`features/sleep_motion.py`）、保存（`features/sleep_store.py`）に分ける。`SleepRecorder` がそれらを束ね、監視の拡張 `SleepExtension` が位置合わせ前の映像（`on_camera_frame`）で呼ぶ。GUI は `gui/sleep_page.py` が値を整え、`templates/sleep.html` が描く。

**Tech Stack:** Python 3.12、OpenCV、NumPy、SQLite、Starlette + Jinja2、pytest、Ruff、mypy、uv。

**Spec:** `docs/superpowers/specs/2026-11-20-sleep-heatmap-design.md`

## Global Constraints

- 状態は `asleep` / `awake` / `absent` / `unknown` の4つ。`absent` は起きているとして数え、`unknown` は合計に含めない。
- 1分の中で観測できた秒数が30秒未満なら `unknown`。混ざったら秒数の多い状態。
- 既定値: `sleep.enabled` = false、`sleep.still_minutes` = 5、`sleep.stir_seconds` = 10、`sleep.daytime` = `09:00-18:00`。
- 静止の条件: 枠の中心の移動が枠の対角の 10% 未満、かつ枠の中の画素の変化が小さいこと。
- 草: 横＝日付、縦＝0〜23時、色は 0 / 1〜15 / 16〜30 / 31〜45 / 46〜60 分の5段階。`unknown` が30分以上のマスは「記録なし」。期間は 7・28・91 日（既定 28）。
- 記録できた割合が 80% 未満の日は「参考」の印を付け、いつもとの差の平均から除く。いつもとの差は直近7日の平均と、その前の28日の平均の比較。
- 時刻は UTC の ISO 8601 で保存し、表示と日・時の区切りはローカル時刻（`timezones.py`）。
- 犬検出が必要（`dog.enabled`）。行動マップと推論を二重に行わない。
- AGENTS.md の規約: テスト先行、日本語のコメントと docstring（`D100`・`D101`）、型注釈、`uv run pytest --cov`・`uv run ruff check .`・`uv run ruff format --check .`・`uv run mypy` を通す。移行処理は `MIGRATIONS` の末尾に足し、既存は書き換えない。HTML はテンプレートで作り、`Markup` を使わない。

## Review Focus

- 監視を起動した直後、犬が丸まって寝ていて検出されない → `absent` ではなく `unknown`（Task 2 の `test_unknown_when_no_history_and_no_detection`）。
- 0時をまたぐ睡眠 → 前の日と次の日の時刻のマスに正しく分かれる（Task 5 の `test_sleep_across_midnight_splits_into_days`）。
- 監視を止めたときに判定中の分が失われない、または「寝ていた」と誤って書かれない → `awake` で書かれる（Task 3 の `test_close_flushes_pending_as_awake`）。
- 記録がまったくない期間を開く → 例外にならず、全マス「記録なし」と「記録がありません」の案内（Task 5 の `test_empty_period_renders`）。
- `sleep.daytime` の終わりが始まりより前（`22:00-06:00`）などの不正な値 → 設定の検証で断る（Task 4 の `test_daytime_rejects_invalid`）。

---

### Task 1: 睡眠の記録のテーブルとストア

**Files:**
- Modify: `src/poop_detector/records/database.py`（`migrate_to_v28` と `MIGRATIONS` の末尾）
- Create: `src/poop_detector/features/sleep_store.py`
- Test: `tests/test_sleep_store.py`、`tests/test_database.py`

**Interfaces:**
- Produces:
  - `SleepState = Literal["asleep", "awake", "absent", "unknown"]`（`sleep_store.py`）
  - `MinuteRecord(minute: datetime, state: SleepState, observed_seconds: float)`（frozen dataclass。`minute` はタイムゾーン付き、分の始まり）
  - `class SleepStore(database: Database)`:
    - `write_minutes(records: Sequence[MinuteRecord]) -> None` — 同じ分があれば上書き（`INSERT ... ON CONFLICT(minute) DO UPDATE`）
    - `add_detection(hour: datetime, present_seconds: float, detected_seconds: float) -> None` — 時の始まりごとに加算
    - `minutes(start: datetime, end: datetime) -> list[MinuteRecord]` — `start <= minute < end`、時刻順
    - `detection_hours(start: datetime, end: datetime) -> dict[datetime, tuple[float, float]]`

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_write_and_read_minutes(tmp_path):
    store = SleepStore(events_database(tmp_path))
    at = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
    store.write_minutes([MinuteRecord(at, "asleep", 60), MinuteRecord(at + timedelta(minutes=1), "absent", 45)])
    store.write_minutes([MinuteRecord(at, "awake", 50)])
    rows = store.minutes(at, at + timedelta(hours=1))
    assert [(r.state, r.observed_seconds) for r in rows] == [("awake", 50), ("absent", 45)]

def test_detection_hours_accumulate(tmp_path):
    store = SleepStore(events_database(tmp_path))
    hour = datetime(2026, 11, 20, 3, tzinfo=UTC)
    store.add_detection(hour, 10, 4)
    store.add_detection(hour, 5, 5)
    assert store.detection_hours(hour, hour + timedelta(hours=1)) == {hour: (15, 9)}
```

`tests/test_database.py` に `test_sleep_migration_preserves_existing_clip(tmp_path)` を既存の移行テスト（`test_tray_stay_migration_preserves_an_existing_video_and_assessment`）と同じ形で足し、版27のデータベースに入れた動画が残り、`sleep_minutes`・`sleep_detection_hours` が作られることを確かめる。

- [ ] **Step 2: 失敗を確認** — `uv run pytest tests/test_sleep_store.py tests/test_database.py -k sleep -v` → ImportError / テーブルなし。
- [ ] **Step 3: 実装** — `migrate_to_v28` で `sleep_minutes(minute TEXT PRIMARY KEY, state TEXT NOT NULL, observed_seconds REAL NOT NULL)` と `sleep_detection_hours(hour TEXT PRIMARY KEY, present_seconds REAL NOT NULL, detected_seconds REAL NOT NULL)` を作る。`SleepStore` は UTC の ISO 8601 で読み書きし、`state` は `get_args(SleepState)` で確かめる。`records/backup.py` の `ALLOWED`・`SECRET_TABLES` は変えない（秘密を含まないテーブルなので）ことを確かめる。
- [ ] **Step 4: 合格を確認** — 同じコマンドで PASS。
- [ ] **Step 5: コミット** — `feat: 睡眠の記録のテーブルとストアを追加`

### Task 2: 1秒ごとの観測から1分ごとの状態を決める状態機械

**Files:**
- Create: `src/poop_detector/features/sleep.py`
- Test: `tests/test_sleep.py`

**Interfaces:**
- Consumes: `SleepState`、`MinuteRecord`（Task 1）
- Produces:
  - `Presence = Literal["still", "moving", "left", "unknown"]` — 1秒分の観測。`still`／`moving` はサークル内にいて静止／動いている、`left` は出ていった（または映っていない）、`unknown` は観測できない。
  - `class SleepJudge(still_seconds: float, stir_seconds: float)`:
    - `observe(stamp: datetime, presence: Presence) -> list[MinuteRecord]` — 確定した分を返す
    - `interrupt() -> None` — 静止の継続を捨てる（接続の途切れ）
    - `close(stamp: datetime) -> list[MinuteRecord]` — 判定中の分を確定して返す

アルゴリズム（シグネチャとテストで決まらないところ）:

```text
秒ごとに「仮の状態」を持つ: still→(静止中), moving→awake, left→absent, unknown→unknown
静止が始まった時刻 still_since を持つ。moving が stir_seconds を超えて続いたら still_since を捨てる
（stir_seconds 以内の moving は静止中の秒として数える）。
still_since から still_seconds 経ったら、still_since 以降の静止中の秒をすべて asleep にする。
still_seconds に届かずに静止が途切れたら、その静止中の秒は awake にする。
分は「その分の秒がすべて確定し、かつ分が終わった」ときに MinuteRecord にして返す。
分の状態は秒数の多い状態（unknown を除く観測秒数が 30 未満なら unknown）。
observed_seconds は unknown 以外の秒数。
```

- [ ] **Step 1: 失敗するテストを書く**（`still_seconds=300, stir_seconds=10`、秒ごとに `observe` を呼ぶ補助を `tests/test_sleep.py` 内に置く）

```python
def test_still_for_five_minutes_becomes_asleep_from_start(): ...
    # 0:00:00 から 6分 still → 0:00〜0:05 の5分が asleep、observed_seconds == 60
def test_short_stillness_is_awake(): ...
    # 4分 still → 1分 moving → 4分 awake, 1分 awake
def test_stir_within_ten_seconds_keeps_sleeping(): ...
    # 3分 still, 8秒 moving, 3分 still → 6分すべて asleep 側（moving の8秒を含む分も asleep）
def test_stir_longer_than_ten_seconds_breaks_stillness(): ...
def test_left_is_absent(): ...
def test_less_than_thirty_observed_seconds_is_unknown(): ...
def test_interrupt_discards_stillness(): ...
    # 3分 still → interrupt → 3分 still → すべて awake
def test_close_returns_pending_minutes_as_awake(): ...
    # 2分 still のあと close → 2分 awake
```

- [ ] **Step 2: 失敗を確認** — `uv run pytest tests/test_sleep.py -v` → ImportError。
- [ ] **Step 3: 実装** — `SleepJudge` を `features/sleep.py` に書く。分の区切りは `stamp` のローカル時刻ではなく UTC の分（分の区切りはどちらでも同じ）。
- [ ] **Step 4: 合格を確認** — 同じコマンドで PASS。
- [ ] **Step 5: コミット** — `feat: 睡眠の状態を1分ごとに決める判定を追加`

### Task 3: 映像から1秒の観測を作り、記録する

**Files:**
- Create: `src/poop_detector/features/sleep_motion.py`
- Modify: `src/poop_detector/features/sleep.py`（`SleepRecorder` を足す）
- Test: `tests/test_sleep_motion.py`、`tests/test_sleep.py`

**Interfaces:**
- Consumes: `SleepJudge`、`Presence`（Task 2）、`SleepStore`（Task 1）、`ActivityLayout`（`features/activity_map.py`）、`Box`・`Predict`（`features/dog.py`）
- Produces:
  - `class MotionSampler(boundary: NDArray[Any] | None)`（`sleep_motion.py`。`boundary` は床面の正規化座標の多角形、None なら画面全体）:
    - `sample(frame: Frame, boxes: Sequence[Box]) -> tuple[Presence, bool]` — (観測, 犬検出が当たったか)
    - `reset() -> None`
  - `class SleepRecorder(store: SleepStore, judge: SleepJudge, sampler: MotionSampler)`（`sleep.py`）:
    - `update(frame: Frame, predict: Predict, tick: float, stamp: datetime | None = None) -> None` — `tick` の差が1秒未満なら何もしない（`ActivityRecorder.update` と同じ間引き）
    - `interrupt() -> None`、`close(stamp: datetime | None = None) -> None`
  - `sleep_recorder(database: Database, root: Path, config: Config) -> SleepRecorder`（`sleep.py`。床面があれば `ActivityLayout.load(root).roi.points` を境界に、なければ None）

`MotionSampler.sample` の規則（設計書の「検出が外れたとき」）:

```text
画像は縮小した灰色（定数 MOTION_WIDTH）。前回の画像との差分が定数 PIXEL_DELTA を超える画素の割合を測る。
床面の中にある枠（中心で判定）がちょうど1つ:
    前回の枠があり、中心の移動 < 対角 * 0.1 かつ 枠の中の変化率 < STILL_RATIO → "still"、ほかは "moving"
    居場所 = 枠、直前の動き = (moving か)
枠がない（床面の外だけ・検出なし）:
    居場所あり かつ 床面の中の変化率 < STILL_RATIO → 直前の観測を保つ（still なら still）
    居場所あり かつ 直前に moving（縁に向かう動き、または床面の変化あり）→ "left"、居場所を消す
    居場所なし かつ 床面の中の変化率 < STILL_RATIO → "unknown"
    居場所なし かつ 変化あり → "unknown"
枠が2つ以上 → "unknown"
```

`left` のあと床面が静かな間は `left` を保つ（犬が外にいて静かなときに `unknown` に戻さない）。定数 `MOTION_WIDTH`・`PIXEL_DELTA`・`STILL_RATIO` の値は人工画像のテストで選ぶ。

`SleepRecorder.update` は、`predict` の例外（`cv2.error`・`OSError`・`ValueError`）を警告のログにして `unknown` として扱い、`store` の `sqlite3.Error` も警告にして監視を止めない（`ActivityRecorder.update` と同じ方針）。サークル内にいるとみなした秒（観測が `still`・`moving`）と、そのうち検出が当たった秒を、ローカル時刻の時の始まりで `store.add_detection` に足す。

- [ ] **Step 1: 失敗するテストを書く**（NumPy の黒い画像に白い矩形を犬として置き、`boxes` を直接渡す）

```python
def test_still_box_is_still(): ...
def test_moving_box_is_moving(): ...
def test_lost_detection_without_motion_keeps_still(): ...
def test_lost_detection_after_moving_toward_edge_is_left(): ...
def test_left_stays_left_while_floor_is_quiet(): ...
def test_unknown_when_no_history_and_no_detection(): ...
def test_box_outside_floor_is_ignored(): ...
def test_two_boxes_are_unknown(): ...
# tests/test_sleep.py
def test_recorder_writes_asleep_minutes(tmp_path): ...  # 偽の predict で6分静止 → store に asleep
def test_close_flushes_pending_as_awake(tmp_path): ...
def test_predict_error_is_unknown_and_logged(tmp_path, caplog): ...
def test_detection_ratio_is_recorded(tmp_path): ...
```

- [ ] **Step 2: 失敗を確認** — `uv run pytest tests/test_sleep_motion.py tests/test_sleep.py -v`
- [ ] **Step 3: 実装** — 上の規則で `MotionSampler`、`SleepRecorder`、`sleep_recorder` を書く。
- [ ] **Step 4: 合格を確認**
- [ ] **Step 5: コミット** — `feat: 映像の動きと犬検出から睡眠を記録`

### Task 4: 設定と監視への組み込み

**Files:**
- Modify: `src/poop_detector/config.py`（`SETTINGS`、`Config` の属性、`enabled_features`、`with_flags`、`check_monitor_features`）
- Modify: `src/poop_detector/features/monitoring.py`（`SharedPredict`、`SleepExtension`、`standard_extensions`）
- Modify: `src/poop_detector/commands/run.py`、`src/poop_detector/main.py`（`--sleep`）
- Modify: `config/config.example.yaml`、`README.md`（機能表に「任意」の行）、`docs/architecture.md`（拡張の表と `events.sqlite3` の台帳）
- Test: `tests/test_config.py`（設定）、`tests/test_monitor_stages.py`（`SharedPredict`・`SleepExtension`・`standard_extensions`）、`tests/test_commands.py`（`check_monitor_features`）、`tests/test_cli.py`（`run --sleep`）

**Interfaces:**
- Consumes: `SleepRecorder`、`sleep_recorder`（Task 3）
- Produces:
  - `Config.sleep_enabled: bool = False`、`sleep_still_minutes: float = 5`（`_number(1, 60)`）、`sleep_stir_seconds: float = 10`（`_number(0, 120)`）、`sleep_daytime: str = "09:00-18:00"`
  - `parse_daytime(value: str) -> tuple[time, time]`（`config.py`。`HH:MM-HH:MM` で始まり < 終わり。違えば `ValueError`）
  - `with_flags(..., sleep: bool = False)`
  - `class SharedPredict(predict: Predict)`（`features/monitoring.py`）: `__call__(frame: Frame) -> list[Box]` — 直前と同じフレームのオブジェクト（`is`）なら前の結果を返す
  - `class SleepExtension(recorder: SleepRecorder, predict: Predict)`: `on_camera_frame` で途切れなら `interrupt`、フレームがあれば `update`
  - `standard_extensions(..., sleep: SleepRecorder | None = None)`

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_daytime_accepts_default():
    assert parse_daytime("09:00-18:00") == (time(9), time(18))
@pytest.mark.parametrize("value", ["22:00-06:00", "9-18", "25:00-26:00", ""])
def test_daytime_rejects_invalid(value): ...  # 設定の読み込みで ConfigError
def test_sleep_requires_dog(): ...  # check_monitor_features が dog_enabled=True を補う
def test_shared_predict_runs_once_per_frame(): ...  # 同じフレームで2回呼んでも predict は1回
def test_standard_extensions_adds_sleep(): ...  # 行動マップと睡眠の両方で SharedPredict を共有
def test_run_with_sleep_records_minutes(tmp_path, monkeypatch): ...  # test_cli.py の偽カメラで run --sleep
```

- [ ] **Step 2: 失敗を確認** — `uv run pytest tests/test_config.py tests/test_monitor_stages.py tests/test_commands.py tests/test_cli.py -k "sleep or daytime or shared_predict" -v`
- [ ] **Step 3: 実装** — `check_monitor_features` で `sleep_enabled` も犬検出を補う条件に加える。`run.py` で `sleep_recorder(events_database(context.root), context.root, config)` を作り `standard_extensions` に渡し、`watch` の `preview.mark_stopped()` と同じ終了処理の中で `recorder.close()` を呼ぶ。`DogObserver.predict` を `SharedPredict` で包んで `ActivityExtension` と `SleepExtension` に同じものを渡す。`enabled_features` に `"sleep_enabled": "睡眠の記録"`。
- [ ] **Step 4: 合格を確認**
- [ ] **Step 5: 全体を確認** — `uv run pytest`・`uv run ruff check .`・`uv run ruff format --check .`・`uv run mypy`
- [ ] **Step 6: コミット** — `feat: 監視中に睡眠を記録する設定と --sleep を追加`

### Task 5: 睡眠の画面の値を整える

**Files:**
- Create: `src/poop_detector/gui/sleep_page.py`
- Test: `tests/test_sleep_page.py`

**Interfaces:**
- Consumes: `SleepStore`（Task 1）、`parse_daytime`（Task 4）、`local_today`（`timezones.py`）
- Produces:
  - `HourCell(day: date, hour: int, asleep_minutes: int, observed_minutes: int, detection_ratio: float | None, level: int | None)` — `level` は 0〜4、記録なしは None
  - `DaySummary(day: date, asleep_minutes: int, observed_minutes: int, daytime_minutes: int, reliable: bool)` — `reliable` は記録できた分が 1,440 の 80% 以上
  - `Trend(recent_minutes: float | None, usual_minutes: float | None, diff_minutes: float | None, diff_ratio: float | None)`
  - `SleepView(days: list[date], rows: list[list[HourCell]], summaries: list[DaySummary], trend: Trend, period: int)` — `rows[hour][i]` は `days[i]` の `hour` 時
  - `sleep_view(store: SleepStore, last_day: date, period: int, daytime: tuple[time, time]) -> SleepView`

行がない分は `unknown` として数える（`observed_minutes` に入らない）。`level`: 記録なし（unknown が30分以上）→ None、asleep 0 → 0、1〜15 → 1、16〜30 → 2、31〜45 → 3、46〜60 → 4。`Trend` は `last_day` までの7日と、その前の28日の `reliable` な日の平均。どちらかに該当日がなければ None。

- [ ] **Step 1: 失敗するテストを書く**（`store.write_minutes` で人工の記録を入れる）

```python
def test_levels_by_asleep_minutes(tmp_path): ...  # 0, 10, 20, 40, 50 分 → 0,1,2,3,4
def test_hour_with_mostly_unknown_is_no_record(tmp_path): ...  # level is None
def test_daytime_minutes_counted(tmp_path): ...  # 9〜18時の asleep だけ daytime_minutes
def test_unreliable_day_excluded_from_trend(tmp_path): ...
def test_sleep_across_midnight_splits_into_days(tmp_path): ...  # 23:30〜0:30 → 前日23時30分・翌日0時30分
def test_empty_period_renders(tmp_path): ...  # すべて level None、trend の値は None
```

- [ ] **Step 2: 失敗を確認** — `uv run pytest tests/test_sleep_page.py -v`
- [ ] **Step 3: 実装** — 期間の始まりから終わりまでのローカル時刻の範囲を UTC にして `store.minutes`・`store.detection_hours` を1回ずつ読み、Python で集計する。
- [ ] **Step 4: 合格を確認**
- [ ] **Step 5: コミット** — `feat: 睡眠の画面の集計を追加`

### Task 6: 睡眠の画面（ルート・テンプレート・メニュー）

**Files:**
- Modify: `src/poop_detector/gui/routes.py`（`@get("/sleep")`）、`src/poop_detector/gui/navigation.py`（メインメニュー）、`src/poop_detector/gui/server.py`（`GuiApp` に `sleep: SleepStore` を足し、作る場所で `SleepStore(database)`）、`src/poop_detector/gui/icons.py`（必要ならアイコン）
- Modify: `src/poop_detector/gui/sleep_page.py`（`render_sleep(app, query) -> str`）
- Create: `src/poop_detector/gui/templates/sleep.html`
- Modify: `src/poop_detector/gui/templates/style.css`（草の色の変数をライト・ダークで）
- Modify: `docs/screens.md`（画面の構成）
- Test: `tests/test_sleep_page.py`、`tests/test_gui_escaping.py`、`tests/test_gui_roles.py`（必要なら権限の表）

**Interfaces:**
- Consumes: `sleep_view`、`SleepView`（Task 5）
- Produces: `render_sleep(app: GuiApp, query: Mapping[str, list[str]]) -> str`

画面の内容: 期間の切り替え（7・28・91日。`?days=` で、既定28、不正な値は28）、草（縦 0〜23時・横 日付、凡例「少ない □□□□ 多い」と「記録なし」）、各マスの `title` に「11/20 3時 睡眠 45分／記録 60分／検出 80%」、日ごとの合計と日中の合計の行（`reliable` でない日は「参考」）、いつもとの差の文（例「直近7日の平均 12時間30分（いつもより +40分、+6%）」）、記録がない期間には「睡眠の記録がありません。`run --sleep` か設定の sleep.enabled で記録を始めます」。`daytime` は `Config.load(app.config_path)` か既定の `Config()` から読む。

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_sleep_page_shows_grid_and_totals(client_with_records): ...  # 24行、凡例、合計の文字列
def test_sleep_page_invalid_days_falls_back_to_28(...): ...
def test_sleep_page_requires_login(...): ...
# test_gui_escaping.py の各画面の一覧に /sleep を加える
```

既存の GUI のテストの補助（`tests/conftest.py` のログイン済みクライアント）を使う。

- [ ] **Step 2: 失敗を確認** — `uv run pytest tests/test_sleep_page.py tests/test_gui_escaping.py tests/test_gui_roles.py -v`
- [ ] **Step 3: 実装** — テンプレートは値を受け取って並べるだけにし、分の時間表記（「12時間30分」）は `sleep_page.py` で作る。色は `style.css` の変数（`--sleep-0`〜`--sleep-4`、`--sleep-none`）で、添付の GitHub の草に近い緑の5段階にする。
- [ ] **Step 4: 合格を確認**
- [ ] **Step 5: 全体を確認** — `uv run pytest --cov`（`fail_under` を下回らないこと。上がったら下限を引き上げる）・`uv run ruff check .`・`uv run ruff format --check .`・`uv run mypy`
- [ ] **Step 6: 人工データでスクリーンショットを撮る** — 一時ディレクトリに人工の記録を入れて `uv run poop-detector gui` を開き、ライト・ダークの両方で `/sleep` を撮る（PR に添える）。
- [ ] **Step 7: コミット** — `feat: 睡眠の草の画面を追加`
