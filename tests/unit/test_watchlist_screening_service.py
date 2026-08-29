import dataclasses
import datetime as dt
from types import SimpleNamespace

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.signals.watchlist_screening import (
    ExclusionReason,
    MatchedCriterion,
    RankingEntry,
    ScreeningPolicyResult,
)
from jstock_advisor.services.watchlist_screening_service import (
    MAX_RANKING_ENTRY_BYTES,
    WatchlistScreeningResult,
    WatchlistScreeningService,
)

_CONFIG = load_config()
_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


def _fake_input(*, disclosure_available: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        missing_required_fields=[],
        missing_scoring_fields=[],
        dividend_yield_pct=4.2,
        equity_ratio_pct=55.0,
        # Issue #81: critical data availability gateが参照する。
        disclosure_available=disclosure_available,
    )


def _entry(stock_code: str, total_score: float) -> RankingEntry:
    return RankingEntry(
        stock_code=stock_code,
        total_score=total_score,
        policy_scores={},
        matched_criteria=[],
        main_metrics={},
    )


class _FakePolicy:
    def __init__(
        self,
        name: str,
        score: float,
        passed: bool,
        matched: list[MatchedCriterion] | None = None,
        exclusions: list[ExclusionReason] | None = None,
    ) -> None:
        self.policy_name = name
        self._score = score
        self._passed = passed
        self._matched = matched or []
        self._exclusions = exclusions or []

    def evaluate(self, input: object, config: object) -> ScreeningPolicyResult:
        return ScreeningPolicyResult(
            policy_name=self.policy_name,
            passed=self._passed,
            score=self._score,
            matched_criteria=self._matched,
            exclusion_reasons=self._exclusions,
            missing_required_fields=[],
            missing_scoring_fields=[],
            score_breakdown={"x": self._score},
        )


def test_single_policy_total_score_equals_policy_score() -> None:
    service = WatchlistScreeningService(
        _CONFIG,
        policies=[
            _FakePolicy(
                "p1", 87.0, True, matched=[MatchedCriterion.HIGH_DIVIDEND_YIELD]
            )
        ],
    )
    result = service.evaluate("1234", "テスト", _fake_input(), _NOW)  # type: ignore[arg-type]
    assert result.total_score == 87.0
    assert result.passed is True
    assert result.matched_criteria == [MatchedCriterion.HIGH_DIVIDEND_YIELD]


def test_passed_requires_all_policies_to_pass() -> None:
    service = WatchlistScreeningService(
        _CONFIG,
        policies=[
            _FakePolicy("p1", 90.0, True),
            _FakePolicy("p2", 40.0, False, exclusions=[ExclusionReason.SCORE_BELOW_THRESHOLD]),
        ],
    )
    result = service.evaluate("1234", None, _fake_input(), _NOW)  # type: ignore[arg-type]
    assert result.passed is False
    assert ExclusionReason.SCORE_BELOW_THRESHOLD in result.exclusion_reasons


def test_matched_criteria_and_exclusion_reasons_are_deduplicated_across_policies() -> None:
    service = WatchlistScreeningService(
        _CONFIG,
        policies=[
            _FakePolicy("p1", 60.0, True, matched=[MatchedCriterion.HIGH_DIVIDEND_YIELD]),
            _FakePolicy("p2", 60.0, True, matched=[MatchedCriterion.HIGH_DIVIDEND_YIELD]),
        ],
    )
    result = service.evaluate("1234", None, _fake_input(), _NOW)  # type: ignore[arg-type]
    assert result.matched_criteria == [MatchedCriterion.HIGH_DIVIDEND_YIELD]


def test_rank_returns_full_sorted_ranking_without_limit() -> None:
    entries = [_entry("1111", 60.0), _entry("2222", 90.0), _entry("3333", 75.0)]
    ranked = WatchlistScreeningService.rank(entries)
    assert [e.stock_code for e in ranked] == ["2222", "3333", "1111"]


def test_rank_and_limit_orders_by_score_descending() -> None:
    entries = [_entry("1111", 60.0), _entry("2222", 90.0), _entry("3333", 75.0)]
    ranked = WatchlistScreeningService.rank_and_limit(entries, limit=10)
    assert [e.stock_code for e in ranked] == ["2222", "3333", "1111"]


def test_rank_and_limit_applies_addition_limit() -> None:
    entries = [_entry(str(i), float(i)) for i in range(5)]
    ranked = WatchlistScreeningService.rank_and_limit(entries, limit=2)
    assert len(ranked) == 2
    assert [e.stock_code for e in ranked] == ["4", "3"]


def test_rank_and_limit_breaks_ties_by_stock_code_ascending() -> None:
    entries = [_entry("9999", 80.0), _entry("1111", 80.0)]
    ranked = WatchlistScreeningService.rank_and_limit(entries, limit=10)
    assert [e.stock_code for e in ranked] == ["1111", "9999"]


def test_ranking_entry_round_trips_through_json() -> None:
    entry = RankingEntry(
        stock_code="1234",
        total_score=87.5,
        policy_scores={"high_dividend_financial_health": 87.5},
        matched_criteria=[
            MatchedCriterion.HIGH_DIVIDEND_YIELD,
            MatchedCriterion.SOLID_EQUITY_RATIO,
        ],
        main_metrics={"配当利回り": "4.2%"},
    )
    restored = RankingEntry.model_validate_json(entry.model_dump_json())
    assert restored == entry


def test_to_ranking_entry_builds_entry_from_result() -> None:
    service = WatchlistScreeningService(
        _CONFIG,
        policies=[_FakePolicy("p1", 87.0, True, matched=[MatchedCriterion.HIGH_DIVIDEND_YIELD])],
    )
    result = service.evaluate("1234", "テスト", _fake_input(), _NOW)  # type: ignore[arg-type]
    entry = service.to_ranking_entry(result)
    assert entry is not None
    assert entry.stock_code == "1234"
    assert entry.total_score == 87.0
    assert entry.policy_scores == {"p1": 87.0}
    assert entry.matched_criteria == [MatchedCriterion.HIGH_DIVIDEND_YIELD]


def _result_with_main_metrics(main_metrics: dict[str, str]) -> WatchlistScreeningResult:
    return WatchlistScreeningResult(
        stock_code="1234",
        stock_name=None,
        passed=True,
        policy_results=[
            ScreeningPolicyResult(
                policy_name="p1",
                passed=True,
                score=87.0,
                matched_criteria=[],
                exclusion_reasons=[],
                missing_required_fields=[],
                missing_scoring_fields=[],
                score_breakdown={},
            )
        ],
        total_score=87.0,
        matched_criteria=[],
        exclusion_reasons=[],
        missing_required_fields=[],
        missing_scoring_fields=[],
        evaluated_at=_NOW,
        main_metrics=main_metrics,
        classification_basis=[],
    )


def test_to_ranking_entry_within_budget_is_returned_unchanged() -> None:
    service = WatchlistScreeningService(_CONFIG, policies=[_FakePolicy("p1", 87.0, True)])
    result = _result_with_main_metrics({"配当利回り": "4.2%"})
    entry = service.to_ranking_entry(result)
    assert entry is not None
    assert entry.main_metrics == {"配当利回り": "4.2%"}


def test_to_ranking_entry_shrinks_main_metrics_when_over_byte_budget() -> None:
    service = WatchlistScreeningService(_CONFIG, policies=[_FakePolicy("p1", 87.0, True)])
    huge_metrics = {f"指標{i}": "x" * 100 for i in range(20)}
    result = _result_with_main_metrics(huge_metrics)
    entry = service.to_ranking_entry(result)
    assert entry is not None
    assert len(entry.model_dump_json().encode("utf-8")) <= MAX_RANKING_ENTRY_BYTES
    assert len(entry.main_metrics) < len(huge_metrics)


def test_to_ranking_entry_uses_japanese_metrics_utf8_byte_length_not_char_count() -> None:
    """日本語(マルチバイト文字)を含むmain_metricsが、文字数ではなくUTF-8バイト数で
    正しく評価されることを確認する(レビュー対応)。
    """
    service = WatchlistScreeningService(_CONFIG, policies=[_FakePolicy("p1", 87.0, True)])
    # 1項目あたり日本語ラベル+値で約20文字(UTF-8で約40〜50バイト)。10項目あれば
    # 文字数だけを見れば500文字未満だが、UTF-8バイト数では上限を超える。
    metrics = {f"日本語の指標名その{i}": f"数値{i}パーセント" for i in range(10)}
    result = _result_with_main_metrics(metrics)
    entry = service.to_ranking_entry(result)
    assert entry is not None
    assert len(entry.model_dump_json().encode("utf-8")) <= MAX_RANKING_ENTRY_BYTES


def test_to_ranking_entry_returns_none_when_still_over_budget_after_emptying_metrics() -> None:
    """main_metricsを空にしても上限を超える場合はNoneを返し、呼び出し側が
    ランキングへ算入せず処理失敗として扱えるようにする(レビュー対応)。
    """
    service = WatchlistScreeningService(_CONFIG, policies=[_FakePolicy("p1", 87.0, True)])
    result = _result_with_main_metrics({})
    huge_stock_code = "1" * 2000  # main_metricsが空でも上限を超えるほど巨大なstock_code
    result = dataclasses.replace(result, stock_code=huge_stock_code)
    entry = service.to_ranking_entry(result)
    assert entry is None
