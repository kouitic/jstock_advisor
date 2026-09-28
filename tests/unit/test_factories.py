"""共有test factory(`tests/factories.py`)自体のテスト(Issue #647)。"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from tests.factories import build_recommendation


def test_build_recommendation_defaults_reasons_and_data_sources_non_empty() -> None:
    """`reasons`/`data_sources`は本番では必ず設定されるため、既定値は
    空にしない(#647の目的そのもの)。"""
    rec = build_recommendation()

    assert rec.reasons != []
    assert rec.data_sources != []


def test_build_recommendation_overrides_only_specified_fields() -> None:
    rec = build_recommendation(
        stock_code="7203",
        recommendation_type=RecommendationType.HOLD,
        reasons=[],
    )

    assert rec.stock_code == "7203"
    assert rec.recommendation_type is RecommendationType.HOLD
    assert rec.reasons == []
    # 上書きしていないfieldは既定値のまま
    assert rec.confidence is ConfidenceLevel.MEDIUM
    assert rec.data_sources != []
