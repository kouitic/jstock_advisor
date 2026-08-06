"""HoldingDecisionService.evaluate()が保有固有情報を使用しないことの回帰テスト
(コードレビュー対応)。

backtest/compareが非保有銘柄でも新方式のみを安全に評価できる、という前提
(placeholder_holding参照)を、実際の挙動で保証する。全フィールド一致では
なく、スコアに関連するフィールドのみを明示的に比較する(将来の正当な
メタデータ追加でテストが壊れやすくならないようにするため)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import AccountType, ExecutionPlanReason
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle

_CFG = load_config()
_NOW = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)
_PROVIDERS = build_mock_provider_bundle(_NOW)

# スコア判定に関わるフィールドのみを比較する(evaluated_at/evaluation_duration_ms/
# holding_decision_result_id/recommendation_id/runtime_config_versionは非決定的
# または本テストの関心事ではないため除外)。
_COMPARED_FIELDS = (
    "company_quality",
    "investment_thesis",
    "risk_deduction",
    "final_score",
    "category",
    "should_notify",
    "coverage",
    "hard_gate",
    "positive_reasons",
    "negative_reasons",
)


def _holding(
    *,
    shares: int,
    average_purchase_price: Decimal,
    total_purchase_amount: Decimal,
    first_purchase_date: dt.date,
    last_purchase_date: dt.date,
    account_type: AccountType,
) -> Holding:
    return Holding(
        stock_code="2914",
        stock_name="x",
        shares=shares,
        average_purchase_price=average_purchase_price,
        total_purchase_amount=total_purchase_amount,
        first_purchase_date=first_purchase_date,
        last_purchase_date=last_purchase_date,
        account_type=account_type,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_holding_decision_service_ignores_holding_specific_fields() -> None:
    """shares/取得単価/取得日/口座種別を極端に変えても、スコア関連フィールドは
    完全に一致する(HoldingDecisionService.evaluate()がholding.stock_codeのみを
    読むことの直接的な回帰確認)。"""
    service = HoldingDecisionService(_PROVIDERS, _CFG)

    holding_a = _holding(
        shares=1,
        average_purchase_price=Decimal("0.01"),
        total_purchase_amount=Decimal("0.01"),
        first_purchase_date=dt.date(2000, 1, 1),
        last_purchase_date=dt.date(2000, 1, 1),
        account_type=AccountType.NISA,
    )
    holding_b = _holding(
        shares=999_999,
        average_purchase_price=Decimal("99999"),
        total_purchase_amount=Decimal("99999900001"),
        first_purchase_date=dt.date(2026, 8, 5),
        last_purchase_date=dt.date(2026, 8, 5),
        account_type=AccountType.GENERAL,
    )

    outcome_a = service.evaluate(holding_a, _NOW, ExecutionPlanReason.NORMAL_SHADOW)
    outcome_b = service.evaluate(holding_b, _NOW, ExecutionPlanReason.NORMAL_SHADOW)

    assert outcome_a.result is not None
    assert outcome_b.result is not None
    for field in _COMPARED_FIELDS:
        assert getattr(outcome_a.result, field) == getattr(outcome_b.result, field), field
