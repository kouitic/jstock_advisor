"""Recommendationへ新規フィールド追加(デプロイ前対応)を行っても、既存の
(新フィールドを持たない)保存済みJSONレコードがそのまま読み込めることの
後方互換テスト。

earnings_release_confirmation_state/earnings_decision_relevanceは
Optional[...] = Noneとして追加したため、Pydantic v2の既定挙動(欠落フィールドは
デフォルト値を使う)だけで後方互換が成立するはずだが、モデル側に
model_config(extra="forbid")が設定されているため、実際にJSONファイル経由で
確認する。
"""

from __future__ import annotations

import json
from pathlib import Path

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
