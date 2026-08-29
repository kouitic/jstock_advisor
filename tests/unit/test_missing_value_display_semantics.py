"""Issue #55 Phase B-2: 欠測・不正値を「0」と断定しない(N5 / N6 / N7 / N8 / N11 / N14)。

Phase B-1 で確立した「真の0」「不明」「制度なし」「評価不能」の区別を、
監査ログ・設定・表示層でも潰さないことを固定する。

  N5  profit-taking の audit log における `or 0`(不明と0年の同一化)
  N6  min_consecutive_years の二重管理と、未設定の沈黙無効化
  N7  平均取得単価が0以下のときの誤った損益表示(F-G4)
  N8  config_values_used のキー欠落を `+0点` と断定する表示(F-G5)
  N11 within_business_days が None のときの意味
  N14 当該 defensive formatter の到達性
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pydantic
import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import MitigatingFactor, MitigatingFactors
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    _apply_mitigating_factors,
)
from jstock_advisor.services.line_notification_service import render_notification_preview

_NOW = dt.datetime(2026, 8, 29, 9, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def app_config():
    return load_config()


# ============================================================================
# N6 / N11: MitigatingFactor のパラメータ欠落を起動時 fail-fast にする
# ============================================================================


def _factors(**overrides: MitigatingFactor) -> MitigatingFactors:
    base: dict[str, MitigatingFactor] = {
        "fair_value_rising_with_earnings_growth": MitigatingFactor(
            enabled=True, downgrade_levels=1
        ),
        "continuous_dividend_increase": MitigatingFactor(
            enabled=True, downgrade_levels=1, min_consecutive_years=2
        ),
        "progressive_dividend_or_doe_policy": MitigatingFactor(enabled=True, downgrade_levels=1),
        "long_term_holding_benefit_imminent": MitigatingFactor(
            enabled=True, downgrade_levels=1, within_business_days=60
        ),
        "few_reinvestment_alternatives": MitigatingFactor(enabled=True, downgrade_levels=1),
        "nisa_long_term_benefit": MitigatingFactor(enabled=True, downgrade_levels=1),
    }
    base.update(overrides)
    return MitigatingFactors(**base)


def test_current_config_loads_and_keeps_expected_values(app_config) -> None:
    """本番configが起動時検証を通り、値が仕様どおりであること(回帰)。"""
    mf = app_config.profit_taking.mitigating_factors

    assert mf.continuous_dividend_increase.min_consecutive_years == 2
    assert mf.long_term_holding_benefit_imminent.within_business_days == 60


def test_min_consecutive_years_valid_value_is_accepted() -> None:
    factors = _factors()

    assert factors.continuous_dividend_increase.min_consecutive_years == 2


def test_min_consecutive_years_missing_fails_fast_when_enabled() -> None:
    """enabled: true のまま設定行を消しても、黙って無効化されず起動時に失敗する。"""
    with pytest.raises(pydantic.ValidationError, match="min_consecutive_years"):
        _factors(
            continuous_dividend_increase=MitigatingFactor(enabled=True, downgrade_levels=1)
        )


def test_min_consecutive_years_zero_fails_fast() -> None:
    """0は正当な設定値ではない(成立時に「実績で0年連続増配」という偽文言になる)。"""
    with pytest.raises(pydantic.ValidationError, match="min_consecutive_years"):
        _factors(
            continuous_dividend_increase=MitigatingFactor(
                enabled=True, downgrade_levels=1, min_consecutive_years=0
            )
        )


def test_min_consecutive_years_may_be_omitted_when_disabled() -> None:
    """enabled: false なら未設定でよい(「この要因を使わない」という明示)。"""
    factors = _factors(
        continuous_dividend_increase=MitigatingFactor(enabled=False, downgrade_levels=1)
    )

    assert factors.continuous_dividend_increase.min_consecutive_years is None


def test_within_business_days_missing_fails_fast_when_enabled() -> None:
    """N11: Noneは「データ不足」ではなく設定ミス。enabled時は起動時に失敗させる。"""
    with pytest.raises(pydantic.ValidationError, match="within_business_days"):
        _factors(
            long_term_holding_benefit_imminent=MitigatingFactor(enabled=True, downgrade_levels=1)
        )


def test_within_business_days_may_be_omitted_when_disabled() -> None:
    factors = _factors(
        long_term_holding_benefit_imminent=MitigatingFactor(enabled=False, downgrade_levels=1)
    )

    assert factors.long_term_holding_benefit_imminent.within_business_days is None


# ============================================================================
# N5: 連続増配年数の「不明」と「0年確定」を判定・記録で同一視しない
# ============================================================================


def _evaluate_years(app_config, years: int | None) -> tuple[int, list[str]]:
    """緩和要因の適用結果を (降格レベル数, 適用理由) で返す。

    `_apply_mitigating_factors` は (降格後level, 適用理由) を返すため、
    元のlevelとの差を降格レベル数として扱う。
    """
    from jstock_advisor.domain.signals.profit_taking import _Level

    level = _Level.FULL
    downgraded, applied = _apply_mitigating_factors(
        level,
        MitigatingFactorInputs(continuous_dividend_increase_years=years),
        app_config.profit_taking.mitigating_factors,
    )
    return level.value - downgraded.value, applied


def test_unknown_years_does_not_apply_mitigating_factor(app_config) -> None:
    """不明(None)は緩和要因に該当させない。"""
    downgrade, applied = _evaluate_years(app_config, None)

    assert downgrade == 0
    assert not any("連続増配" in a for a in applied)


def test_confirmed_zero_years_does_not_apply_mitigating_factor(app_config) -> None:
    """0年確定も該当しない(不明と判定結果は同じ。区別するのは記録の粒度のみ)。"""
    downgrade, applied = _evaluate_years(app_config, 0)

    assert downgrade == 0
    assert not any("連続増配" in a for a in applied)


def test_years_at_threshold_applies_mitigating_factor(app_config) -> None:
    """正値(閾値以上)は従来どおり該当する(回帰)。"""
    downgrade, applied = _evaluate_years(app_config, 2)

    assert downgrade > 0
    assert any("2年連続増配" in a for a in applied)


def test_years_below_threshold_does_not_apply(app_config) -> None:
    downgrade, _ = _evaluate_years(app_config, 1)

    assert downgrade == 0


def test_mitigating_factor_inputs_defaults_to_unknown_not_zero() -> None:
    """既定値が0ではなくNone(不明)であること。0を既定にすると欠測が0年に化ける。"""
    assert MitigatingFactorInputs().continuous_dividend_increase_years is None


# ============================================================================
# N7 / N8 / N14: 保有判断通知の表示
# ============================================================================


def _holding_decision_recommendation(
    *,
    average_purchase_price: Decimal | None = Decimal("1000"),
    shares: int | None = 100,
    config_values_used: dict | None = None,
) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-hd-1",
        stock_code="2914",
        stock_name="テスト株式会社",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL_CONSIDERATION,
        price_at_recommendation=Decimal("1200"),
        average_purchase_price_at_recommendation=average_purchase_price,
        shares_at_recommendation=shares,
        total_score=40.0,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        config_values_used=(
            config_values_used
            if config_values_used is not None
            else {
                "final_score": 42.0,
                "company_quality_score": 30.0,
                "investment_thesis_score": 25.0,
                "risk_deduction_score": 13.0,
            }
        ),
    )


# --- N14: 到達性 -------------------------------------------------------------


def test_holding_decision_formatter_is_reachable_via_preview() -> None:
    """N14: 当該formatterはdead codeではなく、診断用の実経路から到達できる。

    `render_notification_preview()`(before/afterレポートが
    `before_after_report_service.py` から呼ぶ)は、短文エンジンへ分岐せず
    `_format_message()` を直接呼ぶため、保有判断型でこのformatterに到達する。
    削除せず、以下の表示契約をbehaviorとして固定する。
    """
    body = render_notification_preview(_holding_decision_recommendation())

    assert "保有判断スコア：" in body
    assert "スコア内訳：" in body


# --- N8: キー欠落を0点と断定しない -------------------------------------------


def test_final_score_present_is_rendered() -> None:
    body = render_notification_preview(_holding_decision_recommendation())

    assert "保有判断スコア：+42点" in body


def test_final_score_zero_is_rendered_as_zero() -> None:
    """確定した0点は0点として表示する(不明へ倒さない)。"""
    body = render_notification_preview(
        _holding_decision_recommendation(config_values_used={"final_score": 0.0})
    )

    assert "保有判断スコア：+0点" in body


def test_final_score_negative_is_rendered() -> None:
    body = render_notification_preview(
        _holding_decision_recommendation(config_values_used={"final_score": -15.0})
    )

    assert "保有判断スコア：-15点" in body


def test_missing_final_score_is_not_rendered_as_zero() -> None:
    """キー欠落は「不明」。`+0点`は「妥当な水準」と正反対に誤読される。"""
    body = render_notification_preview(
        _holding_decision_recommendation(config_values_used={})
    )

    assert "保有判断スコア：不明" in body
    assert "+0点" not in body


def test_missing_risk_deduction_score_line_is_omitted() -> None:
    """欠落した内訳行は出さない。`0／100`は「リスクなし」と誤読され最も危険。"""
    body = render_notification_preview(
        _holding_decision_recommendation(
            config_values_used={
                "final_score": 42.0,
                "company_quality_score": 30.0,
                "investment_thesis_score": 25.0,
            }
        )
    )

    assert "リスク控除" not in body
    assert "0／100" not in body
    assert "・企業品質：30／50" in body


def test_zero_risk_deduction_score_is_still_rendered() -> None:
    """確定した0は表示する(欠落と混同しない)。"""
    body = render_notification_preview(
        _holding_decision_recommendation(
            config_values_used={
                "final_score": 42.0,
                "company_quality_score": 30.0,
                "investment_thesis_score": 25.0,
                "risk_deduction_score": 0.0,
            }
        )
    )

    assert "・リスク控除：0／100" in body


def test_missing_score_keys_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """欠落を黙って隠さず運用者が気づけるようにする。"""
    with caplog.at_level("WARNING"):
        render_notification_preview(
            _holding_decision_recommendation(config_values_used={})
        )

    assert any("config_values_used" in r.message for r in caplog.records)


# --- N7: 平均取得単価が不正なときの損益表示 -----------------------------------


def test_valid_average_price_renders_pnl() -> None:
    body = render_notification_preview(_holding_decision_recommendation())

    assert "含み損益：+20,000円" in body


def test_zero_average_price_renders_not_computable() -> None:
    """avg==0 では率も金額も断定しない(pnlが時価総額そのものになるため)。"""
    body = render_notification_preview(
        _holding_decision_recommendation(average_purchase_price=Decimal("0"))
    )

    assert "含み損益：算出不可" in body
    assert "+0.0%" not in body
    assert "+120,000円" not in body


def test_negative_average_price_renders_not_computable() -> None:
    body = render_notification_preview(
        _holding_decision_recommendation(average_purchase_price=Decimal("-100"))
    )

    assert "含み損益：算出不可" in body


def test_missing_average_price_omits_pnl_line() -> None:
    """Noneのときは従来どおり損益行自体を出さない(挙動不変の回帰)。"""
    body = render_notification_preview(
        _holding_decision_recommendation(average_purchase_price=None)
    )

    assert "含み損益" not in body
