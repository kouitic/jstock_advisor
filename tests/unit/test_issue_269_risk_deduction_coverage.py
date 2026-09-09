"""Issue #269(横断監査 N-01): リスク控除の coverage を実測値にし、confidence を生かす。

```
問題  `risk_deduction_scoring.py` が `coverage_ratio=1.0` を**無条件に返して**いた。

      overall = 0.25*CQ + 0.25*IT + 0.5*RD  であり RD の重みが 2 分の 1 のため、
      RD が常に 1.0 だと overall の下限が実効 0.5 相当になる。
      -> ★ **confidence が必ず MEDIUM 以上**になり、
         INSUFFICIENT_EVIDENCE / LOW が **到達不能**だった。
      confidence は較正（#28）と週次レビューが前提にする値であり、記録が構造的に歪む。
```

```
方式（USER の DESIGN_APPROVED 2026-09-08 + 管理者判断 JIRO-20260909-012）
  段階 1  RD に実 coverage を実装（**シグナル単位** / 重み = base_points /
          governance_and_listing_risk は NOT_APPLICABLE として分母から除外）
          + `RiskDeductionCategoryDetail.status` の実態化
  段階 2  **同一 PR** で confidence_thresholds を再設定
          （到達可能な最大 0.8356 を満点とした比例配分）
          + `risk_deduction_confidence_minimum` 0.70 -> 0.60
```

```
★ 段階 1 を単独で入れてはならない。
  overall の上限が 0.8356 へ下がるため、旧 `high_minimum = 0.95` が**到達不能**になる。
  = **死んだ閾値を 1 つ直して、別の閾値を 1 つ殺す**ことになる。
★ さらに `risk_deduction_confidence_minimum = 0.70` は RD の上限 0.6712 を**上回る**ため、
  そのままだと :110-118 の分岐が「常に偽」から**「常に真」へ反転**し、
  HIGH になった瞬間に必ず MEDIUM へ降格されて **HIGH が到達不能のまま**になる。
  -> 0.60 へ下げることが「全段が到達可能」の必要条件である（T-5 で固定）。
```

```
★ 不変条件  final_score / category は変わらない。should_notify は**増えない**
  （coverage は 1.0 から下がるだけなので coverage_satisfied は真->偽にしか動かない）。
★ 値はすべて架空値。実在の銘柄コード・所有者名は使わない。
★ Production への注入は行わない（判定関数を直接呼ぶ）。
```
"""

from __future__ import annotations

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    EvidenceCoverageStatus,
    EvidenceGroup,
    HoldingDecisionConfidenceLevel,
    TriggerStatus,
)
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    HoldingDecisionHardGate,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.signals.holding_decision_score import combine_holding_decision
from jstock_advisor.domain.signals.risk_deduction_scoring import (
    RiskDeductionInputs,
    score_risk_deduction,
)
from jstock_advisor.domain.signals.sell_signal import SellRuleEvaluation, SellRuleTriggerInputs

_CONFIG = load_config()
_RULES = _CONFIG.holding_decision
_RISK = _CONFIG.holding_decision_risk
_NO_GATE = HoldingDecisionHardGate(triggered=False)

#: リスク控除の対象シグナル（hard_gate_excluded を除く）。
_TARGET_SIGNALS = {
    name: cfg for name, cfg in _RISK.signals.items() if not cfg.hard_gate_excluded
}

#: 保有判断の経路では **必ず** NOT_EVALUATED になる 5 本（実測）。
#: `sell_signal.build_sell_rule_inputs_from_data()` の `override_rules` がこの 5 本で、
#: `holding_decision_service` は override 引数を 1 つも渡さないため。
_ALWAYS_NOT_EVALUATED = frozenset(
    {
        "interest_bearing_debt_surge",
        "unfavorable_dividend_policy_change",
        "large_earnings_guidance_downgrade",
        "long_term_holding_condition_unfavorable_change",
        "investment_premise_broken",
    }
)

#: データがあれば評価でき、無ければ NOT_EVALUATED になる 3 本。
_DATA_DEPENDENT = frozenset(
    {
        "continuous_operating_cashflow_decline",
        "continuous_operating_income_decline",
        "financial_health_severe_deterioration",
    }
)

#: 構造から決まる coverage の上限・下限（分母 146 / override 5 本 = 48 点）。
_COVERAGE_UPPER = 98 / 146  # 0.6712
_COVERAGE_LOWER = 56 / 146  # 0.3836


def _inputs(statuses: dict[str, TriggerStatus]) -> RiskDeductionInputs:
    """架空の売却ルール評価から RiskDeductionInputs を作る。"""
    evaluations = {
        name: SellRuleEvaluation(
            rule_name=name, status=status, evidence_group=EvidenceGroup.EARNINGS
        )
        for name, status in statuses.items()
    }
    return RiskDeductionInputs(
        sell_rule_inputs=SellRuleTriggerInputs(evaluations=evaluations)
    )


def _statuses(not_evaluated: frozenset[str]) -> dict[str, TriggerStatus]:
    return {
        name: (
            TriggerStatus.NOT_EVALUATED
            if name in not_evaluated
            else TriggerStatus.NOT_TRIGGERED
        )
        for name in _TARGET_SIGNALS
    }


def _combined(cq_cov: float, it_cov: float, rd_cov: float):
    """coverage だけを与えて合成する（score は不変性の確認用に固定値）。"""
    return combine_holding_decision(
        CompanyQualityScore(score=25.0, coverage_ratio=cq_cov),
        InvestmentThesisScore(score=25.0, coverage_ratio=it_cov),
        RiskDeductionScore(score=10.0, coverage_ratio=rd_cov),
        _NO_GATE,
        _RULES,
    )


# =============================================================================
# A) coverage の算出（受入条件 1）
# =============================================================================


def test_t1_coverage_is_below_one_when_signals_are_not_evaluated() -> None:
    """★ T-1: 実運用の状態（override 5 本が NOT_EVALUATED）で coverage が **1.0 未満**。

    **修正前は無条件に 1.0 だった**。本 Issue の回帰の本体である。
    """
    out = score_risk_deduction(_inputs(_statuses(_ALWAYS_NOT_EVALUATED)), _RISK)

    assert out.coverage_ratio < 1.0
    assert out.coverage_ratio == pytest.approx(_COVERAGE_UPPER)


def test_t2_coverage_lower_bound_when_data_dependent_signals_are_missing() -> None:
    """データ依存の 3 本も欠けた場合の下限（= 常に評価できる 4 本のみ）。"""
    out = score_risk_deduction(
        _inputs(_statuses(_ALWAYS_NOT_EVALUATED | _DATA_DEPENDENT)), _RISK
    )

    assert out.coverage_ratio == pytest.approx(_COVERAGE_LOWER)


def test_t3_governance_category_is_not_applicable_and_excluded_from_denominator() -> None:
    """★ T-6(計画): governance_and_listing_risk が **NOT_APPLICABLE** で分母から外れること。

    同カテゴリのシグナルは**すべて hard_gate_excluded** であり、リスク控除の対象が
    **1 本も無い**（データ不足ではなく評価対象外）。
    ★ 分母へ入れると cap 20 / 全体 166 の分だけ coverage が構造的に頭打ちになる。
    """
    out = score_risk_deduction(_inputs(_statuses(frozenset())), _RISK)

    governance = next(c for c in out.categories if c.category == "governance_and_listing_risk")
    assert governance.status is EvidenceCoverageStatus.NOT_APPLICABLE
    # 分母から外れているため、全シグナル評価済みなら coverage はちょうど 1.0 になる
    assert out.coverage_ratio == pytest.approx(1.0)


def test_t4_override_signals_stay_in_the_denominator() -> None:
    """★ T-7(計画): override 未指定の 5 本は **分母に残り分子から外れる**こと。

    「評価できなかった」を「該当しない」と同じ扱いで捨てないこと（本 Issue の根）。
    分母から外すと coverage が 1.0 に戻ってしまう。
    """
    total = sum(cfg.base_points for cfg in _TARGET_SIGNALS.values())
    missing = sum(_TARGET_SIGNALS[name].base_points for name in _ALWAYS_NOT_EVALUATED)
    assert total == 146
    assert missing == 48

    out = score_risk_deduction(_inputs(_statuses(_ALWAYS_NOT_EVALUATED)), _RISK)

    assert out.coverage_ratio == pytest.approx((total - missing) / total)


def test_t5_category_status_reflects_reality() -> None:
    """★ `RiskDeductionCategoryDetail.status` が実態を表すこと（DoD 項目 5）。

    修正前は**無条件 EVALUATED** を書いており、「評価できなかった」ことが
    ★ **記録に残らなかった**（SHADOW 記録からの前後比較ができない原因）。
    """
    # structural_change の 2 本は override 未指定 = 両方 NOT_EVALUATED
    out = score_risk_deduction(_inputs(_statuses(_ALWAYS_NOT_EVALUATED)), _RISK)

    by_category = {c.category: c.status for c in out.categories}
    assert by_category["structural_change"] is EvidenceCoverageStatus.NOT_EVALUATED
    assert by_category["governance_and_listing_risk"] is EvidenceCoverageStatus.NOT_APPLICABLE
    # 一部でも評価できていれば EVALUATED
    assert by_category["shareholder_return_deterioration"] is EvidenceCoverageStatus.EVALUATED


def test_t5b_suspected_counts_as_evaluated() -> None:
    """★ SUSPECTED は「評価できた」側であること。

    SUSPECTED は「一次情報が未確認の推測」であって「データが無くて判定できなかった」
    ではない（TriggerStatus の docstring）。NOT_EVALUATED **だけ**が後者を表す。
    ここを混ぜると、推測が立った銘柄ほど根拠不足に見えるという逆転が起きる。
    """
    statuses = dict.fromkeys(_TARGET_SIGNALS, TriggerStatus.SUSPECTED)

    out = score_risk_deduction(_inputs(statuses), _RISK)

    assert out.coverage_ratio == pytest.approx(1.0)


def test_t5c_missing_evaluation_is_treated_as_not_evaluated() -> None:
    """評価そのものが存在しないシグナルは **fail-closed**（不足として計上）すること。"""
    partial = {
        name: TriggerStatus.NOT_TRIGGERED
        for name in _TARGET_SIGNALS
        if name not in _ALWAYS_NOT_EVALUATED
    }

    out = score_risk_deduction(_inputs(partial), _RISK)

    assert out.coverage_ratio == pytest.approx(_COVERAGE_UPPER)


# =============================================================================
# B) confidence の 4 段がすべて到達可能（受入条件 3 / 改訂後の 4）
# =============================================================================


@pytest.mark.parametrize(
    ("cq", "it", "rd", "expected"),
    [
        (1.00, 1.00, _COVERAGE_UPPER, HoldingDecisionConfidenceLevel.HIGH),
        (0.90, 0.90, _COVERAGE_UPPER, HoldingDecisionConfidenceLevel.MEDIUM),
        (0.60, 0.60, _COVERAGE_UPPER, HoldingDecisionConfidenceLevel.LOW),
        (0.60, 0.60, _COVERAGE_LOWER, HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE),
    ],
    ids=["high", "medium", "low", "insufficient"],
)
def test_t6_all_four_confidence_levels_are_reachable(
    cq: float, it: float, rd: float, expected: HoldingDecisionConfidenceLevel
) -> None:
    """★★ T-4(計画): **4 段すべてに到達する**こと（改訂後の受入条件 4）。

    **修正前は MEDIUM 以上しか出なかった**（RD が常に 1.0 だったため）。
    ★ 1 段でも欠ければ失敗する。閾値を旧値へ戻すと HIGH と INSUFFICIENT が落ちる。
    """
    assert _combined(cq, it, rd).confidence is expected


def test_t7_high_is_not_demoted_by_risk_deduction_confidence_minimum() -> None:
    """★★ T-5(計画): HIGH が `risk_deduction_confidence_minimum` で**不当に降格されない**こと。

    ★ この閾値が RD の上限（0.6712）を上回っていると、
      HIGH になった瞬間に**必ず** MEDIUM へ降格され、HIGH が永久に出ない。
      旧値 0.70 のままだとこのテストが落ちる（本 Issue で見つけた 2 つ目の障害）。
    """
    assert _RULES.coverage_thresholds.risk_deduction_confidence_minimum < _COVERAGE_UPPER

    out = _combined(1.00, 1.00, _COVERAGE_UPPER)

    assert out.confidence is HoldingDecisionConfidenceLevel.HIGH


def test_t7b_high_is_still_demoted_when_risk_evidence_is_thin() -> None:
    """★ 逆側: リスク側の根拠が薄いときは**従来どおり降格する**こと。

    降格そのものを無効化したのではなく、**閾値を範囲の中へ入れ直した**だけである。
    ここが通らないとガードを壊したことになる。
    """
    thin = _RULES.coverage_thresholds.risk_deduction_confidence_minimum - 0.01
    # overall >= high_minimum になるよう CQ/IT を満点にする
    out = _combined(1.00, 1.00, thin)

    assert out.coverage.overall >= _RULES.confidence_thresholds.high_minimum
    assert out.confidence is HoldingDecisionConfidenceLevel.MEDIUM


@pytest.mark.parametrize(
    ("overall_target", "expected"),
    [
        (0.79, HoldingDecisionConfidenceLevel.HIGH),
        (0.78, HoldingDecisionConfidenceLevel.MEDIUM),
        (0.67, HoldingDecisionConfidenceLevel.MEDIUM),
        (0.66, HoldingDecisionConfidenceLevel.LOW),
        (0.50, HoldingDecisionConfidenceLevel.LOW),
        (0.49, HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE),
    ],
    ids=["high_exact", "just_below_high", "medium_exact", "just_below_medium",
         "low_exact", "just_below_low"],
)
def test_t8_confidence_threshold_boundaries(
    overall_target: float, expected: HoldingDecisionConfidenceLevel
) -> None:
    """境界（DoD 項目 1）: 新しい 3 閾値の **ちょうど / 1 つ手前**。

    ★ overall は 0.25*CQ + 0.25*IT + 0.5*RD。RD を上限に固定し CQ = IT を動かして
      目標の overall を作る（RD 由来の降格が混ざらないようにするため）。
    """
    rd = _COVERAGE_UPPER
    cq_it = (overall_target - 0.5 * rd) / 0.5  # 0.25*(cq+it) = 0.25*2*x

    assert _combined(cq_it, cq_it, rd).confidence is expected


# =============================================================================
# C) 不変条件（受入条件 5）
# =============================================================================


def test_t9_final_score_and_category_are_unchanged_by_coverage() -> None:
    """★ T-1/T-2(計画): coverage をどう動かしても **final_score と category は不変**。

    `RiskDeductionScore.score` は coverage を一切参照しない（式で保証される）。
    """
    baseline = _combined(1.0, 1.0, 1.0)
    lowered = _combined(1.0, 1.0, _COVERAGE_LOWER)

    assert lowered.final_score == baseline.final_score
    assert lowered.category is baseline.category


def test_t10_should_notify_never_becomes_more_permissive() -> None:
    """★★ T-3(計画): coverage が下がって **通知が増えることは無い**こと。

    should_notify = score_threshold_met AND (coverage_satisfied OR hard_gate)。
    score_threshold_met は final_score のみに依存し不変で、
    coverage_satisfied は coverage が下がると**真 -> 偽にしか動かない**。
    ★ 「減る」ことは起こりえる（overall_minimum が初めて拘束するため）。
    """
    # ★ 軸別 gate（company_quality / investment_thesis >= 0.60）の**境界**に置く。
    #   ここが overall_minimum の効き方を確かめられる唯一の場所である
    #   （CQ / IT が満点だと RD が下限でも overall が 0.69 となり落ちない）。
    def _outcome(rd_coverage: float):
        return combine_holding_decision(
            CompanyQualityScore(score=0.0, coverage_ratio=0.60),
            InvestmentThesisScore(score=0.0, coverage_ratio=0.60),
            RiskDeductionScore(score=50.0, coverage_ratio=rd_coverage),
            _NO_GATE,
            _RULES,
        )

    notifying = _outcome(1.0)
    assert notifying.should_notify is True

    lowered = _outcome(_COVERAGE_LOWER)

    assert lowered.final_score == notifying.final_score
    assert lowered.should_notify is False  # 減る側へ動く（増えない）


def test_t11_overall_minimum_actually_constrains_at_the_gate_boundary() -> None:
    """★ T-8(計画): 3 軸合成後の overall が `overall_minimum` を**実際に拘束する**こと。

    **修正前は一度も拘束しなかった**（RD が 1.0 固定のため overall >= 0.80 が自動成立）。
    軸別 gate の境界 CQ = IT = 0.60 では overall >= 0.60 ⟺ RD >= 0.60 となる。
    """
    minimum = _RULES.coverage_thresholds.overall_minimum

    passing = _combined(0.60, 0.60, 0.62)
    failing = _combined(0.60, 0.60, 0.58)

    assert passing.coverage.overall >= minimum
    assert passing.coverage_satisfied is True
    assert failing.coverage.overall < minimum
    assert failing.coverage_satisfied is False


# =============================================================================
# D) negative check — 旧 config へ戻すと到達可能性が壊れること
# =============================================================================


def test_t12_old_thresholds_would_break_reachability() -> None:
    """★ 旧閾値のままでは HIGH が到達不能であることを、**式で**固定する。

    段階 1 を単独で入れてはならない理由そのもの。
    ここが崩れたら「閾値を戻しても大丈夫」という誤読が生まれる。
    """
    overall_max = 0.25 * 1.0 + 0.25 * 1.0 + 0.5 * _COVERAGE_UPPER

    assert overall_max == pytest.approx(0.8356, abs=1e-4)
    assert overall_max < 0.95  # 旧 high_minimum には**届かない**
    assert overall_max >= _RULES.confidence_thresholds.high_minimum  # 新 high_minimum は届く

    # 旧 risk_deduction_confidence_minimum 0.70 は RD の上限を上回る = 常に降格
    assert _COVERAGE_UPPER < 0.70
    assert _RULES.coverage_thresholds.risk_deduction_confidence_minimum <= _COVERAGE_UPPER


def test_t13_config_values_match_the_documented_derivation() -> None:
    """★ config の新値が「到達可能な最大 0.8356 の比例配分」であることを固定する。

    数値を書き換えるときに**導出を無視した恣意的な値**が入らないようにする。
    """
    overall_max = 0.5 + 0.5 * _COVERAGE_UPPER
    thresholds = _RULES.confidence_thresholds

    assert thresholds.high_minimum == pytest.approx(0.95 * overall_max, abs=0.005)
    assert thresholds.medium_minimum == pytest.approx(0.80 * overall_max, abs=0.005)
    assert thresholds.low_minimum == pytest.approx(0.60 * overall_max, abs=0.005)
