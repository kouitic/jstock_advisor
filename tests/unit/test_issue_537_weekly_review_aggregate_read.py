"""Issue #537: 週次改善レビューが Aggregate を読む経路(と、backfill・照合・rebuild)のテスト。

確認するもの:
- 旧方式(raw の走査)と同じ結果になること(同値性 = AC11)/ 既存の意味論(AC8)
- 通常の週次処理が raw の EvaluationResultsTable を走査しないこと(AC1・AC2)
- 遅延評価は、その週の Metrics だけが再生成され、過去 4 週の full rebuild をしないこと(AC5・AC6)
- 5 週より古い遅延評価でも、過去 Metrics を部分集計で上書きしないこと(AC14)
- 再計算対象の週が marker から特定できること(AC15)。再生成中に届いた評価の marker は残る
- backfill が済んでいない間は Aggregate を読まないこと / rebuild 中の週は Metrics を作らないこと
- `history_weeks_for_comparison` は raw の再集計範囲ではないこと(AC9)
- backfill / 照合 / rebuild(dry-run が既定。指定週だけ)
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationLabel,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.improvement_candidate_repository import (
    ImprovementCandidateRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.rule_version_repository import (
    RuleVersionRepository,
)
from jstock_advisor.infrastructure.local_repository.weekly_review_metrics_repository import (
    WeeklyReviewMetricsRepository,
)
from jstock_advisor.infrastructure.weekly_evaluation_aggregate_store import (
    LocalWeeklyEvaluationAggregateStore,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.weekly_evaluation_aggregate_service import (
    WeeklyAggregateMaintenanceService,
    aggregate_to_bucket,
)
from jstock_advisor.services.weekly_improvement_review_service import (
    WeeklyImprovementReviewService,
    _resolve_review_period,
)

# 2026-09-21(月)19:00 JST = 10:00Z。レビュー対象週 = 2026-09-14(月)〜09-20(日)= 2026-W38
_RUN_AT = dt.datetime(2026, 9, 21, 10, 0, tzinfo=dt.UTC)
_NOW = dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC)
_WEEK38_DAY = dt.date(2026, 9, 16)
_HORIZON = 7


def _week_day(weeks_before: int) -> dt.date:
    """レビュー週(W38)の `weeks_before` 週前の水曜。"""
    return _WEEK38_DAY - dt.timedelta(weeks=weeks_before)


def _week_label(d: dt.date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


class Env:
    """ローカル(tmp_path)のリポジトリ一式 + ローカル Aggregate ストア。"""

    def __init__(self, tmp_path: Path) -> None:
        self.evaluations = EvaluationResultRepository(store_dir=tmp_path)
        self.recommendations = RecommendationRepository(store_dir=tmp_path)
        self.metrics = WeeklyReviewMetricsRepository(store_dir=tmp_path)
        self.candidates = ImprovementCandidateRepository(store_dir=tmp_path)
        self.rule_versions = RuleVersionRepository(store_dir=tmp_path)
        self.audit = AuditLogRepository(store_dir=tmp_path)
        self.store = LocalWeeklyEvaluationAggregateStore(self.evaluations.insert_if_absent)
        self._n = 0

    def add(
        self,
        evaluation_date: dt.date,
        *,
        label: EvaluationLabel = EvaluationLabel.SUCCESS,
        rec_type: RecommendationType = RecommendationType.BUY,
        rule_version: str = "v1",
        price_return_pct: float = 2.0,
        excess_return_pct: float | None = 1.0,
        via_store: bool = False,
    ) -> EvaluationResult:
        """推奨 1 件 + 評価 1 件を作る。via_store=True なら保存と同時に Aggregate へ加算する。"""
        self._n += 1
        rec_id = f"rec-{self._n}"
        recommended_at = dt.datetime.combine(
            evaluation_date - dt.timedelta(days=_HORIZON), dt.time(3, 0), tzinfo=dt.UTC
        )
        recommendation = Recommendation(
            recommendation_id=rec_id,
            stock_code="1234",
            stock_name="test",
            recommended_at=recommended_at,
            recommendation_type=rec_type,
            price_at_recommendation=Decimal("1000"),
            confidence=ConfidenceLevel.HIGH,
            rule_version=rule_version,
        )
        self.recommendations.save(recommendation)
        evaluation = EvaluationResult(
            evaluation_id=f"ev-{self._n}",
            recommendation_id=rec_id,
            horizon_calendar_days=_HORIZON,
            evaluated_at=_NOW,
            evaluation_date=evaluation_date,
            price_at_evaluation=Decimal("1010"),
            price_return_pct=price_return_pct,
            excess_return_pct=excess_return_pct,
            evaluation_label=label,
            label_evidence="x",
        )
        if via_store:
            assert self.store.commit_evaluation(evaluation, rec_type, rule_version, _NOW)
        else:
            self.evaluations.save(evaluation)
        return evaluation

    def backfill(self) -> None:
        self.maintenance().execute_backfill(_NOW)

    def maintenance(self) -> WeeklyAggregateMaintenanceService:
        # 束縛メソッドそのものを渡す(呼ぶたびに新しい走査になる。#537 レビュー指摘 F2/F3)。
        return WeeklyAggregateMaintenanceService(
            self.store, self.evaluations.iter_all, self.recommendations, _HORIZON
        )

    def service(
        self, *, aggregate_read: bool, history_weeks: int | None = None
    ) -> WeeklyImprovementReviewService:
        config = load_config()
        updates: dict[str, Any] = {"issue_creation_enabled": False}
        if history_weeks is not None:
            updates["history_weeks_for_comparison"] = history_weeks
        config = config.model_copy(
            update={"review_improvement": config.review_improvement.model_copy(update=updates)}
        )
        return WeeklyImprovementReviewService(
            config=config,
            evaluation_repository=self.evaluations,
            recommendation_repository=self.recommendations,
            weekly_review_metrics_repository=self.metrics,
            improvement_candidate_repository=self.candidates,
            rule_version_service=RuleVersionService(self.rule_versions),
            audit_service=AuditService(self.audit),
            aggregate_store=self.store,
            aggregate_read=aggregate_read,
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _metrics_by_key(repo: WeeklyReviewMetricsRepository) -> dict[str, Any]:
    return {m.metrics_id: m for m in repo.list_all()}


def _seed_mixed_history(env_: Env, *, via_store: bool) -> None:
    """レビュー週 + 過去の週に、種別・rule_version・ラベルの混在した評価を入れる。"""
    labels = [
        EvaluationLabel.SUCCESS,
        EvaluationLabel.ACCEPTABLE,
        EvaluationLabel.PRICE_TOO_HIGH,
        EvaluationLabel.INCONCLUSIVE,
        EvaluationLabel.DATA_ISSUE,
        EvaluationLabel.EARLY,
    ]
    i = 0
    for weeks_before in range(0, 6):
        for rec_type, rv in (
            (RecommendationType.BUY, "v1"),
            (RecommendationType.BUY, "v2"),
            (RecommendationType.SELL, "v1"),
        ):
            for _ in range(4):
                i += 1
                env_.add(
                    _week_day(weeks_before),
                    label=labels[i % len(labels)],
                    rec_type=rec_type,
                    rule_version=rv,
                    price_return_pct=round(((i * 37) % 101) / 7 - 5, 6),
                    excess_return_pct=None if i % 5 == 0 else round(((i * 13) % 53) / 9 - 2, 6),
                    via_store=via_store,
                )


# --- AC11: 旧方式との同値性 --------------------------------------------------------


def test_aggregate_read_produces_the_same_metrics_as_the_raw_scan(env: Env, tmp_path: Path) -> None:
    _seed_mixed_history(env, via_store=True)  # raw にも Aggregate にも入る
    env.store.set_backfill_complete(_NOW, 0, 0)

    old_env = Env(tmp_path / "old")  # 同じ raw を、旧方式(走査)で処理する別のリポジトリ一式
    for evaluation in env.evaluations.iter_all():
        old_env.evaluations.save(evaluation)
    for rec in env.recommendations.list_all():
        old_env.recommendations.save(rec)
    old_outcome = old_env.service(aggregate_read=False).run(_RUN_AT)
    new_outcome = env.service(aggregate_read=True).run(_RUN_AT)

    assert new_outcome.aggregate_read is True and old_outcome.aggregate_read is False
    old = {k: v for k, v in _metrics_by_key(old_env.metrics).items() if k.endswith("2026-W38")}
    new = {k: v for k, v in _metrics_by_key(env.metrics).items() if k.endswith("2026-W38")}
    assert set(old) == set(new) and old
    for key, expected in old.items():
        actual = new[key]
        assert actual.sample_count == expected.sample_count
        assert actual.conclusive_count == expected.conclusive_count
        assert actual.success_rate_pct == expected.success_rate_pct
        for field in ("average_return_pct", "average_excess_return_pct"):
            want, have = getattr(expected, field), getattr(actual, field)
            assert (want is None) == (have is None)
            if want is not None:
                assert have == pytest.approx(want, rel=1e-9, abs=1e-12)
    assert new_outcome.total_evaluation_results == old_outcome.total_evaluation_results
    assert new_outcome.joined_count == old_outcome.joined_count


def test_aggregate_bucket_matches_build_metrics_bucket_for_one_row(env: Env) -> None:
    from jstock_advisor.services.performance_metrics_service import build_metrics_bucket

    evaluations = [
        env.add(_WEEK38_DAY, label=label, price_return_pct=p, excess_return_pct=e, via_store=True)
        for label, p, e in [
            (EvaluationLabel.SUCCESS, 3.25, 1.0),
            (EvaluationLabel.INCONCLUSIVE, -2.5, None),
            (EvaluationLabel.EARLY, 0.125, 0.5),
        ]
    ]
    [row] = env.store.query_week("2026-W38")

    expected = build_metrics_bucket("BUY", evaluations)
    actual = aggregate_to_bucket(row, "BUY")

    assert actual.count == expected.count
    assert actual.conclusive_count == expected.conclusive_count
    assert actual.success_rate_pct == expected.success_rate_pct
    assert actual.avg_price_return_pct == pytest.approx(expected.avg_price_return_pct)
    assert actual.avg_excess_return_pct == pytest.approx(expected.avg_excess_return_pct)
    assert actual.label_counts == expected.label_counts


# --- AC1・AC2: raw の EvaluationResultsTable を走査しない ---------------------------------


def test_normal_review_does_not_scan_the_raw_evaluations(env: Env, monkeypatch) -> None:
    _seed_mixed_history(env, via_store=True)
    env.store.set_backfill_complete(_NOW, 0, 0)

    def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("週次レビューが raw の EvaluationResult を走査した")

    monkeypatch.setattr(EvaluationResultRepository, "iter_all", boom)
    monkeypatch.setattr(EvaluationResultRepository, "list_all", boom)

    outcome = env.service(aggregate_read=True).run(_RUN_AT)

    assert outcome.aggregate_read is True
    assert outcome.total_evaluation_results > 0


# --- AC5・AC6・AC15: 遅延評価はその週だけが再生成される ------------------------------------


def test_late_evaluation_regenerates_only_that_week(env: Env) -> None:
    _seed_mixed_history(env, via_store=True)
    env.store.set_backfill_complete(_NOW, 0, 0)
    env.service(aggregate_read=True).run(_RUN_AT)  # 全ての marker を解消する(初回)
    assert env.store.list_pending_weeks() == []
    before = _metrics_by_key(env.metrics)
    w36_key = "BUY|v1|ALL|2026-W36"

    # W36 に、遅れて確定した評価が 1 件届く(evaluated_at は今週)
    env.add(_week_day(2), rec_type=RecommendationType.BUY, rule_version="v1", via_store=True)
    assert env.store.list_pending_weeks() == ["2026-W36"]  # ★ marker から対象週を特定できる

    run_later = _RUN_AT + dt.timedelta(days=7)  # 翌週の月曜(W39 のレビュー)
    outcome = env.service(aggregate_read=True).run(run_later)

    after = _metrics_by_key(env.metrics)
    assert after[w36_key].sample_count == before[w36_key].sample_count + 1
    # ★ 他の過去週(W35・W37 等)の Metrics は書き換えていない(生成日時が変わっていない)
    for key in before:
        if key.endswith(("2026-W35", "2026-W37", "2026-W34")):
            assert after[key].generated_at == before[key].generated_at
    assert outcome.past_weeks_metrics_recomputed_by_week.keys() == {"2026-W36"}
    assert env.store.list_pending_weeks() == []  # marker は解消された
    assert "2026-W36" in outcome.aggregate_weeks_recomputed


def test_evaluation_older_than_five_weeks_does_not_overwrite_history_with_a_partial_sum(
    env: Env,
) -> None:
    """AC14: backfill 済み(全履歴)の Aggregate。古い週への遅延評価 1 件が部分集計にならない。"""
    old_day = _week_day(9)  # 5 週より古い
    for _ in range(3):
        env.add(old_day, via_store=False)  # backfill の対象(raw のみ)
    env.backfill()
    env.metrics.save(
        _existing_metrics("BUY|v1|ALL|" + _week_label(old_day), _week_label(old_day), sample=3)
    )

    env.add(old_day, via_store=True)  # 遅れて 1 件確定
    env.service(aggregate_read=True).run(_RUN_AT)

    regenerated = _metrics_by_key(env.metrics)["BUY|v1|ALL|" + _week_label(old_day)]
    assert regenerated.sample_count == 4  # 3(backfill)+ 1(遅延)。★ 1 ではない


def _existing_metrics(metrics_id: str, week: str, sample: int) -> Any:
    from jstock_advisor.domain.entities.improvement import WeeklyReviewMetrics

    monday = dt.date.fromisocalendar(int(week[:4]), int(week[-2:]), 1)
    return WeeklyReviewMetrics(
        metrics_id=metrics_id,
        review_week=week,
        recommendation_type=RecommendationType.BUY,
        rule_version="v1",
        sample_count=sample,
        conclusive_count=sample,
        period_start=monday,
        period_end=monday + dt.timedelta(days=6),
        generated_at=_NOW - dt.timedelta(days=30),
    )


def test_backfill_alone_does_not_request_a_recompute_of_historical_metrics(env: Env) -> None:
    for weeks_before in range(0, 5):
        env.add(_week_day(weeks_before), via_store=False)
    env.backfill()

    assert env.store.list_pending_weeks() == []  # 過去の Metrics を一斉に再生成しない
    assert env.store.get_backfill_status().complete


# --- 再生成中に届いた評価の marker は残る --------------------------------------------------


def test_marker_is_kept_when_an_evaluation_arrives_during_regeneration(env: Env) -> None:
    env.add(_week_day(2), via_store=True)
    env.store.set_backfill_complete(_NOW, 0, 0)
    arriving = env

    class _Arrival:
        """集計行を読む間に、同じ週へ評価が届く状況を作る。"""

        def __init__(self, inner: LocalWeeklyEvaluationAggregateStore) -> None:
            self._inner = inner
            self.fired = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def query_week(self, review_week: str) -> Any:
            rows = self._inner.query_week(review_week)
            if review_week == "2026-W36" and not self.fired:
                self.fired = True
                arriving.add(_week_day(2), via_store=True)  # 読んだ後に届く
            return rows

    wrapped = _Arrival(env.store)
    service = env.service(aggregate_read=True)
    service._aggregate_store = wrapped  # type: ignore[assignment]

    outcome = service.run(_RUN_AT)

    assert "2026-W36" in outcome.aggregate_weeks_marker_kept
    assert env.store.list_pending_weeks() == ["2026-W36"]  # 取りこぼさず、次回に再生成される
    run_later = _RUN_AT + dt.timedelta(days=7)
    later = env.service(aggregate_read=True).run(run_later)
    assert "2026-W36" in later.aggregate_weeks_recomputed
    assert _metrics_by_key(env.metrics)["BUY|v1|ALL|2026-W36"].sample_count == 2


# --- backfill が済んでいない間は読まない / rebuild 中は Metrics を作らない --------------------


def test_aggregate_is_not_read_until_backfill_is_complete(env: Env) -> None:
    env.add(_WEEK38_DAY, via_store=False)  # raw のみ(Aggregate は空)
    assert not env.store.get_backfill_status().complete

    outcome = env.service(aggregate_read=True).run(_RUN_AT)

    assert outcome.aggregate_read is False  # 従来の経路へ戻る
    assert outcome.total_evaluation_results == 1  # raw から作られている(空の Aggregate ではない)


def test_current_week_requiring_rebuild_fails_the_run(env: Env) -> None:
    env.add(_WEEK38_DAY, via_store=True)
    env.store.set_backfill_complete(_NOW, 0, 0)
    env.store.mark_rebuild_required("2026-W38", "AGGREGATION_FAILURE", _NOW)

    with pytest.raises(RuntimeError, match="rebuild"):
        env.service(aggregate_read=True).run(_RUN_AT)

    assert env.metrics.list_all() == []  # 疑わしい値で Metrics を作らない


def test_past_week_requiring_rebuild_is_skipped_and_its_marker_kept(env: Env) -> None:
    env.add(_WEEK38_DAY, via_store=True)
    env.add(_week_day(2), via_store=True)
    env.store.set_backfill_complete(_NOW, 0, 0)
    env.store.mark_rebuild_required("2026-W36", "MANUAL_REBUILD_REQUEST", _NOW)

    outcome = env.service(aggregate_read=True).run(_RUN_AT)

    assert outcome.past_weeks_join_failed == {"2026-W36": "phase=aggregate_rebuild_required"}
    assert "BUY|v1|ALL|2026-W36" not in _metrics_by_key(env.metrics)
    assert "2026-W36" in env.store.list_pending_weeks()  # marker は残る


# --- AC9: history_weeks_for_comparison は raw の再集計範囲ではない ------------------------------


@pytest.mark.parametrize("history_weeks", [0, 4, 12])
def test_history_weeks_for_comparison_does_not_change_which_weeks_are_regenerated(
    tmp_path: Path, history_weeks: int
) -> None:
    env_ = Env(tmp_path)
    env_.add(_WEEK38_DAY, via_store=True)
    env_.add(_week_day(6), via_store=True)  # 過去の週(marker が付く)
    env_.store.set_backfill_complete(_NOW, 0, 0)

    outcome = env_.service(aggregate_read=True, history_weeks=history_weeks).run(_RUN_AT)

    assert set(outcome.past_weeks_metrics_recomputed_by_week) == {_week_label(_week_day(6))}


# --- 監査 --------------------------------------------------------------------------


def test_audit_records_the_aggregate_read(env: Env) -> None:
    env.add(_WEEK38_DAY, via_store=True)
    env.store.set_backfill_complete(_NOW, 0, 0)

    env.service(aggregate_read=True).run(_RUN_AT)

    [entry] = [e for e in env.audit.list_all() if e.decision_type == "weekly_improvement_review"]
    assert entry.output_values["aggregate_read"] is True
    assert entry.output_values["aggregate_weeks_recomputed"] == ["2026-W38"]


def test_default_is_the_raw_scan_when_the_environment_variable_is_unset(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WEEKLY_AGGREGATE_READ_ENABLED", raising=False)
    env.add(_WEEK38_DAY, via_store=False)
    config = load_config()
    service = WeeklyImprovementReviewService(
        config=config.model_copy(
            update={
                "review_improvement": config.review_improvement.model_copy(
                    update={"issue_creation_enabled": False}
                )
            }
        ),
        evaluation_repository=env.evaluations,
        recommendation_repository=env.recommendations,
        weekly_review_metrics_repository=env.metrics,
        improvement_candidate_repository=env.candidates,
        rule_version_service=RuleVersionService(env.rule_versions),
        audit_service=AuditService(env.audit),
        aggregate_store=env.store,
    )

    outcome = service.run(_RUN_AT)

    assert outcome.aggregate_read is False and outcome.total_evaluation_results == 1


# --- backfill / 照合 / rebuild --------------------------------------------------------------


def test_backfill_dry_run_writes_nothing_and_reports_the_plan(env: Env) -> None:
    for weeks_before in range(0, 4):
        env.add(_week_day(weeks_before), via_store=False)
        env.add(_week_day(weeks_before), rule_version="v2", via_store=False)

    plan = env.maintenance().plan_backfill(_NOW)

    assert plan.week_count == 4 and plan.row_count == 8
    assert plan.matched_evaluations == 8 and plan.scanned_evaluations == 8
    assert plan.first_week == "2026-W35" and plan.last_week == "2026-W38"
    assert plan.estimated_write_items == 8 + 4 * 3
    assert env.store.query_week("2026-W38") == []  # ★ dry-run は何も書かない
    assert not env.store.get_backfill_status().complete


def test_backfill_execute_builds_all_history_and_is_idempotent(env: Env) -> None:
    for weeks_before in range(0, 3):
        env.add(_week_day(weeks_before), via_store=False)
    env.backfill()
    env.backfill()  # 再実行しても二重加算にならない

    assert [r.sample_count for r in env.store.query_week("2026-W38")] == [1]
    assert env.store.get_backfill_status().complete
    assert env.maintenance().verify(_NOW).consistent


def test_backfill_ignores_other_horizons_and_counts_missing_recommendations(env: Env) -> None:
    valid = env.add(_WEEK38_DAY, via_store=False)
    other_horizon = valid.model_copy(
        update={"evaluation_id": "ev-other", "horizon_calendar_days": 30}
    )
    env.evaluations.save(other_horizon)
    orphan = valid.model_copy(
        update={"evaluation_id": "ev-orphan", "recommendation_id": "rec-missing"}
    )
    env.evaluations.save(orphan)

    plan = env.maintenance().plan_backfill(_NOW)

    assert plan.missing_recommendation_count == 1  # 結合できない評価は数えず、件数だけ報告する
    assert plan.row_count == 1
    [row] = env.maintenance()._scan(_NOW).rows_by_week["2026-W38"].values()
    assert row.sample_count == 1  # 別ホライズン(30)は対象外 / 結合できない 1 件は数えない


def test_verify_detects_a_mismatch_and_rebuild_fixes_only_the_named_week(env: Env) -> None:
    for weeks_before in range(0, 3):
        env.add(_week_day(weeks_before), via_store=True)
    env.store.set_backfill_complete(_NOW, 3, 3)
    assert env.maintenance().verify(_NOW).consistent

    # W37 の Aggregate だけを壊す(件数を水増しした行で置き換える)
    [row] = env.store.query_week("2026-W37")
    broken = row.model_copy(update={"sample_count": row.sample_count + 5})
    seq = env.store.get_state("2026-W37").mark_seq
    assert env.store.replace_week("2026-W37", [broken], seq, _NOW, request_recompute=False)

    report = env.maintenance().verify(_NOW, mark_rebuild_required=True)

    assert [m.review_week for m in report.mismatches] == ["2026-W37"]
    assert report.marked_rebuild_required == ("2026-W37",)
    assert env.store.list_rebuild_weeks() == ["2026-W37"]
    # dry-run(既定)は何も変えない
    dry = env.maintenance().rebuild_weeks(frozenset({"2026-W37"}), _NOW)
    assert dry == {"2026-W37": 1}
    assert env.store.query_week("2026-W37")[0].sample_count == row.sample_count + 5
    # execute は指定週だけを直す
    w38_before = env.store.query_week("2026-W38")
    env.maintenance().rebuild_weeks(frozenset({"2026-W37"}), _NOW, execute=True)
    assert env.store.query_week("2026-W37")[0].sample_count == row.sample_count
    assert env.store.query_week("2026-W38") == w38_before
    assert env.store.list_rebuild_weeks() == []
    assert "2026-W37" in env.store.list_pending_weeks()  # Metrics の再生成を要求する
    assert env.maintenance().verify(_NOW).consistent


def test_plan_backfill_then_execute_backfill_on_the_same_service_still_sees_all_data(
    env: Env,
) -> None:
    """レビュー指摘 F3: `plan_backfill()` の後に同じ service で `execute_backfill()` を
    呼ぶという自然な使い方(dry-run で確認してから実行する)で、2 回目の走査が
    空にならないこと(呼び出し済みの iterator を渡すと、2 回目が黙って 0 件になっていた)。
    """
    for weeks_before in range(0, 5):
        env.add(_week_day(weeks_before), via_store=False)
    service = env.maintenance()

    plan = service.plan_backfill(_NOW)
    executed = service.execute_backfill(_NOW)  # ★ 同じ service インスタンスで 2 回目の走査

    assert plan.week_count == executed.week_count == 5
    assert plan.row_count == executed.row_count == 5
    assert executed.scanned_evaluations == 5
    assert env.store.get_backfill_status().complete
    assert env.store.get_backfill_status().week_count == 5


def test_execute_backfill_does_not_complete_on_an_empty_scan(env: Env) -> None:
    """レビュー指摘 F3: raw を 1 件も読めなかった(走査元が空)場合は COMPLETE にせず例外にする。

    genuinely 空のテーブル(scanned=0)と、対象ホライズンの評価が単に無いだけ(scanned>0 /
    matched=0)を区別する: 後者は正当な空の backfill として許容する(下のテストで確認)。
    """
    empty_service = WeeklyAggregateMaintenanceService(
        env.store, lambda: iter(()), env.recommendations, _HORIZON
    )

    with pytest.raises(RuntimeError, match="1 件も読めません"):
        empty_service.execute_backfill(_NOW)

    assert not env.store.get_backfill_status().complete


def test_execute_backfill_completes_when_no_evaluation_matches_the_horizon(env: Env) -> None:
    """scanned > 0(他のホライズンの評価は存在する)だが matched = 0 は、正当な空の backfill。"""
    other_horizon_only = env.add(_WEEK38_DAY, via_store=False).model_copy(
        update={"evaluation_id": "ev-other-only", "horizon_calendar_days": _HORIZON + 1}
    )
    env.evaluations.save(other_horizon_only)
    only_service = WeeklyAggregateMaintenanceService(
        env.store, lambda: [other_horizon_only], env.recommendations, _HORIZON
    )

    plan = only_service.execute_backfill(_NOW)

    assert plan.week_count == 0 and plan.scanned_evaluations == 1 and plan.matched_evaluations == 0
    assert env.store.get_backfill_status().complete


def test_backfill_reads_pre_existing_week_state_before_scanning_raw_not_after(env: Env) -> None:
    """レビュー指摘 F2 + Phase 2 追記(DoD3): 「既存 state を引き継いだ状態」での backfill を
    実際に通す。事前に評価を確定してある週(mark_seq > 0 が既に付いている)に対して、
    `_discover_weeks()` の後・本走査の前にもう 1 件確定すると、`states_before` はその新着より
    **前**の値を捉えていなければならない。もし本走査の**後**に状態を読んでいたら
    (是正前の順序)、`states_before` が新着分だけ進んだ値を拾ってしまい、
    `replace_week()` の楽観ロックが「変化なし」と誤認して新着分を欠いたまま上書き・
    COMPLETE してしまう(is_from_stale_state の欠陥)。是正後は、新着を検出して
    RuntimeError で拒否する(もう一度実行すれば新着分を含めて成功する)。
    """
    for weeks_before in range(0, 2):
        env.add(_week_day(weeks_before), via_store=True)  # 各週の state を mark_seq=1 にする
    before_counts = {r.item_key: r.sample_count for r in env.store.query_week("2026-W38")}
    assert before_counts  # 前提: 既に state が存在する週がある
    service = env.maintenance()
    original_scan = service._scan

    def scan_then_arrival(now: dt.datetime, only_weeks: Any = None) -> Any:
        # ★ 状態(_discover_weeksによる`states_before`)は素のまま(pre-arrival)。
        #   raw の本走査だけを、完了後に新着が届く形で差し替える(既存の rebuild 用テストと同型)。
        scan = original_scan(now, only_weeks)
        env.add(_WEEK38_DAY, via_store=True)  # raw の本走査を読み終えた直後に新着が届く
        return scan

    service._scan = scan_then_arrival  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="新しい評価"):
        service.execute_backfill(_NOW)

    # ★ 拒否されたので、backfill 自身は書き込んでいない。新着(commit_evaluation の増分書き込み)
    #   による正しい値(1 + 新着1件 = 2)がそのまま残っている。もし is_from_stale_state のまま
    #   (是正前の順序)なら、backfill はここを raw スキャン時点の古い値(新着を含まない1)で
    #   SET してしまい、増分書き込みの結果を消してしまう。
    after_counts = {r.item_key: r.sample_count for r in env.store.query_week("2026-W38")}
    assert after_counts == {k: v + 1 for k, v in before_counts.items()}
    assert not env.store.get_backfill_status().complete

    # もう一度(素の状態で)実行すれば、新着を含めて成功する(raw と一致する値になる)。
    env.maintenance().execute_backfill(_NOW)
    final_counts = {r.item_key: r.sample_count for r in env.store.query_week("2026-W38")}
    assert final_counts == after_counts
    assert env.store.get_backfill_status().complete


def test_rebuild_refuses_when_a_new_evaluation_arrives_after_the_raw_read(env: Env) -> None:
    env.add(_week_day(1), via_store=True)
    service = env.maintenance()
    original_scan = service._scan

    def scan_then_arrival(now: dt.datetime, only_weeks: Any = None) -> Any:
        scan = original_scan(now, only_weeks)
        env.add(_week_day(1), via_store=True)  # raw を読んだ後に届く
        return scan

    service._scan = scan_then_arrival  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="新しい評価"):
        service.rebuild_weeks(frozenset({"2026-W37"}), _NOW, execute=True)

    assert env.store.query_week("2026-W37")[0].sample_count == 2  # 上書きで消えていない


def test_review_week_label_matches_the_review_service_week_labels() -> None:
    from jstock_advisor.domain.entities.weekly_evaluation_aggregate import review_week_label

    _, _, label = _resolve_review_period(_RUN_AT)
    assert review_week_label(dt.date(2026, 9, 16)) == label == "2026-W38"
    # 年またぎ(ISO 週): 2026-12-31 は 2026-W53、2027-01-04 は 2027-W01
    assert review_week_label(dt.date(2026, 12, 31)) == "2026-W53"
    assert review_week_label(dt.date(2027, 1, 4)) == "2027-W01"


def test_domain_constants_are_the_same_as_the_metrics_service_constants() -> None:
    from jstock_advisor.domain.entities.weekly_evaluation_aggregate import (
        EXCLUDED_FROM_SUCCESS_RATE,
        SUCCESS_LABELS,
    )
    from jstock_advisor.services import performance_metrics_service as pms

    assert EXCLUDED_FROM_SUCCESS_RATE == pms._EXCLUDED_FROM_SUCCESS_RATE
    assert SUCCESS_LABELS == pms._SUCCESS_LABELS


# --- CLI(ローカル専用・dry-run 既定) -------------------------------------------------


def test_cli_backfill_is_dry_run_by_default_and_execute_writes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from jstock_advisor.cli import weekly_aggregate
    from jstock_advisor.cli.main import app

    for weeks_before in range(0, 3):
        env.add(_week_day(weeks_before), via_store=False)
    monkeypatch.setattr(weekly_aggregate, "_service", env.maintenance)
    runner = CliRunner()

    dry = runner.invoke(app, ["weekly-aggregate", "backfill"])
    assert dry.exit_code == 0, dry.output
    assert "mode=DRY_RUN" in dry.output and "weeks=3" in dry.output
    assert env.store.query_week("2026-W38") == []  # 何も書いていない

    monkeypatch.setattr(weekly_aggregate, "_service", env.maintenance)
    done = runner.invoke(app, ["weekly-aggregate", "backfill", "--execute"])
    assert done.exit_code == 0, done.output
    assert "mode=EXECUTE" in done.output
    assert env.store.get_backfill_status().complete


def test_cli_verify_exits_nonzero_on_mismatch_and_rebuild_needs_a_week(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from jstock_advisor.cli import weekly_aggregate
    from jstock_advisor.cli.main import app

    env.add(_WEEK38_DAY, via_store=False)  # raw にあるが Aggregate に無い
    monkeypatch.setattr(weekly_aggregate, "_service", env.maintenance)
    runner = CliRunner()

    verified = runner.invoke(app, ["weekly-aggregate", "verify"])
    assert verified.exit_code == 1 and "MISMATCH 2026-W38" in verified.output

    monkeypatch.setattr(weekly_aggregate, "_service", env.maintenance)
    no_week = runner.invoke(app, ["weekly-aggregate", "rebuild"])
    assert no_week.exit_code != 0  # --week は必須
