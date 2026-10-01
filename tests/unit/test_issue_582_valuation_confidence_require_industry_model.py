"""Issue #582: determine_valuation_confidence()へrequire_industry_model引数を追加する。

#208の調査により、industry_model_appliedは恒久的にFalseとなることが判明しており、
本関数のHIGH tierはProductionで一度も到達していない。require_industry_model=Falseを
渡すと、industry_model_appliedをreasons_not_highの判定対象から除外する。既定値Trueは
既存呼び出し元(buy_signal_service.py:884、production唯一の呼び出し箇所)の挙動を
完全に保つ(LOCK_LEVEL_1のCOMPATIBILITY_EVIDENCE)。

時刻・営業日には触れない(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.valuation.valuation_confidence import determine_valuation_confidence

_MEDIUM_MAX = 1.60
_ANCHOR_BLOCK = 50.0

_BASE_KWARGS: dict[str, object] = dict(
    methods_used_count=3,
    dispersion_ratio=1.0,
    dispersion_medium_max=_MEDIUM_MAX,
    dispersion_anchor_block=_ANCHOR_BLOCK,
    industry_model_applied=False,
    uses_simplified_dcf=False,
    normalized_eps_confidence=None,
)


def test_default_omitted_require_industry_model_keeps_existing_behavior() -> None:
    """既存呼び出し(引数省略)は従来どおりindustry_model_appliedを評価する(回帰固定)。"""
    result = determine_valuation_confidence(**_BASE_KWARGS)  # type: ignore[arg-type]

    assert result.level is ConfidenceLevel.MEDIUM
    assert "業種別適正価格モデル未適用" in result.reasons_not_high


def test_require_industry_model_true_is_identical_to_omitting_it() -> None:
    """require_industry_model=Trueを明示しても、省略時と完全に同じ結果になる。"""
    omitted = determine_valuation_confidence(**_BASE_KWARGS)  # type: ignore[arg-type]
    explicit_true = determine_valuation_confidence(
        **_BASE_KWARGS,  # type: ignore[arg-type]
        require_industry_model=True,
    )

    assert explicit_true == omitted


def test_require_industry_model_false_removes_only_the_industry_model_reason() -> None:
    """require_industry_model=Falseはindustry_model_appliedの理由のみを除外し、HIGHへ格上げする。

    他の理由(dispersion/簡易DCF/平準化EPS信頼度)が無い場合、industry_model_applied=Falseの
    唯一の理由が除外されればHIGHへ到達する。
    """
    result = determine_valuation_confidence(
        **_BASE_KWARGS,  # type: ignore[arg-type]
        require_industry_model=False,
    )

    assert result.level is ConfidenceLevel.HIGH
    assert result.reasons_not_high == []


def test_require_industry_model_false_still_reports_other_reasons() -> None:
    """require_industry_model=Falseでも、他の理由(簡易DCF使用)はそのまま残る(MEDIUMのまま)。"""
    kwargs = dict(_BASE_KWARGS, uses_simplified_dcf=True)

    result = determine_valuation_confidence(**kwargs, require_industry_model=False)  # type: ignore[arg-type]

    assert result.level is ConfidenceLevel.MEDIUM
    assert result.reasons_not_high == ["簡易DCF(固定割引率・固定成長率の前提)を使用"]
    assert "業種別適正価格モデル未適用" not in result.reasons_not_high


def test_require_industry_model_false_does_not_weaken_the_low_gates() -> None:
    """require_industry_model=Falseでも、方式数不足によるLOW判定は緩まない(安全側は不変)。"""
    kwargs = dict(_BASE_KWARGS, methods_used_count=1)

    result = determine_valuation_confidence(**kwargs, require_industry_model=False)  # type: ignore[arg-type]

    assert result.level is ConfidenceLevel.LOW


def test_require_industry_model_false_does_not_weaken_the_dispersion_block_gate() -> None:
    """require_industry_model=Falseでも、ばらつきによるanchor_block判定は緩まない(安全側は不変)。"""
    kwargs = dict(_BASE_KWARGS, dispersion_ratio=_ANCHOR_BLOCK + 1)

    result = determine_valuation_confidence(**kwargs, require_industry_model=False)  # type: ignore[arg-type]

    assert result.level is ConfidenceLevel.LOW


def test_require_industry_model_true_when_industry_model_applied_is_unaffected() -> None:
    """industry_model_applied=Trueの場合、require_industry_modelの値によらず結果は同じ。"""
    kwargs = dict(_BASE_KWARGS, industry_model_applied=True)

    with_true = determine_valuation_confidence(**kwargs, require_industry_model=True)  # type: ignore[arg-type]
    with_false = determine_valuation_confidence(**kwargs, require_industry_model=False)  # type: ignore[arg-type]

    assert with_true == with_false
    assert with_true.level is ConfidenceLevel.HIGH
