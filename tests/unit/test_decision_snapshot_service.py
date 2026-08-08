"""services/decision_snapshot_service.py(save_decision_snapshot_safely)のテスト
(判定精度向上機能Phase A)。

コードレビュー対応: DecisionSnapshotの保存失敗が既存のRecommendation保存・
LINE通知フローを絶対にブロックしないこと(Shadow導入の保証)と、失敗時に
CloudWatchで検索・メトリクスフィルタ可能な固定イベントキーで構造化ログを
残すことを検証する。秘密情報・巨大オブジェクトをログへ出さないことも確認する。

再レビュー対応(insert-only保証): 同一decision_idの再実行が「完全に同一内容」
なら正常な冪等再実行(warning不要)、「内容が異なる」ならdecision_snapshot_conflict
として検知し既存の記録を上書きしないことを検証する。

再々レビュー対応(誤conflict検知の解消): insert_if_absent()に負けた直後の内容
比較にはget_consistent()(strongly consistent read)を使うため、DynamoDBの
結果整合性読み取りによる一時的なNoneを正常な冪等再実行と誤ってconflict扱い
しないことを、フェイクRepositoryによる並行実行シミュレーションで検証する。
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.decision_snapshot_builder import build_decision_snapshot
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.decision_snapshot import DecisionSnapshot
from jstock_advisor.domain.entities.enums import ConfidenceLevel, DecisionType, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.services.decision_snapshot_service import (
    DECISION_SNAPSHOT_CONFLICT_EVENT,
    DECISION_SNAPSHOT_SAVE_FAILED_EVENT,
    save_decision_snapshot_safely,
)

_STOCK_CODE = "2914"


def _recommendation(price_at_recommendation: Decimal = Decimal("1150")) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-1",
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        recommended_at=dt.datetime(2026, 8, 8, 3, 0, tzinfo=dt.UTC),
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("1100"), rationale="x"),
        ),
        price_at_recommendation=price_at_recommendation,
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


def test_save_decision_snapshot_safely_is_idempotent_on_identical_resave(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """同一Recommendation・同一DecisionTypeでの再実行(完全に同一内容)は、
    件数が増えず、warningも出さない正常な冪等再実行として扱われる。"""
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    logger = logging.getLogger("test.idempotent")
    recommendation = _recommendation()

    with caplog.at_level(logging.WARNING, logger="test.idempotent"):
        save_decision_snapshot_safely(repo, recommendation, DecisionType.BUY, logger)
        save_decision_snapshot_safely(repo, recommendation, DecisionType.BUY, logger)

    items = repo.list_all()
    assert len(items) == 1
    assert items[0].market_price == Decimal("1150")
    assert caplog.records == []


def test_save_decision_snapshot_safely_logs_conflict_on_content_mismatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """同一decision_idだが内容が異なる場合、既存の記録は上書きせずdecision_snapshot_conflict
    をWARNINGログへ残す(データ不整合の検知。保存失敗とは区別する)。"""
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    logger = logging.getLogger("test.conflict")

    first = _recommendation(price_at_recommendation=Decimal("1150"))
    save_decision_snapshot_safely(repo, first, DecisionType.BUY, logger)

    second = _recommendation(price_at_recommendation=Decimal("1300"))  # 同じrecommendation_id
    with caplog.at_level(logging.WARNING, logger="test.conflict"):
        save_decision_snapshot_safely(repo, second, DecisionType.BUY, logger)

    items = repo.list_all()
    assert len(items) == 1
    assert items[0].market_price == Decimal("1150")  # 既存の値が保持される

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert DECISION_SNAPSHOT_CONFLICT_EVENT in message
    assert "stock_code=2914" in message
    assert "recommendation_id=rec-1" in message
    assert "decision_type=BUY" in message


def test_save_decision_snapshot_safely_conflict_does_not_raise(tmp_path: Path) -> None:
    """conflict検知時も例外を送出しない(既存のRecommendation保存・LINE通知フローを
    ブロックしない、Shadow導入の保証)。"""
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    logger = logging.getLogger("test.conflict2")

    save_decision_snapshot_safely(
        repo, _recommendation(price_at_recommendation=Decimal("1150")), DecisionType.BUY, logger
    )
    # 例外を送出せずに正常終了することそのものがテスト対象。
    save_decision_snapshot_safely(
        repo, _recommendation(price_at_recommendation=Decimal("1300")), DecisionType.BUY, logger
    )


def test_save_decision_snapshot_safely_swallows_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """保存失敗が例外として呼び出し元へ伝播しないこと(既存のRecommendation保存・
    LINE通知フローをブロックしない、Shadow導入の保証)。"""

    class _FailingRepository:
        def get_consistent(self, decision_id: str) -> None:
            raise RuntimeError("boom")

        def insert_if_absent(self, decision: object) -> bool:
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
        def get_consistent(self, decision_id: str) -> None:
            raise RuntimeError("boom")

        def insert_if_absent(self, decision: object) -> bool:
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
        def get_consistent(self, decision_id: str) -> None:
            raise RuntimeError("boom")

        def insert_if_absent(self, decision: object) -> bool:
            raise RuntimeError("boom")

    logger = logging.getLogger("test.decision_snapshot2")
    with caplog.at_level(logging.WARNING, logger="test.decision_snapshot2"):
        save_decision_snapshot_safely(
            _FailingRepository(), recommendation, DecisionType.BUY, logger  # type: ignore[arg-type]
        )

    for record in caplog.records:
        assert "should-not-appear-in-logs" not in record.getMessage()


class _RaceRepository:
    """1回目のget_consistent()はNone(未挿入に見える)、insert_if_absent()は常に
    False(並行実行で他プロセスが先にinsertした=raceに負けたことを模す)、2回目の
    get_consistent()で初めて「先に書き込まれた側」の内容が見える、という並行実行を
    模したフェイクRepository(item 6.2/6.3: insert_if_absentの並行競合相当ケースの
    テスト用。strongly consistent readを使うため、DynamoDBの結果整合性読み取りに
    よる一時的なNoneでこの2回目の呼び出しがNoneになることは想定しない)。
    """

    def __init__(self, winner: DecisionSnapshot) -> None:
        self._winner = winner
        self.get_call_count = 0
        self.insert_if_absent_called = False

    def get_consistent(self, decision_id: str) -> DecisionSnapshot | None:
        self.get_call_count += 1
        if self.get_call_count == 1:
            return None
        return self._winner

    def insert_if_absent(self, decision: DecisionSnapshot) -> bool:
        self.insert_if_absent_called = True
        return False


class _RaceRepositoryPermanentlyMissing:
    """insert_if_absent()は常にFalse(競合したように見える)だが、その後の
    get_consistent()も常にNoneを返す、通常は起こり得ない想定外状態を模した
    フェイクRepository(item 6.4: insert_if_absent=Falseなのにstrongly consistent
    readでも存在しない場合は、内容不一致conflictではなく保存系障害として扱う
    ことを検証する)。
    """

    def get_consistent(self, decision_id: str) -> DecisionSnapshot | None:
        return None

    def insert_if_absent(self, decision: DecisionSnapshot) -> bool:
        return False


def test_save_decision_snapshot_safely_race_with_identical_winner_is_idempotent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """並行実行で自分がrace負けしても、先に書き込まれた内容が自分の構築結果と
    完全一致するなら正常な冪等再実行として扱い、warningを出さない。"""
    recommendation = _recommendation()
    winner = build_decision_snapshot(recommendation, DecisionType.BUY)
    repo = _RaceRepository(winner)
    logger = logging.getLogger("test.race_identical")

    with caplog.at_level(logging.WARNING, logger="test.race_identical"):
        save_decision_snapshot_safely(repo, recommendation, DecisionType.BUY, logger)

    assert repo.insert_if_absent_called is True
    assert repo.get_call_count == 2
    assert caplog.records == []


def test_save_decision_snapshot_safely_race_with_conflicting_winner_logs_conflict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """並行実行で自分がrace負けし、かつ先に書き込まれた内容が自分の構築結果と
    異なる場合はdecision_snapshot_conflictを検知する(上書きは一切行わない)。"""
    recommendation = _recommendation(price_at_recommendation=Decimal("1150"))
    conflicting_recommendation = _recommendation(price_at_recommendation=Decimal("1300"))
    winner = build_decision_snapshot(conflicting_recommendation, DecisionType.BUY)
    repo = _RaceRepository(winner)
    logger = logging.getLogger("test.race_conflict")

    with caplog.at_level(logging.WARNING, logger="test.race_conflict"):
        save_decision_snapshot_safely(repo, recommendation, DecisionType.BUY, logger)

    assert repo.insert_if_absent_called is True
    assert len(caplog.records) == 1
    assert DECISION_SNAPSHOT_CONFLICT_EVENT in caplog.records[0].getMessage()


def test_save_decision_snapshot_safely_treats_permanently_missing_as_save_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """insert_if_absent()=Falseなのにstrongly consistent readでも存在しない
    (通常は起こり得ない想定外状態)場合、decision_snapshot_conflictにはせず、
    decision_snapshot_save_failedとして扱う(item 6.4)。"""
    repo = _RaceRepositoryPermanentlyMissing()
    logger = logging.getLogger("test.permanently_missing")

    with caplog.at_level(logging.WARNING, logger="test.permanently_missing"):
        save_decision_snapshot_safely(repo, _recommendation(), DecisionType.BUY, logger)

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert DECISION_SNAPSHOT_SAVE_FAILED_EVENT in message
    assert DECISION_SNAPSHOT_CONFLICT_EVENT not in message
