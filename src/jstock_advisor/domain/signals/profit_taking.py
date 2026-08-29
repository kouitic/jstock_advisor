"""利確判定(要求仕様12節、2026-07仕様レビュー対応)。

含み益率・適正価格超過率・総合利回り低下を組み合わせて判定候補レベルを算出したうえで、
緩和要因(業績成長・増配継続・長期優待直前等)に応じて判定を弱める。上昇率だけで
機械的に売却判定を出さないよう、緩和要因は必ず考慮する。

価格フィールドは最終判定(final_action)に基づいて再構成する。格下げされた判定に
格下げ前の強い価格提案(即時執行価格・指値候補)が残らないようにする。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import IntEnum

from jstock_advisor.config.models import MitigatingFactors, ProfitTakingRulesConfig
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DividendComparisonOutcome,
    IndustryClassification,
    PriceBasisType,
    PriceFieldBasis,
    ProfitTakingIndustrySector,
    RecommendationType,
    SellIntensity,
    StockType,
    TimingAction,
    TrendClassification,
)
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.valuation import FairValueRange
from jstock_advisor.domain.signals.profit_protection import ProfitProtectionMetrics
from jstock_advisor.domain.valuation.fair_value import (
    compute_target_total_yield_price,
    compute_target_yield_price,
    round_yen,
)


class _Level(IntEnum):
    HOLD = 0
    WATCH = 1
    PARTIAL = 2
    FULL = 3


_LEVEL_TO_RECOMMENDATION = {
    _Level.HOLD: RecommendationType.HOLD,
    _Level.WATCH: RecommendationType.WATCH,
    _Level.PARTIAL: RecommendationType.PARTIAL_PROFIT_TAKE,
    _Level.FULL: RecommendationType.FULL_PROFIT_TAKE,
}


class _RawLevelOrigin(IntEnum):
    """raw_levelを実際に押し上げた根拠の種別(コードレビュー対応2026-08、
    上値余地の導入・mitigating/timing両層のsoftening制御)。

    数値が大きいほど優先順位が高い。同じraw_levelに複数の経路が同時に
    成立した場合、最も優先順位の高いoriginを採用する(softening制御の
    強さがoriginごとに異なるため、複数成立時の扱いを曖昧にしない)。
    """

    NONE = 0  # raw_level == HOLD
    OTHER_CONDITIONS = 1  # 非価格系の独立条件数のみで到達(現行どおり無制限softening)
    PRICE_POSITION = 2  # 含み益率×上値余地の基本マトリクス(強候補でないFULL/PARTIAL)
    # 上記に加え、ユーザー設定目標到達もPRICE_POSITION相当として扱う。
    # 利益保全(Profit Protection、2026-08追加): 高値からの含み益吐き出し(peak
    # gainのgiveback)を根拠とする強い一部利確候補。Fair Value confidenceに依存
    # しないため、FAIR_VALUE_STRONGとは独立した経路として扱う。PRICE_POSITIONと
    # 同様にorigin floor・ceiling-aware価格算出の対象とする(下記_CEILING_AWARE_
    # ORIGINS参照。ceiling_price自体は使わないが、PARTIAL時は現在値付近を
    # 執行目安とする点で同じ算出方針を共有する)。
    PROFIT_PROTECTION_STRONG = 3
    FAIR_VALUE_STRONG = 4  # 既存_fair_value_strong_condition/_fair_value_partial_gate_met
    # (適正価格ベースの強いゲート)。
    FUNDAMENTAL_CRITICAL_RISK = 5  # 投資前提崩壊・会計不祥事・確定減配+CF悪化(softening対象外)


def resolve_sell_intensity(
    origin: str, momentum: MomentumSnapshot | None
) -> SellIntensity:
    """PARTIAL_PROFIT_TAKE成立後の売却強度を、成立経路(origin)とトレンドから
    決定する(コードレビュー対応2026-08、指摘Part B)。「売るべきか」
    (RecommendationType/origin)とは独立して、「何株売るか」を少数の構造化
    条件のみから説明可能な形で決める(過剰な新スコアモデルは作らない)。

    - origin=PROFIT_PROTECTION_STRONG: 上値根拠に依存しない強い一部利確。
      さらに株価トレンドも悪化している(DOWNTREND/STRONG_DOWNTREND)場合は
      VERY_STRONGへ引き上げる(Strong単独では機械的に高比率にしない、
      要求仕様§B-7)。
    - origin=OTHER_CONDITIONS: 非価格系の複数条件のみで成立した、相対的に
      弱い根拠のPARTIAL。上値余地を多く残すためLIGHT。
    - それ以外(PRICE_POSITION/FAIR_VALUE_STRONG等): 従来からの「通常の
      PARTIAL」としてSTANDARD(既存の「概ね半分」に近い比率を踏襲)。
    """
    if origin == _RawLevelOrigin.PROFIT_PROTECTION_STRONG.name:
        aggravating_trend = momentum is not None and momentum.trend_classification in (
            TrendClassification.DOWNTREND,
            TrendClassification.STRONG_DOWNTREND,
        )
        return SellIntensity.VERY_STRONG if aggravating_trend else SellIntensity.STRONG
    if origin == _RawLevelOrigin.OTHER_CONDITIONS.name:
        return SellIntensity.LIGHT
    return SellIntensity.STANDARD


@dataclass(frozen=True)
class UnrealizedPnl:
    unrealized_pnl: Decimal
    unrealized_pnl_pct: float
    total_return_including_income: Decimal
    total_return_pct: float


@dataclass(frozen=True)
class MitigatingFactorInputs:
    """各緩和要因の該当有無。判定不能・未評価の場合はFalse扱いとする(判定を弱めない)。"""

    fair_value_rising_with_earnings_growth: bool = False
    # Issue #55 Phase B-2(N5)。`None` は「連続増配年数が不明(取得できていない)」、
    # `0` は「0年であることが確定している」を意味する。従来は供給側が `or 0` で
    # 両者を同一化しており、監査ログから事後に区別できなかった。
    # 判定上の扱いはdocstringの原則どおり不明はFalse相当(緩和要因に該当しない
    # =判定を弱めない)で、**従来の挙動と同じ**。区別するのは記録の粒度のみ。
    continuous_dividend_increase_years: int | None = None
    # Issue #30 Phase 1: DividendInfoの3状態化(bool | None)に伴う型整合のための
    # 最小限のinterface適応。None(未確認/UNKNOWN)はdocstringの原則どおり
    # False扱い(緩和要因に該当しない=判定を弱めない)。判定条件自体は不変。
    is_progressive_or_doe_policy: bool | None = False
    long_term_holding_benefit_imminent: bool = False
    few_reinvestment_alternatives: bool = False
    is_nisa_account: bool = False


@dataclass(frozen=True)
class ProfitTakingConditionInputs:
    """PARTIAL/FULL判定に使う複数条件の該当有無・関連値(要求仕様6節・8節・9節)。

    判定不能・未評価の項目はFalse/None扱いとし、捏造した根拠で強い判定を
    出さない(推測で補完しない原則)。
    """

    stock_types: list[StockType] = field(default_factory=list)
    fair_value_range: FairValueRange | None = None
    momentum: MomentumSnapshot | None = None
    dividend_comparison_outcome: DividendComparisonOutcome | None = None
    cashflow_fundamentally_driven: bool | None = None
    guidance_revision_disclosed: bool = False
    severe_earnings_decline: bool = False
    investment_premise_broken: bool = False
    accounting_or_scandal_or_delisting_risk: bool = False
    portfolio_concentration_over_limit: bool = False
    earnings_event_risk_reduction_rationale: bool = False
    profit_target_price: Decimal | None = None
    profit_target_rate: float | None = None
    # --- 利確判定レビュー対応で追加 ---
    # 適正価格算出に使った入力(EPS/BPS/実績配当等)が、直近の確定決算を反映しているか。
    # 判定できない場合はNone(判定不能をFalse=反映していない、と扱わない)。
    fair_value_reflects_latest_earnings: bool | None = None

    # --- 利確判定エンジン再レビュー対応(2026-07)で追加(要求仕様§3・§5・§7) ---
    # 業種別適正価格モデルが適用済みか(現行データソースでは恒久的にFalseとなる
    # ことが多い。適正価格単独での強い判定を許すゲートの1つ)。
    industry_model_applied: bool = False
    # profit_taking_industry.pyの区分(classify_profit_taking_industry_sector()の
    # 戻り値)。表示用途(_INDUSTRY_SECTOR_LABELS等)にのみ使う。
    industry_sector: ProfitTakingIndustrySector | None = None
    # 再コードレビュー対応(2026-08、指摘5): ceiling_price(fair_value_range.bull)を
    # 上値余地グリッドの主要根拠として使ってよいかの業種別ゲート
    # (_fair_value_action_usable())には、profit_taking_industry.py(銀行・
    # リース金融のみ識別)ではなく、既存のfinancial_industry.py(保険・証券等
    # 金融業全般をキーワードで識別し、未知の値は安全側にUNKNOWNとする三値分類)
    # のIndustryClassificationを使う。呼び出し元が渡さない場合はUNKNOWN相当として
    # 安全側に扱う。
    industry_classification: IndustryClassification | None = None
    # 保有株数・売買単位から一部売却が実行可能か。
    partial_sale_executable: bool = True
    # 次回決算までの営業日数(取得できない場合はNone)。
    days_to_next_earnings_business_days: int | None = None
    # 増益・増配等、利確判定に対する強い反対材料があるか。
    has_strong_counter_material: bool = False

    # --- 利益保全(Profit Protection)判定(2026-08追加、要求仕様§1〜§9) ---
    # peak_price_since_entry等から算出したcandidate/strongシグナル。Noneの場合は
    # 未算出(呼び出し側が算出しない、または算出不能)として扱い、Profit Protection
    # 軸は一切成立しない(捏造した根拠で判定を出さない原則)。
    profit_protection: ProfitProtectionMetrics | None = None
    # compute_profit_protection_metrics()呼び出しに使ったbasis_dateをそのまま
    # 転記する(2026-08、ATTENTION event identity用途)。判定ロジックには一切
    # 使わず、ProfitTakingResultへの伝播のみに用いる。
    profit_protection_basis_date: dt.date | None = None


@dataclass(frozen=True)
class ProfitTakingResult:
    recommendation_type: RecommendationType  # final_actionと同値(後方互換のため維持)
    fundamental_action: RecommendationType
    timing_action: TimingAction
    final_action: RecommendationType
    triggered_reasons: list[str]
    mitigating_factors_applied: list[str]
    hold_reasons: list[str]
    sell_prices: SellPriceLevels
    pnl: UnrealizedPnl
    # --- 利確判定レビュー対応で追加: 信頼度計算に必要な補助情報 ---
    independent_condition_count: int
    fair_value_used_as_sole_strong_basis: bool
    # --- 利確判定エンジン再レビュー対応(2026-07)で追加 ---
    # 現在株価が中立/強気適正価格をどれだけ超過(または下回る)しているか(%)。
    # 監視開始価格等の閾値ベースの価格ではなく、必ず実際の現在株価から算出する。
    current_price_vs_neutral_fair_value_pct: float | None
    current_price_vs_bull_fair_value_pct: float | None
    # --- コードレビュー対応(2026-08、上値余地の導入) ---
    # ceiling_price(fv_range.bull)を上値余地グリッドの主要根拠として使えたか。
    fair_value_action_usable: bool
    ceiling_price: Decimal | None
    upside_pct: float | None
    # 再コードレビュー対応(2026-08、指摘1): raw_levelを実際に押し上げた根拠の
    # 種別(_RawLevelOrigin.name)。呼び出し元(Recommendation)へ構造化フィールドと
    # して伝播し、通知直前の整合性検証(recommendation_consistency_validator.py)が
    # reasons文字列を解析せずに「価格マトリクス由来のFULL/PARTIALか」を判定できる
    # ようにする。raw_level==HOLDの場合は"NONE"。
    origin: str
    # --- 利益保全(Profit Protection)判定(2026-08追加、要求仕様§8: 判定理由の
    # 追跡可能性)。condition_inputs.profit_protectionと同一の値をそのまま転記する
    # (呼び出し側がProfitTakingResultだけを見て理由を再現できるようにする)。
    profit_protection_signal: str
    profit_protection_basis_date: dt.date | None
    profit_protection_peak_price: Decimal | None
    profit_protection_peak_date: dt.date | None
    profit_protection_peak_gain_pct: float | None
    profit_protection_current_gain_pct: float | None
    profit_protection_drawdown_from_peak_pct: float | None
    profit_protection_gain_giveback_ratio_pct: float | None
    # コードレビュー対応(2026-08、指摘2): DATA_INSUFFICIENT時の具体的理由
    # (株式分割・履歴不足等)を監査・原因調査用に永続化する。signal以外の
    # 場合(NONE/CANDIDATE/STRONG)は常にNone。
    profit_protection_insufficient_reason: str | None
    # コードレビュー対応(2026-08、指摘Part B): final_action==PARTIAL_PROFIT_TAKE
    # の場合のみ設定する(「何株売るか」の判定強度)。それ以外は常にNone
    # (WATCH/HOLD/FULLでは適用しない)。
    sell_intensity: SellIntensity | None


def compute_unrealized_pnl(
    current_price: Decimal,
    average_purchase_price: Decimal,
    shares: int,
    total_purchase_amount: Decimal,
    cumulative_dividend_received: Decimal,
    cumulative_benefit_value_received: Decimal,
) -> UnrealizedPnl:
    unrealized_pnl = (current_price - average_purchase_price) * shares
    unrealized_pnl_pct = (
        float(current_price / average_purchase_price - 1) * 100
        if average_purchase_price > 0
        else 0.0
    )
    total_return = unrealized_pnl + cumulative_dividend_received + cumulative_benefit_value_received
    total_return_pct = (
        float(total_return / total_purchase_amount * 100) if total_purchase_amount > 0 else 0.0
    )
    return UnrealizedPnl(
        unrealized_pnl=unrealized_pnl,
        unrealized_pnl_pct=unrealized_pnl_pct,
        total_return_including_income=total_return,
        total_return_pct=total_return_pct,
    )


def _level_from_gain(gain_pct: float, config: ProfitTakingRulesConfig) -> _Level:
    t = config.thresholds
    if gain_pct >= t.unrealized_gain_full_pct:
        return _Level.FULL
    if gain_pct >= t.unrealized_gain_partial_pct:
        return _Level.PARTIAL
    if gain_pct >= t.unrealized_gain_watch_pct:
        return _Level.WATCH
    return _Level.HOLD


def _level_from_fair_value_excess(
    current_price: Decimal,
    fv_range: FairValueRange | None,
    config: ProfitTakingRulesConfig,
) -> _Level:
    """適正価格ベースの候補水準(要求仕様§6: 強気適正価格を主軸とする)。

    中立適正価格以下なら懸念なし(HOLD)。中立超過〜強気適正価格以下はWATCH。
    強気適正価格をfair_value_excess_partial_pct(既定25%)以上超過でPARTIAL、
    fair_value_excess_full_pct(既定40%)以上超過でFULLの候補水準とする。
    ここでの「候補水準」は価格フィールド算出(_compute_sell_prices)専用の補助値であり、
    実際の判定レベル自体は複数条件・MEDIUM信頼度ゲート等を経て別途決定する。
    """
    if (
        fv_range is None
        or not fv_range.usable_for_trading_judgment
        or fv_range.neutral is None
        or fv_range.neutral <= 0
    ):
        return _Level.HOLD
    if current_price <= fv_range.neutral:
        return _Level.HOLD
    if fv_range.bull is None or fv_range.bull <= 0:
        return _Level.WATCH
    t = config.thresholds
    bull_excess_pct = float(current_price / fv_range.bull - 1) * 100
    if bull_excess_pct >= t.fair_value_excess_full_pct:
        return _Level.FULL
    if bull_excess_pct >= t.fair_value_excess_partial_pct:
        return _Level.PARTIAL
    return _Level.WATCH


def _fair_value_excess_pcts(
    current_price: Decimal, fv_range: FairValueRange | None
) -> tuple[float | None, float | None]:
    """現在株価が中立/強気適正価格をどれだけ超過しているか(%)。

    要求仕様§1: 監視開始価格等の閾値ベースの価格ではなく、必ず実際の現在株価を使う。
    """
    if fv_range is None or current_price <= 0:
        return None, None
    neutral_pct = (
        float(current_price / fv_range.neutral - 1) * 100
        if fv_range.neutral is not None and fv_range.neutral > 0
        else None
    )
    bull_pct = (
        float(current_price / fv_range.bull - 1) * 100
        if fv_range.bull is not None and fv_range.bull > 0
        else None
    )
    return neutral_pct, bull_pct


def _fair_value_action_usable(
    fv_range: FairValueRange | None,
    inputs: ProfitTakingConditionInputs,
    config: ProfitTakingRulesConfig,
) -> bool:
    """ceiling_price(fv_range.bull)を上値余地グリッド(_level_from_price_position）
    の主要根拠として使ってよいか(コードレビュー対応2026-08、再コードレビュー対応)。

    本関数の責務は「ceilingのデータ/モデルが売買判断に利用できる品質か」の
    判定に限定する(再コードレビュー対応、指摘4)。増配継続・増益等の
    「売らずに持つ合理性」(has_strong_counter_material)は、ceilingの利用
    可否ではなくmitigating layer(_apply_mitigating_factors)でのみ評価する。
    同一材料でceiling利用禁止とaction softeningの二重に効かせない。

    金融業(銀行・保険・証券・リース等)・業種不明(UNKNOWN、sector/industryが
    欠損・空文字等)は、一般事業会社向けのPER/PBR/配当利回りモデルの前提が
    成り立ちにくいため、業種別モデル適用済み(industry_model_applied=True)で
    ない限りceilingを使わない(再コードレビュー対応、指摘5: profit_taking_
    industry.py(銀行・リース金融のみ識別)ではなく、既存のfinancial_industry.py
    ベースのIndustryClassificationで判定する。金融業判定ロジックを二重実装
    しない)。GENERAL_CORPORATEと明確に判定できた場合のみ業種別モデル未対応でも
    汎用モデルの前提自体は成り立つとみなす(現行はindustry_model_appliedが
    全銘柄false固定のため、GENERAL_CORPORATEにまで必須にすると本判定が恒久的に
    不成立になる)。
    """
    if fv_range is None or not fv_range.usable_for_trading_judgment or fv_range.bull is None:
        return False
    if (
        inputs.industry_classification != IndustryClassification.GENERAL_CORPORATE
        and not inputs.industry_model_applied
    ):
        return False
    cbj = config.condition_based_judgment
    spread_ratio = (
        float(fv_range.bull / fv_range.bear)
        if fv_range.bear is not None and fv_range.bear > 0
        else None
    )
    return (
        len(fv_range.methods_used) >= cbj.min_fair_value_methods_for_partial
        and spread_ratio is not None
        and spread_ratio <= cbj.max_fair_value_spread_ratio_for_partial
        and inputs.fair_value_reflects_latest_earnings is True
        and (
            inputs.days_to_next_earnings_business_days is None
            or inputs.days_to_next_earnings_business_days
            >= cbj.min_business_days_to_earnings_for_fair_value_action
        )
    )


def _level_from_price_position(
    gain_pct: float,
    upside_pct: float | None,
    config: ProfitTakingRulesConfig,
) -> tuple[_Level, bool]:
    """含み益率×上値余地(ceiling_priceまでの距離)の基本アクションレベル
    (コードレビュー対応2026-08、要求仕様: 含み益率単独・適正価格超過率単独では
    利確判定を決めず、両者の組み合わせを判定の中心に据える)。

    戻り値の2つ目はFULL強候補(現在値がceilingを既に超過している)かどうか。
    upside_pct=None(ceiling_price利用不能、_fair_value_action_usable=False)の
    場合、含み益率単独ではPARTIAL/FULLへ到達させない(WATCHが上限)。
    """
    pp = config.price_position
    if gain_pct < pp.watch_gain_pct:
        return _Level.HOLD, False
    if upside_pct is None:
        return _Level.WATCH, False
    if upside_pct >= pp.partial_upside_max_pct:
        # 上値余地が大きい(まだ天井に近くない)場合はWATCH据え置き。
        return _Level.WATCH, False
    if gain_pct >= pp.full_gain_pct and upside_pct < pp.full_upside_max_pct:
        return _Level.FULL, upside_pct < pp.ceiling_exceeded_pct
    if gain_pct >= pp.partial_gain_pct:
        return _Level.PARTIAL, False
    return _Level.WATCH, False


def _apply_mitigating_factors(
    level: _Level,
    inputs: MitigatingFactorInputs,
    config: MitigatingFactors,
    downgrade_disabled: bool = False,
) -> tuple[_Level, list[str]]:
    """該当する緩和要因のdowngrade_levelsを合算し、判定レベルを弱める。

    downgrade_disabled=True(コードレビュー対応2026-08、origin=
    FUNDAMENTAL_CRITICAL_RISKの場合に呼び出し元が指定する)の場合、該当した
    緩和要因自体は引き続き記録・返却するが、実際の降格は行わない(投資前提
    崩壊等の重大リスク由来の判定を、緩和要因だけで打ち消させないため)。
    """
    applied: list[str] = []
    total_downgrade = 0

    if (
        config.fair_value_rising_with_earnings_growth.enabled
        and inputs.fair_value_rising_with_earnings_growth
    ):
        total_downgrade += config.fair_value_rising_with_earnings_growth.downgrade_levels
        # 2026-07仕様レビュー対応: 前回評価値との実比較データが無いため、「上昇している」
        # と断定せず、根拠(直近四半期の営業利益が非減少)から言える範囲まで弱めた表現に
        # とどめる(トリガー条件・downgrade_levelsの適用自体は変更しない)。
        applied.append("現在の利益水準を考慮すると、適正価格を一定程度支えている可能性があります")

    cdi = config.continuous_dividend_increase
    # Issue #55 Phase B-2(N6)。`min_consecutive_years` は enabled: true のとき
    # 1以上であることをconfigの起動時検証(MitigatingFactors)が保証する。
    # ここでは None を「無効化」へ黙って読み替えず、enabled と分けて扱う。
    # Issue #55 Phase B-2(N5)。年数不明(None)は緩和要因に該当させない
    # (従来 `or 0` で0年へ潰していたときと判定結果は同じ)。
    min_years = cdi.min_consecutive_years
    actual_years = inputs.continuous_dividend_increase_years
    if (
        cdi.enabled
        and min_years is not None
        and actual_years is not None
        and actual_years >= min_years
    ):
        total_downgrade += cdi.downgrade_levels
        # 「連続増配」は実績確定年数のみを指す(予想は含まない、要求仕様レビュー対応)。
        applied.append(f"実績で{inputs.continuous_dividend_increase_years}年連続増配している")

    if config.progressive_dividend_or_doe_policy.enabled and inputs.is_progressive_or_doe_policy:
        total_downgrade += config.progressive_dividend_or_doe_policy.downgrade_levels
        applied.append("累進配当またはDOE方針がある")

    if (
        config.long_term_holding_benefit_imminent.enabled
        and inputs.long_term_holding_benefit_imminent
    ):
        total_downgrade += config.long_term_holding_benefit_imminent.downgrade_levels
        applied.append("長期保有優待の条件達成が近い")

    if config.few_reinvestment_alternatives.enabled and inputs.few_reinvestment_alternatives:
        total_downgrade += config.few_reinvestment_alternatives.downgrade_levels
        applied.append("売却後に同等以上の再投資候補が少ない")

    if config.nisa_long_term_benefit.enabled and inputs.is_nisa_account:
        total_downgrade += config.nisa_long_term_benefit.downgrade_levels
        applied.append("NISA口座で長期保有メリットが大きい")

    if downgrade_disabled:
        total_downgrade = 0
    new_level = _Level(max(0, int(level) - total_downgrade))
    return new_level, applied


def _count_partial_conditions(
    current_total_yield_pct: float | None,
    inputs: ProfitTakingConditionInputs,
    config: ProfitTakingRulesConfig,
    weak_fair_value_forward_return_reason: str | None,
) -> tuple[int, list[str]]:
    """一部利確(PARTIAL)の根拠となる、価格系(含み益率・適正価格超過率)以外の
    独立条件を数える(要求仕様9節)。

    コードレビュー対応(2026-08、上値余地の導入): 含み益率・強気適正価格超過率の
    条件は_level_from_price_position()の2次元マトリクスへ統合したため、本関数
    からは削除した(二重計上防止)。
    """
    t = config.thresholds
    is_growth = StockType.GROWTH in inputs.stock_types
    reasons: list[str] = []

    if weak_fair_value_forward_return_reason is not None:
        # 中立適正価格基準の期待リターンが閾値以下だが、強い条件としての要件(手法数・
        # 手法間一致度・信頼度等)を満たさない場合は、PARTIALの根拠の1つとしてのみ数える
        # (要求仕様レビュー対応: 中立適正価格単独でFULLの強条件にしない)。
        reasons.append(weak_fair_value_forward_return_reason)

    # 成長株は業績予想の下方修正・急激な業績悪化があった場合のみ「成長鈍化」を条件化する
    # (要求仕様7節: GROWTHは配当利回り低下だけを利確理由にしない)。
    if is_growth and (inputs.guidance_revision_disclosed or inputs.severe_earnings_decline):
        reasons.append("成長鈍化または業績予想の下方修正の可能性")

    if inputs.momentum is not None and inputs.momentum.trend_classification in (
        TrendClassification.DOWNTREND,
        TrendClassification.STRONG_DOWNTREND,
    ):
        reasons.append("株価トレンドが悪化")

    # GROWTHは配当・優待利回り低下を利確条件に含めない(要求仕様7節)。
    if (
        not is_growth
        and current_total_yield_pct is not None
        and current_total_yield_pct < t.total_yield_caution_pct
    ):
        reasons.append(f"総合利回りが{current_total_yield_pct:.2f}%まで低下")

    if inputs.portfolio_concentration_over_limit:
        reasons.append("ポートフォリオ内の保有比率が上限を超過")

    if inputs.earnings_event_risk_reduction_rationale:
        reasons.append("決算イベントに備えたリスク低減の合理性")

    pp = inputs.profit_protection
    if pp is not None and pp.candidate_signal:
        assert pp.peak_gain_pct is not None
        assert pp.current_gain_pct is not None
        assert pp.drawdown_from_peak_pct is not None
        assert pp.gain_giveback_ratio_pct is not None
        reasons.append(
            f"最大含み益{pp.peak_gain_pct:.1f}%から現在{pp.current_gain_pct:.1f}%まで低下し、"
            f"高値から{pp.drawdown_from_peak_pct:.1f}%下落、"
            f"最大含み益の{pp.gain_giveback_ratio_pct:.1f}%を吐き出した(利益保全シグナル)"
        )

    return len(reasons), reasons


def _extra_action_gates_met(
    inputs: ProfitTakingConditionInputs,
    min_earnings_business_days: int,
) -> bool:
    """適正価格ベースの強い判定(PARTIAL/FULL)に共通で要求する追加ゲート
    (要求仕様§5)。実行可能性・タイミング・反対材料の観点から、適正価格の
    数値条件だけでは強い判定を出さない。

    含み益率の基準(§5「含み益率が一部利確基準以上」)はMEDIUM信頼度専用の
    _fair_value_partial_gate_metのみで課す。HIGH信頼度の強いFULL条件
    (_fair_value_strong_condition)は、含み益がわずかでも適正価格が著しく
    乖離していれば成立するという既存の設計を維持するため、ここには含めない。
    """
    return (
        inputs.industry_model_applied
        and inputs.days_to_next_earnings_business_days is not None
        and inputs.days_to_next_earnings_business_days >= min_earnings_business_days
        and inputs.partial_sale_executable
        and not inputs.has_strong_counter_material
    )


def _fair_value_partial_gate_met(
    current_price: Decimal,
    pnl: UnrealizedPnl,
    inputs: ProfitTakingConditionInputs,
    config: ProfitTakingRulesConfig,
) -> tuple[bool, float | None]:
    """MEDIUM信頼度でも適正価格(強気基準)ベースでPARTIAL相当を許可するための
    厳格ゲート(要求仕様§5)。列挙された条件をすべて満たす場合にのみTrueを返す。
    満たさない場合はWATCHへ格下げする(呼び出し側の責務)。
    """
    fv_range = inputs.fair_value_range
    if fv_range is None or not fv_range.usable_for_trading_judgment:
        return False, None
    if fv_range.bull is None or fv_range.bull <= 0:
        return False, None
    bull_excess_pct = float(current_price / fv_range.bull - 1) * 100
    t = config.thresholds
    cbj = config.condition_based_judgment
    if bull_excess_pct < t.fair_value_excess_partial_pct:
        return False, bull_excess_pct

    spread_ratio = (
        float(fv_range.bull / fv_range.bear)
        if fv_range.bear is not None and fv_range.bear > 0
        else None
    )
    gate_ok = (
        inputs.fair_value_reflects_latest_earnings is True
        and len(fv_range.methods_used) >= cbj.min_fair_value_methods_for_partial
        and spread_ratio is not None
        and spread_ratio <= cbj.max_fair_value_spread_ratio_for_partial
        # 要求仕様§5「含み益率が一部利確基準以上」: MEDIUM信頼度専用ゲートでのみ課す。
        and pnl.unrealized_pnl_pct >= t.unrealized_gain_partial_pct
        and _extra_action_gates_met(
            inputs, cbj.min_business_days_to_earnings_for_fair_value_action
        )
    )
    return gate_ok, bull_excess_pct


def _fair_value_strong_condition(
    current_price: Decimal,
    pnl: UnrealizedPnl,
    inputs: ProfitTakingConditionInputs,
    config: ProfitTakingRulesConfig,
) -> tuple[str | None, str | None]:
    """適正価格基準の強いFULL条件を評価する(要求仕様レビュー対応・§5)。

    中立適正価格基準の期待リターンが閾値以下であっても、それだけではFULLの強い
    条件にしない。以下をすべて満たす場合にのみ強い条件(1件目の戻り値)として扱う:
    - usable_for_trading_judgment=True
    - overall_confidence=HIGH
    - 現在値がbull(強気)適正価格を一定率超過
    - 有効手法数が設定件数以上
    - 手法間乖離(bull/bear)が設定値以下
    - 業績予想の鈍化または下方修正が確認されている
    - 適正価格入力値が最新決算を反映している
    - 業種別適正価格モデル適用済み・決算まで一定営業日以上・一部売却実行可能・
      強い反対材料がない(要求仕様§5の追加ゲート。含み益率の基準はMEDIUM信頼度専用の
      _fair_value_partial_gate_metのみで課し、含み益がわずかでも適正価格の乖離だけで
      成立するというHIGH信頼度側の既存設計は維持する)

    要件を満たさない場合は、2件目の戻り値としてPARTIAL候補用の弱い理由文を返す
    (中立適正価格ベースの期待リターンが閾値以下、という観測自体は無かったことに
    しない)。
    """
    fv_range = inputs.fair_value_range
    if (
        fv_range is None
        or not fv_range.usable_for_trading_judgment
        or fv_range.neutral is None
        or fv_range.neutral <= 0
        or current_price <= 0
    ):
        return None, None

    forward_return_pct = float(fv_range.neutral / current_price - 1) * 100
    cbj = config.condition_based_judgment
    if forward_return_pct > cbj.forward_return_inferior_threshold_pct:
        return None, None

    weak_reason = (
        f"適正価格基準の期待リターンが{forward_return_pct:.1f}%と、保有継続の合理性が低い(参考水準)"
    )

    method_count = len(fv_range.methods_used)
    spread_ratio = (
        float(fv_range.bull / fv_range.bear)
        if fv_range.bear is not None and fv_range.bull is not None and fv_range.bear > 0
        else None
    )
    bull_excess_ok = fv_range.bull is not None and current_price > fv_range.bull * (
        1 + Decimal(str(cbj.bull_excess_margin_pct_for_full)) / 100
    )
    strong_ok = (
        fv_range.overall_confidence == ConfidenceLevel.HIGH
        and bull_excess_ok
        and method_count >= cbj.min_fair_value_methods_for_full
        and spread_ratio is not None
        and spread_ratio <= cbj.max_fair_value_spread_ratio_for_full
        and (inputs.guidance_revision_disclosed or inputs.severe_earnings_decline)
        and inputs.fair_value_reflects_latest_earnings is True
        and _extra_action_gates_met(
            inputs, cbj.min_business_days_to_earnings_for_fair_value_action
        )
    )
    if strong_ok:
        return (
            f"適正価格基準の期待リターンが{forward_return_pct:.1f}%であり、"
            "手法間の一致度・強気適正価格超過・業績予想の鈍化を含め、保有継続の"
            "合理性が低いと複数条件で確認できる",
            weak_reason,
        )
    return None, weak_reason


def _full_strong_conditions(
    current_price: Decimal,
    pnl: UnrealizedPnl,
    inputs: ProfitTakingConditionInputs,
    config: ProfitTakingRulesConfig,
    fair_value_strong_reason: str | None,
) -> list[tuple[str, _RawLevelOrigin]]:
    """全株利確(FULL)を単独で正当化できる強い条件(要求仕様9節)。

    「含み益率が高い」というだけの条件はここに含めない(gain単独でFULLに
    到達させないという要求仕様9節の明示的な制約)。

    コードレビュー対応(2026-08、mitigating/timing両層のsoftening制御): 各理由に
    _RawLevelOriginを付与して返す。投資前提崩壊・会計不祥事・確定減配+CF悪化は
    FUNDAMENTAL_CRITICAL_RISK(softening対象外)、適正価格ベースの強条件は
    FAIR_VALUE_STRONG、ユーザー設定目標到達は価格系トリガーのためPRICE_POSITION
    として扱う。
    """
    reasons: list[tuple[str, _RawLevelOrigin]] = []
    is_income = StockType.INCOME in inputs.stock_types

    if inputs.investment_premise_broken:
        reasons.append(("投資前提が明確に崩れた", _RawLevelOrigin.FUNDAMENTAL_CRITICAL_RISK))

    if inputs.accounting_or_scandal_or_delisting_risk:
        reasons.append(
            ("会計・不祥事・上場維持リスクが発生", _RawLevelOrigin.FUNDAMENTAL_CRITICAL_RISK)
        )

    if (
        is_income
        and inputs.dividend_comparison_outcome == DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT
        and inputs.cashflow_fundamentally_driven is True
    ):
        reasons.append(
            (
                "配当投資銘柄で確定的な減配とフリーキャッシュフロー悪化が重なった",
                _RawLevelOrigin.FUNDAMENTAL_CRITICAL_RISK,
            )
        )

    if fair_value_strong_reason is not None:
        reasons.append((fair_value_strong_reason, _RawLevelOrigin.FAIR_VALUE_STRONG))

    if inputs.profit_target_price is not None and current_price >= inputs.profit_target_price:
        reasons.append(
            (
                f"ユーザー設定の全利確目標価格({inputs.profit_target_price}円)に到達",
                _RawLevelOrigin.PRICE_POSITION,
            )
        )
    elif (
        inputs.profit_target_rate is not None
        and pnl.unrealized_pnl_pct >= inputs.profit_target_rate
    ):
        reasons.append(
            (
                f"ユーザー設定の全利確目標利回り({inputs.profit_target_rate}%)に到達",
                _RawLevelOrigin.PRICE_POSITION,
            )
        )

    return reasons


def _count_full_moderate_conditions(
    current_total_yield_pct: float | None,
    inputs: ProfitTakingConditionInputs,
    config: ProfitTakingRulesConfig,
) -> tuple[int, list[str]]:
    """全株利確(FULL)を、複数該当した場合にのみ正当化する中程度の条件
    (価格系(含み益率・適正価格超過率)以外)。

    コードレビュー対応(2026-08、上値余地の導入): 含み益率・強気適正価格超過率の
    条件は_level_from_price_position()の2次元マトリクスへ統合したため、本関数
    からは削除した(二重計上防止)。
    """
    t = config.thresholds
    is_growth = StockType.GROWTH in inputs.stock_types
    reasons: list[str] = []

    if (
        not is_growth
        and current_total_yield_pct is not None
        and current_total_yield_pct < t.total_yield_strong_caution_pct
    ):
        reasons.append(f"総合利回りが{current_total_yield_pct:.2f}%まで大幅低下")

    if inputs.momentum is not None and inputs.momentum.trend_classification == (
        TrendClassification.STRONG_DOWNTREND
    ):
        reasons.append("株価トレンドが強く悪化")

    if is_growth and inputs.guidance_revision_disclosed and inputs.severe_earnings_decline:
        reasons.append("業績予想の下方修正と深刻な業績悪化が重なった")

    return len(reasons), reasons


def _wrap(
    price: Decimal | None,
    rationale: str,
    basis: PriceFieldBasis = PriceFieldBasis.TARGET_PRICE,
    basis_type: PriceBasisType | None = None,
    price_low: Decimal | None = None,
    price_high: Decimal | None = None,
) -> PriceWithRationale | None:
    """算出不能(price is None)の場合はNoneのまま返す(現在値へのフォールバックは行わない、
    要求仕様11節)。"""
    if not price:
        return None
    return PriceWithRationale(
        price=price,
        rationale=rationale,
        basis=basis,
        basis_type=basis_type,
        price_low=price_low,
        price_high=price_high,
    )


def _tick_size(price: Decimal) -> Decimal:
    """東証の呼値の簡易近似(要求仕様レビュー対応)。

    実際の呼値は価格帯に応じてさらに細かく区分されるが、ここでは代表的な区分の
    簡易近似を用いる(小型・低流動性銘柄で1円単位の精密な指値を避けることが目的
    であり、正確な公式呼値テーブルの再現ではないことに留意)。
    """
    p = float(price)
    if p <= 3000:
        return Decimal("1")
    if p <= 5000:
        return Decimal("5")
    if p <= 30000:
        return Decimal("10")
    if p <= 50000:
        return Decimal("50")
    if p <= 300000:
        return Decimal("100")
    return Decimal("1000")


def _round_to_tick(price: Decimal) -> Decimal:
    tick = _tick_size(price)
    return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def _price_range(price: Decimal, width_pct: float = 1.5) -> tuple[Decimal, Decimal]:
    """指値のレンジ表示用に、呼値へ丸めた上下限を返す(要求仕様レビュー対応)。

    出来高・ATR等の市場マイクロ構造データは現時点で本判定エンジンへ渡されて
    いないため、簡易的に価格の±width_pct%を呼値丸めしたレンジとする(真の
    ボラティリティ・呼値ベースのレンジではない簡易近似)。
    """
    low = _round_to_tick(price * Decimal(str(1 - width_pct / 100)))
    high = _round_to_tick(price * Decimal(str(1 + width_pct / 100)))
    return low, high


_CEILING_AWARE_ORIGINS = frozenset(
    {
        _RawLevelOrigin.PRICE_POSITION,
        _RawLevelOrigin.FAIR_VALUE_STRONG,
        _RawLevelOrigin.FUNDAMENTAL_CRITICAL_RISK,
        # PROFIT_PROTECTION_STRONGはfinal_level==PARTIALにしか到達しないため、
        # _ceiling_aware_sell_prices()のFULL分岐(ceiling_price使用)は通らない。
        # PARTIAL分岐は現在値のみを使い、ceiling_price(=None)の有無に影響されない。
        _RawLevelOrigin.PROFIT_PROTECTION_STRONG,
    }
)


def _ceiling_aware_sell_prices(
    current_price: Decimal,
    ceiling_price: Decimal | None,
    final_level: _Level,
) -> SellPriceLevels:
    """PRICE_POSITION/FAIR_VALUE_STRONG/FUNDAMENTAL_CRITICAL_RISK origin向けの
    価格フィールド算出(再コードレビュー対応2026-08、指摘2)。

    これらのoriginは「現在値がceiling_price(上限価格の想定)にどれだけ近いか」を
    判定の主要根拠とするため、価格フィールドも同じ基準に揃える。旧来の含み益率・
    強気適正価格超過率ベースの固定閾値(unrealized_gain_full_pct=50%、
    fair_value_excess_full_pct=40%等)は、ceiling_priceを大きく超える未来の
    参考値になりうるため、これらのorigin向けの目安価格には使わない
    (過去互換の内部参考値としては_compute_sell_prices側のlevel_gain/level_fv
    ベースの旧算出を他originで引き続き使う)。

    - PARTIAL: 判定は既に成立しているため、現在値付近の実行可能な指値を
      recommended_limit_priceとする(ceiling_price>現在値である限り、
      構造的にceiling_price以下になる)。
    - FULL: 判定が既に成立しているため、現在値付近を即時執行目安
      (immediate_execution_price)として優先する。全株利確検討価格
      (full_profit_consideration_price)はceiling_priceが現在値を上回る場合
      のみそれを使い、ceiling_priceより上へは設定しない。
    """
    if final_level == _Level.PARTIAL:
        partial_field = _wrap(
            current_price,
            "含み益率と上限価格(想定ceiling)までの上値余地から一部利確水準に"
            "到達していると判断。現在値付近での指値・執行を検討する目安",
            basis=PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE,
        )
        return SellPriceLevels(
            partial_profit_start_price=partial_field,
            recommended_limit_price=partial_field,
        )

    # FULL: 上限価格が現在値を上回っている場合のみそれを全株利確検討価格の
    # 参考上限として使い、それ以外(ceiling_price未使用またはceiling_price<=
    # 現在値=既に超過)は現在値そのものを使う(未来の遠い参考値を使わない)。
    full_take = (
        ceiling_price
        if ceiling_price is not None and ceiling_price > current_price
        else current_price
    )
    full_field = _wrap(
        full_take,
        "含み益率と上限価格(想定ceiling)までの上値余地から全株利確水準に"
        "到達していると判断した参考水準(上限価格を上回る未来の参考値は使わない)",
        basis_type=PriceBasisType.FAIR_VALUE_THRESHOLD if ceiling_price is not None else None,
    )
    immediate_field = _wrap(
        current_price,
        "全株利確条件が既に成立しているため、現在値付近での執行を検討する目安",
        basis=PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE,
    )
    return SellPriceLevels(
        full_profit_consideration_price=full_field,
        immediate_execution_price=immediate_field,
    )


def _compute_sell_prices(
    current_price: Decimal,
    average_purchase_price: Decimal,
    fair_value_range: FairValueRange | None,
    forecast_annual_dividend_per_share: Decimal | None,
    annual_benefit_value_at_min_lot: Decimal | None,
    benefit_min_shares_required: int | None,
    is_benefit_eligible: bool,
    level_gain: _Level,
    level_fv: _Level,
    final_level: _Level,
    origin: _RawLevelOrigin,
    ceiling_price: Decimal | None,
    config: ProfitTakingRulesConfig,
) -> SellPriceLevels:
    """final_action(格下げ後の最終判定)に基づいて価格フィールドを再構成する
    (要求仕様レビュー対応)。

    - HOLD: 価格提案なし
    - WATCH: 割高判定開始価格(監視専用、即時売却を意味しない)と将来の再評価条件のみ。
      recommended_limit_price・immediate_execution_priceは常にNone。
    - PARTIAL_PROFIT_TAKE: 一部利確開始価格・推奨指値候補(レンジ付き)。
    - FULL_PROFIT_TAKE: 即時または全株利確検討価格を表示可能。

    旧実装は判定レベル算出前のraw水準(level_gain/level_fv)だけを使っており、
    緩和要因・タイミング層による格下げ後もその水準の価格が残る矛盾があった。

    再コードレビュー対応(2026-08、指摘2): origin(PRICE_POSITION/FAIR_VALUE_STRONG/
    FUNDAMENTAL_CRITICAL_RISK)がceiling_priceを判定の主要根拠とする場合、
    PARTIAL/FULLの価格フィールドは_ceiling_aware_sell_prices()へ委譲し、
    ceiling_priceを超える・現在値から大きく乖離した未来の参考値を提示しない。
    """
    if final_level == _Level.HOLD:
        return SellPriceLevels()

    if final_level in (_Level.PARTIAL, _Level.FULL) and origin in _CEILING_AWARE_ORIGINS:
        return _ceiling_aware_sell_prices(current_price, ceiling_price, final_level)

    t = config.thresholds
    # コードレビュー対応(2026-08、LINE通知/監査分離): usable_for_trading_judgment=False
    # (適正価格を判定に使えないと明示的にマークされた状態)の場合、判定ロジック側
    # (level_fv算出)は既にこのフラグを見て割高判定を行わないようにしているが、
    # ここで無条件にfair_value_range.bullを使うと、判定には使わないと決めた適正
    # 価格をユーザー向け目安価格にだけ使ってしまう矛盾が生じる。取得単価ベースの
    # 候補(gain_partial_price/gain_full_price)はfv_bullに依存しないため、この
    # ゲートにより自動的に生き残る(Fair Value使用不能 ≠ 全目安価格が算定不能)。
    fv_bull = (
        fair_value_range.bull
        if fair_value_range is not None
        and fair_value_range.usable_for_trading_judgment
        and fair_value_range.bull is not None
        else None
    )

    gain_partial_price = round_yen(
        average_purchase_price * (1 + Decimal(str(t.unrealized_gain_partial_pct)) / 100)
    )
    gain_full_price = round_yen(
        average_purchase_price * (1 + Decimal(str(t.unrealized_gain_full_pct)) / 100)
    )

    # 一部/全部利確の価格候補は、要求仕様§6により強気適正価格を主軸として算出する
    # (中立適正価格ベースの旧算出は割高判定の起点としてのみ使う)。
    fv_partial_price = (
        round_yen(fv_bull * (1 + Decimal(str(t.fair_value_excess_partial_pct)) / 100))
        if fv_bull is not None
        else None
    )
    fv_full_price = (
        round_yen(fv_bull * (1 + Decimal(str(t.fair_value_excess_full_pct)) / 100))
        if fv_bull is not None
        else None
    )

    # 割高判定開始価格/一部利確開始価格: 含み益・適正価格いずれか早く到達する方
    # (=より緩い基準)。WATCHの場合は「監視専用」であることをbasisで明示し、
    # 「一部利確開始価格」という表示は通知層で行わない。
    partial_candidates = [p for p in (gain_partial_price, fv_partial_price) if p is not None]
    partial_start = min(partial_candidates) if partial_candidates else None
    partial_basis_type = (
        PriceBasisType.FAIR_VALUE_THRESHOLD
        if fv_partial_price is not None and partial_start == fv_partial_price
        else PriceBasisType.PURCHASE_PRICE_RETURN_TARGET
    )

    if final_level == _Level.WATCH:
        partial_field = _wrap(
            partial_start,
            f"含み益{t.unrealized_gain_partial_pct}%到達、または強気適正価格超過"
            f"{t.fair_value_excess_partial_pct}%到達の早い方(監視開始水準。"
            "即時売却を意味しない)",
            basis=PriceFieldBasis.MONITORING_ONLY_NOT_A_SELL_TARGET,
            basis_type=partial_basis_type,
        )
        return SellPriceLevels(partial_profit_start_price=partial_field)

    # PARTIAL / FULL: 実際に「利確検討」水準へ到達させた軸だけを、指値候補の根拠に使う。
    recommended_candidates: list[tuple[Decimal, PriceBasisType]] = []
    if level_gain >= _Level.PARTIAL:
        recommended_candidates.append(
            (gain_full_price, PriceBasisType.PURCHASE_PRICE_RETURN_TARGET)
        )
    if level_fv >= _Level.PARTIAL and fv_full_price is not None:
        recommended_candidates.append((fv_full_price, PriceBasisType.FAIR_VALUE_THRESHOLD))
    recommended, recommended_basis_type = (
        min(recommended_candidates, key=lambda x: x[0]) if recommended_candidates else (None, None)
    )
    recommended_basis = PriceFieldBasis.TARGET_PRICE
    recommended_low, recommended_high = None, None
    if recommended is not None:
        if recommended <= current_price:
            recommended_basis = PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE
        else:
            recommended_low, recommended_high = _price_range(recommended)

    partial_field = _wrap(
        partial_start,
        f"含み益{t.unrealized_gain_partial_pct}%到達、または強気適正価格超過"
        f"{t.fair_value_excess_partial_pct}%到達の早い方",
        basis=(
            PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE
            if partial_start is not None and partial_start <= current_price
            else PriceFieldBasis.TARGET_PRICE
        ),
        basis_type=partial_basis_type,
    )
    recommended_field = _wrap(
        recommended,
        "利確検討水準に実際に到達した軸(含み益・強気適正価格超過)から算出した指値候補レンジ。"
        "総合利回り低下のみが根拠の場合は具体的な指値を算出しない",
        basis=recommended_basis,
        basis_type=recommended_basis_type,
        price_low=recommended_low,
        price_high=recommended_high,
    )

    if final_level == _Level.PARTIAL:
        return SellPriceLevels(
            partial_profit_start_price=partial_field,
            recommended_limit_price=recommended_field,
        )

    # FULL_PROFIT_TAKE: 即時または全株利確検討価格を表示する。
    full_candidates: list[tuple[Decimal, PriceBasisType]] = [
        (p, basis_type)
        for p, basis_type in (
            (gain_full_price, PriceBasisType.PURCHASE_PRICE_RETURN_TARGET),
            (fv_full_price, PriceBasisType.FAIR_VALUE_THRESHOLD),
        )
        if p is not None
    ]
    full_take, full_take_basis_type = (
        max(full_candidates, key=lambda x: x[0]) if full_candidates else (None, None)
    )
    full_field = _wrap(
        full_take,
        f"含み益{t.unrealized_gain_full_pct}%かつ強気適正価格超過{t.fair_value_excess_full_pct}%の"
        "両方を満たす、より強い確信が持てる参考水準(現在値を上回っていても矛盾ではない)",
        basis_type=full_take_basis_type,
    )
    immediate_field = None
    if full_take is not None and full_take <= current_price:
        immediate_field = _wrap(
            current_price,
            "全株利確条件が既に成立しているため、現在値付近での執行を検討する目安",
            basis=PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE,
            basis_type=full_take_basis_type,
        )

    # 総合利回り再評価価格: 優待対象銘柄では優待価値を含めて計算する(要求仕様レビュー対応)。
    # 優待対象なのに優待価値が取得できない場合は算出不能(None)とする。
    reevaluation_upside = None
    reevaluation_basis_type = None
    if is_benefit_eligible:
        if (
            annual_benefit_value_at_min_lot is not None
            and benefit_min_shares_required is not None
            and benefit_min_shares_required > 0
            and forecast_annual_dividend_per_share is not None
        ):
            reevaluation_upside = compute_target_total_yield_price(
                forecast_annual_dividend_per_share,
                annual_benefit_value_at_min_lot,
                benefit_min_shares_required,
                t.total_yield_strong_caution_pct,
            )
            reevaluation_basis_type = PriceBasisType.TOTAL_YIELD_THRESHOLD
    else:
        reevaluation_upside = compute_target_yield_price(
            forecast_annual_dividend_per_share, t.total_yield_strong_caution_pct
        )
        reevaluation_basis_type = PriceBasisType.DIVIDEND_YIELD_THRESHOLD

    reevaluation_upside = (
        round_yen(reevaluation_upside) if reevaluation_upside is not None else None
    )
    # 「上昇時の再評価価格」は定義上、現在値より高い水準でなければ意味をなさない
    # (既に現在値がこの水準を下回って計算される=とうに通過済み、という場合は
    # 「上昇時」の名にそぐわないため算出不能扱いとする。現在値へのフォールバックは行わない)。
    if reevaluation_upside is not None and reevaluation_upside <= current_price:
        reevaluation_upside = None

    reevaluation_label = (
        "総合利回り(配当+株主優待)が"
        if reevaluation_basis_type == PriceBasisType.TOTAL_YIELD_THRESHOLD
        else "配当利回りが"
    )
    reevaluation_field = _wrap(
        reevaluation_upside,
        f"{reevaluation_label}{t.total_yield_strong_caution_pct}%まで低下する水準"
        "(上昇時の再評価目安)",
        basis_type=reevaluation_basis_type,
    )

    return SellPriceLevels(
        partial_profit_start_price=partial_field,
        recommended_limit_price=recommended_field,
        full_profit_consideration_price=full_field,
        reevaluation_price_upside=reevaluation_field,
        immediate_execution_price=immediate_field,
    )


def evaluate_profit_taking(
    current_price: Decimal,
    average_purchase_price: Decimal,
    shares: int,
    total_purchase_amount: Decimal,
    cumulative_dividend_received: Decimal,
    cumulative_benefit_value_received: Decimal,
    current_total_yield_pct: float | None,
    forecast_annual_dividend_per_share: Decimal | None,
    mitigating_inputs: MitigatingFactorInputs,
    config: ProfitTakingRulesConfig,
    condition_inputs: ProfitTakingConditionInputs | None = None,
    annual_benefit_value_at_min_lot: Decimal | None = None,
    benefit_min_shares_required: int | None = None,
    is_benefit_eligible: bool = False,
) -> ProfitTakingResult:
    """利確判定(要求仕様6節・7節・8節・9節・10節)。

    含み益率・適正価格超過率単独ではPARTIAL/FULLへ到達できない設計とする
    (複数の独立条件が該当した場合のみ、または強い条件が1つ該当した場合のみ
    到達する)。ファンダメンタル評価(fundamental_action)とタイミング評価
    (timing_action)を分離し、上昇トレンドはfundamental_actionを最大1段階
    までしか緩和できない(final_action)。
    """
    condition_inputs = condition_inputs or ProfitTakingConditionInputs()
    pnl = compute_unrealized_pnl(
        current_price,
        average_purchase_price,
        shares,
        total_purchase_amount,
        cumulative_dividend_received,
        cumulative_benefit_value_received,
    )

    fv_range = condition_inputs.fair_value_range
    neutral_excess_pct, bull_excess_pct = _fair_value_excess_pcts(current_price, fv_range)

    # 「利確」は含み益があって初めて成立する概念のため、含み損の状態では
    # 適正価格超過・総合利回り低下・その他の条件による判定は考慮しない
    # (株価下落そのものによる売却判断はsell_signal側の投資前提悪化判定の担当)。
    has_unrealized_gain = pnl.unrealized_pnl_pct > 0

    # level_gain/level_fvは価格フィールド算出(_compute_sell_prices)専用の補助値であり、
    # 判定レベル自体(raw_level)は下記の複数条件方式で別途決定する。含み損の場合は
    # 適正価格超過だけで指値候補が出てしまわないよう、level_fvもHOLDに固定する。
    level_gain = _level_from_gain(pnl.unrealized_pnl_pct, config)
    level_fv = (
        _level_from_fair_value_excess(current_price, fv_range, config)
        if has_unrealized_gain
        else _Level.HOLD
    )

    # コードレビュー対応(2026-08、上値余地の導入): ceiling_price(fv_range.bull)を
    # 上値余地グリッドの主要根拠として使えるかを判定し、使える場合のみupside_pctを
    # 算出する。含み損の場合は「利確」自体が成立しないためHOLD/Noneに固定する。
    fair_value_action_usable = (
        _fair_value_action_usable(fv_range, condition_inputs, config)
        if has_unrealized_gain
        else False
    )
    ceiling_price = (
        fv_range.bull if fair_value_action_usable and fv_range is not None else None
    )
    upside_pct = (
        float(ceiling_price / current_price - 1) * 100
        if ceiling_price is not None and current_price > 0
        else None
    )
    price_level, price_full_strong = (
        _level_from_price_position(pnl.unrealized_pnl_pct, upside_pct, config)
        if has_unrealized_gain
        else (_Level.HOLD, False)
    )

    fair_value_used_as_sole_strong_basis = False
    if has_unrealized_gain:
        fv_strong_reason, fv_weak_reason = _fair_value_strong_condition(
            current_price, pnl, condition_inputs, config
        )
        fv_partial_gate_ok, _ = _fair_value_partial_gate_met(
            current_price, pnl, condition_inputs, config
        )
        partial_count, partial_reasons = _count_partial_conditions(
            current_total_yield_pct,
            condition_inputs,
            config,
            fv_weak_reason,
        )
        full_strong_reasons = _full_strong_conditions(
            current_price, pnl, condition_inputs, config, fv_strong_reason
        )
        full_moderate_count, full_moderate_reasons = _count_full_moderate_conditions(
            current_total_yield_pct, condition_inputs, config
        )
    else:
        fv_strong_reason = None
        fv_partial_gate_ok = False
        partial_count, partial_reasons = 0, []
        full_strong_reasons = []
        full_moderate_count, full_moderate_reasons = 0, []

    cbj = config.condition_based_judgment

    # コードレビュー対応(2026-08): raw_levelは「価格系(_level_from_price_position)」と
    # 「非価格系の独立条件数」の2つの独立した経路から到達しうる候補(level, origin, reasons)
    # のリストを作り、最大レベル→最大優先度originの順で採用する(複数経路が同時に成立
    # した場合の扱いを曖昧にしない、レビュー対応)。
    candidates: list[tuple[_Level, _RawLevelOrigin, list[str]]] = []

    if full_strong_reasons:
        best_full_strong_origin = max(origin for _, origin in full_strong_reasons)
        candidates.append(
            (_Level.FULL, best_full_strong_origin, [r for r, _ in full_strong_reasons])
        )
        fair_value_used_as_sole_strong_basis = (
            len(full_strong_reasons) == 1 and full_strong_reasons[0][0] == fv_strong_reason
        )

    if full_moderate_count >= cbj.min_moderate_conditions_for_full:
        candidates.append(
            (_Level.FULL, _RawLevelOrigin.OTHER_CONDITIONS, full_moderate_reasons)
        )

    if price_level == _Level.FULL:
        assert upside_pct is not None
        ceiling_note = (
            "現在値が上限価格(想定ceiling)を上回っており"
            if price_full_strong
            else "上限価格(想定ceiling)までの上値余地がほぼ無く"
        )
        candidates.append(
            (
                _Level.FULL,
                _RawLevelOrigin.PRICE_POSITION,
                [
                    f"含み益率{pnl.unrealized_pnl_pct:.1f}%かつ{ceiling_note}"
                    f"(上値余地{upside_pct:.1f}%)、全株利確水準に到達"
                ],
            )
        )

    # コードレビュー対応(2026-08、PARTIAL数量欠落不具合): 以下2経路は
    # profit_protection strong経路(_extra_action_gates_met/1300行目付近)と
    # 同様にcondition_inputs.partial_sale_executableをゲートする。保有株数が
    # 売買単位以下でodd_lot_trading_availableもFalseの場合、これらの経路から
    # _Level.PARTIALへ到達するとProfitTakingService側でsuggested_sell_shares
    # を算出できず(trading_unit_feasibility.py参照)、「一部売却」なのに
    # 売却株数が確定しない通知になってしまうため。ゲートによりPARTIAL候補
    # 自体を成立させず、WATCH側の独立した到達条件(下記watch_reasons)へ
    # 自然にフォールバックする。
    if (
        partial_count >= cbj.min_conditions_for_partial or fv_partial_gate_ok
    ) and condition_inputs.partial_sale_executable:
        partial_reasons_with_gate = list(partial_reasons)
        origin = _RawLevelOrigin.OTHER_CONDITIONS
        if fv_partial_gate_ok and bull_excess_pct is not None:
            origin = _RawLevelOrigin.FAIR_VALUE_STRONG
            gate_reason = (
                f"強気適正価格を{bull_excess_pct:.1f}%超過しており、業種別モデル適用・"
                "決算までの余裕・一部売却の実行可能性・反対材料の不在を含め複数条件で確認できる"
            )
            if gate_reason not in partial_reasons_with_gate:
                partial_reasons_with_gate.append(gate_reason)
        candidates.append((_Level.PARTIAL, origin, partial_reasons_with_gate))

    if price_level == _Level.PARTIAL and condition_inputs.partial_sale_executable:
        assert upside_pct is not None
        candidates.append(
            (
                _Level.PARTIAL,
                _RawLevelOrigin.PRICE_POSITION,
                [
                    f"含み益率{pnl.unrealized_pnl_pct:.1f}%かつ上値余地{upside_pct:.1f}%、"
                    "一部利確水準に到達"
                ],
            )
        )

    # 利益保全(Profit Protection、2026-08追加)のstrong条件(要求仕様§3B)。
    # Fair Value confidenceに一切依存せず、単独でPARTIAL候補を成立させる。
    # 業種別モデル・決算までの余裕日数等、適正価格ベースのゲート(_extra_action_
    # gates_met)は課さない(適正価格を根拠にしないため)。一部売却が実行可能な
    # 場合のみ成立させる(実行不能な推奨を出さないため)。
    profit_protection = condition_inputs.profit_protection
    if (
        profit_protection is not None
        and profit_protection.strong_signal
        and condition_inputs.partial_sale_executable
    ):
        assert profit_protection.peak_gain_pct is not None
        assert profit_protection.current_gain_pct is not None
        assert profit_protection.drawdown_from_peak_pct is not None
        assert profit_protection.gain_giveback_ratio_pct is not None
        candidates.append(
            (
                _Level.PARTIAL,
                _RawLevelOrigin.PROFIT_PROTECTION_STRONG,
                [
                    f"最大含み益{profit_protection.peak_gain_pct:.1f}%から現在"
                    f"{profit_protection.current_gain_pct:.1f}%まで低下。高値から"
                    f"{profit_protection.drawdown_from_peak_pct:.1f}%下落し、"
                    f"最大含み益の{profit_protection.gain_giveback_ratio_pct:.1f}%を"
                    "吐き出したため、Strong Profit Protection条件に該当し、一部利確を推奨"
                ],
            )
        )

    # WATCHの起点(要求仕様§6): 強気適正価格の超過閾値には届かない、または中立適正価格を
    # わずかに上回るのみの場合でも、監視開始としては扱う。PARTIALへの到達に必要な独立
    # 条件数(partial_count)には数えない(gain単独でのPARTIAL誤到達を防ぐ)。含み損の場合は
    # 「利確」自体が成立しないため、has_unrealized_gainを必ず条件に含める。
    watch_reasons = list(partial_reasons)
    watch_origin = _RawLevelOrigin.OTHER_CONDITIONS
    if price_level == _Level.WATCH and upside_pct is not None:
        watch_reasons.append(
            f"含み益率{pnl.unrealized_pnl_pct:.1f}%・上値余地{upside_pct:.1f}%で監視水準"
        )
        watch_origin = _RawLevelOrigin.PRICE_POSITION
    watch_gain_threshold = config.thresholds.unrealized_gain_watch_pct
    if (
        partial_count >= 1
        or (has_unrealized_gain and pnl.unrealized_pnl_pct >= watch_gain_threshold)
        or (has_unrealized_gain and neutral_excess_pct is not None and neutral_excess_pct > 0)
        or price_level == _Level.WATCH
    ):
        if not watch_reasons:
            if has_unrealized_gain and neutral_excess_pct is not None and neutral_excess_pct > 0:
                watch_reasons.append(f"中立適正価格を{neutral_excess_pct:.1f}%上回る")
            else:
                watch_reasons.append(f"含み益率{pnl.unrealized_pnl_pct:.1f}%が監視水準に到達")
        candidates.append((_Level.WATCH, watch_origin, watch_reasons))

    if candidates:
        raw_level = max(level for level, _, _ in candidates)
        top_candidates = [c for c in candidates if c[0] == raw_level]
        origin = max(o for _, o, _ in top_candidates)
        triggered_reasons: list[str] = []
        for _, _, reasons in top_candidates:
            for r in reasons:
                if r not in triggered_reasons:
                    triggered_reasons.append(r)
    else:
        raw_level = _Level.HOLD
        origin = _RawLevelOrigin.NONE
        triggered_reasons = []

    if raw_level == _Level.HOLD:
        fundamental_level = _Level.HOLD
        applied_factors: list[str] = []
        hold_reasons = ["利確シグナルに該当する条件がない"]
    else:
        # コードレビュー対応(2026-08): origin=FUNDAMENTAL_CRITICAL_RISK(投資前提崩壊・
        # 会計不祥事等)は、緩和要因だけで判定を打ち消させない(降格を無効化する)。
        fundamental_level, applied_factors = _apply_mitigating_factors(
            raw_level,
            mitigating_inputs,
            config.mitigating_factors,
            downgrade_disabled=(origin == _RawLevelOrigin.FUNDAMENTAL_CRITICAL_RISK),
        )
        # 何らかの利確シグナルが実際に発生している場合、緩和要因によってもHOLD(無評価)まで
        # 完全に打ち消すのではなく、最低でもWATCH(監視継続)として可視化する。
        if fundamental_level == _Level.HOLD:
            fundamental_level = _Level.WATCH
        # 再コードレビュー対応(2026-08、指摘6): 最終floor(下記)をfinal_levelにのみ
        # 適用すると、fundamental_action(mitigating適用後)がタイミング層による降格の
        # 影響を受けずfinal_actionより弱く見える(fundamental_action=WATCH・
        # final_action=PARTIALのように、最終判定がfundamentalより強く見える)矛盾が
        # 生じうる。origin別floorはmitigating層適用直後のfundamental_levelにも同じ
        # 基準で適用し、各層の意味を一貫させる(このあとのtiming層はfloor済みの
        # fundamental_levelを起点に計算される)。
        if origin in (
            _RawLevelOrigin.PRICE_POSITION,
            _RawLevelOrigin.FAIR_VALUE_STRONG,
            _RawLevelOrigin.PROFIT_PROTECTION_STRONG,
        ) and (raw_level >= _Level.PARTIAL):
            fundamental_level = _Level(max(int(fundamental_level), int(_Level.PARTIAL)))
        hold_reasons = list(applied_factors)

    # タイミング層(要求仕様9節・10節): ファンダメンタル評価とは独立した軸として算出する。
    # 上昇トレンドはfundamental_actionを最大1段階までしか緩和できず、適正価格レンジ上限を
    # 明確に超過している(usable かつ 信頼度がLOWでない)場合は緩和自体を禁止する
    # (上昇トレンドだけを理由に割高評価そのものを無効化しない)。
    timing_action = TimingAction.NEUTRAL
    final_level = fundamental_level
    momentum = condition_inputs.momentum
    if momentum is not None:
        trend = momentum.trend_classification
        if trend in (TrendClassification.STRONG_UPTREND, TrendClassification.UPTREND):
            timing_action = TimingAction.WAIT_UPTREND_CONTINUES
            # コードレビュー対応(2026-08): origin=FUNDAMENTAL_CRITICAL_RISKはmitigating層と
            # 同様、タイミング層による降格も適用しない(重大リスク由来の判定を上昇トレンド
            # だけで打ち消させない)。
            if origin != _RawLevelOrigin.FUNDAMENTAL_CRITICAL_RISK:
                margin = config.condition_based_judgment.timing_downgrade_block_margin_pct
                fv_range = condition_inputs.fair_value_range
                hard_overvalued = (
                    fv_range is not None
                    and fv_range.usable_for_trading_judgment
                    and fv_range.overall_confidence != ConfidenceLevel.LOW
                    and fv_range.bull is not None
                    and current_price > fv_range.bull * (1 + Decimal(str(margin)) / 100)
                )
                if fundamental_level > _Level.HOLD and not hard_overvalued:
                    final_level = _Level(max(0, int(fundamental_level) - 1))
        elif trend in (TrendClassification.STRONG_DOWNTREND, TrendClassification.DOWNTREND):
            timing_action = TimingAction.ACCELERATE_DOWNTREND_CONFIRMED
        else:
            timing_action = TimingAction.PROCEED_NO_TIMING_SIGNAL

    # コードレビュー対応(2026-08): origin=PRICE_POSITION/FAIR_VALUE_STRONGでraw_levelが
    # PARTIAL以上の場合、mitigating+timing両層を通した合計softeningでもPARTIAL未満へは
    # 落とさない(最終floor)。FUNDAMENTAL_CRITICAL_RISKは上記の両層で降格自体を無効化
    # 済みのため、ここでは対象外(raw_level == final_levelが既に保証されている)。
    if origin in (
        _RawLevelOrigin.PRICE_POSITION,
        _RawLevelOrigin.FAIR_VALUE_STRONG,
        _RawLevelOrigin.PROFIT_PROTECTION_STRONG,
    ) and (raw_level >= _Level.PARTIAL):
        final_level = _Level(max(int(final_level), int(_Level.PARTIAL)))

    fundamental_action = _LEVEL_TO_RECOMMENDATION[fundamental_level]
    final_action = _LEVEL_TO_RECOMMENDATION[final_level]

    sell_prices = _compute_sell_prices(
        current_price,
        average_purchase_price,
        fv_range,
        forecast_annual_dividend_per_share,
        annual_benefit_value_at_min_lot,
        benefit_min_shares_required,
        is_benefit_eligible,
        level_gain,
        level_fv,
        final_level,
        origin,
        ceiling_price,
        config,
    )
    if (
        final_level != _Level.HOLD
        and momentum is not None
        and momentum.trailing_stop_reference_price is not None
    ):
        sell_prices = sell_prices.model_copy(
            update={
                "trailing_stop_reference_price": _wrap(
                    momentum.trailing_stop_reference_price,
                    "直近高値からのトレーリングストップ参考水準(モメンタム層算出)",
                    basis_type=PriceBasisType.TECHNICAL_PRICE_LEVEL,
                )
            }
        )

    return ProfitTakingResult(
        recommendation_type=final_action,
        fundamental_action=fundamental_action,
        timing_action=timing_action,
        final_action=final_action,
        triggered_reasons=triggered_reasons,
        mitigating_factors_applied=applied_factors,
        hold_reasons=hold_reasons,
        sell_prices=sell_prices,
        pnl=pnl,
        independent_condition_count=max(
            partial_count, full_moderate_count, len(full_strong_reasons)
        ),
        fair_value_used_as_sole_strong_basis=fair_value_used_as_sole_strong_basis,
        current_price_vs_neutral_fair_value_pct=neutral_excess_pct,
        current_price_vs_bull_fair_value_pct=bull_excess_pct,
        fair_value_action_usable=fair_value_action_usable,
        ceiling_price=ceiling_price,
        upside_pct=upside_pct,
        origin=origin.name,
        profit_protection_signal=(
            profit_protection.signal_label if profit_protection is not None else "NONE"
        ),
        profit_protection_basis_date=(
            condition_inputs.profit_protection_basis_date if profit_protection is not None else None
        ),
        profit_protection_peak_price=(
            profit_protection.peak_price_since_entry if profit_protection is not None else None
        ),
        profit_protection_peak_date=(
            profit_protection.peak_date if profit_protection is not None else None
        ),
        profit_protection_peak_gain_pct=(
            profit_protection.peak_gain_pct if profit_protection is not None else None
        ),
        profit_protection_current_gain_pct=(
            profit_protection.current_gain_pct if profit_protection is not None else None
        ),
        profit_protection_drawdown_from_peak_pct=(
            profit_protection.drawdown_from_peak_pct if profit_protection is not None else None
        ),
        profit_protection_gain_giveback_ratio_pct=(
            profit_protection.gain_giveback_ratio_pct if profit_protection is not None else None
        ),
        profit_protection_insufficient_reason=(
            profit_protection.insufficient_data_reason if profit_protection is not None else None
        ),
        sell_intensity=(
            resolve_sell_intensity(origin.name, momentum)
            if final_level == _Level.PARTIAL
            else None
        ),
    )
