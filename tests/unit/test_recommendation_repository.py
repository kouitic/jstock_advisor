"""RecommendationRepository.get_latest_by_typeのテスト(振り返り機能改修)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.enums import ConfidenceLevel, ExecutionMode, RecommendationType
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    PRODUCTION_FILE_NAME,
    VALIDATION_FILE_NAME,
    RecommendationRepository,
)


def _recommendation(
    rec_id: str,
    rec_type: RecommendationType,
    recommended_at: dt.datetime,
    rule_version: str,
) -> Recommendation:
    return Recommendation(
        recommendation_id=rec_id,
        stock_code="2914",
        stock_name="test",
        recommended_at=recommended_at,
        recommendation_type=rec_type,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version=rule_version,
    )


def test_get_latest_by_type_returns_most_recent(tmp_path: Path) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    repo.save(
        _recommendation(
            "r1", RecommendationType.BUY, dt.datetime(2026, 8, 1, tzinfo=dt.UTC), "v10"
        )
    )
    repo.save(
        _recommendation(
            "r2", RecommendationType.BUY, dt.datetime(2026, 8, 5, tzinfo=dt.UTC), "v11"
        )
    )
    repo.save(
        _recommendation(
            "r3", RecommendationType.SELL, dt.datetime(2026, 8, 6, tzinfo=dt.UTC), "v20"
        )
    )

    latest_buy = repo.get_latest_by_type(RecommendationType.BUY)
    assert latest_buy is not None
    assert latest_buy.recommendation_id == "r2"
    assert latest_buy.rule_version == "v11"


def test_get_latest_by_type_returns_none_when_no_match(tmp_path: Path) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    repo.save(
        _recommendation(
            "r1", RecommendationType.BUY, dt.datetime(2026, 8, 1, tzinfo=dt.UTC), "v10"
        )
    )

    assert repo.get_latest_by_type(RecommendationType.SELL) is None


# --- 通知検証モード機能(2026-08追加) -------------------------------------


def test_for_execution_context_normal_targets_production_file_name(tmp_path: Path) -> None:
    repo = RecommendationRepository.for_execution_context(
        ExecutionContext.normal(), store_dir=tmp_path
    )
    assert repo.file_name == PRODUCTION_FILE_NAME


def test_for_execution_context_validation_targets_validation_file_name(tmp_path: Path) -> None:
    repo = RecommendationRepository.for_execution_context(
        ExecutionContext(mode=ExecutionMode.VALIDATION), store_dir=tmp_path
    )
    assert repo.file_name == VALIDATION_FILE_NAME


def test_normal_and_validation_repositories_are_physically_isolated(tmp_path: Path) -> None:
    """同一store_dirでも、NORMAL用リポジトリの保存内容はVALIDATION用からは見えない
    (逆方向も同様)。本番Recommendationテーブルを検証実行が汚染しないことの保証。
    """
    normal_repo = RecommendationRepository.for_execution_context(
        ExecutionContext.normal(), store_dir=tmp_path
    )
    validation_repo = RecommendationRepository.for_execution_context(
        ExecutionContext(mode=ExecutionMode.VALIDATION), store_dir=tmp_path
    )

    normal_rec = _recommendation(
        "normal-1", RecommendationType.BUY, dt.datetime(2026, 8, 1, tzinfo=dt.UTC), "v1"
    )
    validation_rec = _recommendation(
        "validation-1", RecommendationType.BUY, dt.datetime(2026, 8, 1, tzinfo=dt.UTC), "v1"
    )
    normal_repo.save(normal_rec)
    validation_repo.save(validation_rec)

    assert normal_repo.get("validation-1") is None
    assert validation_repo.get("normal-1") is None
    assert [r.recommendation_id for r in normal_repo.list_all()] == ["normal-1"]
    assert [r.recommendation_id for r in validation_repo.list_all()] == ["validation-1"]


def test_delete_removes_recommendation(tmp_path: Path) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    repo.save(
        _recommendation(
            "r1", RecommendationType.BUY, dt.datetime(2026, 8, 1, tzinfo=dt.UTC), "v10"
        )
    )

    assert repo.delete("r1") is True
    assert repo.get("r1") is None


def test_delete_returns_false_when_not_found(tmp_path: Path) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    assert repo.delete("does-not-exist") is False
