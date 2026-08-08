"""services/decision_snapshot_service.py(save_decision_snapshot_safely)のテスト
(判定精度向上機能Phase A)。

コードレビュー対応: DecisionSnapshotの保存失敗が既存のRecommendation保存・
LINE通知フローを絶対にブロックしないこと(Shadow導入の保証)と、失敗時に
CloudWatchで検索・メトリクスフィルタ可能な固定イベントキーで構造化ログを
残すことを検証する。秘密情報・巨大オブジェクトをログへ出さないことも確認する。
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import ConfidenceLevel, DecisionType, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.services.decision_snapshot_service import (
    DECISION_SNAPSHOT_SAVE_FAILED_EVENT,
    save_decision_snapshot_safely,
)

_STOCK_CODE = "2914"


def _recommendation() -> Recommendation:
    return Recommendation(
        recommendation_id="rec-1",
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        recommended_at=dt.datetime(2026, 8, 8, 3, 0, tzinfo=dt.UTC),
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("1100"), rationale="x"),
        ),
        price_at_recommendation=Decimal("1150"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def test_save_decision_snapshot_safely_persists_normally(tmp_path: Path) -> None:
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    logger = logging.getLogger("test")

    save_decision_snapshot_safely(repo, _recommendation(), DecisionType.BUY, logger)

    items = repo.list_all()
    assert len(items) == 1
    assert items[0].recommendation_id == "rec-1"


def test_save_decision_snapshot_safely_swallows_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """保存失敗が例外として呼び出し元へ伝播しないこと(既存のRecommendation保存・
    LINE通知フローをブロックしない、Shadow導入の保証)。"""

    class _FailingRepository:
        def save(self, decision: object) -> None:
            raise RuntimeError("boom")

    logger = logging.getLogger("test")
    with caplog.at_level(logging.WARNING):
        # 例外を送出せずに正常終了することそのものがテスト対象。
        save_decision_snapshot_safely(
            _FailingRepository(), _recommendation(), DecisionType.BUY, logger  # type: ignore[arg-type]
        )


def test_save_decision_snapshot_safely_logs_fixed_event_key_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """失敗時、CloudWatchで検索・メトリクスフィルタ可能な固定イベントキーと
    stock_code/recommendation_id/decision_typeを構造化ログへ残すこと。"""

    class _FailingRepository:
        def save(self, decision: object) -> None:
            raise RuntimeError("boom")

    logger = logging.getLogger("test.decision_snapshot")
    with caplog.at_level(logging.WARNING, logger="test.decision_snapshot"):
        save_decision_snapshot_safely(
            _FailingRepository(), _recommendation(), DecisionType.BUY, logger  # type: ignore[arg-type]
        )

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert DECISION_SNAPSHOT_SAVE_FAILED_EVENT in message
    assert "stock_code=2914" in message
    assert "recommendation_id=rec-1" in message
    assert "decision_type=BUY" in message


def test_save_decision_snapshot_safely_does_not_log_config_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ログに秘匿情報・巨大オブジェクト(config_values_used等)を含めないこと。"""
    recommendation = _recommendation().model_copy(
        update={"config_values_used": {"secret_like_key": "should-not-appear-in-logs"}}
    )

    class _FailingRepository:
        def save(self, decision: object) -> None:
            raise RuntimeError("boom")

    logger = logging.getLogger("test.decision_snapshot2")
    with caplog.at_level(logging.WARNING, logger="test.decision_snapshot2"):
        save_decision_snapshot_safely(
            _FailingRepository(), recommendation, DecisionType.BUY, logger  # type: ignore[arg-type]
        )

    for record in caplog.records:
        assert "should-not-appear-in-logs" not in record.getMessage()
