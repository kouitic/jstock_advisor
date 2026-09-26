"""Issue #531(#71 F-C14): 監査記録を決定的IDのrecord_if_absent()へ統一する
(buy_candidates_handler.py側。PR #622〔watchlist_screening_audit.py側〕の
続き。D5解放後に別PRへ分離した部分)。

対象は#528/#558と同型の欠陥(非同期fan-out/CLI再試行の再配信で、`AuditService.
record()`〔呼び出しごとにaudit_idをuuid4で新規生成〕が監査ログを重複記録する)
のうち、batch_id/stock_codeが呼び出し時点で既に引数として存在する最小scope
(Tier 1)である。

- `lambda_handlers/buy_candidates_handler.py::_record_evaluation_audit()`
- `lambda_handlers/buy_candidates_handler.py::_record_notification_outcome_audit()`

`AuditService(repository=fake_repo)`(実サービス+`save_if_absent()`の意味論を
最小限で模倣したfake repository。test_issue_558_batch_id_idempotency.pyの
`_FakeAuditRepository`と同型)で検証する。architecturally正しいAuditService
自体の挙動を経由することで、テスト側のfakeがrecord_if_absent()の意味論を
誤って再実装するリスクを避ける。

mutation-based negative verification: production関数を一時的に
record_if_absent()ではなくrecord()(常に成功)を呼ぶよう変異させ、このテストが
実際に赤くなることを確認したうえで復元した(下記コメント参照)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    CandidateSource,
    ConfidenceLevel,
    PortfolioValuationBasis,
    RecommendationType,
)
from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.lambda_handlers import buy_candidates_handler
from jstock_advisor.services.audit_service import AuditService

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


class _FakeAuditRepository:
    """`AuditLogRepository.save_if_absent()`の意味論(決定的audit_idの原子的な
    条件付き挿入)を最小限で模倣する(test_issue_558_batch_id_idempotency.pyの
    `_FakeAuditRepository`と同型)。
    """

    def __init__(self) -> None:
        self.saved_audit_ids: list[str] = []
        self.save_calls: int = 0

    def save(self, entry: object) -> None:
        self.save_calls += 1
        self.saved_audit_ids.append(entry.audit_id)  # type: ignore[attr-defined]

    def save_if_absent(self, entry: object) -> bool:
        audit_id = entry.audit_id  # type: ignore[attr-defined]
        if audit_id in self.saved_audit_ids:
            return False
        self.save_calls += 1
        self.saved_audit_ids.append(audit_id)
        return True


def _fake_audit_service() -> tuple[AuditService, _FakeAuditRepository]:
    repo = _FakeAuditRepository()
    return AuditService(repository=repo), repo


def _call_record_evaluation_audit(
    audit_service: AuditService, batch_id: str | None, stock_code: str = "2914"
) -> None:
    buy_candidates_handler._record_evaluation_audit(
        audit_service,
        "v1-mvp",
        _NOW,
        stock_code,
        CandidateSource.WATCHLIST,
        None,
        None,
        current_market_value=None,
        unrealized_profit_loss=None,
        unrealized_profit_loss_pct=None,
        base_buy_action=BuyAction.EXCLUDED,
        final_buy_action=BuyAction.EXCLUDED,
        conflicting_holding_action=None,
        holding_data_inconsistent=False,
        batch_id=batch_id,
    )


def test_record_evaluation_audit_is_not_duplicated_on_retry_with_the_same_batch_id() -> None:
    """AC1: 同一(batch_id, stock_code)の評価監査が非同期fan-out再配信で複製されない。"""
    audit_service, repo = _fake_audit_service()

    _call_record_evaluation_audit(audit_service, batch_id="batch-531-a")
    _call_record_evaluation_audit(audit_service, batch_id="batch-531-a")  # retry相当

    assert repo.save_calls == 1
    assert repo.saved_audit_ids == ["unified_buy_candidate_evaluation:batch-531-a:2914"]


def test_record_evaluation_audit_uses_a_separate_id_for_a_different_stock_code() -> None:
    """異なるstock_codeは別の監査記録として残る(取り違えて潰さない)。"""
    audit_service, repo = _fake_audit_service()

    _call_record_evaluation_audit(audit_service, batch_id="batch-531-b")
    _call_record_evaluation_audit(audit_service, batch_id="batch-531-b", stock_code="7239")

    assert repo.save_calls == 2


def test_record_evaluation_audit_uses_a_separate_id_for_a_different_batch_id() -> None:
    """F2(粒度): 同一stock_codeでもbatch_idが異なれば別の監査記録として残る
    (batch_idを落とすと、別batchの同一銘柄評価が黙って抑止されて失われることの
    固定。#622 F2〔build_removal_audit_id()前例〕と同型)。
    """
    audit_service, repo = _fake_audit_service()

    _call_record_evaluation_audit(audit_service, batch_id="batch-531-c1")
    _call_record_evaluation_audit(audit_service, batch_id="batch-531-c2")

    assert repo.save_calls == 2
    assert repo.saved_audit_ids == [
        "unified_buy_candidate_evaluation:batch-531-c1:2914",
        "unified_buy_candidate_evaluation:batch-531-c2:2914",
    ]


def test_record_evaluation_audit_without_batch_id_keeps_using_record_uuid4() -> None:
    """AC3: batch_id=None(白箱テスト等の既存呼び出し)は従来どおりrecord()
    (uuid4)のまま(後方互換ガード)。2回呼べば2件になる(dedupしない)。
    """
    audit_service, repo = _fake_audit_service()

    _call_record_evaluation_audit(audit_service, batch_id=None)
    _call_record_evaluation_audit(audit_service, batch_id=None)

    assert repo.save_calls == 2
    assert len(set(repo.saved_audit_ids)) == 2  # uuid4なので毎回別のaudit_id


def test_record_evaluation_audit_negative_verification_without_record_if_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mutation-based negative verification: production関数を一時的に
    record_if_absent()ではなくrecord()を呼ぶよう変異させ、このテストが
    検出したい実装欠陥(#528/#558と同型の重複記録)そのものを再現する。
    """
    audit_service, repo = _fake_audit_service()

    def _broken_record_if_absent(**kwargs: object) -> object:
        kwargs.pop("audit_id", None)
        return audit_service.record(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(audit_service, "record_if_absent", _broken_record_if_absent)

    _call_record_evaluation_audit(audit_service, batch_id="batch-531-broken")
    _call_record_evaluation_audit(audit_service, batch_id="batch-531-broken")

    assert repo.save_calls == 2, "この壊れた実装は2件保存する(=重複防止が効いていない確認)"


# --- buy_candidates_handler.py::_record_notification_outcome_audit() ---------


def _make_recommendation(stock_code: str) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-1",
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("3500"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3300"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("3100"), rationale="x"),
        ),
        price_at_recommendation=Decimal("4200"),
        total_score=60.0,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        buy_action=BuyAction.BUY,
        base_buy_action=BuyAction.BUY,
        company_quality_score=60.0,
        purchase_attractiveness_score=50.0,
    )


def _call_record_notification_outcome_audit(
    audit_service: AuditService,
    batch_id: str,
    stock_code: str = "2914",
    notification_pathway: str = "buy",
) -> None:
    buy_candidates_handler._record_notification_outcome_audit(
        audit_service,
        "v1-mvp",
        _NOW,
        _make_recommendation(stock_code),
        1,
        None,
        "NOT_REQUIRED",
        NotificationEligibility(eligible=True),
        PortfolioValuationBasis.MARKET_VALUE,
        None,
        1.0,
        batch_id=batch_id,
        notification_pathway=notification_pathway,
    )


def test_record_notification_outcome_audit_is_not_duplicated_on_retry_with_the_same_batch_id() -> (
    None
):
    """AC1: 同一(batch_id, stock_code)の通知結果監査が非同期fan-out再配信で
    複製されない。
    """
    audit_service, repo = _fake_audit_service()

    _call_record_notification_outcome_audit(audit_service, batch_id="batch-531-n1")
    _call_record_notification_outcome_audit(audit_service, batch_id="batch-531-n1")  # retry相当

    assert repo.save_calls == 1
    assert repo.saved_audit_ids == [
        "unified_buy_candidate_notification_outcome:batch-531-n1:buy:2914"
    ]


def test_record_notification_outcome_audit_uses_a_separate_id_for_a_different_stock_code() -> (
    None
):
    """異なるstock_codeは別の監査記録として残る(取り違えて潰さない)。"""
    audit_service, repo = _fake_audit_service()

    _call_record_notification_outcome_audit(audit_service, batch_id="batch-531-n2")
    _call_record_notification_outcome_audit(
        audit_service, batch_id="batch-531-n2", stock_code="7239"
    )

    assert repo.save_calls == 2


def test_record_notification_outcome_audit_uses_a_separate_id_for_a_different_batch_id() -> (
    None
):
    """F2(粒度): 同一stock_codeでもbatch_idが異なれば別の監査記録として残る
    (batch_idを落とすと、別batchの同一銘柄の通知結果が黙って抑止されて失われる
    ことの固定。#622 F2と同型)。
    """
    audit_service, repo = _fake_audit_service()

    _call_record_notification_outcome_audit(audit_service, batch_id="batch-531-n3a")
    _call_record_notification_outcome_audit(audit_service, batch_id="batch-531-n3b")

    assert repo.save_calls == 2
    assert repo.saved_audit_ids == [
        "unified_buy_candidate_notification_outcome:batch-531-n3a:buy:2914",
        "unified_buy_candidate_notification_outcome:batch-531-n3b:buy:2914",
    ]


def test_record_evaluation_and_notification_outcome_audit_ids_do_not_collide() -> None:
    """F2(粒度): decision_type prefixが異なるため、同一batch_id・stock_codeでも
    evaluation監査とnotification_outcome監査は互いを抑止しない。
    """
    audit_service, repo = _fake_audit_service()

    _call_record_evaluation_audit(audit_service, batch_id="batch-531-n4")
    _call_record_notification_outcome_audit(audit_service, batch_id="batch-531-n4")

    assert repo.save_calls == 2


def test_record_notification_outcome_audit_uses_a_separate_id_for_a_different_pathway() -> (
    None
):
    """F1(サブちゃんレビュー対応。PR #625自身が持ち込んだ退行の修正):
    同一batch_id・同一stock_codeでもnotification_pathwayが異なれば別の監査
    記録として残る。NEAR_BUY監視中の銘柄が決算接近で当日WATCH_BEFORE_EARNINGS
    へ切り替わる場合、同一銘柄がnear_buyパスとwatch_endパスの両方から異なる
    内容で記録されうる(_finalize_batch()の別々のループから)。
    notification_pathwayを欠くと2件目が黙って抑止される。
    """
    audit_service, repo = _fake_audit_service()

    _call_record_notification_outcome_audit(
        audit_service, batch_id="batch-531-n6", notification_pathway="near_buy"
    )
    _call_record_notification_outcome_audit(
        audit_service, batch_id="batch-531-n6", notification_pathway="watch_end"
    )

    assert repo.save_calls == 2
    assert repo.saved_audit_ids == [
        "unified_buy_candidate_notification_outcome:batch-531-n6:near_buy:2914",
        "unified_buy_candidate_notification_outcome:batch-531-n6:watch_end:2914",
    ]


def test_record_notification_outcome_audit_negative_verification_without_record_if_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mutation-based negative verification: production関数を一時的に
    record_if_absent()ではなくrecord()を呼ぶよう変異させ、このテストが
    検出したい実装欠陥(#528/#558と同型の重複記録)そのものを再現する。
    """
    audit_service, repo = _fake_audit_service()

    def _broken_record_if_absent(**kwargs: object) -> object:
        kwargs.pop("audit_id", None)
        return audit_service.record(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(audit_service, "record_if_absent", _broken_record_if_absent)

    _call_record_notification_outcome_audit(audit_service, batch_id="batch-531-n5")
    _call_record_notification_outcome_audit(audit_service, batch_id="batch-531-n5")

    assert repo.save_calls == 2, "この壊れた実装は2件保存する(=重複防止が効いていない確認)"
