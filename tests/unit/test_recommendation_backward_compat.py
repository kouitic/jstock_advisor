"""Recommendationへ新規フィールド追加(デプロイ前対応)を行っても、既存の
(新フィールドを持たない)保存済みJSONレコードがそのまま読み込めることの
後方互換テスト。

earnings_release_confirmation_state/earnings_decision_relevanceは
Optional[...] = Noneとして追加したため、Pydantic v2の既定挙動(欠落フィールドは
デフォルト値を使う)だけで後方互換が成立するはずだが、モデル側に
model_config(extra="forbid")が設定されているため、実際にJSONファイル経由で
確認する。

判定精度向上機能次フェーズSTEP2(Entry/Exit Price Range Shadow)で追加した
20フィールドも同様に全てOptional/デフォルト値持ちであるため、entry_price_
range_*/exit_price_range_*キーを一切持たない旧形式JSONが引き続き読み込める
ことを確認する(コードレビュー対応STEP2 §17)。

判定精度向上機能Phase D(Market/Sector Environment Shadow)で追加した
market_*/sector_*/environment_*フィールドも同様。DecisionSnapshotは
market_score/sector_score/environment_scoreのみ以前から予約済みだったが、
confidence/coverage/reason_codes/metricsは新規追加であるため、これらを
一切持たない旧形式JSONが引き続き読み込めることを確認する。
"""

from __future__ import annotations

import json
from pathlib import Path

from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)

_OLD_SHAPE_RECOMMENDATION = {
    "recommendation_id": "old-rec-1",
    "stock_code": "2914",
    "stock_name": "日本たばこ産業",
    "recommended_at": "2026-07-01T00:00:00Z",
    "recommendation_type": "PARTIAL_PROFIT_TAKE",
    "price_at_recommendation": "4200",
    "confidence": "MEDIUM",
    "rule_version": "v1-mvp",
}


def test_old_shape_recommendation_without_new_earnings_fields_loads(tmp_path: Path) -> None:
    store_dir = tmp_path / "local_store"
    store_dir.mkdir()
    (store_dir / "recommendations.json").write_text(
        json.dumps([_OLD_SHAPE_RECOMMENDATION]), encoding="utf-8"
    )

    repo = RecommendationRepository(store_dir=store_dir)
    rec = repo.get("old-rec-1")

    assert rec is not None
    assert rec.stock_code == "2914"
    assert rec.earnings_date_status is None
    assert rec.earnings_date_raw is None
    assert rec.earnings_release_confirmation_state is None
    assert rec.earnings_decision_relevance is None
    assert rec.entry_price_range_state is None
    assert rec.entry_price_range_confidence is None
    assert rec.entry_price_range_coverage is None
    assert rec.entry_price_range_reason_codes == ()
    assert rec.entry_price_range_metrics == {}
    assert rec.entry_price_range_starter_price is None
    assert rec.entry_price_range_preferred_price is None
    assert rec.entry_price_range_strong_price is None
    assert rec.entry_price_range_max_price is None
    assert rec.entry_price_range_stop_review_price is None
    assert rec.exit_price_range_state is None
    assert rec.exit_price_range_confidence is None
    assert rec.exit_price_range_coverage is None
    assert rec.exit_price_range_reason_codes == ()
    assert rec.exit_price_range_metrics == {}
    assert rec.exit_price_range_partial_low_price is None
    assert rec.exit_price_range_partial_high_price is None
    assert rec.exit_price_range_strong_price is None
    assert rec.exit_price_range_downside_review_price is None
    assert rec.exit_price_range_exit_review_price is None
    assert rec.market_score is None
    assert rec.market_confidence is None
    assert rec.market_coverage is None
    assert rec.market_reason_codes == ()
    assert rec.market_metrics == {}
    assert rec.sector_score is None
    assert rec.sector_confidence is None
    assert rec.sector_coverage is None
    assert rec.sector_reason_codes == ()
    assert rec.sector_metrics == {}
    assert rec.environment_score is None
    assert rec.environment_confidence is None
    assert rec.environment_coverage is None
    assert rec.environment_reason_codes == ()
    assert rec.environment_metrics == {}


_OLD_SHAPE_DECISION_SNAPSHOT = {
    "decision_id": "decision|old-rec-1",
    "decision_type": "SELL",
    "stock_code": "2914",
    "evaluated_at": "2026-07-01T00:00:00Z",
    "evaluation_date_jst": "2026-07-01",
    "market_price": "4200",
    "rule_version": "v1-mvp",
    "model_version": "phase_c_earnings_v3",
}


def test_old_shape_decision_snapshot_without_entry_exit_price_range_fields_loads(
    tmp_path: Path,
) -> None:
    store_dir = tmp_path / "local_store"
    store_dir.mkdir()
    (store_dir / "decision_snapshots.json").write_text(
        json.dumps([_OLD_SHAPE_DECISION_SNAPSHOT]), encoding="utf-8"
    )

    repo = DecisionSnapshotRepository(store_dir=store_dir)
    snapshot = repo.get("decision|old-rec-1")

    assert snapshot is not None
    assert snapshot.stock_code == "2914"
    assert snapshot.entry_price_range_state is None
    assert snapshot.entry_price_range_confidence is None
    assert snapshot.entry_price_range_coverage is None
    assert snapshot.entry_price_range_reason_codes == ()
    assert snapshot.entry_price_range_metrics == {}
    assert snapshot.entry_price_range_starter_price is None
    assert snapshot.entry_price_range_preferred_price is None
    assert snapshot.entry_price_range_strong_price is None
    assert snapshot.entry_price_range_max_price is None
    assert snapshot.entry_price_range_stop_review_price is None
    assert snapshot.exit_price_range_state is None
    assert snapshot.exit_price_range_confidence is None
    assert snapshot.exit_price_range_coverage is None
    assert snapshot.exit_price_range_reason_codes == ()
    assert snapshot.exit_price_range_metrics == {}
    assert snapshot.exit_price_range_partial_low_price is None
    assert snapshot.exit_price_range_partial_high_price is None
    assert snapshot.exit_price_range_strong_price is None
    assert snapshot.exit_price_range_downside_review_price is None
    assert snapshot.exit_price_range_exit_review_price is None
    assert snapshot.market_score is None
    assert snapshot.market_confidence is None
    assert snapshot.market_coverage is None
    assert snapshot.market_reason_codes == ()
    assert snapshot.market_metrics == {}
    assert snapshot.sector_score is None
    assert snapshot.sector_confidence is None
    assert snapshot.sector_coverage is None
    assert snapshot.sector_reason_codes == ()
    assert snapshot.sector_metrics == {}
    assert snapshot.environment_score is None
    assert snapshot.environment_confidence is None
    assert snapshot.environment_coverage is None
    assert snapshot.environment_reason_codes == ()
    assert snapshot.environment_metrics == {}


# --- Issue #22 Phase 3.5(2026-08-28): company_quality_score_model_version -----
# 買い側company_quality_score専用の第4の版概念(DecisionSnapshot.model_version・
# 各Shadowスコアのmodel_version・保有判断側scoring_model_versionとは別物)。
# フィールドを持たない既存レコードはdefaultにより"v1"として読む(backfillしない)。
# buy_score_input_facts_schema_versionキーを持たない既存factsは
# LEGACY_UNVERSIONEDとして扱う(キー数から世代を推測しない)。


def test_old_shape_recommendation_defaults_company_quality_score_model_version(
    tmp_path: Path,
) -> None:
    store_dir = tmp_path / "local_store"
    store_dir.mkdir()
    (store_dir / "recommendations.json").write_text(
        json.dumps([_OLD_SHAPE_RECOMMENDATION]), encoding="utf-8"
    )

    repo = RecommendationRepository(store_dir=store_dir)
    rec = repo.get("old-rec-1")

    assert rec is not None
    assert rec.company_quality_score_model_version == "v1"


def test_old_shape_recommendation_facts_without_schema_version_is_legacy(
    tmp_path: Path,
) -> None:
    """schema versionキーを持たない旧factsはLEGACY_UNVERSIONEDとして扱う
    (消費側は「キーが無い=当時未実装」と「キーが無い=判定不能」を混同しない)。
    既存データのbackfillは行わないため、読み込み時にキーが自然に欠けたままで
    あることを確認する。"""
    old_with_facts = dict(_OLD_SHAPE_RECOMMENDATION)
    old_with_facts["recommendation_id"] = "old-rec-legacy-facts"
    old_with_facts["buy_score_input_facts"] = {"total_yield_pct": 3.0}
    store_dir = tmp_path / "local_store"
    store_dir.mkdir()
    (store_dir / "recommendations.json").write_text(
        json.dumps([old_with_facts]), encoding="utf-8"
    )

    repo = RecommendationRepository(store_dir=store_dir)
    rec = repo.get("old-rec-legacy-facts")

    assert rec is not None
    assert rec.buy_score_input_facts is not None
    assert "buy_score_input_facts_schema_version" not in rec.buy_score_input_facts


def test_old_shape_decision_snapshot_defaults_company_quality_score_model_version(
    tmp_path: Path,
) -> None:
    store_dir = tmp_path / "local_store"
    store_dir.mkdir()
    (store_dir / "decision_snapshots.json").write_text(
        json.dumps([_OLD_SHAPE_DECISION_SNAPSHOT]), encoding="utf-8"
    )

    repo = DecisionSnapshotRepository(store_dir=store_dir)
    snapshot = repo.get("decision|old-rec-1")

    assert snapshot is not None
    assert snapshot.company_quality_score_model_version == "v1"


# --- Issue #21(2026-08-28): fair_value使用可否スナップショット3フィールド -----
# fair_value_usable_for_trading_judgment / fair_value_unusable_reason_code /
# fair_value_unusable_reason を持たない既存レコードはNoneとして読む。
# Noneは「保存なし(改修前の記録)」を意味し、現在configで理由を再構築しない
# (backfillも行わない)。


def test_old_shape_recommendation_without_fair_value_usability_fields_loads(
    tmp_path: Path,
) -> None:
    store_dir = tmp_path / "local_store"
    store_dir.mkdir()
    (store_dir / "recommendations.json").write_text(
        json.dumps([_OLD_SHAPE_RECOMMENDATION]), encoding="utf-8"
    )

    repo = RecommendationRepository(store_dir=store_dir)
    rec = repo.get("old-rec-1")

    assert rec is not None
    assert rec.fair_value_usable_for_trading_judgment is None
    assert rec.fair_value_unusable_reason_code is None
    assert rec.fair_value_unusable_reason is None
