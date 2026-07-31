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
    _MAX_RANKING_ENTRY_BYTES,
    WatchlistScreeningResult,
    WatchlistScreeningService,
)

_CONFIG = load_config()
_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


def _fake_input() -> SimpleNamespace:
    return SimpleNamespace(
        missing_required_fields=[],
        missing_scoring_fields=[],
        dividend_yield_pct=4.2,
        equity_ratio_pct=55.0,
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
    assert entry.stock_code == "1234"
    assert entry.total_score == 87.0
    assert entry.policy_scores == {"p1": 87.0}
    assert entry.matched_criteria == [MatchedCriterion.HIGH_DIVIDEND_YIELD]


def test_to_ranking_entry_shrinks_main_metrics_when_over_byte_budget() -> None:
    service = WatchlistScreeningService(_CONFIG, policies=[_FakePolicy("p1", 87.0, True)])
    huge_metrics = {f"指標{i}": "x" * 100 for i in range(20)}
    result = WatchlistScreeningResult(
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
        main_metrics=huge_metrics,
    )
    entry = service.to_ranking_entry(result)
    assert len(entry.model_dump_json().encode("utf-8")) <= _MAX_RANKING_ENTRY_BYTES
    assert len(entry.main_metrics) < len(huge_metrics)
