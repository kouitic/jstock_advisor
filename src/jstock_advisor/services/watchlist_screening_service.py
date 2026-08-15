"""ウォッチリスト自動追加: Policy実行・集約・ランキング(WatchlistScreeningService)。

複数のScreeningPolicyを統一的に実行し、スコアを集約してWatchlistScreeningResultを
組み立てる。ランキング・追加件数上限の適用もここに集約する(要求仕様§4)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.config.models import AppConfig, WatchlistScreeningRulesConfig
from jstock_advisor.domain.signals.watchlist_screening import (
    ExclusionReason,
    HighDividendFinancialHealthPolicy,
    MatchedCriterion,
    MultiStyleMonitoringPolicy,
    RankingEntry,
    ScreeningPolicy,
    ScreeningPolicyResult,
)
from jstock_advisor.services.screening_data_provider import WatchlistScreeningInput

# RankingEntryはDynamoDBの文字列セット(ranking_entries)へJSON文字列として格納する。
# 既存のsector_entries機構(batch_tracker.py)が採用している「1件あたりの上限バイト数を
# 事前検証してから渡す」という既存パターンを踏襲する。infrastructure/aws/batch_tracker.py
# のMAX_RANKING_ENTRY_BYTES(dispatch前のMAX_RANKING_ENTRIES算出根拠)と同じ値を
# 前提とする(レイヤ分離のためimportはせず、値を変更する場合は両方揃えて変更すること)。
MAX_RANKING_ENTRY_BYTES = 500
_MAIN_METRICS_TRIM_COUNT = 3


@dataclass(frozen=True)
class WatchlistScreeningResult:
    stock_code: str
    stock_name: str | None
    passed: bool
    policy_results: list[ScreeningPolicyResult]
    total_score: float
    matched_criteria: list[MatchedCriterion]
    exclusion_reasons: list[ExclusionReason]
    missing_required_fields: list[str]
    missing_scoring_fields: list[str]
    evaluated_at: dt.datetime
    main_metrics: dict[str, str]
    # ウォッチリスト自動追加基準の再設計(2026-08)で追加。StockTypeClassification.
    # classification_basisをそのまま伝播する(監査から「なぜこの銘柄が対象タイプに
    # 該当した/しなかったか」を再現可能にするため)。Policyに依存しない汎用情報の
    # ためactive Policyの種類に関わらず常に設定する。
    classification_basis: list[str]


def _aggregate_total_score(policy_results: list[ScreeningPolicyResult]) -> float:
    """v1(Policyは1つのみ)はそのまま返す。

    将来の複数Policy集約方式の候補(実装はしない):
    単純合計/重み付き合計/最大スコア/必須Policyと加点Policyの組み合わせ/
    Policyごとの正規化後に合算。
    """
    return policy_results[0].score


def _build_main_metrics(input: WatchlistScreeningInput) -> dict[str, str]:
    metrics: dict[str, str] = {}
    if input.dividend_yield_pct is not None:
        metrics["配当利回り"] = f"{input.dividend_yield_pct:.1f}%"
    if input.equity_ratio_pct is not None:
        metrics["自己資本比率"] = f"{input.equity_ratio_pct:.1f}%"
    return metrics


def _build_policy(policy_name: str, config: AppConfig) -> ScreeningPolicy:
    if policy_name == "high_dividend_financial_health":
        return HighDividendFinancialHealthPolicy()
    if policy_name == "multi_style_monitoring":
        return MultiStyleMonitoringPolicy(config.screening)
    raise ValueError(f"unknown screening policy: {policy_name}")


def _entry_byte_size(entry: RankingEntry) -> int:
    return len(entry.model_dump_json().encode("utf-8"))


def _shrink_ranking_entry_if_needed(entry: RankingEntry) -> RankingEntry | None:
    """MAX_RANKING_ENTRY_BYTES以内に収まるよう段階的にmain_metricsを縮退させる。

    main_metricsを空にしてもなお上限を超える場合(stock_code/policy_scores/
    matched_criteria自体が大きい場合。v1の単一Policyでは実質発生しないが、将来の
    複数Policy化に備えた安全策)は、この銘柄をランキングへ算入できないものとして
    Noneを返す。呼び出し側はこの銘柄を"passed"ではなく処理失敗として扱うこと。
    """
    if _entry_byte_size(entry) <= MAX_RANKING_ENTRY_BYTES:
        return entry
    trimmed = entry.model_copy(
        update={"main_metrics": dict(list(entry.main_metrics.items())[:_MAIN_METRICS_TRIM_COUNT])}
    )
    if _entry_byte_size(trimmed) <= MAX_RANKING_ENTRY_BYTES:
        return trimmed
    emptied = trimmed.model_copy(update={"main_metrics": {}})
    if _entry_byte_size(emptied) <= MAX_RANKING_ENTRY_BYTES:
        return emptied
    return None


class WatchlistScreeningService:
    def __init__(self, config: AppConfig, policies: list[ScreeningPolicy] | None = None) -> None:
        self._config: WatchlistScreeningRulesConfig = config.watchlist_screening
        self._policies = policies or [_build_policy(self._config.screening_policy, config)]

    def evaluate(
        self,
        stock_code: str,
        stock_name: str | None,
        input: WatchlistScreeningInput,
        now: dt.datetime,
    ) -> WatchlistScreeningResult:
        policy_results = [policy.evaluate(input, self._config) for policy in self._policies]
        total_score = _aggregate_total_score(policy_results)

        matched_criteria: list[MatchedCriterion] = []
        exclusion_reasons: list[ExclusionReason] = []
        for result in policy_results:
            for criterion in result.matched_criteria:
                if criterion not in matched_criteria:
                    matched_criteria.append(criterion)
            for reason in result.exclusion_reasons:
                if reason not in exclusion_reasons:
                    exclusion_reasons.append(reason)

        # multi_style_monitoring専用の監査用情報(StockTypeClassification.
        # classification_basis)。テストダブル(SimpleNamespace等)にはこの属性が
        # 無いことがあるため、無い場合は空リストとする。
        classification = getattr(input, "stock_type_classification", None)
        classification_basis = (
            list(classification.classification_basis) if classification is not None else []
        )

        return WatchlistScreeningResult(
            stock_code=stock_code,
            stock_name=stock_name,
            passed=all(result.passed for result in policy_results),
            policy_results=policy_results,
            total_score=total_score,
            matched_criteria=matched_criteria,
            classification_basis=classification_basis,
            exclusion_reasons=exclusion_reasons,
            missing_required_fields=input.missing_required_fields,
            missing_scoring_fields=input.missing_scoring_fields,
            evaluated_at=now,
            main_metrics=_build_main_metrics(input),
        )

    def to_ranking_entry(self, result: WatchlistScreeningResult) -> RankingEntry | None:
        """RankingEntryを組み立てる。MAX_RANKING_ENTRY_BYTESを超過し、main_metricsを
        空にしても収まらない場合はNoneを返す(呼び出し側はこの銘柄を"passed"として
        扱わず、処理失敗(failed)として記録すること)。
        """
        entry = RankingEntry(
            stock_code=result.stock_code,
            total_score=result.total_score,
            policy_scores={pr.policy_name: pr.score for pr in result.policy_results},
            matched_criteria=result.matched_criteria,
            main_metrics=result.main_metrics,
        )
        return _shrink_ranking_entry_if_needed(entry)

    @staticmethod
    def rank(entries: list[RankingEntry]) -> list[RankingEntry]:
        """総合スコア降順、同点は証券コード昇順で安定ソートした全件を返す(上限適用なし)。

        追加件数上限適用「前」の全合格ランキングが必要な場合(上限外銘柄をRepository結果
        AuditLogへskipped_over_limitとして記録する場合等)に使う。
        """
        return sorted(entries, key=lambda entry: (-entry.total_score, entry.stock_code))

    @staticmethod
    def rank_and_limit(entries: list[RankingEntry], limit: int) -> list[RankingEntry]:
        """rank()の結果に追加件数上限を適用する。"""
        return WatchlistScreeningService.rank(entries)[:limit]
