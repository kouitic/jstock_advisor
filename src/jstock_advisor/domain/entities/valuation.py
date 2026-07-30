"""適正価格の複数手法化(要求仕様8節)。

単一の適正価格を絶対値として扱わず、複数手法の結果をレンジ(弱気/中立/強気)と
して保持する。各手法は算出できなかった場合、捏造せずexclusion_reasonを残す。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import ConfidenceLevel


class ValuationExclusionReason(ImmutableSnapshot):
    """適正価格の算出方式が集計から除外された理由の構造化記録
    (BUYパイプライン第2次修正(2026-07)で追加。要求仕様10節)。

    「除外した」という事実だけでなく、どの条件でどの基準値に対して
    外れ値と判定したかを監査可能な形で残す。
    """

    code: str
    message: str
    actual_value: Decimal | float | None = None
    reference_value: Decimal | float | None = None


class FairValueMethodResult(ImmutableSnapshot):
    method: str  # "target_yield" | "per" | "pbr" | "historical_range" | "dcf"
    fair_value: Decimal | None
    input_values: dict[str, str] = {}
    input_dates: dict[str, dt.date] = {}
    assumptions: dict[str, str] = {}
    confidence: ConfidenceLevel
    exclusion_reason: str | None = None

    # --- BUYパイプライン再設計(2026-07)で追加。算出できたことと、その結果を
    # 適正価格集計に採用してよいことは別(要求仕様9節・10節)。不適切な前提
    # (EPS負数・分割未調整・特別配当の恒常化等)の場合はapplicable=Falseとし、
    # exclusion_reasonへ理由を残したうえで集計(min/max/median/mean)から除外する ---
    applicable: bool = True
    source_date: dt.date | None = None

    # --- BUYパイプライン第2次修正(2026-07)で追加。要求仕様10節: 下方外れ値
    # (現在値の10%未満・他方式中央値の40%未満等)の構造化記録。DCFの単年度
    # キャッシュフロー歪み等により、上方乖離フィルタでは検出できない異常に
    # 低い算出値を検出する ---
    exclusion_detail: ValuationExclusionReason | None = None


class FairValueRange(ImmutableSnapshot):
    bear: Decimal | None
    neutral: Decimal | None
    bull: Decimal | None
    overall_confidence: ConfidenceLevel
    methods_used: list[FairValueMethodResult]
    methods_excluded: list[FairValueMethodResult]
    usable_for_trading_judgment: bool
    unusable_reason: str | None = None

    # --- BUYパイプライン再設計(2026-07)で追加。単一の「最終適正価格」ではなく
    # 手法間のバラつきを扱えるようにする(要求仕様9節)。SELL側の
    # usable_for_trading_judgmentはそのまま維持し、これらは追加情報として扱う ---
    valuation_min: Decimal | None = None
    valuation_max: Decimal | None = None
    valuation_median: Decimal | None = None
    valuation_mean: Decimal | None = None
    valuation_dispersion_ratio: float | None = None  # = valuation_max / valuation_min
    methods_used_count: int | None = None

    # --- BUYパイプライン第2次修正(2026-07)で追加。要求仕様9節: 通知に表示する
    # 「購入判断に実際に使用したレンジ」(下方外れ値除外後)と、監査用の
    # 「全手法参考値」を分離する。valuation_min/maxは下方外れ値除外後の値
    # (=購入判断用)を指すよう意味を変更し、decision_valuation_min/maxは
    # そのエイリアスとして通知層が明示的に参照できるようにする ---
    decision_valuation_min: Decimal | None = None
    decision_valuation_max: Decimal | None = None
