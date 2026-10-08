"""Issue #742: RuleProposalService の評価件数の取得で、全評価を list へ読み込まない。

バックテスト未対応の経路(current_value / proposed_value が数値でない、または
バックテストが supported = False)では、評価の「件数」だけが必要である。従来は
`len(self._evaluations.list_all())` で全評価を list へ保持していたため、
`iter_all()`(1件ずつ遅延生成)を数える形へ改めた。振る舞い(件数・エラー
メッセージ・RuleProposal に記録する値)は不変である。

固定するもの:
- 件数の取得に `list_all()` を使わない(使うと AssertionError で落ちる spy)
- 件数は従来(`list_all()` の長さ)と一致する(0・最低件数の直前/ちょうど/直後・多数)
- RuleProposal.evaluation_count に記録される値が、評価の総数と一致する
- エラーメッセージが従来と同一である
- 数えている間、評価を同時に保持しない(全件を list へ読み込む実装は落ちる)
- バックテスト対応の経路は変更しない(`backtest_result.evaluation_count_current` を使い、
  評価の repository を本サービスは走査しない)
"""

from __future__ import annotations

import datetime as dt
import weakref
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationLabel,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.rule_version import RuleProposal
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.rule_version_repository import (
    RuleProposalRepository,
)
from jstock_advisor.services.backtest_service import BacktestService
from jstock_advisor.services.rule_proposal_service import RuleProposalService
from tests.factories import build_recommendation

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
_MIN = RuleProposal.MIN_EVALUATION_COUNT_FOR_PROPOSAL
_SUPPORTED_TARGET = "screening.total_yield.min_total_yield_pct"
_UNSUPPORTED_TARGET = "sell.rules.some_new_rule.enabled"


class _SpyEvaluationRepository(EvaluationResultRepository):
    """list_all() を呼ぶと失敗し、iter_all() の呼び出し回数を数える。"""

    def __init__(self, store_dir: Path) -> None:
        super().__init__(store_dir=store_dir)
        self.iter_all_calls = 0

    def list_all(self) -> list[EvaluationResult]:
        raise AssertionError("件数の取得に list_all() を使ってはならない(Issue #742)")

    def iter_all(self) -> Iterator[EvaluationResult]:
        self.iter_all_calls += 1
        return super().iter_all()


class _Probe:
    """生存数を数えるための軽量な代役(弱参照を持てる通常のクラス)。"""


class _LazyProbeRepository(EvaluationResultRepository):
    """iter_all() が _Probe を1件ずつ遅延生成し、同時に生存している最大数を記録する。"""

    def __init__(self, store_dir: Path, total: int) -> None:
        super().__init__(store_dir=store_dir)
        self._total = total
        self.alive = 0
        self.peak_alive = 0
        self._refs: set[weakref.ReferenceType[_Probe]] = set()

    def list_all(self) -> list[EvaluationResult]:
        raise AssertionError("件数の取得に list_all() を使ってはならない(Issue #742)")

    def _released(self, ref: weakref.ReferenceType[_Probe]) -> None:
        self._refs.discard(ref)
        self.alive -= 1

    def iter_all(self) -> Iterator[EvaluationResult]:
        for _ in range(self._total):
            probe = _Probe()
            self.alive += 1
            self.peak_alive = max(self.peak_alive, self.alive)
            self._refs.add(weakref.ref(probe, self._released))
            yield probe  # type: ignore[misc]
            del probe


def _evaluation(i: int, recommendation_id: str) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=f"e-{i}",
        recommendation_id=recommendation_id,
        horizon_business_days=20,
        evaluated_at=_NOW,
        evaluation_date=_NOW.date(),
        price_at_evaluation=Decimal("1100"),
        price_return_pct=5.0,
        evaluation_label=EvaluationLabel.SUCCESS,
        label_evidence="x",
    )


def _seed_evaluations(tmp_path: Path, count: int) -> None:
    repo = EvaluationResultRepository(store_dir=tmp_path)
    for i in range(count):
        repo.save(_evaluation(i, f"rec-{i}"))


def _seed_recommendations(tmp_path: Path, count: int) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    for i in range(count):
        repo.save(
            build_recommendation(
                recommendation_id=f"rec-{i}",
                stock_code="2914",
                stock_name="test",
                recommended_at=_NOW,
                recommendation_type=RecommendationType.BUY,
                price_at_recommendation=Decimal("1000"),
                total_yield_pct_at_recommendation=3.6 + (i % 5) * 0.5,
                confidence=ConfidenceLevel.HIGH,
                rule_version="v1",
            )
        )


def _service(
    tmp_path: Path,
) -> tuple[RuleProposalService, _SpyEvaluationRepository]:
    spy = _SpyEvaluationRepository(tmp_path)
    backtest = BacktestService(
        recommendation_repository=RecommendationRepository(store_dir=tmp_path),
        evaluation_repository=EvaluationResultRepository(store_dir=tmp_path),
    )
    service = RuleProposalService(
        proposal_repository=RuleProposalRepository(store_dir=tmp_path),
        backtest_service=backtest,
        evaluation_repository=spy,
    )
    return service, spy


def _create(
    service: RuleProposalService, *, current: object, proposed: object, target: str
) -> RuleProposal:
    return service.create_proposal(
        target=target,
        current_value=current,
        proposed_value=proposed,
        reason="test",
        risk_impact="low",
        overfitting_risk_assessment="low",
        rollback_condition="revert",
        now=_NOW,
    )


# バックテスト未対応の2経路(数値でない値 / 数値だが未対応の target)
_UNSUPPORTED_PATHS = [
    pytest.param("on", "off", "sell.rules.some_new_rule.mode", id="non-numeric-values"),
    pytest.param(0.0, 1.0, _UNSUPPORTED_TARGET, id="numeric-unsupported-target"),
]


@pytest.mark.parametrize(("current", "proposed", "target"), _UNSUPPORTED_PATHS)
@pytest.mark.parametrize("count", [0, _MIN - 1, _MIN, _MIN + 1, _MIN + 15])
def test_evaluation_count_matches_all_evaluations_without_list_all(
    tmp_path: Path, count: int, current: object, proposed: object, target: str
) -> None:
    """T7a/T7b/T7c: 件数・記録される値・エラーメッセージが従来(list_all の長さ)と同一。"""
    _seed_evaluations(tmp_path, count)
    expected = len(EvaluationResultRepository(store_dir=tmp_path).list_all())
    assert expected == count  # 前提: 従来の取り方での件数
    service, spy = _service(tmp_path)

    if count < _MIN:
        with pytest.raises(ValueError) as excinfo:
            _create(service, current=current, proposed=proposed, target=target)
        assert str(excinfo.value) == (
            f"評価件数が不足しているため提案を作成できません(現在{count}件、最低{_MIN}件必要)"
        )
    else:
        proposal = _create(service, current=current, proposed=proposed, target=target)
        assert proposal.evaluation_count == expected
        assert proposal.proposed_rule_backtest_performance["supported"] is False
    assert spy.iter_all_calls == 1  # 1回の走査で数える


def test_backtest_supported_path_does_not_scan_evaluations(tmp_path: Path) -> None:
    """バックテスト対応の経路は変更しない: backtest_result.evaluation_count_current を使い、
    本サービスの評価 repository は走査しない。"""
    count = RuleProposal.MIN_EVALUATION_COUNT_FOR_THRESHOLD_CHANGE
    _seed_recommendations(tmp_path, count)
    _seed_evaluations(tmp_path, count)
    service, spy = _service(tmp_path)

    proposal = _create(service, current=3.5, proposed=4.0, target=_SUPPORTED_TARGET)

    assert proposal.evaluation_count == count
    assert spy.iter_all_calls == 0


def test_counting_does_not_hold_all_evaluations_at_once(tmp_path: Path) -> None:
    """件数だけが要るので、評価を同時に保持しない(全件を list へ読み込むと peak が総数になる)。"""
    total = _MIN + 20
    lazy = _LazyProbeRepository(tmp_path, total)
    service = RuleProposalService(
        proposal_repository=RuleProposalRepository(store_dir=tmp_path),
        backtest_service=BacktestService(
            recommendation_repository=RecommendationRepository(store_dir=tmp_path),
            evaluation_repository=EvaluationResultRepository(store_dir=tmp_path),
        ),
        evaluation_repository=lazy,
    )

    proposal = _create(service, current="on", proposed="off", target="sell.rules.x.mode")

    assert proposal.evaluation_count == total
    assert lazy.peak_alive <= 2  # 数えている最中に同時に生きているのは高々 1〜2 件
