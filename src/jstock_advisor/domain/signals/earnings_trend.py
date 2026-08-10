"""判定精度向上機能Phase C: Earnings Trend Score v3(業績トレンドスコア)。

Earnings Surprise Score(earnings_surprise.py)とは独立した評価軸。実装前
調査の結果、営業利益トレンド・営業CFトレンド・配当方向の3要素を中心に
構成する(売上トレンド・EPSトレンド・利益率改善・会社予想方向は現行
Providerでは算出できないため対象外。domain/entities/earnings_trend.py参照)。

look-ahead bias防止: Earnings Surprise Scoreと同様、最新決算が確定反映
されたかどうかはこの関数では判定せず、呼び出し側が
`EarningsReleaseConfirmationState`を解決したうえで渡す。決算予定日を経過
していながら財務データへの反映が未確認の場合、NOT_APPLICABLEを返す。

コードレビュー対応(v3): 上記のNOT_APPLICABLE判定に、既存の
`EarningsDecisionRelevance`(resolve_earnings_decision_relevance()、
domain/signals/earnings_window.py参照、ProfitTakingが既に使用している
仕組み)を追加で組み合わせるようにした(Earnings Surprise Scoreと同じ
理由、earnings_surprise.pyのモジュールdocstring参照)。あわせて、成分算出に
使った値がどの期間のものかをlist[Decimal]の裸の系列ではなく
FinancialPeriodValue(value/period_end/period_typeが対応した構造)で
受け取るようにし、period_end/period_typeを監査情報として保持する(index
依存で値と期間の対応が曖昧になることを避けるため)。

コードレビュー対応(第3回): NOT_APPLICABLE判定条件はrelease_confirmation_
state/earnings_decision_relevanceの組み合わせで決まるため、Result側には
既にearnings_decision_relevanceのみ保持していたのをrelease_confirmation_
stateも合わせて保持するようにした(EarningsSurpriseResultと同じ2値
セット)。判定条件・スコア算出式自体の変更は無く、model_versionは
据え置き(earnings_trend_v3)。

コードレビュー対応(v2): 変化率計算`(latest-previous)/abs(previous)*100`は
previousが負(赤字・マイナスCF)の場合でも改善/悪化の方向を正しく評価する
(旧式`latest/previous-1`はpreviousが負の場合に符号が逆転する不具合が
あった)。previous=0は0除算となるため、パーセントを介さずlatestの符号で
直接評価する。acceleration成分は、隣接する2区間のいずれかで黒字/赤字の
符号跨ぎが起きている場合、2階差分が比較可能な意味を持たないため評価不能
とする。また、FinancialSummary.recent_periods_source(四半期実績由来か
年次フォールバック由来か)をconfidenceへ反映する(ANNUAL_FALLBACKは
MEDIUM上限、UNAVAILABLEは財務トレンド系成分を強制的に評価不能とする)。

外部I/Oを一切行わない純関数(domain/signals/timing_score.pyと同じパターン)。

コードレビュー対応(Shadow計測): この評価結果はDecisionSnapshotへ記録する
専用のものであり、BUY候補判定・保有判断スコア・旧売却判定・ProfitTaking
判定・LINE通知など既存の判定ロジックからは一切参照されない。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import EarningsTrendRulesConfig
from jstock_advisor.domain.entities.earnings_trend import EarningsTrendResult
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DividendComparisonOutcome,
    EarningsDecisionRelevance,
    EarningsReleaseConfirmationState,
    EarningsTrendCategory,
    EarningsTrendEvaluationState,
    PeriodType,
    RecentPeriodsSource,
)
from jstock_advisor.domain.financial_series import FinancialPeriodValue
from jstock_advisor.domain.jst import require_timezone_aware

REASON_AWAITING_EARNINGS_CONFIRMATION = "AWAITING_EARNINGS_CONFIRMATION"
REASON_OPERATING_INCOME_TREND_UNAVAILABLE = "OPERATING_INCOME_TREND_UNAVAILABLE"
REASON_OPERATING_CASHFLOW_TREND_UNAVAILABLE = "OPERATING_CASHFLOW_TREND_UNAVAILABLE"
REASON_DIVIDEND_DIRECTION_UNAVAILABLE = "DIVIDEND_DIRECTION_UNAVAILABLE"
REASON_ACCELERATION_UNAVAILABLE = "ACCELERATION_UNAVAILABLE"
# コードレビュー対応(v2): 四半期実績ではなく年次決算へフォールバックした
# データを使った場合に付与する(confidence上限キャップと対になる)。
REASON_ANNUAL_FALLBACK_USED = "ANNUAL_FALLBACK_USED"

_AWAITING_STATES = (
    EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
    EarningsReleaseConfirmationState.DELAYED,
)

_DIVIDEND_SCORE_MAP_KEYS = (
    DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT,
    DividendComparisonOutcome.FORECAST_DIVIDEND_CUT,
    DividendComparisonOutcome.DIVIDEND_MAINTAINED,
    DividendComparisonOutcome.DIVIDEND_INCREASE,
)


@dataclass(frozen=True)
class _TrendComponentResult:
    """_trend_component()の戻り値。値と、その値がどの期間のものかを
    (period_end/period_type)を組にして保持する(コードレビュー対応v3)。"""

    score: float | None
    previous_value: Decimal | None
    latest_value: Decimal | None
    change_pct: float | None
    previous_period_end: dt.date | None
    latest_period_end: dt.date | None
    period_type: PeriodType | None


@dataclass(frozen=True)
class _AccelerationResult:
    """_acceleration_component()の戻り値。period_endsは(prev2, prev1, curr)
    の3点の期末日(直近の2区間比較に使った3四半期分)。"""

    score: float | None
    raw_delta2: float | None
    period_ends: tuple[dt.date, dt.date, dt.date] | None


def _clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _is_sign_crossing(previous: Decimal, latest: Decimal) -> bool:
    """黒字/赤字(またはCFプラス/マイナス)の符号が跨いだかどうか。0は
    非負側として扱う(previous>=0 and latest<0、previous<0 and latest>=0の
    いずれか)。"""
    return (previous < 0) != (latest < 0)


def _change_pct(previous: Decimal, latest: Decimal) -> float | None:
    """符号付き変化率(%)。

    コードレビュー対応(v2): `(latest - previous) / abs(previous) * 100`と
    いう共通の式を使うことで、previousが負(赤字・マイナスCF)の場合でも
    改善/悪化の方向が逆転しない(旧式の`latest / previous - 1`はpreviousが
    負の場合に符号が反転する不具合があった)。黒字転換
    (previous<0<=latest)・赤字転落(previous>=0>latest)もこの式のまま
    自然に大きな正/負の値となり、追加の分岐なしで強い改善/悪化として
    評価される。previous=0は0除算となるためNoneを返す(呼び出し側で
    符号に応じた明示的な評価に切り替える)。
    """
    if previous == 0:
        return None
    return float((latest - previous) / abs(previous)) * 100.0


def _banded_score(change_pct: float, config: EarningsTrendRulesConfig) -> float:
    if change_pct >= config.trend_strong_improve_pct:
        return 100.0
    if change_pct >= config.trend_improve_pct:
        return 50.0
    if change_pct > config.trend_decline_pct:
        return 0.0
    if change_pct > config.trend_strong_decline_pct:
        return -50.0
    return -100.0


def _trend_component(
    series: list[FinancialPeriodValue], config: EarningsTrendRulesConfig
) -> _TrendComponentResult:
    """seriesは時系列昇順(最後が最新)を前提とする(domain/financial_series.py
    のbuild_financial_period_series()と同じ規約)。直近期の前期比変化率を
    段階評価する。

    コードレビュー対応(v3): 裸のlist[Decimal]ではなくFinancialPeriodValue
    の系列を受け取ることで、valueとperiod_end/period_typeの対応をindexに
    依存せず直接保持する。

    previous=0の場合(コードレビュー対応v2)、0除算となるパーセント計算は
    行わず、latestの符号に応じて明示的に評価する(推測による極端な変化率を
    作らない)。0へ到達した後latest>0なら改善方向、latest<0なら悪化方向と
    し、_banded_score()と同じ固定の離散値(改善/悪化の中位=50.0/-50.0)を
    使う(起点が0のため変化の大きさは不明であり、最上位の100.0/-100.0では
    なく中位に留める)。
    """
    if len(series) < 2:
        return _TrendComponentResult(None, None, None, None, None, None, None)
    previous_point, latest_point = series[-2], series[-1]
    previous, latest = previous_point.value, latest_point.value
    period_type = latest_point.period_type
    previous_period_end = previous_point.period_end
    latest_period_end = latest_point.period_end
    if previous == 0:
        if latest == 0:
            score = 0.0
        elif latest > 0:
            score = 50.0
        else:
            score = -50.0
        return _TrendComponentResult(
            score, previous, latest, None, previous_period_end, latest_period_end, period_type
        )
    change_pct = _change_pct(previous, latest)
    if change_pct is None:
        return _TrendComponentResult(
            None, previous, latest, None, previous_period_end, latest_period_end, period_type
        )
    return _TrendComponentResult(
        _banded_score(change_pct, config),
        previous,
        latest,
        change_pct,
        previous_period_end,
        latest_period_end,
        period_type,
    )


def _acceleration_component(
    series: list[FinancialPeriodValue], config: EarningsTrendRulesConfig
) -> _AccelerationResult:
    """直近の前期比変化率と、その1つ前の前期比変化率の差(2階差分)を評価する。
    最低3四半期分のデータが必要(データが薄いため補助成分として扱う)。

    コードレビュー対応(v2): 隣接する2区間(t-2→t-1、t-1→t)のいずれかで
    黒字/赤字(またはCFプラス/マイナス)の符号跨ぎが起きている場合、2階差分は
    比較可能な意味を持たない(_change_pct()の分母基準が区間ごとに大きく
    異なりうる)ため、無理に2階差分を作らずNoneを返す。

    コードレビュー対応(v3): 3四半期分のperiod_end(prev2, prev1, curr)を
    period_endsとして保持する(スコアが算出不能な場合も、少なくとも
    「どの3四半期を比較しようとしたか」は監査可能にする)。
    """
    if len(series) < 3:
        return _AccelerationResult(None, None, None)
    p2, p1, p0 = series[-3], series[-2], series[-1]
    prev2, prev1, curr = p2.value, p1.value, p0.value
    period_ends = (p2.period_end, p1.period_end, p0.period_end)
    if prev2 == 0 or prev1 == 0:
        return _AccelerationResult(None, None, period_ends)
    if _is_sign_crossing(prev2, prev1) or _is_sign_crossing(prev1, curr):
        return _AccelerationResult(None, None, period_ends)
    change_previous = _change_pct(prev2, prev1)
    change_latest = _change_pct(prev1, curr)
    if change_previous is None or change_latest is None:
        return _AccelerationResult(None, None, period_ends)
    delta2 = change_latest - change_previous
    return _AccelerationResult(
        _clamp(delta2 / config.acceleration_full_scale_pct * 100.0), delta2, period_ends
    )


def _dividend_component(
    outcome: DividendComparisonOutcome | None, config: EarningsTrendRulesConfig
) -> float | None:
    if outcome is None:
        return None
    mapping = dict(
        zip(
            _DIVIDEND_SCORE_MAP_KEYS,
            (
                config.dividend_actual_cut_score,
                config.dividend_forecast_cut_score,
                config.dividend_maintained_score,
                config.dividend_increase_score,
            ),
            strict=True,
        )
    )
    return mapping.get(outcome)


def _classify_category(score: float, config: EarningsTrendRulesConfig) -> EarningsTrendCategory:
    t = config.category_thresholds
    if score >= t.strong_improving:
        return EarningsTrendCategory.STRONG_IMPROVING
    if score >= t.improving:
        return EarningsTrendCategory.IMPROVING
    if score <= t.strong_deteriorating:
        return EarningsTrendCategory.STRONG_DETERIORATING
    if score <= t.deteriorating:
        return EarningsTrendCategory.DETERIORATING
    return EarningsTrendCategory.STABLE


def evaluate_earnings_trend(
    quarterly_operating_incomes: list[FinancialPeriodValue],
    quarterly_operating_cashflows: list[FinancialPeriodValue],
    dividend_comparison_outcome: DividendComparisonOutcome | None,
    recent_periods_source: RecentPeriodsSource,
    release_confirmation_state: EarningsReleaseConfirmationState,
    decision_relevance: EarningsDecisionRelevance,
    evaluated_at: dt.datetime,
    config: EarningsTrendRulesConfig,
) -> EarningsTrendResult:
    """営業利益トレンド・営業CFトレンド・配当方向・(補助的な)accelerationの
    加重平均でEarnings Trend Scoreを算出する。

    quarterly_operating_incomes/quarterly_operating_cashflowsは季節調整済み
    (TTM)系列をFinancialPeriodValueとして渡すこと(StockSnapshot.
    quarterly_operating_income_periods/quarterly_operating_cashflow_periods、
    時系列昇順。コードレビュー対応v3: 値とperiod_end/period_typeの対応を
    indexに依存させないため、裸のlist[Decimal]ではなくFinancialPeriodValue
    を受け取る)。

    recent_periods_sourceはFinancialSummary.recent_periods_sourceを渡す
    こと(コードレビュー対応v2)。QUARTERLY(四半期実績由来)は通常どおり
    coverageベースでconfidenceを算出する。ANNUAL_FALLBACK(年次決算への
    フォールバック由来)はスコア自体は通常どおり算出するが、情報粒度が
    粗いことを踏まえconfidenceをMEDIUM上限にキャップし、理由コード
    ANNUAL_FALLBACK_USEDを付与する。UNAVAILABLE(由来不明・実質データ
    無し)は財務トレンド系成分(営業利益/営業CF/acceleration)を入力系列の
    中身に関わらず強制的に評価不能とする(古い/不整合なデータを信頼しない)。

    release_confirmation_stateが「決算予定日を経過したが財務データへの反映が
    未確認」(AWAITING_CONFIRMATION/DELAYED)を示し、かつdecision_relevanceが
    RELEVANTの場合のみNOT_APPLICABLEを返し評価を意図的に見送る
    (コードレビュー対応v3、Earnings Surprise Scoreと同じ前提条件・同じ
    EarningsDecisionRelevance判定を使う)。
    """
    require_timezone_aware(evaluated_at)

    if (
        release_confirmation_state in _AWAITING_STATES
        and decision_relevance == EarningsDecisionRelevance.RELEVANT
    ):
        return EarningsTrendResult(
            state=EarningsTrendEvaluationState.NOT_APPLICABLE,
            reason_codes=(REASON_AWAITING_EARNINGS_CONFIRMATION,),
            evaluated_at=evaluated_at,
            model_version=config.model_version,
            recent_periods_source=recent_periods_source,
            earnings_decision_relevance=decision_relevance,
            release_confirmation_state=release_confirmation_state,
        )

    reason_codes: set[str] = set()
    components: list[tuple[float, float]] = []

    income_component: float | None = None
    prev_income: Decimal | None = None
    latest_income: Decimal | None = None
    income_change_pct: float | None = None
    prev_income_period_end: dt.date | None = None
    latest_income_period_end: dt.date | None = None
    income_period_type: PeriodType | None = None

    cashflow_component: float | None = None
    prev_cashflow: Decimal | None = None
    latest_cashflow: Decimal | None = None
    cashflow_change_pct: float | None = None
    prev_cashflow_period_end: dt.date | None = None
    latest_cashflow_period_end: dt.date | None = None
    cashflow_period_type: PeriodType | None = None

    acceleration_component: float | None = None
    acceleration_raw_pct: float | None = None
    acceleration_period_ends: tuple[dt.date, dt.date, dt.date] | None = None

    if recent_periods_source == RecentPeriodsSource.UNAVAILABLE:
        reason_codes.add(REASON_OPERATING_INCOME_TREND_UNAVAILABLE)
        reason_codes.add(REASON_OPERATING_CASHFLOW_TREND_UNAVAILABLE)
        reason_codes.add(REASON_ACCELERATION_UNAVAILABLE)
    else:
        income_result = _trend_component(quarterly_operating_incomes, config)
        income_component = income_result.score
        prev_income = income_result.previous_value
        latest_income = income_result.latest_value
        income_change_pct = income_result.change_pct
        prev_income_period_end = income_result.previous_period_end
        latest_income_period_end = income_result.latest_period_end
        income_period_type = income_result.period_type
        if income_component is not None:
            components.append((income_component, config.operating_income_trend_weight))
        else:
            reason_codes.add(REASON_OPERATING_INCOME_TREND_UNAVAILABLE)

        cashflow_result = _trend_component(quarterly_operating_cashflows, config)
        cashflow_component = cashflow_result.score
        prev_cashflow = cashflow_result.previous_value
        latest_cashflow = cashflow_result.latest_value
        cashflow_change_pct = cashflow_result.change_pct
        prev_cashflow_period_end = cashflow_result.previous_period_end
        latest_cashflow_period_end = cashflow_result.latest_period_end
        cashflow_period_type = cashflow_result.period_type
        if cashflow_component is not None:
            components.append((cashflow_component, config.operating_cashflow_trend_weight))
        else:
            reason_codes.add(REASON_OPERATING_CASHFLOW_TREND_UNAVAILABLE)

        acceleration_result = _acceleration_component(quarterly_operating_incomes, config)
        acceleration_component = acceleration_result.score
        acceleration_raw_pct = acceleration_result.raw_delta2
        acceleration_period_ends = acceleration_result.period_ends
        if acceleration_component is not None:
            components.append((acceleration_component, config.acceleration_weight))
        else:
            reason_codes.add(REASON_ACCELERATION_UNAVAILABLE)

        if recent_periods_source == RecentPeriodsSource.ANNUAL_FALLBACK:
            reason_codes.add(REASON_ANNUAL_FALLBACK_USED)

    dividend_component = _dividend_component(dividend_comparison_outcome, config)
    if dividend_component is not None:
        components.append((dividend_component, config.dividend_direction_weight))
    else:
        reason_codes.add(REASON_DIVIDEND_DIRECTION_UNAVAILABLE)

    total_config_weight = (
        config.operating_income_trend_weight
        + config.operating_cashflow_trend_weight
        + config.dividend_direction_weight
        + config.acceleration_weight
    )
    available_weight = sum(weight for _, weight in components)
    coverage = available_weight / total_config_weight if total_config_weight > 0 else 0.0

    if coverage < config.min_coverage_required:
        return EarningsTrendResult(
            state=EarningsTrendEvaluationState.NOT_EVALUATED,
            coverage=coverage,
            operating_income_trend_component=income_component,
            operating_cashflow_trend_component=cashflow_component,
            dividend_direction_component=dividend_component,
            acceleration_component=acceleration_component,
            latest_operating_income=latest_income,
            previous_operating_income=prev_income,
            operating_income_change_pct=income_change_pct,
            latest_operating_cashflow=latest_cashflow,
            previous_operating_cashflow=prev_cashflow,
            operating_cashflow_change_pct=cashflow_change_pct,
            acceleration_raw_pct=acceleration_raw_pct,
            recent_periods_source=recent_periods_source,
            latest_operating_income_period_end=latest_income_period_end,
            previous_operating_income_period_end=prev_income_period_end,
            operating_income_period_type=income_period_type,
            latest_operating_cashflow_period_end=latest_cashflow_period_end,
            previous_operating_cashflow_period_end=prev_cashflow_period_end,
            operating_cashflow_period_type=cashflow_period_type,
            acceleration_period_ends=acceleration_period_ends,
            earnings_decision_relevance=decision_relevance,
            release_confirmation_state=release_confirmation_state,
            reason_codes=tuple(sorted(reason_codes)),
            evaluated_at=evaluated_at,
            model_version=config.model_version,
        )

    score = sum(s * weight for s, weight in components) / available_weight
    category = _classify_category(score, config)

    if coverage >= config.coverage_high_threshold:
        confidence = ConfidenceLevel.HIGH
    elif coverage >= config.coverage_medium_threshold:
        confidence = ConfidenceLevel.MEDIUM
    else:
        confidence = ConfidenceLevel.LOW

    # コードレビュー対応(v2): 年次決算へのフォールバックデータを使った場合、
    # 情報粒度が粗いためconfidenceはHIGHへ到達しない。
    if recent_periods_source == RecentPeriodsSource.ANNUAL_FALLBACK and confidence == (
        ConfidenceLevel.HIGH
    ):
        confidence = ConfidenceLevel.MEDIUM

    return EarningsTrendResult(
        state=EarningsTrendEvaluationState.EVALUATED,
        score=score,
        category=category,
        confidence=confidence,
        coverage=coverage,
        operating_income_trend_component=income_component,
        operating_cashflow_trend_component=cashflow_component,
        dividend_direction_component=dividend_component,
        acceleration_component=acceleration_component,
        latest_operating_income=latest_income,
        previous_operating_income=prev_income,
        operating_income_change_pct=income_change_pct,
        latest_operating_cashflow=latest_cashflow,
        previous_operating_cashflow=prev_cashflow,
        operating_cashflow_change_pct=cashflow_change_pct,
        acceleration_raw_pct=acceleration_raw_pct,
        recent_periods_source=recent_periods_source,
        latest_operating_income_period_end=latest_income_period_end,
        previous_operating_income_period_end=prev_income_period_end,
        operating_income_period_type=income_period_type,
        latest_operating_cashflow_period_end=latest_cashflow_period_end,
        previous_operating_cashflow_period_end=prev_cashflow_period_end,
        operating_cashflow_period_type=cashflow_period_type,
        acceleration_period_ends=acceleration_period_ends,
        earnings_decision_relevance=decision_relevance,
        release_confirmation_state=release_confirmation_state,
        reason_codes=tuple(sorted(reason_codes)),
        evaluated_at=evaluated_at,
        model_version=config.model_version,
    )


def earnings_trend_result_to_metrics(result: EarningsTrendResult) -> dict[str, object]:
    """EarningsTrendResultを、Recommendation.earnings_trend_metrics
    (延いてはDecisionSnapshot.earnings_trend_metrics)へ保存する監査用dict
    へ変換する。DecimalはJSON安全のためstr化、日付はISO8601文字列化、enumは
    .valueで保存する(コードレビュー対応v2/v3: 判定当時の入力生値・期間情報
    を後から検証できるようにする)。
    """
    return {
        "state": result.state.value,
        "category": result.category.value if result.category is not None else None,
        "operating_income_trend_component": result.operating_income_trend_component,
        "operating_cashflow_trend_component": result.operating_cashflow_trend_component,
        "dividend_direction_component": result.dividend_direction_component,
        "acceleration_component": result.acceleration_component,
        "latest_operating_income": (
            str(result.latest_operating_income)
            if result.latest_operating_income is not None
            else None
        ),
        "previous_operating_income": (
            str(result.previous_operating_income)
            if result.previous_operating_income is not None
            else None
        ),
        "operating_income_change_pct": result.operating_income_change_pct,
        "latest_operating_cashflow": (
            str(result.latest_operating_cashflow)
            if result.latest_operating_cashflow is not None
            else None
        ),
        "previous_operating_cashflow": (
            str(result.previous_operating_cashflow)
            if result.previous_operating_cashflow is not None
            else None
        ),
        "operating_cashflow_change_pct": result.operating_cashflow_change_pct,
        "acceleration_raw_pct": result.acceleration_raw_pct,
        "recent_periods_source": (
            result.recent_periods_source.value if result.recent_periods_source is not None else None
        ),
        "latest_operating_income_period_end": (
            result.latest_operating_income_period_end.isoformat()
            if result.latest_operating_income_period_end is not None
            else None
        ),
        "previous_operating_income_period_end": (
            result.previous_operating_income_period_end.isoformat()
            if result.previous_operating_income_period_end is not None
            else None
        ),
        "operating_income_period_type": (
            result.operating_income_period_type.value
            if result.operating_income_period_type is not None
            else None
        ),
        "latest_operating_cashflow_period_end": (
            result.latest_operating_cashflow_period_end.isoformat()
            if result.latest_operating_cashflow_period_end is not None
            else None
        ),
        "previous_operating_cashflow_period_end": (
            result.previous_operating_cashflow_period_end.isoformat()
            if result.previous_operating_cashflow_period_end is not None
            else None
        ),
        "operating_cashflow_period_type": (
            result.operating_cashflow_period_type.value
            if result.operating_cashflow_period_type is not None
            else None
        ),
        "acceleration_period_ends": (
            [d.isoformat() for d in result.acceleration_period_ends]
            if result.acceleration_period_ends is not None
            else None
        ),
        "earnings_decision_relevance": (
            result.earnings_decision_relevance.value
            if result.earnings_decision_relevance is not None
            else None
        ),
        "release_confirmation_state": (
            result.release_confirmation_state.value
            if result.release_confirmation_state is not None
            else None
        ),
        "model_version": result.model_version,
    }


def earnings_trend_config_values(config: EarningsTrendRulesConfig) -> dict[str, object]:
    """判定当時に実際に使用したEarnings Trend Score設定値
    (Recommendation.config_values_used["earnings_trend"]として保存する)。"""
    return {
        "model_version": config.model_version,
        "operating_income_trend_weight": config.operating_income_trend_weight,
        "operating_cashflow_trend_weight": config.operating_cashflow_trend_weight,
        "dividend_direction_weight": config.dividend_direction_weight,
        "acceleration_weight": config.acceleration_weight,
        "trend_strong_decline_pct": config.trend_strong_decline_pct,
        "trend_decline_pct": config.trend_decline_pct,
        "trend_improve_pct": config.trend_improve_pct,
        "trend_strong_improve_pct": config.trend_strong_improve_pct,
        "acceleration_full_scale_pct": config.acceleration_full_scale_pct,
        "dividend_actual_cut_score": config.dividend_actual_cut_score,
        "dividend_forecast_cut_score": config.dividend_forecast_cut_score,
        "dividend_maintained_score": config.dividend_maintained_score,
        "dividend_increase_score": config.dividend_increase_score,
        "min_coverage_required": config.min_coverage_required,
        "coverage_high_threshold": config.coverage_high_threshold,
        "coverage_medium_threshold": config.coverage_medium_threshold,
        "category_thresholds": config.category_thresholds.model_dump(),
    }
