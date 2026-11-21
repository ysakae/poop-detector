# ごはんの量の目安と相談 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 子犬の月齢・体重・体格・フードのカロリーから1日のごはんの量の目安を計算して示す。家族で共有する相談の会話で、その目安と記録を前提に AI に相談できるようにする。

**Architecture:** 目安の計算は副作用のない関数（`records/feeding_advice.py`）にする。フード・会話・カロリー探しは `events.sqlite3` の版31のテーブルに置く。質問とカロリー探しは台帳に `queued` で入れ、GUI のサーバーの中の作業スレッド（`gui/advice_worker.py`）が既存の判定器の CLI を呼ぶ。画面は回答が届くまで待ちの表示を出す。

**Tech Stack:** Python 3.12、SQLite、Starlette と Jinja2、既存の判定器の CLI（Codex CLI・Claude Code CLI・Antigravity CLI）、pytest・Ruff・mypy（`uv run`）。

**Spec:** `docs/superpowers/specs/2026-11-21-feeding-advisor-design.md`

## Global Constraints

- コメント・docstring・画面の文言・コミットメッセージは日本語で書く。コミットは Conventional Commits 形式にする。
- 各タスクで `uv run pytest`、`uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy` を通す。カバレッジの下限は `pyproject.toml` の `fail_under = 94.83`。実測が上がったら引き上げる。
- `src/` の関数には、引数と戻り値の型注釈を付ける。`any` 型の代わりに `object` か `Any` を使い、値の形は実行時に確かめる。
- スキーマの変更は `MIGRATIONS` の末尾に `migrate_to_v31` として1つだけ足す。既存の移行処理は書き換えない。
- HTML は Jinja2 のテンプレートで作る（自動エスケープ）。`Markup` は使わない。JavaScript は `gui/static/` に置き、`asset(...)` で読み込む。
- テストでは実際の CLI を呼ばない。判定器は偽物に差し替え、`tmp_path` を使う。
- 量の数字は計算で決める。AI の回答から給餌の予定や記録を書き換えない。
- 画面には「目安です。獣医師の指示があればそちらを優先してください。」を常に出す。
- カロリーの範囲は 30〜700 kcal/100g、保存は 0.1 kcal 単位の整数（列名の接尾辞 `_dk`）。
- 相談とカロリー探しの呼び出しは、合わせて1時間に20回まで（`HOURLY_LIMIT = 20`）。動画の判定の上限（`review.max_per_hour`）の数には入れない。動画の判定が待っていても、相談を後回しにしない。

## Review Focus

1. 生年月日が体重の測定日より後、または未来になっている：目安を出さず、「生年月日を確かめてください」を `missing` に入れる（Task 4 にテストあり）。
2. 送信を二度押す・家族が同時に送る：回答待ちの会話への2回目の質問を断る（Task 6 にテストあり）。同じフード名のカロリー探しは1件にまとめる（Task 5 にテストあり）。
3. AI が範囲外のカロリーや https でない URL を返す：その値を下書きに入れず、空のまま手入力を促す（Task 8 にテストあり）。
4. 2つの GUI が同じデータを見ている：同じ仕事を二重に取らない（Task 7 にテストあり）。
5. プロフィールのフードを止めた（`active=0`）・消えた：フードなしとして扱い、kcal/日だけを出す（Task 4・Task 11 にテストあり）。

---

### Task 1: 版31のスキーマ

**Files:**
- Modify: `src/poop_detector/records/database.py`（`migrate_to_v31` を足し、`MIGRATIONS` の末尾に加える）
- Modify: `src/poop_detector/records/weights.py:57-63`（`save_birth_date` の INSERT に列名を書く）
- Test: `tests/test_database.py`

**Interfaces:**
- Produces: 次のテーブルと列。
  - `pet_profile` に `breed TEXT`、`size_class TEXT CHECK(size_class IN ('toy','small','medium','large','giant'))`、`adult_grams INTEGER CHECK(adult_grams > 0)`、`neutered INTEGER CHECK(neutered IN (0,1))`、`food_id TEXT` を足す。
  - `foods(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, kcal_dk INTEGER NOT NULL CHECK(kcal_dk BETWEEN 300 AND 7000), source_url TEXT NOT NULL DEFAULT '', created_by TEXT, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)`
  - `food_lookups(id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','running','done','failed')), product_name TEXT NOT NULL DEFAULT '', maker TEXT NOT NULL DEFAULT '', kcal_dk INTEGER, source_url TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', failure TEXT, requested_by TEXT, requested_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)`
  - `advice_threads(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT)`
  - `advice_messages(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES advice_threads(id), seq INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')), author TEXT, body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK(status IN ('queued','running','done','failed')), failure TEXT, basis TEXT NOT NULL DEFAULT '', provider TEXT, model TEXT, created_at TEXT NOT NULL, started_at TEXT, answered_at TEXT, UNIQUE(thread_id, seq))`
  - インデックス `advice_messages_status ON advice_messages(status)` と `food_lookups_status ON food_lookups(status)`。
  - 時刻はすべて UTC のタイムゾーン付き ISO 8601。質問の行は作った時点で `status='done'` にする。

- [ ] **Step 1: 失敗するテストを書く**

`test_v30_database_keeps_profile_and_weights_after_v31`：版30のデータベースを一時ディレクトリに作り（既存のテストの作り方に合わせる）、`pet_profile` に `(1, '2026-06-15')`、`weights` に1行を入れる。`Database(path)` で開き直すと版が31で、`birth` と体重が残り、`breed` が NULL で、5つの新しいテーブルがあることを確かめる。

`test_save_birth_date_after_profile_columns_are_added`：版31で `save_birth_date(db, "2026-06-15")` が通り、`birth_date(db) == date(2026, 6, 15)` になる。

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_database.py -k "v31 or birth_date_after" -v`
Expected: FAIL（版が30、または `INSERT INTO pet_profile VALUES` の列数の誤り）

- [ ] **Step 3: `migrate_to_v31(db: sqlite3.Connection) -> None` を書き、`save_birth_date` を `INSERT INTO pet_profile (id, birth) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET birth=excluded.birth` に直す**

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_database.py tests/test_gui_weights.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/records/database.py src/poop_detector/records/weights.py tests/test_database.py
git commit -m "feat: ごはんの相談のためのテーブルを版31で追加"
```

---

### Task 2: 判定の上限を用途で数え分ける

**Files:**
- Modify: `src/poop_detector/video/judge_runs.py:27`（`PURPOSES`）、`:122-129`（`count_since`）
- Modify: `src/poop_detector/gui/models_routes.py:49`（`PURPOSE_NAMES`）
- Test: `tests/test_judge_runs.py`

**Interfaces:**
- Produces: `PURPOSES = ("review", "benchmark", "advice", "food_lookup")`。`REVIEW_PURPOSES = ("review", "benchmark")`。`JudgeRunLog.count_since(self, since: datetime, purposes: Sequence[str] = REVIEW_PURPOSES) -> int`。
- 既存の呼び出し（`video/review.py:860`、`video/review_status.py:43`）は既定のまま使い、変えない。
- `PURPOSE_NAMES` に `"advice": "ごはんの相談"`、`"food_lookup": "フードのカロリー探し"` を足す。

- [ ] **Step 1: 失敗するテストを書く**

`test_count_since_ignores_advice_by_default`：`review` を1回、`advice` を2回、`food_lookup` を1回記録する。`count_since(t)` は 1、`count_since(t, ("advice", "food_lookup"))` は 3 を返す。

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_judge_runs.py -k count_since_ignores -v`
Expected: FAIL（`advice` が用途として断られる）

- [ ] **Step 3: `PURPOSES` を広げ、`count_since` に `purpose IN (...)` の条件を足す**

プレースホルダーは用途の数だけ作る。

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_judge_runs.py tests/test_gui_models.py tests/test_review*.py -q`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/video/judge_runs.py src/poop_detector/gui/models_routes.py tests/test_judge_runs.py
git commit -m "feat: 判定器の呼び出しの上限を用途ごとに数える"
```

---

### Task 3: 犬種の表とプロフィール

**Files:**
- Create: `src/poop_detector/records/breeds.py`
- Create: `src/poop_detector/records/profile.py`
- Test: `tests/test_profile.py`

**Interfaces:**
- Produces（`breeds.py`）：
  - `SizeClass = Literal["toy", "small", "medium", "large", "giant"]`、`SIZE_CLASSES: tuple[SizeClass, ...]`、`SIZE_NAMES: dict[SizeClass, str]`（超小型・小型・中型・大型・超大型）
  - `ADULT_MONTHS: dict[SizeClass, int] = {"toy": 10, "small": 10, "medium": 12, "large": 15, "giant": 18}`
  - `@dataclass(frozen=True) class Breed: name: str; size_class: SizeClass; adult_grams: int`
  - `BREEDS: tuple[Breed, ...]`（20前後。中身は作る人に任せる。トイ・プードル、チワワ、ミニチュア・ダックスフンド、ポメラニアン、柴、フレンチ・ブルドッグ、ゴールデン・レトリーバーを含める）
  - `find_breed(name: str) -> Breed | None`（前後の空白を除いて完全一致）
  - `size_from_adult(grams: int) -> SizeClass`（4000 g 未満 toy、10000 未満 small、25000 未満 medium、45000 未満 large、それ以上 giant）
- Produces（`profile.py`）：
  - `@dataclass(frozen=True) class PetProfile: birth: date | None; breed: str; size_class: SizeClass | None; adult_grams: int | None; neutered: bool | None; food_id: str | None`
  - `load_profile(database: Database) -> PetProfile`（行がなければすべて空）
  - `save_profile(database: Database, *, breed: str, size_class: str, adult_grams: int | None, neutered: str, food_id: str) -> None`（`birth` は変えない。行がなければ作る）
  - 検証の文言（`InvalidInputError`）：犬種は60文字以内で制御文字なし→「犬種は60文字以内で入力してください」。`size_class` は `SIZE_CLASSES` か空→「体格を選んでください」。`neutered` は `"1"`・`"0"`・`""`→「去勢・避妊の有無を選んでください」。`food_id` は空か `foods` の `active=1` の行→「選んだフードが見つかりません」。

- [ ] **Step 1: 失敗するテストを書く**

- `test_size_from_adult_thresholds`：3999→toy、4000→small、9999→small、10000→medium、24999→medium、25000→large、44999→large、45000→giant
- `test_find_breed_matches_exact_name`：`find_breed(" 柴 ").size_class` が表の値。`find_breed("雑種")` は None
- `test_profile_round_trip_keeps_birth`：`save_birth_date` のあとで `save_profile(breed="柴", size_class="medium", adult_grams=9000, neutered="1", food_id="")` を呼ぶと、`load_profile` の `birth` が残り、`neutered is True`、`food_id is None`
- `test_profile_rejects_unknown_food_and_size`：存在しない `food_id` と `size_class="huge"` で、上の文言の `InvalidInputError`

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_profile.py -v`
Expected: FAIL（モジュールがない）

- [ ] **Step 3: `breeds.py` と `profile.py` を書く**

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_profile.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/records/breeds.py src/poop_detector/records/profile.py tests/test_profile.py
git commit -m "feat: 犬種の表と犬のプロフィールの保存を追加"
```

---

### Task 4: 目安の計算

**Files:**
- Create: `src/poop_detector/records/feeding_advice.py`
- Modify: `src/poop_detector/records/care.py`（`feeding_totals` を足す）
- Test: `tests/test_feeding_advice.py`

**Interfaces:**
- Consumes: `PetProfile`、`find_breed`、`size_from_adult`、`ADULT_MONTHS`（Task 3）、`Weight`（`records/weights.py`）
- Produces：
  - `@dataclass(frozen=True) class Guide: months: int; weight_grams: int; weight_day: date; size_class: SizeClass; size_source: Literal["profile", "estimated", "default"]; adult_grams: int | None; ratio: float | None; stage: Literal["growth", "adult"]; rer: float; factor: float; kcal_per_day: float; grams_per_day: float | None; grams_per_meal: float | None; meals: int`
  - `@dataclass(frozen=True) class Advice: guide: Guide | None; warnings: tuple[str, ...]; missing: tuple[str, ...]`
  - `compute_advice(profile: PetProfile, latest: Weight | None, kcal_dk: int | None, meals: int, today: date) -> Advice`
  - `weekly_gain(rows: Sequence[Weight], today: date) -> float | None`（直近28日の測定の最小二乗の傾き×7。g/週。2件未満か、測定日が1種類なら None）
  - `@dataclass(frozen=True) class DayIntake: day: date; grams: float; kcal: float | None`
  - `intake_summary(totals: Mapping[date, Mapping[str, int]], kcal_by_food: Mapping[str, int], days: Sequence[date]) -> tuple[DayIntake, ...]`（`kcal` は、その日のフードがすべて台帳にあるときだけ出す）
  - `care.feeding_totals(database: Database, first: date, last: date) -> dict[date, dict[str, int]]`（ローカルの日付→フード名（前後の空白を除く）→ `amount_dg` の合計。削除済みと量のない記録は除く）

計算の規則（設計書の3節のとおり）：
- 月齢は、生年月日から `today` までの満の月数。
- 体格：`profile.size_class`→`size_source="profile"`。なければ予想体重（`profile.adult_grams`、なければ `find_breed(profile.breed).adult_grams`）から `size_from_adult`→`"estimated"`。どちらもなければ `"medium"`（成犬月齢12か月）→`"default"`。
- 成長期は `months < ADULT_MONTHS[size]`。係数は、予想体重があれば p<0.5 で3.0、p<0.8 で2.5、それ以外は2.0。予想体重がなければ、4か月未満で3.0、それ以外は2.0。成犬は `neutered is False` で1.8、それ以外は1.6。
- `rer = 70 * kg ** 0.75`、`kcal_per_day = rer * factor`、`grams_per_day = kcal_per_day / (kcal_dk / 1000)`（`kcal_dk` は0.1 kcal/100g 単位なので、1 g あたりは `kcal_dk / 1000`）、`grams_per_meal = grams_per_day / meals`（`meals >= 1` のときだけ）。

警告の文言（そのまま使う）：
- 「体重の記録が{n}日前のものです。量り直すと目安が正確になります。」（成長期で14日超、成犬で60日超）
- 「犬種か成犬時の予想体重を入れると目安が正確になります。」（`size_source != "profile"` のとき、または成長期で予想体重がないとき。重ねて出さない）
- 「フードのカロリーが分からないので、量（g）は出せません。」（`kcal_dk is None`）
- 「生後2か月未満の目安はこの計算の対象外です。獣医師に相談してください。」（`months < 2`）

`missing` の文言：「生年月日を入れてください」「体重を記録してください」「生年月日を確かめてください」（生年月日が体重の測定日より後）。`missing` があれば `guide` は None。

- [ ] **Step 1: 失敗するテストを書く**

- `test_puppy_guide_matches_hand_calculation`：誕生日 2026-06-15、今日 2026-11-21、体重 3200 g（2026-11-18）、`size_class="small"`、`adult_grams=5000`、`kcal_dk=3850`、`meals=3`。`months == 5`、`stage == "growth"`、`factor == 2.5`、`rer ≈ 167.48`、`kcal_per_day ≈ 418.70`、`grams_per_day ≈ 108.75`、`grams_per_meal ≈ 36.25`（`pytest.approx(abs=0.01)`）、`warnings == ()`
- `test_ratio_thresholds`：p=0.49→3.0、0.5→2.5、0.79→2.5、0.8→2.0
- `test_adult_factor_by_neutering`：24か月・10000 g・medium。`neutered=True`→`kcal ≈ 629.81`、`False`→`≈ 708.54`、`None`→1.6
- `test_unknown_size_defaults_to_medium_with_warning`：犬種なし・体格なし・予想体重なし・11か月→成長期、`factor == 2.0`、`size_source == "default"`、予想体重の警告が1つだけ
- `test_breed_table_supplies_adult_weight`：`breed="柴"`、体格なし→`size_source == "estimated"`、`adult_grams` が表の値
- `test_stale_weight_and_missing_kcal_warn_but_still_compute`：成長期で体重が15日前、`kcal_dk=None`→`guide.grams_per_day is None`、警告に「15日前」とカロリーの警告
- `test_under_two_months_warns`
- `test_missing_birth_or_weight_gives_no_guide`
- `test_birth_after_weight_day_is_rejected`（Review Focus 1）：誕生日が体重の測定日より後→`guide is None`、`missing == ("生年月日を確かめてください",)`
- `test_weekly_gain_uses_last_28_days`：28日より前の測定は無視し、7日で +120 g の2点→`120.0`。1点→None
- `test_intake_summary_kcal_only_when_all_foods_known`
- `test_feeding_totals_excludes_deleted_and_amountless`

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_feeding_advice.py -v`
Expected: FAIL（モジュールがない）

- [ ] **Step 3: `feeding_advice.py` と `care.feeding_totals` を書く**

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_feeding_advice.py tests/test_gui_care.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/records/feeding_advice.py src/poop_detector/records/care.py tests/test_feeding_advice.py
git commit -m "feat: 月齢と体格からごはんの量の目安を計算する"
```

---

### Task 5: フードの台帳とカロリー探しの依頼

**Files:**
- Create: `src/poop_detector/records/foods.py`
- Test: `tests/test_foods.py`

**Interfaces:**
- Produces：
  - `@dataclass(frozen=True) class Food: id: str; name: str; kcal_dk: int; source_url: str`
  - `parse_kcal(value: str) -> int`（全角数字は扱わない。`,` は `.` とみなす。30.0〜700.0、0.1単位。誤りは「カロリーは30〜700 kcal/100gの範囲で、0.1単位で入力してください」）
  - `parse_source_url(value: str) -> str`（空か、`https://` で始まり500文字以内で空白・制御文字なし。誤りは「出典はhttpsのURLで入力してください」）
  - `save_food(database: Database, *, name: str, kcal_dk: int, source_url: str, user_id: str, food_id: str = "", now: datetime | None = None) -> str`（名前は前後の空白を除いて1〜80文字。重複は「同じ名前のフードがすでにあります」。`food_id` があれば訂正）
  - `stop_food(database: Database, food_id: str) -> None`（`active=0`）
  - `active_foods(database: Database) -> list[Food]`（名前の順）
  - `kcal_by_name(database: Database) -> dict[str, int]`
  - `@dataclass(frozen=True) class Lookup: id: str; name: str; status: str; product_name: str; maker: str; kcal_dk: int | None; source_url: str; note: str; failure: str | None`
  - `request_lookup(database: Database, name: str, user_id: str, now: datetime | None = None) -> str`（同じ名前の `queued`・`running` があれば、その ID を返して新しい行を作らない）
  - `recent_lookups(database: Database, limit: int = 10) -> list[Lookup]`（新しい順）
  - `unregistered_names(database: Database) -> list[str]`（給餌の記録にあって `foods` にない名前。`care.feeding_history` の並びを使う）

- [ ] **Step 1: 失敗するテストを書く**

- `test_parse_kcal_range_and_step`：`"385"`→3850、`"385.5"`→3855、`"29.9"`・`"700.1"`・`"385.55"`・`"abc"` は誤り
- `test_parse_source_url_requires_https`：`"http://x"`・`"javascript:alert(1)"` は誤り
- `test_save_food_rejects_duplicate_name`
- `test_request_lookup_reuses_pending_request_for_same_name`（Review Focus 2）
- `test_unregistered_names_lists_feeding_foods_not_in_ledger`
- `test_stopped_food_leaves_active_list`

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_foods.py -v`
Expected: FAIL

- [ ] **Step 3: `foods.py` を書く**

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_foods.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/records/foods.py tests/test_foods.py
git commit -m "feat: フードの台帳とカロリー探しの依頼を追加"
```

---

### Task 6: 相談の会話の台帳

**Files:**
- Create: `src/poop_detector/records/advice.py`
- Test: `tests/test_advice_store.py`

**Interfaces:**
- Produces：
  - `@dataclass(frozen=True) class Message: id: str; thread_id: str; seq: int; role: Literal["user", "assistant"]; author_name: str; body: str; status: str; failure: str | None; created_at: datetime`
  - `@dataclass(frozen=True) class Thread: id: str; title: str; created_by: str | None; author_name: str; updated_at: datetime; state: str`（`state` は最後の回答の行の `status`）
  - `start_thread(database: Database, *, title: str, question: str, user_id: str, basis: str, now: datetime | None = None) -> str`
  - `ask(database: Database, thread_id: str, *, question: str, user_id: str, basis: str, now: datetime | None = None) -> None`
  - `retry(database: Database, message_id: str) -> None`（`failed` の回答だけを `queued` に戻し、`failure` を消す）
  - `threads(database: Database) -> list[Thread]`（削除済みを除き、`updated_at` の新しい順）
  - `thread(database: Database, thread_id: str) -> Thread | None`
  - `messages(database: Database, thread_id: str) -> list[Message]`（`seq` の順）
  - `delete_thread(database: Database, thread_id: str, *, user_id: str, admin: bool) -> None`
- 質問を1件入れるたびに、`role='user'`・`status='done'` の行と、`role='assistant'`・`status='queued'` の行を同じトランザクションで足し、`updated_at` を更新する。`author_name` は `users.name` を LEFT JOIN で読む。
- 文言：題名は1〜60文字→「題名は60文字以内で入力してください」。質問は1〜2000文字→「質問は2000文字以内で入力してください」。回答待ち→「前の質問の回答を待っています」。会話がない・削除済み→「相談が見つかりません」。削除できない人→「この相談を消せるのは、始めた人か管理者だけです」。

- [ ] **Step 1: 失敗するテストを書く**

- `test_start_thread_queues_answer`：`messages` が2件（`user`/`done`、`assistant`/`queued`）、`threads()[0].state == "queued"`
- `test_ask_is_refused_while_answer_pending`（Review Focus 2）：`queued` と `running` のどちらでも「前の質問の回答を待っています」
- `test_retry_requeues_only_failed_answer`
- `test_delete_thread_only_by_creator_or_admin`：別の人は断られ、管理者は消せる。消した会話は `threads()` から消え、`ask` は「相談が見つかりません」

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_advice_store.py -v`
Expected: FAIL

- [ ] **Step 3: `advice.py` を書く**

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_advice_store.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/records/advice.py tests/test_advice_store.py
git commit -m "feat: 家族で共有する相談の会話の台帳を追加"
```

---

### Task 7: 仕事の取り出し・上限・取り残しの整理

**Files:**
- Create: `src/poop_detector/features/advice/__init__.py`
- Create: `src/poop_detector/features/advice/jobs.py`
- Test: `tests/test_advice_jobs.py`

**Interfaces:**
- Produces：
  - `HOURLY_LIMIT = 20`、`STALE_SECONDS = 600`（カロリー探しの時間切れ300秒の2倍。Task 9 で `LOOKUP_TIMEOUT * 2 == STALE_SECONDS` をテストで確かめる）
  - `@dataclass(frozen=True) class Job: kind: Literal["answer", "lookup"]; id: str`
  - `claim_next(database: Database, now: datetime, used_last_hour: int) -> Job | None`：`used_last_hour >= HOURLY_LIMIT` なら None。`BEGIN IMMEDIATE` の中で、`advice_messages`（`role='assistant'`・`status='queued'`、`created_at` の順）と `food_lookups`（`status='queued'`、`requested_at` の順）のうち最も古い1件を `running` にし、`started_at` を入れて返す。
  - `expire_stale(database: Database, now: datetime) -> int`：`running` のまま `started_at` から `STALE_SECONDS` を過ぎた行を `failed`・`failure='timeout'` にし、件数を返す。
  - `finish_answer(database: Database, message_id: str, body: str, provider: str, model: str | None, now: datetime) -> None`
  - `@dataclass(frozen=True) class LookupResult: product_name: str; maker: str; kcal_dk: int | None; source_url: str; note: str`
  - `finish_lookup(database: Database, lookup_id: str, result: LookupResult, now: datetime) -> None`
  - `fail_job(database: Database, job: Job, failure: str, now: datetime) -> None`
  - `waiting_position(database: Database, message_id: str) -> int`（自分より前の `queued` の数。画面の「順番を待っています」に使う）
- `finish_*`・`fail_job` は `status='running'` の行だけを更新する（取り残しの整理で `failed` にした行を上書きしない）。

- [ ] **Step 1: 失敗するテストを書く**

- `test_claim_next_takes_oldest_across_answers_and_lookups`
- `test_claim_next_respects_hourly_limit`：`used_last_hour=20` で None、19で仕事を返す
- `test_two_claimers_never_take_same_job`（Review Focus 4）：別々の `Database` を同じファイルに2つ開き、`threading.Barrier` で同時に `claim_next` を呼ぶ。仕事が1件なら、片方だけが受け取る
- `test_expire_stale_fails_running_rows_after_timeout`：`STALE_SECONDS` ちょうどでは残し、過ぎたら `failed`
- `test_finish_after_expire_does_not_overwrite`

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_advice_jobs.py -v`
Expected: FAIL

- [ ] **Step 3: `jobs.py` を書く**

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_advice_jobs.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/features/advice tests/test_advice_jobs.py
git commit -m "feat: 相談とカロリー探しの仕事を1件ずつ取り出す"
```

---

### Task 8: 指示文と応答の検証

**Files:**
- Create: `src/poop_detector/features/advice/prompts.py`
- Create: `src/poop_detector/features/advice/context.py`
- Test: `tests/test_advice_prompts.py`

**Interfaces:**
- Consumes: `compute_advice`・`weekly_gain`・`intake_summary`（Task 4）、`load_profile`（Task 3）、`active_foods`・`parse_kcal`・`parse_source_url`（Task 5）、`Message`（Task 6）、`LookupResult`（Task 7）、`records.events.Store.records()`（`excretion_type` と `actual_start`/`timestamp`）
- Produces：
  - `ANSWER_SCHEMA: dict[str, Any]`：`{"type": "object", "properties": {"answer": {"type": "string", "maxLength": 6000}}, "required": ["answer"], "additionalProperties": False}`
  - `LOOKUP_SCHEMA: dict[str, Any]`：`product_name`・`maker`・`source_url`・`note`（文字列）と `kcal_per_100g`（数か null）がすべて必須で、`additionalProperties: False`
  - `@dataclass(frozen=True) class AdviceContext: pet_name: str; profile: PetProfile; advice: Advice; weights: tuple[Weight, ...]; intake: tuple[DayIntake, ...]; meals: int; excretions: tuple[tuple[date, int, int], ...]; foods: tuple[Food, ...]`（`excretions` は日付・排便の数・排尿の数）
  - `build_context(database: Database, store: Store, pet_name: str, today: date) -> AdviceContext`（体重は直近90日、多ければ等間隔に30点まで間引き、最新は必ず残す。給餌と排泄は直近14日）
  - `basis_text(advice: Advice) -> str`（質問の行の `basis` に入れる短い JSON。`months`・`weight_grams`・`kcal_per_day`・`grams_per_day` を丸めて入れる）
  - `answer_prompt(context: AdviceContext, history: Sequence[Message], question: str) -> str`（`history` は直近20件、本文の合計2万文字まで、古い方から削る。設計書の4節の指示を入れる）
  - `lookup_prompt(name: str) -> str`（メーカーの公式ページを優先し、代謝エネルギーの kcal/100g と出典の URL を返す。見つからなければ `kcal_per_100g` を null にする）
  - `parse_answer(output: object) -> str`（形が違う・空なら `ValueError`）
  - `parse_lookup(output: object) -> LookupResult`（`kcal_per_100g` を `parse_kcal(str(value))` で確かめ、範囲外は None にする。URL は `parse_source_url` を通らなければ空にする。文字列は各200文字、`note` は500文字に切る）

- [ ] **Step 1: 失敗するテストを書く**

- `test_answer_prompt_includes_guide_and_records`：目安の g/日、体重、直近の給餌、排泄の数、フード名が入る
- `test_answer_prompt_trims_history_to_limits`：21件・合計2万文字超の履歴で、最も古いものから落ちる
- `test_weights_are_thinned_to_30_points_keeping_latest`
- `test_parse_lookup_drops_out_of_range_kcal_and_non_https_url`（Review Focus 3）：`kcal_per_100g=900`→`kcal_dk is None`、`source_url="http://maker.example/"`→`""`
- `test_parse_answer_rejects_wrong_shape`

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_advice_prompts.py -v`
Expected: FAIL

- [ ] **Step 3: `context.py` と `prompts.py` を書く**

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_advice_prompts.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/features/advice tests/test_advice_prompts.py
git commit -m "feat: 相談とカロリー探しの指示文と応答の検証を追加"
```

---

### Task 9: CLI のウェブ検索の口

**Files:**
- Modify: `src/poop_detector/video/codex_cli.py`（`CodexCliJudge.lookup`）
- Modify: `src/poop_detector/video/providers.py`（`ClaudeCodeJudge.lookup`、`WebLookup`、`LOOKUP_TIMEOUT`、`supports_lookup`）
- Test: `tests/test_review_providers.py`

**Interfaces:**
- Produces：
  - `LOOKUP_TIMEOUT = 300`
  - `class WebLookup(Protocol): name: str; def lookup(self, folder: Path, prompt: str, schema: dict[str, Any]) -> object: ...`
  - `supports_lookup(judge: object) -> bool`（`lookup` を持つか）
  - `CodexCliJudge.lookup`：`judge` と同じ引数に、`codex` の直後の最上位のフラグとして `--search` を足し、画像なし・`timeout=LOOKUP_TIMEOUT`。`--disable shell_tool`・`--disable unified_exec`・`--sandbox read-only` は残す。
  - `ClaudeCodeJudge.lookup`：`judge` と同じ引数で、`--tools ""` の代わりに `--tools WebSearch,WebFetch` と `--allowedTools WebSearch,WebFetch` を渡し、画像なし・`timeout=LOOKUP_TIMEOUT`。
  - `AntigravityJudge` には `lookup` を足さない（`supports_lookup` が False）。
  - 共通の部分は、`judge` の中の CLI の呼び出しを小さな内部関数に分けて使い回す（引数の並びの違いだけを渡す）。

- [ ] **Step 1: 失敗するテストを書く**

- `test_codex_lookup_enables_only_web_search`：偽の `runner` で引数を受け取り、`--search` を含み、`shell_tool` の無効化が残り、`--image` がない
- `test_claude_lookup_allows_only_web_tools`：偽の `subprocess.run`（既存のテストの差し替え方に合わせる）で、`--tools` の次が `WebSearch,WebFetch` で、`Bash` を含まない
- `test_antigravity_does_not_support_lookup`
- `test_stale_seconds_covers_lookup_timeout`：`LOOKUP_TIMEOUT * 2 == jobs.STALE_SECONDS`

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_review_providers.py -k lookup -v`
Expected: FAIL

- [ ] **Step 3: `lookup` を書く**

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_review_providers.py tests/test_codex*.py -q`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/video/codex_cli.py src/poop_detector/video/providers.py tests/test_review_providers.py
git commit -m "feat: フードのカロリー探しのためにウェブ検索だけを許す口を追加"
```

---

### Task 10: 作業スレッド

**Files:**
- Create: `src/poop_detector/features/advice/worker.py`
- Create: `src/poop_detector/gui/advice_worker.py`
- Modify: `src/poop_detector/gui/server.py:131-145`（`serve_forever` で `feeding_reminders` と並べて起動する）
- Modify: `tests/conftest.py`（自動で使うフィクスチャで、GUI の作業スレッドの判定器を偽物にする）
- Modify: `tests/support.py`（`FakeAdvisor`）
- Test: `tests/test_advice_worker.py`

**Interfaces:**
- Consumes: Task 2・5〜9 の関数
- Produces：
  - `JudgeChoice = tuple[VideoJudge, str, str | None]`（判定器・用途を記録する provider 名・model）
  - `class AdviceWorker: def __init__(self, database: Database, store: Store, choose: Callable[[], JudgeChoice], pet_name: Callable[[], str], workdir: Path, clock: Callable[[], datetime] = local_now) -> None`
  - `AdviceWorker.poll_once(self) -> bool`：`expire_stale` → `JudgeRunLog.count_since(now - 1時間, ("advice", "food_lookup"))` → `claim_next` → 仕事があれば一時ディレクトリ（`workdir` の下）で判定器を呼ぶ。回答は `judge(folder, answer_prompt(...), (), ANSWER_SCHEMA)`、カロリー探しは `supports_lookup` なら `lookup(folder, lookup_prompt(name), LOOKUP_SCHEMA)`、そうでなければ `fail_job(..., "unsupported")`。呼んだら `JudgeRunLog.record(provider, model, "advice" | "food_lookup", failed=..., usage=judge.last_usage, failure=..., failure_detail=...)`。例外は `failure_cause` で種類に変える。処理した仕事があれば True。
  - `gui/advice_worker.py`：
    - `configured_judge(config_path: Path | None) -> JudgeChoice`（`Config.load(config_path)` か `Config()` の `review_provider`・`review_model` から `build_judge`。呼ぶたびに読み直す）
    - `@contextmanager def advice_worker(app: "GuiApp") -> Iterator[None]`（2秒ごとに `poll_once`。仕事を処理したら待たずに次を見る。`OSError`・`sqlite3.Error`・`ValueError` は文言を出さずに警告のログだけにする。止めるときは `join(timeout=1)`）
  - `pet_name` は `Config` の `pet_name` から読む。
  - 応答の本文と指示文はログに出さない。

- [ ] **Step 1: 失敗するテストを書く**

- `test_worker_answers_queued_question_and_records_run`：偽の判定器が `{"answer": "量は今のままで大丈夫です"}` を返す。`poll_once()` のあと、回答の行が `done` で本文が入り、`judge_runs` に `advice` の行が1つ
- `test_worker_marks_failure_kind_and_allows_retry`：判定器が `JudgeCallError("…", "timeout")` を送出→`failed`・`failure == "timeout"`。`retry` のあと、成功する判定器で `done`
- `test_worker_lookup_drafts_result`：偽の `lookup` が `{"product_name": "○○パピー", "maker": "○○", "kcal_per_100g": 385, "source_url": "https://maker.example/p", "note": ""}`→`food_lookups` が `done`・`kcal_dk == 3850`。`foods` には入らない
- `test_worker_fails_lookup_when_judge_has_no_web_search`：`failure == "unsupported"`
- `test_worker_waits_when_hourly_limit_reached`：`advice` を20回記録してから `poll_once()`→False、行は `queued` のまま
- `test_review_runs_do_not_block_advice`：`review` を100回記録しても、相談は処理される
- `test_gui_server_starts_and_stops_advice_worker`：`gui` フィクスチャのサーバーで、質問を台帳に直接入れると、偽の判定器（conftest の自動のフィクスチャ）で数秒以内に `done` になる

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_advice_worker.py -v`
Expected: FAIL

- [ ] **Step 3: `worker.py`・`gui/advice_worker.py` を書き、`serve_forever` で `with feeding_reminders(...), advice_worker(self.app):` にする**

conftest の自動で使うフィクスチャは、`poop_detector.gui.advice_worker.configured_judge` を `FakeAdvisor` を返す関数に差し替える（`FakeAdvisor` は `support.py` に置き、`judge` と `lookup` の戻り値を変えられるようにする）。

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_advice_worker.py tests/test_gui_care.py tests/test_feeding_plans.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/features/advice/worker.py src/poop_detector/gui/advice_worker.py src/poop_detector/gui/server.py tests/conftest.py tests/support.py tests/test_advice_worker.py
git commit -m "feat: GUI の中で相談の回答とカロリー探しを作る作業スレッドを追加"
```

---

### Task 11: フードの画面

**Files:**
- Create: `src/poop_detector/gui/advice_routes.py`（この Task ではフードの部分だけ）
- Create: `src/poop_detector/gui/templates/advice_foods.html`
- Modify: `src/poop_detector/gui/asgi.py:50`付近（`advice_routes` を読み込む）
- Test: `tests/test_gui_advice.py`

**Interfaces:**
- Consumes: Task 5 の関数、`app.judge_readiness.problem`、`supports_lookup`（Task 9）、`build_judge`
- Produces（ルート。すべてログインした人なら使える）：
  - `GET /advice/foods`：登録済みのフード、最近のカロリー探し（状態・下書き・失敗）、未登録のフード名の候補、手入力の欄。判定器が使えないかウェブ検索に対応しないときは「カロリー探し」の欄の代わりに理由を出す。下書きの出典の URL は、文字と `<a href=… rel="noopener noreferrer" target="_blank">` の両方で出す。
  - `POST /advice/foods/lookup`（`name`）→ `request_lookup` → `/advice/foods` へ 303
  - `POST /advice/foods`（`food_id`・`name`・`kcal`・`source_url`）→ `save_food` → 303。誤りは 400 で、入力を残して画面を出し直す
  - `POST /advice/foods/stop`（`food_id`）→ `stop_food` → 303
- 下書きの「この値で登録」は、下書きの値を手入力の欄に入れた同じフォーム（`POST /advice/foods`）にする。人が直してから送れる。

- [ ] **Step 1: 失敗するテストを書く**

- `test_food_lookup_draft_then_confirm`：`/advice/foods/lookup` に「○○パピー」→303。台帳の依頼を `finish_lookup` で `done` にし、画面に 385 と出典が出る。`POST /advice/foods` で登録すると一覧に出る
- `test_food_manual_entry_errors_keep_input`：`kcal="900"` で400、入力した名前が残る
- `test_food_lookup_hidden_when_judge_unavailable`：`CodexCliJudge.check` が `ValueError` を送出→ログインの案内の文言が出て、カロリー探しのフォームがない
- `test_advice_foods_requires_login`：ログインなしは403

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_gui_advice.py -k food -v`
Expected: FAIL

- [ ] **Step 3: ルートとテンプレートを書く**

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_gui_advice.py tests/test_gui_roles.py tests/test_gui_routes.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/gui/advice_routes.py src/poop_detector/gui/templates/advice_foods.html src/poop_detector/gui/asgi.py tests/test_gui_advice.py
git commit -m "feat: フードのカロリーを探して確かめてから登録する画面を追加"
```

---

### Task 12: 目安・プロフィール・相談の画面

**Files:**
- Modify: `src/poop_detector/gui/advice_routes.py`
- Create: `src/poop_detector/gui/advice_page.py`（画面に出す値を整える）
- Create: `src/poop_detector/gui/templates/advice.html`、`src/poop_detector/gui/templates/advice_thread.html`
- Create: `src/poop_detector/gui/static/advice.js`
- Modify: `src/poop_detector/gui/routes.py`（`asset` のパスに `advice.js`）
- Modify: `src/poop_detector/gui/templates/style.css`（必要な分だけ。既存の変数を使う）
- Test: `tests/test_gui_advice.py`、`tests/test_gui_escaping.py`

**Interfaces:**
- Consumes: Task 3〜8 の関数
- Produces（ルート）：
  - `GET /advice`：目安（設計書の7節の画面のイメージ）、警告、`missing` の案内、免責の文、直近7日の実績、体重の伸び、相談の一覧（題名・更新日・始めた人・状態）、新しい相談のフォーム、プロフィールのフォーム（犬種の候補は `BREEDS` の `<datalist>`。体格は5つの選択と「分からない」。予想体重は g。去勢・避妊は「済み」「まだ」「未入力」。いまのフードは `active_foods` の選択）。判定器が使えないときは、相談のフォームの代わりに理由を出す。
  - `POST /advice/profile` → `save_profile` → 303（誤りは400）。犬種が表にあり、体格と予想体重が空で送られたときは、表の値で埋めて保存する。
  - `POST /advice/threads`（`title`・`question`）→ `start_thread(basis=basis_text(...))` → `/advice/thread?id=…` へ 303
  - `GET /advice/thread?id=…`：やりとり（名前・時刻・本文。本文は改行を保つ）。回答待ちは「回答を作っています」と、順番待ちなら「混み合っているので順番を待っています（前に{n}件）」。失敗は種類に応じた短い文と「もう一度聞く」。質問のフォームは回答待ちの間は出さない。削除のボタンは始めた人と管理者だけに出す。
  - `POST /advice/ask`（`thread_id`・`question`）→ 303。回答待ちなら400
  - `POST /advice/retry`（`message_id`）→ 303
  - `POST /advice/delete`（`thread_id`）→ `/advice` へ 303。権限がなければ403
  - `GET /advice/status?thread=…` → `{"state": "queued" | "running" | "done" | "failed"}`（JSON。預けない）
- `advice.js`：`data-advice-status` のある要素があれば、4秒ごとに `/advice/status` を確かめ、`queued`・`running` でなくなったら `location.reload()`。「読み直す」のリンクも常に出す。
- 本文は `white-space: pre-wrap` で出し、HTML としては解釈しない。

- [ ] **Step 1: 失敗するテストを書く**

- `test_advice_page_shows_guide_from_records`：生年月日・体重・プロフィール（small・5000 g）・フード（385 kcal）・給餌の予定3件を入れ、`/advice` に「109 g」「36 g」「獣医師の指示があればそちらを優先してください」が出る
- `test_advice_page_guides_missing_inputs`：生年月日なしで「生年月日を入れてください」
- `test_profile_breed_fills_size_and_adult_weight`
- `test_question_round_trip`：新しい相談→会話の画面に質問と「回答を作っています」。作業スレッド（偽の判定器）が答えたあと、回答が出る。`/advice/status` が `done`
- `test_second_question_refused_while_pending`：400 と「前の質問の回答を待っています」
- `test_delete_thread_permissions`：一般ユーザーは他人の相談を消せず403、管理者は消せる（既存の2人目のユーザーの作り方に合わせる）
- `test_inactive_profile_food_is_treated_as_none`（Review Focus 5）：プロフィールのフードを止めると、「フードのカロリーが分からないので」の警告が出て、g の数字が出ない
- `tests/test_gui_escaping.py` に、題名・質問・回答・フード名・犬種に `<script>"&'` を入れて各画面でエスケープされるケースを足す

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_gui_advice.py tests/test_gui_escaping.py -v`
Expected: FAIL

- [ ] **Step 3: ルート・画面の値・テンプレート・スクリプトを書く**

- [ ] **Step 4: 通ることを確かめる**

Run: `uv run pytest tests/test_gui_advice.py tests/test_gui_escaping.py tests/test_gui_roles.py tests/test_gui_routes.py tests/test_gui_cache.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/gui tests/test_gui_advice.py tests/test_gui_escaping.py
git commit -m "feat: ごはんの目安と家族で共有する相談の画面を追加"
```

---

### Task 13: メニュー・ホームの1行・文書

**Files:**
- Modify: `src/poop_detector/gui/navigation.py:11-20`（`MEMBER_SECTIONS` の `/weight` の次に `("/advice", "ごはんの相談", "chat")`）
- Modify: `src/poop_detector/gui/icons.py`（`chat` の形を足す）
- Modify: `src/poop_detector/gui/pages.py` とホームのテンプレート（目安があれば「今日の目安 {g} g」を給餌量の表示の近くに1行）
- Modify: `docs/architecture.md`（`events.sqlite3` の台帳の一覧、GUI のモジュールの表、作業スレッド、判定の上限を用途で数えること）、`docs/screens.md`、`README.md` の機能表
- Test: `tests/test_gui_navigation.py`（既存のメニューのテストのファイル。なければ `tests/test_gui_advice.py`）、`tests/test_gui_home.py`（既存のホームのテストのファイルに合わせる）

**Interfaces:**
- Consumes: `compute_advice`（Task 4）と、Task 12 で画面の値を作る関数（`advice_page.py` の `today_grams(database: Database, today: date) -> int | None` を Task 12 で用意し、ここで使う）

- [ ] **Step 1: 失敗するテストを書く**

- メニューに「ごはんの相談」があり、`/advice/thread?id=…` を開いているとき現在地になる
- ホームに、目安があれば「今日の目安 109 g」、なければその行がない

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run pytest tests/test_gui_navigation.py tests/test_gui_home.py -v`
Expected: FAIL

- [ ] **Step 3: メニュー・アイコン・ホームの1行・文書を書く**

- [ ] **Step 4: すべての確認を通す**

Run: `uv run pytest --cov && uv run ruff check . && uv run ruff format --check . && uv run mypy && git diff --check`
Expected: すべて成功し、カバレッジが下限以上

- [ ] **Step 5: コミット**

```bash
git add src/poop_detector/gui docs README.md tests
git commit -m "feat: ごはんの相談をメニューとホームに出し、設計の文書を更新"
```
