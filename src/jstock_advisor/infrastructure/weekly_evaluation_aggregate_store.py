"""週次評価集計(WeeklyEvaluationAggregate)の永続化の契約と、ローカル実装(Issue #537)。

契約(`WeeklyEvaluationAggregateStore`)は、Lambda では DynamoDB(`aws/
  weekly_evaluation_aggregate_dynamodb.py`)、
ローカル・テストでは本モジュールの `LocalWeeklyEvaluationAggregateStore` が満たす。

## 1 つの保存 = 1 つの原子的コミット(USER 決定 Q-1 / Q-2)

`commit_evaluation()` は、EvaluationResult の条件付き insert と、集計の加算・週の状態の更新を
**1 回の Transaction** で行う。EvaluationResult の insert に成功した実行だけが加算する
(決定的な evaluation_id = #325。再処理・並行実行・retry でも二重加算しない。別の ledger は不要)。
集計の更新に失敗した場合は、EvaluationResult の保存も成立しない(Raw と Aggregate の原子的整合を
優先する)。保存されなかった評価は CompletedHorizonIndex に載らないため、
  翌日の日次実行で再試行される。

## 再計算対象週の特定(Aggregate 全件の探索をしない)

同じ Transaction の中で、当該 review_week について「WeeklyReviewMetrics の再生成が要る」という
marker(`mark_seq` の加算 + `PENDING` 一覧への登録)を原子的に記録する。**2 つの状態は別管理**である。

```
REVIEW_RECOMPUTE_PENDING      mark_seq > recomputed_seq   Metrics を Aggregate から再生成する(raw
  の rebuild は不要)
AGGREGATE_REBUILD_REQUIRED    rebuild_required = True      Aggregate 自体が不整合。指定週だけ raw
  から rebuild する
```

## 切替の状態

`BackfillStatus.complete` が False の間、週次レビューは Aggregate を読まない
(部分的な Aggregate で既存の WeeklyReviewMetrics を上書きしないため)。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.weekly_evaluation_aggregate import (
    WeeklyEvaluationAggregate,
    aggregate_item_key,
    delta_of,
)
from jstock_advisor.infrastructure.collection_store import running_on_lambda

#  集計の書き込み(評価の保存 Transaction への組み込み)を有効にする環境変数。既定 =
# 無効(従来どおり)。
WRITE_ENABLED_ENV = "WEEKLY_AGGREGATE_WRITE_ENABLED"
#: 週次レビューが Aggregate を読む経路を有効にする環境変数。既定 = 無効(従来どおり raw の走査)。
READ_ENABLED_ENV = "WEEKLY_AGGREGATE_READ_ENABLED"
#: DynamoDB のテーブル名(SAM の Ref)。
TABLE_ENV = "WEEKLY_EVALUATION_AGGREGATE_TABLE"

REBUILD_REASONS = frozenset(
    {"AGGREGATION_FAILURE", "MANUAL_REBUILD_REQUEST", "SCHEMA_MIGRATION", "RECONCILE_MISMATCH"}
)


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def aggregate_write_enabled() -> bool:
    return _env_true(WRITE_ENABLED_ENV)


def aggregate_read_enabled() -> bool:
    return _env_true(READ_ENABLED_ENV)


@dataclass(frozen=True)
class WeekState:
    """1 週の状態。2 つの状態(REVIEW_RECOMPUTE_PENDING / AGGREGATE_REBUILD_REQUIRED)は別管理。"""

    review_week: str
    mark_seq: int = 0
    recomputed_seq: int = 0
    rebuild_required: bool = False
    rebuild_reason: str | None = None

    @property
    def recompute_pending(self) -> bool:
        return self.mark_seq > self.recomputed_seq


@dataclass(frozen=True)
class BackfillStatus:
    complete: bool
    completed_at: dt.datetime | None = None
    week_count: int = 0
    row_count: int = 0


class WeeklyEvaluationAggregateStore(Protocol):
    def commit_evaluation(
        self,
        evaluation: EvaluationResult,
        recommendation_type: RecommendationType,
        rule_version: str,
        now: dt.datetime,
    ) -> bool:
        """EvaluationResult の insert + 集計の加算 + marker を原子的に行う。

        insert できたら True。既に同じ evaluation_id が存在すれば False(**加算も marker
          も起きない**)。
        集計の更新に失敗した場合は例外を送出する(EvaluationResult も保存されない)。
        """

    def query_week(self, review_week: str) -> list[WeeklyEvaluationAggregate]:
        """1 週の全集計行(Query。Scan ではない)。"""

    def get_state(self, review_week: str) -> WeekState: ...

    def list_pending_weeks(self) -> list[str]:
        """REVIEW_RECOMPUTE_PENDING の週(1 件の一覧の読み取り。Aggregate の探索ではない)。"""

    def list_rebuild_weeks(self) -> list[str]:
        """AGGREGATE_REBUILD_REQUIRED の週。"""

    def finish_recompute(self, review_week: str, seen_mark_seq: int) -> bool:
        """Metrics の再生成が済んだことを記録する。読んだ後に新しい評価が届いていれば(mark_seq が
        進んでいれば)False を返し、marker を残す(取りこぼさない)。"""

    def mark_rebuild_required(self, review_week: str, reason: str, now: dt.datetime) -> None: ...

    def replace_week(
        self,
        review_week: str,
        rows: Iterable[WeeklyEvaluationAggregate],
        expected_mark_seq: int | None,
        now: dt.datetime,
        *,
        request_recompute: bool = True,
    ) -> bool:
        """指定週の集計を、raw から作り直した内容で置き換える(SET。ADD ではない = 冪等)。

        `expected_mark_seq` は作り直しの元にした raw を読んだ時点の mark_seq(状態が無ければ None)。
        その間に新しい評価が届いていれば False を返し、何も変更しない(呼び出し側が再実行する)。
        成功すると rebuild_required を解除する。

        `request_recompute=True`(既定 = rebuild)は、Metrics の再生成を要求する(mark_seq を進め、
        PENDING へ登録する)。`False`(初回 backfill)は要求しない: 既に保存済みの過去の Metrics を、
        backfill だけを理由に書き換えない(全履歴の Metrics が一斉に再生成されるのを防ぐ)。
        """

    def get_backfill_status(self) -> BackfillStatus: ...

    def set_backfill_complete(self, now: dt.datetime, week_count: int, row_count: int) -> None: ...


class LocalWeeklyEvaluationAggregateStore:
    """ローカル・テスト用の実装(単一プロセス前提。原子性は check-then-act)。

    `path` を渡すとその JSON ファイルへ保存する(None なら in-memory)。
    ★ DynamoDB の Transaction の代替ではない。挙動の契約(二重加算しない・marker の扱い)を、
      同じテストで両実装に対して確認するために置く。
    """

    def __init__(
        self,
        evaluation_inserter: Callable[[EvaluationResult], bool],
        path: Path | None = None,
    ) -> None:
        self._insert_evaluation = evaluation_inserter
        self._path = path
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], WeeklyEvaluationAggregate] = {}
        self._states: dict[str, WeekState] = {}
        self._pending: set[str] = set()
        self._rebuild: set[str] = set()
        self._backfill = BackfillStatus(complete=False)
        if path is not None and path.exists():
            self._load(path)

    # --- 書き込み -------------------------------------------------------

    def commit_evaluation(
        self,
        evaluation: EvaluationResult,
        recommendation_type: RecommendationType,
        rule_version: str,
        now: dt.datetime,
    ) -> bool:
        delta = delta_of(evaluation)  # 非有限値は、保存より前に例外にする(保存しない)
        with self._lock:
            if not self._insert_evaluation(evaluation):
                return False
            key = (delta.review_week, aggregate_item_key(recommendation_type, rule_version))
            row = self._rows.get(key)
            if row is None:
                row = self._rows[key] = WeeklyEvaluationAggregate(
                    review_week=delta.review_week,
                    recommendation_type=recommendation_type,
                    rule_version=rule_version,
                    updated_at=now,
                )
            row.apply(delta, now)
            state = self._states.get(delta.review_week, WeekState(delta.review_week))
            self._states[delta.review_week] = WeekState(
                review_week=state.review_week,
                mark_seq=state.mark_seq + 1,
                recomputed_seq=state.recomputed_seq,
                rebuild_required=state.rebuild_required,
                rebuild_reason=state.rebuild_reason,
            )
            self._pending.add(delta.review_week)
            self._save()
            return True

    def finish_recompute(self, review_week: str, seen_mark_seq: int) -> bool:
        with self._lock:
            state = self._states.get(review_week, WeekState(review_week))
            if state.mark_seq != seen_mark_seq:
                return False
            self._states[review_week] = WeekState(
                review_week=review_week,
                mark_seq=state.mark_seq,
                recomputed_seq=seen_mark_seq,
                rebuild_required=state.rebuild_required,
                rebuild_reason=state.rebuild_reason,
            )
            self._pending.discard(review_week)
            self._save()
            return True

    def mark_rebuild_required(self, review_week: str, reason: str, now: dt.datetime) -> None:
        if reason not in REBUILD_REASONS:
            raise ValueError(f"未知の rebuild 理由です: {reason}")
        with self._lock:
            state = self._states.get(review_week, WeekState(review_week))
            self._states[review_week] = WeekState(
                review_week=review_week,
                mark_seq=state.mark_seq,
                recomputed_seq=state.recomputed_seq,
                rebuild_required=True,
                rebuild_reason=reason,
            )
            self._rebuild.add(review_week)
            self._save()

    def replace_week(
        self,
        review_week: str,
        rows: Iterable[WeeklyEvaluationAggregate],
        expected_mark_seq: int | None,
        now: dt.datetime,
        *,
        request_recompute: bool = True,
    ) -> bool:
        with self._lock:
            state = self._states.get(review_week)
            # mark_seq が 0(= 一度も評価が反映されていない)の状態は「無い」と同じ扱い(DynamoDB の
            # attribute_not_exists(mark_seq) と揃える)。
            current_seq = state.mark_seq if (state is not None and state.mark_seq) else None
            if current_seq != expected_mark_seq:
                return False
            for key in [k for k in self._rows if k[0] == review_week]:
                del self._rows[key]
            for row in rows:
                if row.review_week != review_week:
                    raise ValueError("置き換える行の週が一致しません")
                self._rows[(review_week, row.item_key)] = row.model_copy(deep=True)
            base = state or WeekState(review_week)
            self._states[review_week] = WeekState(
                review_week=review_week,
                mark_seq=base.mark_seq + (1 if request_recompute else 0),
                recomputed_seq=base.recomputed_seq,
                rebuild_required=False,
                rebuild_reason=None,
            )
            if request_recompute:
                self._pending.add(review_week)
            self._rebuild.discard(review_week)
            self._save()
            return True

    def set_backfill_complete(self, now: dt.datetime, week_count: int, row_count: int) -> None:
        with self._lock:
            self._backfill = BackfillStatus(
                complete=True, completed_at=now, week_count=week_count, row_count=row_count
            )
            self._save()

    # --- 読み取り -------------------------------------------------------

    def query_week(self, review_week: str) -> list[WeeklyEvaluationAggregate]:
        with self._lock:
            return [
                row.model_copy(deep=True)
                for (week, _), row in sorted(self._rows.items())
                if week == review_week
            ]

    def get_state(self, review_week: str) -> WeekState:
        with self._lock:
            return self._states.get(review_week, WeekState(review_week))

    def list_pending_weeks(self) -> list[str]:
        with self._lock:
            return sorted(self._pending)

    def list_rebuild_weeks(self) -> list[str]:
        with self._lock:
            return sorted(self._rebuild)

    def get_backfill_status(self) -> BackfillStatus:
        with self._lock:
            return self._backfill

    # --- 永続化(任意) ---------------------------------------------------

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            "rows": [row.model_dump(mode="json") for row in self._rows.values()],
            "states": {
                week: {
                    "mark_seq": s.mark_seq,
                    "recomputed_seq": s.recomputed_seq,
                    "rebuild_required": s.rebuild_required,
                    "rebuild_reason": s.rebuild_reason,
                }
                for week, s in self._states.items()
            },
            "pending": sorted(self._pending),
            "rebuild": sorted(self._rebuild),
            "backfill": {
                "complete": self._backfill.complete,
                "completed_at": (
                    self._backfill.completed_at.isoformat() if self._backfill.completed_at else None
                ),
                "week_count": self._backfill.week_count,
                "row_count": self._backfill.row_count,
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _load(self, path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for raw in payload.get("rows", []):
            row = WeeklyEvaluationAggregate.model_validate(raw)
            self._rows[(row.review_week, row.item_key)] = row
        for week, s in payload.get("states", {}).items():
            self._states[week] = WeekState(review_week=week, **s)
        self._pending = set(payload.get("pending", []))
        self._rebuild = set(payload.get("rebuild", []))
        backfill = payload.get("backfill") or {}
        completed_at = backfill.get("completed_at")
        self._backfill = BackfillStatus(
            complete=bool(backfill.get("complete", False)),
            completed_at=dt.datetime.fromisoformat(completed_at) if completed_at else None,
            week_count=int(backfill.get("week_count", 0)),
            row_count=int(backfill.get("row_count", 0)),
        )


def build_weekly_evaluation_aggregate_store(
    evaluation_inserter: Callable[[EvaluationResult], bool] | None = None,
    local_path: Path | None = None,
) -> WeeklyEvaluationAggregateStore:
    """Lambda では DynamoDB、それ以外はローカル実装を返す(`build_collection_store` と同じ規約)。

    ローカルでは、`evaluation_inserter`(既定 = EvaluationResultRepository.insert_if_absent)を
    使って EvaluationResult を保存する。ローカルは本番のテーブルへ一切アクセスしない。

    既定の保存先は `json_store.DEFAULT_STORE_DIR`(他の全リポジトリと同じ `data/local_store`)である。
    **モジュール属性を都度読む**(`from ... import DEFAULT_STORE_DIR` で値を束縛しない)。
    `tests/conftest.py` の autouse fixture(Issue #229)が `json_store.DEFAULT_STORE_DIR` を
    テストごとの一時ディレクトリへ monkeypatch するため、束縛すると単体テスト実行中に
    リポジトリ直下(`data/local_store` の外・.gitignore 対象外)へ実ファイルを作ってしまう
    (#229 の audit_log.json 91.8MB の事故と同じ形)。
    """
    if running_on_lambda():
        from jstock_advisor.infrastructure.aws.weekly_evaluation_aggregate_dynamodb import (
            DynamoWeeklyEvaluationAggregateStore,
        )

        return DynamoWeeklyEvaluationAggregateStore.from_environment()
    if evaluation_inserter is None:
        from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
            EvaluationResultRepository,
        )

        evaluation_inserter = EvaluationResultRepository().insert_if_absent
    from jstock_advisor.infrastructure.local_repository import json_store

    path = local_path or (json_store.DEFAULT_STORE_DIR / "weekly_evaluation_aggregate.json")
    return LocalWeeklyEvaluationAggregateStore(evaluation_inserter, path)
