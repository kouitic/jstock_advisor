"""推奨の定点評価結果(要求仕様29〜36節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import model_validator

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import EvaluationLabel


class EvaluationResult(Entity):
    evaluation_id: str
    recommendation_id: str
    # 既存の営業日ベースホライズン(horizon_business_days)と、振り返り機能改修で
    # 追加したJST暦日ベースホライズン(horizon_calendar_days)は排他的であり、
    # 1レコードにつき必ずどちらか一方のみを設定する(_validate_horizon参照)。
    horizon_business_days: int | None = None
    horizon_calendar_days: int | None = None
    # evaluation_date: 評価基準日(ホライズンの到来日)。evaluated_at: 実際に処理が
    # 成功しこの結果が確定した日時。株価取得失敗等により両者はずれることがある
    # (振り返り機能改修で明確化。週次集計はevaluated_atを基準にする)。
    evaluated_at: dt.datetime
    evaluation_date: dt.date

    @model_validator(mode="after")
    def _validate_horizon(self) -> EvaluationResult:
        business = self.horizon_business_days is not None
        calendar = self.horizon_calendar_days is not None
        if business == calendar:
            raise ValueError(
                "horizon_business_daysとhorizon_calendar_daysはどちらか一方のみ設定してください"
            )
        return self

    price_at_evaluation: Decimal
    price_return_pct: float
    buy_price_based_return_pct: float | None = None

    total_return_amount: Decimal | None = None
    total_return_pct: float | None = None

    max_gain_pct: float | None = None  # 推奨後の最高値ベース
    max_drawdown_pct: float | None = None  # 推奨後の最安値ベース

    reached_tentative_buy_price: bool | None = None
    reached_standard_buy_price: bool | None = None
    reached_aggressive_buy_price: bool | None = None
    business_days_to_reach_price: int | None = None

    benchmark_symbol: str | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None

    evaluation_label: EvaluationLabel
    label_evidence: str
    notes: str | None = None

    # --- 判定精度向上機能(Phase A)で追加。既存recommendation_idベースの
    # 冪等性ロジック(exists_for_horizon/exists_for_calendar_horizon)とは独立した
    # 新しい軸。decision_idはDecisionSnapshotに紐づく評価のみ設定される
    # (recommendation_idベースの既存評価では常にNoneのまま)。 ---
    decision_id: str | None = None
    sector_benchmark_symbol: str | None = None
    sector_return_pct: float | None = None
    excess_return_vs_sector_pct: float | None = None
