"""ウォッチリスト自動追加のスクリーニング判定(ウォッチリスト自動追加機能)。

必須条件(1つでも不成立で不合格)とスコア条件(config化した配点、合計が閾値以上で合格)
の組み合わせで判定する。個別銘柄向けの条件分岐は一切行わず、config化された
閾値・配点のみで判定する。

配点方式は既存の domain/scoring/score.py の score_<factor>(...) -> (points, formula)
+ _linear_score ヘルパーと同じパターンをこのモジュール専用に踏襲する
(ScoringWeightsConfig専用のため既存関数はそのまま流用できない)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from jstock_advisor.config.models import (
    DividendGrowthScoringConfig,
    DividendYieldScoringConfig,
    EquityRatioScoringConfig,
    PayoutRatioScoringConfig,
    ScreeningRulesConfig,
    ShareholderBenefitScoringConfig,
    WatchlistScreeningRulesConfig,
)
from jstock_advisor.domain.classification.financial_industry import classify_industry
from jstock_advisor.domain.entities.enums import IndustryClassification, StockType
from jstock_advisor.services.screening_data_provider import WatchlistScreeningInput


class MatchedCriterion(StrEnum):
    HIGH_DIVIDEND_YIELD = "HIGH_DIVIDEND_YIELD"
    SOLID_EQUITY_RATIO = "SOLID_EQUITY_RATIO"
    HEALTHY_PAYOUT_RATIO = "HEALTHY_PAYOUT_RATIO"
    DIVIDEND_GROWTH_TRACK_RECORD = "DIVIDEND_GROWTH_TRACK_RECORD"
    SHAREHOLDER_BENEFIT = "SHAREHOLDER_BENEFIT"
    # --- ウォッチリスト自動追加基準の再設計(2026-08)で追加。multi_style_monitoring
    # Policy専用。StockTypeClassification(既存の銘柄タイプ分類)のうち、
    # ウォッチリスト追加対象とする5タイプにそのまま対応する ---
    TARGET_INCOME = "TARGET_INCOME"
    TARGET_DIVIDEND_GROWTH = "TARGET_DIVIDEND_GROWTH"
    TARGET_GROWTH = "TARGET_GROWTH"
    TARGET_VALUE = "TARGET_VALUE"
    TARGET_QUALITY = "TARGET_QUALITY"


class HardExclusionCode(StrEnum):
    """`_evaluate_hard_exclusions()`が返す個別除外理由の構造化コード
    (横断整合性レビュー対応2026-08、指摘4)。以前はwatchlist_maintenance_
    service.pyがhard_exclusion_reasons(人間可読な日本語文言)に対して
    `str.startswith()`で即時削除対象かどうかを判定しており、文言を変更する
    だけで判定ロジックが静かに壊れる脆弱性があった。判定用のコードと表示用の
    メッセージを分離し、判定はこのenumのみに依存させる。

    メンバーは`_evaluate_hard_exclusions()`が生成しうる7種類の除外理由と
    1対1で対応する(このモジュール外では新規に生成しない)。
    """

    REIT_EXCLUDED = "REIT_EXCLUDED"
    ETF_EXCLUDED = "ETF_EXCLUDED"
    NEGATIVE_EQUITY = "NEGATIVE_EQUITY"
    GOING_CONCERN_DOUBT = "GOING_CONCERN_DOUBT"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    UNSUPPORTED_INDUSTRY = "UNSUPPORTED_INDUSTRY"
    DISCLOSURE_RISK = "DISCLOSURE_RISK"
    SEVERE_EARNINGS_DECLINE = "SEVERE_EARNINGS_DECLINE"


class ExclusionReason(StrEnum):
    ALREADY_HELD = "ALREADY_HELD"
    ALREADY_WATCHLISTED = "ALREADY_WATCHLISTED"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    MARKET_CAP_BELOW_THRESHOLD = "MARKET_CAP_BELOW_THRESHOLD"
    NEGATIVE_OPERATING_CASHFLOW = "NEGATIVE_OPERATING_CASHFLOW"
    SEVERE_DIVIDEND_CUT = "SEVERE_DIVIDEND_CUT"
    DEBT_EXCESS = "DEBT_EXCESS"
    DEFICIT = "DEFICIT"
    GOING_CONCERN_DOUBT = "GOING_CONCERN_DOUBT"
    EXCLUDED_SECURITY_TYPE = "EXCLUDED_SECURITY_TYPE"
    SCORE_BELOW_THRESHOLD = "SCORE_BELOW_THRESHOLD"
    RANK_OUTSIDE_ADDITION_LIMIT = "RANK_OUTSIDE_ADDITION_LIMIT"
    # --- ウォッチリスト自動追加基準の再設計(2026-08)で追加。multi_style_monitoring
    # Policy専用。個別のハード除外理由はScreeningPolicyResult.hard_exclusion_reasons
    # (人間可読な文字列)に持たせ、このenumは「重大リスクによる除外だった」という
    # 事実のみを表す(高配当Policy固有のMARKET_CAP_BELOW_THRESHOLD等とは意味が
    # 異なるため、既存メンバーを転用せず新設する) ---
    HARD_EXCLUDED = "HARD_EXCLUDED"
    FAILED_NO_TARGET_TYPE = "FAILED_NO_TARGET_TYPE"


# 必須条件(R1〜R7)由来のExclusionReason。DATA_INSUFFICIENT/SCORE_BELOW_THRESHOLDとは
# 区別してカテゴリ集計する(実装プラン§9)。Lambdaハンドラ・CLIの両方から
# categorize_screening_result()経由で参照する(判定ロジックを分散させない)。
_REQUIRED_CONDITION_REASONS = frozenset(
    {
        ExclusionReason.MARKET_CAP_BELOW_THRESHOLD,
        ExclusionReason.NEGATIVE_OPERATING_CASHFLOW,
        ExclusionReason.SEVERE_DIVIDEND_CUT,
        ExclusionReason.DEBT_EXCESS,
        ExclusionReason.DEFICIT,
        ExclusionReason.GOING_CONCERN_DOUBT,
        ExclusionReason.EXCLUDED_SECURITY_TYPE,
    }
)


def categorize_exclusion_reasons(
    exclusion_reasons: list[ExclusionReason],
) -> tuple[str, str]:
    """(batch_tracker用category, AuditLog用evaluation_result)を返す。

    優先順位: データ不足 > 必須条件不成立(重大リスクによるハード除外・対象
    タイプ0件を含む) > スコア不足(旧Policy専用) > 合格。

    HARD_EXCLUDED/FAILED_NO_TARGET_TYPE(multi_style_monitoring専用)は、
    「必須条件が満たされなかった」という意味では既存の
    _REQUIRED_CONDITION_REASONSと同じ性質のためcategoryは共通の
    "required_condition_failed"へ分類する(watchlist_batch_finalizer.py等の
    既存の集計・分岐はcategory文字列のみに依存しており、個別のExclusionReason
    メンバーには依存しないため、この分類方式を変えても既存の呼び出し側は
    影響を受けない)。evaluation_result(2値目)はより具体的な理由文字列を返し、
    「対象タイプ0件」と「重大リスク除外」を監査ログ上で区別できるようにする。
    """
    reasons = set(exclusion_reasons)
    if ExclusionReason.DATA_INSUFFICIENT in reasons:
        return "data_insufficient", "DATA_INSUFFICIENT"
    if ExclusionReason.HARD_EXCLUDED in reasons:
        return "required_condition_failed", "FAILED_REQUIRED"
    if ExclusionReason.FAILED_NO_TARGET_TYPE in reasons:
        return "required_condition_failed", "FAILED_NO_TARGET_TYPE"
    if reasons & _REQUIRED_CONDITION_REASONS:
        return "required_condition_failed", "FAILED_REQUIRED"
    if ExclusionReason.SCORE_BELOW_THRESHOLD in reasons:
        return "score_failed", "FAILED_SCORE"
    return "passed", "PASSED"


@dataclass(frozen=True)
class ScreeningPolicyResult:
    policy_name: str
    passed: bool
    score: float
    matched_criteria: list[MatchedCriterion]
    exclusion_reasons: list[ExclusionReason]
    missing_required_fields: list[str]
    missing_scoring_fields: list[str]
    score_breakdown: dict[str, float]
    # ウォッチリスト自動追加基準の再設計(2026-08)で追加。人間可読なハード除外
    # 理由(BUY一次スクリーニングと同じ文言)。旧Policyは常に空リスト。
    hard_exclusion_reasons: list[str] = field(default_factory=list)
    # 横断整合性レビュー対応(2026-08、指摘4)で追加。hard_exclusion_reasonsと
    # 同じ順序・同じ長さで対応する構造化コード(判定用、文言は表示専用)。
    hard_exclusion_codes: list[HardExclusionCode] = field(default_factory=list)


class ScreeningPolicy(Protocol):
    @property
    def policy_name(self) -> str: ...

    def evaluate(
        self, input: WatchlistScreeningInput, config: WatchlistScreeningRulesConfig
    ) -> ScreeningPolicyResult: ...


# 構造化されたmatched_criteriaから通知・ウォッチリスト登録理由の日本語文言を生成する
# 唯一の変換辞書(通知層・Lambdaハンドラの両方がdescribe_matched_criteria()経由で使う。
# 固定文言を複数箇所へ分散させない)。
_MATCHED_CRITERIA_LABELS: dict[MatchedCriterion, str] = {
    MatchedCriterion.HIGH_DIVIDEND_YIELD: "高配当",
    MatchedCriterion.SOLID_EQUITY_RATIO: "財務健全",
    MatchedCriterion.HEALTHY_PAYOUT_RATIO: "配当性向良好",
    MatchedCriterion.DIVIDEND_GROWTH_TRACK_RECORD: "増配実績あり",
    MatchedCriterion.SHAREHOLDER_BENEFIT: "株主優待あり",
    MatchedCriterion.TARGET_INCOME: "高配当",
    MatchedCriterion.TARGET_DIVIDEND_GROWTH: "連続増配",
    MatchedCriterion.TARGET_GROWTH: "成長",
    MatchedCriterion.TARGET_VALUE: "割安",
    MatchedCriterion.TARGET_QUALITY: "優良",
}


def describe_matched_criteria(matched_criteria: list[MatchedCriterion]) -> str:
    labels = [_MATCHED_CRITERIA_LABELS[criterion] for criterion in matched_criteria]
    return "、".join(labels) if labels else "スクリーニング条件に合致"


class RankingEntry(BaseModel):
    """fan-out集約用の構造化ランキング情報(第3回レビュー対応)。

    batch_tracker.record_result()のranking_entry(str)引数へは
    model_dump_json()でJSON文字列化して渡し、finalize側はmodel_validate_json()で
    型安全に復元する。"score|stock_code"のような手組み文字列パースは行わない。
    """

    stock_code: str
    total_score: float
    policy_scores: dict[str, float]
    matched_criteria: list[MatchedCriterion]
    main_metrics: dict[str, str]


class ScoreCriterionValue(BaseModel):
    """通知品質改善(2026-08)で追加。1銘柄・1配点項目あたりのスコア根拠。"""

    criterion_key: str
    label: str
    score: float
    metric_value: str | None


class WatchlistScoreDetail(BaseModel):
    """通知品質改善(2026-08)で追加。合格銘柄の通知再構築用スコア詳細。

    infrastructure/aws/batch_tracker.pyのCandidateProgressRecord.notification_detail
    (passed銘柄のみ)へモデルのまま保持し、JSON化はbatch_tracker.py内部でのみ行う。
    RankingEntry(既存、無変更)とは独立したDynamoDB列・バイト予算を持つ。
    """

    stock_code: str
    criteria: list[ScoreCriterionValue]


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _linear_score(value: float, weight: float, zero_at: float, full_at: float) -> float:
    if full_at == zero_at:
        return 0.0
    ratio = (value - zero_at) / (full_at - zero_at)
    return weight * _clip(ratio, 0.0, 1.0)


def _score_dividend_yield(
    dividend_yield_pct: float | None, params: DividendYieldScoringConfig
) -> tuple[float, list[MatchedCriterion]]:
    if dividend_yield_pct is None:
        return 0.0, []
    score = _linear_score(dividend_yield_pct, params.weight, params.zero_at_pct, params.full_at_pct)
    matched = (
        [MatchedCriterion.HIGH_DIVIDEND_YIELD] if dividend_yield_pct >= params.zero_at_pct else []
    )
    return score, matched


def _score_equity_ratio(
    equity_ratio_pct: float | None, params: EquityRatioScoringConfig
) -> tuple[float, list[MatchedCriterion]]:
    if equity_ratio_pct is None:
        return 0.0, []
    score = _linear_score(equity_ratio_pct, params.weight, params.zero_at_pct, params.full_at_pct)
    matched = (
        [MatchedCriterion.SOLID_EQUITY_RATIO] if equity_ratio_pct >= params.zero_at_pct else []
    )
    return score, matched


def _score_payout_ratio(
    payout_ratio_pct: float | None, params: PayoutRatioScoringConfig
) -> tuple[float, list[MatchedCriterion]]:
    """配当性向は低すぎず高すぎない範囲(healthy_min_pct〜healthy_max_pct)を満点とする
    山型の配点。範囲外は健全域からの乖離に応じて線形に逓減する。"""
    if payout_ratio_pct is None:
        return 0.0, []
    healthy_min = params.healthy_min_pct
    healthy_max = params.healthy_max_pct
    weight = params.weight
    if healthy_min <= payout_ratio_pct <= healthy_max:
        return weight, [MatchedCriterion.HEALTHY_PAYOUT_RATIO]
    if payout_ratio_pct < healthy_min:
        ratio = payout_ratio_pct / healthy_min if healthy_min > 0 else 0.0
    else:
        span = healthy_max if healthy_max > 0 else 1.0
        ratio = 1.0 - (payout_ratio_pct - healthy_max) / span
    return weight * _clip(ratio, 0.0, 1.0), []


def _score_dividend_growth(
    consecutive_years: int | None, params: DividendGrowthScoringConfig
) -> tuple[float, list[MatchedCriterion]]:
    if consecutive_years is None or consecutive_years <= 0:
        return 0.0, []
    score = _linear_score(
        float(consecutive_years),
        params.weight,
        float(params.zero_at_years),
        float(params.full_at_years),
    )
    return score, [MatchedCriterion.DIVIDEND_GROWTH_TRACK_RECORD]


def _score_shareholder_benefit(
    exists: bool, yield_pct: float | None, params: ShareholderBenefitScoringConfig
) -> tuple[float, list[MatchedCriterion]]:
    if not exists:
        return 0.0, []
    weight = params.weight
    if yield_pct is None:
        return weight * params.presence_only_score_ratio, [MatchedCriterion.SHAREHOLDER_BENEFIT]
    score = _linear_score(yield_pct, weight, 0.0, params.yield_full_at_pct)
    return score, [MatchedCriterion.SHAREHOLDER_BENEFIT]


class HighDividendFinancialHealthPolicy:
    """高配当・財務健全性を軸とした初期スクリーニングPolicy(v1で実装する唯一のPolicy)。"""

    policy_name = "high_dividend_financial_health"

    def evaluate(
        self, input: WatchlistScreeningInput, config: WatchlistScreeningRulesConfig
    ) -> ScreeningPolicyResult:
        if input.missing_required_fields:
            return ScreeningPolicyResult(
                policy_name=self.policy_name,
                passed=False,
                score=0.0,
                matched_criteria=[],
                exclusion_reasons=[ExclusionReason.DATA_INSUFFICIENT],
                missing_required_fields=input.missing_required_fields,
                missing_scoring_fields=input.missing_scoring_fields,
                score_breakdown={},
            )

        # missing_required_fieldsが空の時点でmarket_cap/operating_cashflowは非Noneが保証される
        # (services/screening_data_provider.pyの_to_screening_input参照)。
        assert input.market_cap is not None
        assert input.operating_cashflow is not None

        thresholds = config.thresholds
        exclusion_reasons: list[ExclusionReason] = []

        if input.market_cap < thresholds.minimum_market_cap_yen:
            exclusion_reasons.append(ExclusionReason.MARKET_CAP_BELOW_THRESHOLD)
        if thresholds.require_positive_operating_cash_flow and input.operating_cashflow <= 0:
            exclusion_reasons.append(ExclusionReason.NEGATIVE_OPERATING_CASHFLOW)
        if thresholds.exclude_dividend_cut_announced and (
            input.is_dividend_cut_announced or input.is_dividend_omission_announced
        ):
            exclusion_reasons.append(ExclusionReason.SEVERE_DIVIDEND_CUT)
        if thresholds.exclude_debt_excess and input.is_debt_excess:
            exclusion_reasons.append(ExclusionReason.DEBT_EXCESS)
        if thresholds.exclude_deficit and input.is_deficit:
            exclusion_reasons.append(ExclusionReason.DEFICIT)
        if thresholds.exclude_going_concern_doubt and input.is_going_concern_doubt:
            exclusion_reasons.append(ExclusionReason.GOING_CONCERN_DOUBT)
        if (thresholds.exclude_etf and input.security_type == "ETF") or (
            thresholds.exclude_reit and input.security_type == "REIT"
        ):
            exclusion_reasons.append(ExclusionReason.EXCLUDED_SECURITY_TYPE)

        scoring = config.scoring
        score_breakdown: dict[str, float] = {}
        matched_criteria: list[MatchedCriterion] = []

        dy_score, dy_matched = _score_dividend_yield(
            input.dividend_yield_pct, scoring.dividend_yield
        )
        score_breakdown["dividend_yield"] = dy_score
        matched_criteria += dy_matched

        er_score, er_matched = _score_equity_ratio(input.equity_ratio_pct, scoring.equity_ratio)
        score_breakdown["equity_ratio"] = er_score
        matched_criteria += er_matched

        pr_score, pr_matched = _score_payout_ratio(input.payout_ratio_pct, scoring.payout_ratio)
        score_breakdown["payout_ratio"] = pr_score
        matched_criteria += pr_matched

        dg_score, dg_matched = _score_dividend_growth(
            input.consecutive_dividend_increase_years, scoring.dividend_growth
        )
        score_breakdown["dividend_growth"] = dg_score
        matched_criteria += dg_matched

        sb_score, sb_matched = _score_shareholder_benefit(
            input.shareholder_benefit_exists,
            input.shareholder_benefit_yield_pct,
            scoring.shareholder_benefit,
        )
        score_breakdown["shareholder_benefit"] = sb_score
        matched_criteria += sb_matched

        total_score = sum(score_breakdown.values())

        if len(input.missing_scoring_fields) > config.max_missing_fields:
            exclusion_reasons.append(ExclusionReason.DATA_INSUFFICIENT)
        if total_score < scoring.minimum_total_score:
            exclusion_reasons.append(ExclusionReason.SCORE_BELOW_THRESHOLD)

        return ScreeningPolicyResult(
            policy_name=self.policy_name,
            passed=not exclusion_reasons,
            score=total_score,
            matched_criteria=matched_criteria,
            exclusion_reasons=exclusion_reasons,
            missing_required_fields=input.missing_required_fields,
            missing_scoring_fields=input.missing_scoring_fields,
            score_breakdown=score_breakdown,
        )


# ウォッチリスト自動追加基準の再設計(2026-08)で追加。「高配当だけでなく、連続増配・
# 成長・割安・優良株を対象とし、重大リスク以外は過度にハード除外しない」という
# 下流(BUY候補判定・保有銘柄の売却基準)と同じ方針をウォッチリスト自動追加へも
# 適用する(HighDividendFinancialHealthPolicyは後方互換・比較用にそのまま残す)。
_TARGET_STOCK_TYPES: frozenset[StockType] = frozenset(
    {
        StockType.INCOME,
        StockType.DIVIDEND_GROWTH,
        StockType.GROWTH,
        StockType.VALUE,
        StockType.QUALITY,
    }
)

_TARGET_TYPE_TO_CRITERION: dict[StockType, MatchedCriterion] = {
    StockType.INCOME: MatchedCriterion.TARGET_INCOME,
    StockType.DIVIDEND_GROWTH: MatchedCriterion.TARGET_DIVIDEND_GROWTH,
    StockType.GROWTH: MatchedCriterion.TARGET_GROWTH,
    StockType.VALUE: MatchedCriterion.TARGET_VALUE,
    StockType.QUALITY: MatchedCriterion.TARGET_QUALITY,
}


@dataclass(frozen=True)
class HardExclusionFinding:
    code: HardExclusionCode
    message: str


def _evaluate_hard_exclusions(
    input: WatchlistScreeningInput, screening_rules: ScreeningRulesConfig
) -> list[HardExclusionFinding]:
    """downstream BUY一次スクリーニング(domain/screening/rules.py::evaluate_screening/
    domain/signals/buy_decision.py::screen_investment_universe)と同じ設定値
    (config.screening、config.screening.financial_health.min_equity_ratio_pct等)・
    同じ判定材料を再利用する(閾値・業種判定ロジックを二重実装しない)。

    以下2点は意図的にBUY側と揃えない:
    - データ鮮度(screening.data_quality.max_data_age_business_days)。この経路は
      build_stock_snapshot()を経由しない(screening_data_provider.py参照)。
      横断整合性レビュー対応(2026-08、指摘2)で判明: 以前の本コメントは
      「営業日をまたぐ長期キャッシュを挟まない」としていたが誤りで、実際には
      `watchlist_data_cache.build_cached_provider_bundle()`経由でprice=24時間・
      financial=168時間(7日)というBUY側より長いTTLのキャッシュを挟んでいる。
      それでもBUY側のmax_data_age_business_daysをそのまま流用しない理由は、
      鮮度制御の仕組みが違うため(BUY側は「取得データの日付が古すぎないか」を
      事後チェックするのに対し、こちら側はキャッシュ層自体がJST暦日境界(price/
      average_trading_value)とTTLで鮮度を制御する設計であり、二重に閾値を
      持つと基準が食い違う恐れがあるため一本化していない)。財務データの
      168時間TTLに起因する残存する鮮度課題(決算更新後の取り込み遅延)は
      GitHub Issueで追跡している。
    - 株主優待の廃止発表(BUY側screen_investment_universe()の追加条件)。優待の
      有無・廃止は「投資対象として監視する価値」そのものとは無関係なため対象外。
    """
    findings: list[HardExclusionFinding] = []
    universe = screening_rules.universe
    if universe.exclude_reit and input.security_type == "REIT":
        findings.append(HardExclusionFinding(HardExclusionCode.REIT_EXCLUDED, "REITは対象外です"))
    if universe.exclude_etf and input.security_type == "ETF":
        findings.append(HardExclusionFinding(HardExclusionCode.ETF_EXCLUDED, "ETFは対象外です"))

    fh = screening_rules.financial_health
    if fh.exclude_negative_equity and input.is_debt_excess:
        findings.append(HardExclusionFinding(HardExclusionCode.NEGATIVE_EQUITY, "債務超過"))

    ce = screening_rules.corporate_events
    if ce.exclude_going_concern_doubt and input.is_going_concern_doubt:
        findings.append(
            HardExclusionFinding(
                HardExclusionCode.GOING_CONCERN_DOUBT, "継続企業の前提に重大な疑義"
            )
        )

    if input.avg_trading_value is not None:
        min_value = universe.min_avg_trading_value_20d_yen
        if input.avg_trading_value < min_value:
            findings.append(
                HardExclusionFinding(
                    HardExclusionCode.INSUFFICIENT_LIQUIDITY,
                    f"平均売買代金{input.avg_trading_value:,.0f}円が基準{min_value:,}円未満",
                )
            )

    # Issue #29(2026-08-28): BUY一次スクリーニング(domain/screening/rules.py)と
    # 同じバグ(日本語TSE33ラベルと英語GICS industry値の比較不一致で金融業除外が
    # 機能していなかった)がこちらにもあったため、同一の分類器classify_industry()へ
    # 両経路同時に切り替える(片方だけ直すと、通常screeningでは除外されるが
    # watchlist自動追加では流入する不整合が残るため)。UNKNOWNは除外しない。
    industry_rules = screening_rules.industry_specific_rules
    industry_result = classify_industry(input.sector, input.industry)
    if (
        industry_result.classification == IndustryClassification.FINANCIAL
        and industry_result.financial_category in industry_rules.target_industry_classification
        and industry_rules.financial_sector_action == "exclude_with_warning"
    ):
        findings.append(
            HardExclusionFinding(
                HardExclusionCode.UNSUPPORTED_INDUSTRY,
                f"金融業({industry_result.financial_category}: {input.industry})は"
                "個別評価ルール未実装のため対象外",
            )
        )

    if input.disclosure_risk_keywords_found and ce.scandal_or_delisting_risk_action == "exclude":
        findings.append(
            HardExclusionFinding(
                HardExclusionCode.DISCLOSURE_RISK,
                "開示情報にリスクキーワードを検出: "
                + ", ".join(input.disclosure_risk_keywords_found),
            )
        )

    if input.severe_earnings_decline:
        findings.append(
            HardExclusionFinding(
                HardExclusionCode.SEVERE_EARNINGS_DECLINE,
                "直近決算で重大な業績悪化(営業利益が前期比30%超悪化)",
            )
        )

    return findings


class MultiStyleMonitoringPolicy:
    """高配当・連続増配・成長・割安・優良の5タイプのいずれかに該当すれば
    ウォッチリスト追加候補とするPolicy(ウォッチリスト自動追加基準の再設計、2026-08)。

    合否とランキングを分離する: 合否はStockTypeClassification(既存の銘柄タイプ
    分類)が対象5タイプへ1件でも該当するかのみで判定し、価格の割安さ・買い
    タイミングは一切見ない(BuySignalService側の責務のまま変更しない)。
    ランキング専用のMonitoringScore(「ウォッチリストへ優先して入れる価値」)は
    BUY側のcompany_quality_score・purchase_attractiveness_score等とは無関係の
    独自指標。
    """

    policy_name = "multi_style_monitoring"

    def __init__(self, screening_rules: ScreeningRulesConfig) -> None:
        self._screening_rules = screening_rules

    def evaluate(
        self, input: WatchlistScreeningInput, config: WatchlistScreeningRulesConfig
    ) -> ScreeningPolicyResult:
        hard_exclusion_findings = _evaluate_hard_exclusions(input, self._screening_rules)
        if hard_exclusion_findings:
            return ScreeningPolicyResult(
                policy_name=self.policy_name,
                passed=False,
                score=0.0,
                matched_criteria=[],
                exclusion_reasons=[ExclusionReason.HARD_EXCLUDED],
                missing_required_fields=input.missing_required_fields,
                missing_scoring_fields=input.missing_scoring_fields,
                score_breakdown={},
                hard_exclusion_reasons=[f.message for f in hard_exclusion_findings],
                hard_exclusion_codes=[f.code for f in hard_exclusion_findings],
            )

        matched_types = [
            t for t in input.stock_type_classification.types if t in _TARGET_STOCK_TYPES
        ]
        if not matched_types:
            return ScreeningPolicyResult(
                policy_name=self.policy_name,
                passed=False,
                score=0.0,
                matched_criteria=[],
                exclusion_reasons=[ExclusionReason.FAILED_NO_TARGET_TYPE],
                missing_required_fields=input.missing_required_fields,
                missing_scoring_fields=input.missing_scoring_fields,
                score_breakdown={},
            )
        matched_criteria = [_TARGET_TYPE_TO_CRITERION[t] for t in matched_types]

        ms = config.monitoring_score
        breakdown: dict[str, float] = {"base": ms.base_score}
        additional_types = len(matched_types) - 1
        if additional_types > 0:
            breakdown["type_bonus"] = min(
                additional_types * ms.additional_type_bonus, ms.max_type_bonus
            )

        fh_screening = self._screening_rules.financial_health
        if (
            input.equity_ratio_pct is not None
            and input.equity_ratio_pct >= fh_screening.min_equity_ratio_pct
        ):
            breakdown["equity_ratio_bonus"] = ms.equity_ratio_bonus
        if input.operating_cashflow is not None and input.operating_cashflow > 0:
            breakdown["cashflow_bonus"] = ms.positive_operating_cashflow_bonus
        if not input.is_deficit:
            breakdown["no_deficit_bonus"] = ms.no_deficit_bonus
        if not (input.is_dividend_cut_announced or input.is_dividend_omission_announced):
            breakdown["no_dividend_cut_bonus"] = ms.no_recent_dividend_cut_bonus
        if (
            input.market_cap is not None
            and input.market_cap >= config.thresholds.minimum_market_cap_yen
        ):
            breakdown["market_cap_bonus"] = ms.market_cap_bonus

        total_score = min(100.0, sum(breakdown.values()))

        return ScreeningPolicyResult(
            policy_name=self.policy_name,
            passed=True,
            score=total_score,
            matched_criteria=matched_criteria,
            exclusion_reasons=[],
            missing_required_fields=input.missing_required_fields,
            missing_scoring_fields=input.missing_scoring_fields,
            score_breakdown=breakdown,
        )
