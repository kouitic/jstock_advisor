"""利益保全(Profit Protection)判定の単体テスト(2026-08、サンリオ8136回帰含む)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.signals.profit_protection import compute_profit_protection_metrics
from jstock_advisor.interfaces.types import PriceBar

_CONFIG = load_config().profit_taking.profit_protection
_ENTRY = dt.date(2026, 1, 5)
_AS_OF = dt.date(2026, 8, 14)


def _bars(peak_high: Decimal, peak_date: dt.date = dt.date(2026, 6, 1)) -> list[PriceBar]:
    """保有開始日ちょうどのバーと、指定した高値を含むバーの2本を返す。"""
    return [
        PriceBar(
            date=_ENTRY,
            open=Decimal("900"),
            high=Decimal("910"),
            low=Decimal("890"),
            close=Decimal("900"),
            volume=1000,
        ),
        PriceBar(
            date=peak_date,
            open=peak_high,
            high=peak_high,
            low=peak_high,
            close=peak_high,
            volume=1000,
        ),
    ]


def _metrics(
    current_price: Decimal,
    average_purchase_price: Decimal = Decimal("920"),
    peak_high: Decimal = Decimal("1454.5"),
    ratio_adjustment_event_since_basis: bool = False,
    bars: list[PriceBar] | None = None,
    basis_date: dt.date = _ENTRY,
    as_of_date: dt.date = _AS_OF,
):
    return compute_profit_protection_metrics(
        bars=bars if bars is not None else _bars(peak_high),
        current_price=current_price,
        average_purchase_price=average_purchase_price,
        basis_date=basis_date,
        as_of_date=as_of_date,
        ratio_adjustment_event_since_basis=ratio_adjustment_event_since_basis,
        config=_CONFIG,
    )


# --- サンリオ8136回帰(要求仕様§10) ---
# 平均取得単価920円・保有株数500株・peak_price=1454.5円


def test_case_a_current_1400_no_signal() -> None:
    m = _metrics(Decimal("1400"))
    assert m.insufficient_data_reason is None
    assert m.candidate_signal is False
    assert m.strong_signal is False


def test_case_b_current_1350_no_signal() -> None:
    m = _metrics(Decimal("1350"))
    assert m.insufficient_data_reason is None
    # drawdown(7.18%)がcandidate閾値(8%)未満のため、candidateも不成立。
    assert m.candidate_signal is False
    assert m.strong_signal is False


def test_case_c_current_1300_strong_boundary_not_met_but_candidate_met() -> None:
    m = _metrics(Decimal("1300"))
    assert m.insufficient_data_reason is None
    assert m.current_gain_pct is not None
    assert m.drawdown_from_peak_pct is not None
    assert m.gain_giveback_ratio_pct is not None
    assert round(m.current_gain_pct, 1) == 41.3
    assert round(m.drawdown_from_peak_pct, 1) == 10.6
    assert round(m.gain_giveback_ratio_pct, 1) == 28.9
    # giveback(約28.9%)がstrong閾値(30%)未満のためstrongは不成立。
    assert m.strong_signal is False
    # candidate(20/8/20)はすべて満たすため成立。
    assert m.candidate_signal is True


def test_case_d_current_1282_5_strong_signal() -> None:
    m = _metrics(Decimal("1282.5"))
    assert m.insufficient_data_reason is None
    assert m.current_gain_pct is not None
    assert m.drawdown_from_peak_pct is not None
    assert m.gain_giveback_ratio_pct is not None
    assert round(m.current_gain_pct, 1) == 39.4
    assert round(m.drawdown_from_peak_pct, 1) == 11.8
    assert round(m.gain_giveback_ratio_pct, 1) == 32.2
    assert m.strong_signal is True
    assert m.candidate_signal is True


def test_case_e_current_1227_strong_signal() -> None:
    m = _metrics(Decimal("1227"))
    assert m.insufficient_data_reason is None
    assert m.current_gain_pct is not None
    assert m.drawdown_from_peak_pct is not None
    assert m.gain_giveback_ratio_pct is not None
    assert round(m.current_gain_pct, 1) == 33.4
    assert round(m.drawdown_from_peak_pct, 1) == 15.6
    # 要求仕様の例示値(≒42.5%)は概算であり、実際の厳密な計算結果(約42.56%)を
    # 使う(浮動小数点の丸めで判定を誤らせないための厳密計算を確認する)。
    assert abs(m.gain_giveback_ratio_pct - 42.5) < 0.5
    assert m.strong_signal is True
    assert m.candidate_signal is True


# --- 買い増し(basis_date)回帰(コードレビュー対応2026-08、指摘1) ---
# 平均取得単価920円は「買い増し後」の加重平均という想定。basis_date
# (last_purchase_date相当)を2026-06-15とし、それより前の極端な高値
# (2000円、買い増し前の取得原価とは無関係)がpeakに混入しないことを確認する。

_BASIS_DATE = dt.date(2026, 6, 15)


def _bars_with_pre_and_post_basis_highs() -> list[PriceBar]:
    return [
        PriceBar(
            date=dt.date(2026, 1, 5),
            open=Decimal("900"),
            high=Decimal("910"),
            low=Decimal("890"),
            close=Decimal("900"),
            volume=1000,
        ),
        # basis_dateより前の極端な高値(買い増し前の価格変動、現在の平均取得単価
        # 920円とは無関係)。peakに含めてはならない。
        PriceBar(
            date=dt.date(2026, 3, 1),
            open=Decimal("2000"),
            high=Decimal("2000"),
            low=Decimal("2000"),
            close=Decimal("2000"),
            volume=1000,
        ),
        PriceBar(
            date=_BASIS_DATE,
            open=Decimal("1000"),
            high=Decimal("1010"),
            low=Decimal("990"),
            close=Decimal("1000"),
            volume=1000,
        ),
        # basis_date以降の高値(サンリオ回帰と同じ1454.5円)。これがpeakになる
        # べき。
        PriceBar(
            date=dt.date(2026, 7, 1),
            open=Decimal("1454.5"),
            high=Decimal("1454.5"),
            low=Decimal("1454.5"),
            close=Decimal("1454.5"),
            volume=1000,
        ),
    ]


def test_buy_more_case_b_pre_basis_high_excluded_from_peak() -> None:
    """買い増し前の高値(2000円)はpeakに含めず、basis_date以降の高値
    (1454.5円)のみをpeakとする(要求仕様§10 Case B相当)。
    """
    m = _metrics(
        current_price=Decimal("1227"),
        bars=_bars_with_pre_and_post_basis_highs(),
        basis_date=_BASIS_DATE,
    )
    assert m.insufficient_data_reason is None
    assert m.peak_price_since_entry == Decimal("1454.5")
    # 買い増し前の2000円が誤って使われていれば、peak_gain_pctはこの値の近くに
    # なるはずだが、実際にはbasis_date以降の1454.5円基準の値になる。
    assert m.peak_gain_pct is not None
    assert round(m.peak_gain_pct, 1) == 58.1


def test_buy_more_case_c_only_post_basis_high_used_for_strong_signal() -> None:
    """basis_date以降の高値のみでpeakを正しく算出し、Strong条件を判定する
    (要求仕様§10 Case C相当)。買い増し前の2000円を根拠にした場合の
    (誤った)giveback比率にはならない。
    """
    m = _metrics(
        current_price=Decimal("1227"),
        bars=_bars_with_pre_and_post_basis_highs(),
        basis_date=_BASIS_DATE,
    )
    assert m.gain_giveback_ratio_pct is not None
    assert abs(m.gain_giveback_ratio_pct - 42.5) < 0.5
    assert m.strong_signal is True


def test_buy_more_case_d_history_not_reaching_basis_date_is_insufficient() -> None:
    """買い増し日(basis_date)まで価格履歴が遡れない場合はDATA_INSUFFICIENT
    とする(要求仕様§10 Case D相当)。
    """
    late_bars = [
        b for b in _bars_with_pre_and_post_basis_highs() if b.date > _BASIS_DATE
    ]
    m = _metrics(
        current_price=Decimal("1227"),
        bars=late_bars,
        basis_date=_BASIS_DATE,
    )
    assert m.insufficient_data_reason is not None


# --- 追加境界値ケース(要求仕様§10) ---


def test_high_gain_but_insufficient_drawdown_and_giveback_no_signal() -> None:
    """含み益率が高くても、高値からの下落・吐き出しが小さい通常の押し目では
    シグナルを成立させない(要求仕様§5: 上昇途中の早売り防止)。
    """
    m = _metrics(
        current_price=Decimal("1300"),
        average_purchase_price=Decimal("1000"),
        peak_high=Decimal("1320"),
    )
    assert m.current_gain_pct is not None and m.current_gain_pct >= 25.0
    assert m.candidate_signal is False
    assert m.strong_signal is False


def test_drawdown_and_giveback_sufficient_but_current_gain_insufficient() -> None:
    """高値からの下落率・吐き出し率が閾値を満たしても、現在の含み益率自体が
    小さい場合はシグナルを成立させない。
    """
    m = _metrics(
        current_price=Decimal("1050"),
        average_purchase_price=Decimal("1000"),
        peak_high=Decimal("1500"),
    )
    assert m.current_gain_pct is not None and round(m.current_gain_pct, 1) == 5.0
    assert m.drawdown_from_peak_pct is not None and m.drawdown_from_peak_pct >= 30.0
    assert m.candidate_signal is False
    assert m.strong_signal is False


def test_candidate_boundary_exact_threshold_is_met() -> None:
    """candidate条件(20/8/20)をすべて満たす場合に成立する(以上判定)。"""
    avg = Decimal("1000")
    peak = Decimal("1320")  # peak_gain=32%
    current = Decimal("1200")  # current_gain=20%、drawdown≒9.09%、giveback=37.5%
    m = _metrics(current_price=current, average_purchase_price=avg, peak_high=peak)
    assert m.current_gain_pct is not None and round(m.current_gain_pct, 4) == 20.0
    assert m.drawdown_from_peak_pct is not None
    assert abs(m.drawdown_from_peak_pct - float((1 - current / peak) * 100)) < 1e-6
    assert m.gain_giveback_ratio_pct is not None
    assert m.candidate_signal is True


def test_peak_gain_zero_or_negative_no_signal() -> None:
    """高値時点でも含み損だった場合、giveback比率は定義できず不成立とする。"""
    m = _metrics(
        current_price=Decimal("800"),
        average_purchase_price=Decimal("1000"),
        peak_high=Decimal("950"),
    )
    assert m.insufficient_data_reason is None
    assert m.gain_giveback_ratio_pct is None
    assert m.candidate_signal is False
    assert m.strong_signal is False


# --- データ品質ガード(要求仕様§9) ---


def test_ratio_adjustment_event_since_basis_makes_data_insufficient() -> None:
    m = _metrics(Decimal("1227"), ratio_adjustment_event_since_basis=True)
    assert m.insufficient_data_reason is not None
    assert m.candidate_signal is False
    assert m.strong_signal is False
    assert m.peak_price_since_entry is None


def test_no_bars_covering_entry_period_is_insufficient() -> None:
    """保有開始日以降の価格データが取得できない場合はスキップする。"""
    stale_bars = [
        PriceBar(
            date=dt.date(2025, 1, 1),
            open=Decimal("900"),
            high=Decimal("910"),
            low=Decimal("890"),
            close=Decimal("900"),
            volume=1000,
        )
    ]
    m = _metrics(Decimal("1227"), bars=stale_bars)
    assert m.insufficient_data_reason is not None


def test_history_not_reaching_back_to_entry_date_is_insufficient() -> None:
    """価格データが保有開始日まで遡れない場合、真の最高値を見逃す可能性がある
    ためスキップする。
    """
    late_start_bars = [
        PriceBar(
            date=_ENTRY + dt.timedelta(days=30),
            open=Decimal("1000"),
            high=Decimal("1454.5"),
            low=Decimal("1000"),
            close=Decimal("1000"),
            volume=1000,
        )
    ]
    m = _metrics(Decimal("1227"), bars=late_start_bars)
    assert m.insufficient_data_reason is not None


def test_empty_bars_is_insufficient() -> None:
    m = _metrics(Decimal("1227"), bars=[])
    assert m.insufficient_data_reason is not None


def test_signal_label_property() -> None:
    assert _metrics(Decimal("1400")).signal_label == "NONE"
    assert _metrics(Decimal("1300")).signal_label == "CANDIDATE"
    assert _metrics(Decimal("1227")).signal_label == "STRONG"
    assert (
        _metrics(Decimal("1227"), ratio_adjustment_event_since_basis=True).signal_label
        == "DATA_INSUFFICIENT"
    )


def test_disabled_config_never_signals() -> None:
    from jstock_advisor.config.models import ProfitProtectionConfig

    disabled_config = ProfitProtectionConfig(
        enabled=False, candidate=_CONFIG.candidate, strong=_CONFIG.strong
    )
    m = compute_profit_protection_metrics(
        bars=_bars(Decimal("1454.5")),
        current_price=Decimal("1227"),
        average_purchase_price=Decimal("920"),
        basis_date=_ENTRY,
        as_of_date=_AS_OF,
        ratio_adjustment_event_since_basis=False,
        config=disabled_config,
    )
    assert m.candidate_signal is False
    assert m.strong_signal is False
