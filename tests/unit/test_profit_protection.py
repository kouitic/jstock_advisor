"""利益保全(Profit Protection)判定の単体テスト(2026-08、サンリオ8136回帰含む)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.signals.profit_protection import compute_profit_protection_metrics
from jstock_advisor.interfaces.types import PriceBar

_APP_CONFIG = load_config()
_CONFIG = _APP_CONFIG.profit_taking.profit_protection
_CALENDAR = BusinessCalendar.from_config(_APP_CONFIG.holiday_calendar)
_ENTRY = dt.date(2026, 1, 5)  # 営業日(次の営業日は2026-01-06)
_AS_OF = dt.date(2026, 8, 14)


def _bars(peak_high: Decimal, peak_date: dt.date = dt.date(2026, 6, 1)) -> list[PriceBar]:
    """basis_dateちょうどのバー・basis_date翌営業日のカバレッジ用バー・
    指定した高値を含むバーの3本を返す(basis_date当日のhighはpeakに含めない
    仕様のため、basis_date当日バーのhighは常にpeak_highより低く設定する)。
    """
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
            date=_CALENDAR.next_business_day(_ENTRY),
            open=Decimal("900"),
            high=Decimal("905"),
            low=Decimal("895"),
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
        business_calendar=_CALENDAR,
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


# --- basis_date当日high除外(コードレビュー対応2026-08、指摘A-1) ---


def test_a1_case_a_basis_date_own_day_high_excluded_from_peak() -> None:
    """basis_date当日の高値(1200円)はpeakに含めず、basis_date翌営業日以降の
    高値(1050円)のみをpeakとする。
    """
    basis = dt.date(2026, 3, 2)  # 月曜(次の営業日は2026-03-03)
    bars = [
        PriceBar(
            date=basis,
            open=Decimal("800"),
            high=Decimal("1200"),  # basis_date当日の高値(除外されるべき)
            low=Decimal("800"),
            close=Decimal("800"),
            volume=1000,
        ),
        PriceBar(
            date=_CALENDAR.next_business_day(basis),
            open=Decimal("1000"),
            high=Decimal("1010"),
            low=Decimal("990"),
            close=Decimal("1000"),
            volume=1000,
        ),
        PriceBar(
            date=dt.date(2026, 4, 1),
            open=Decimal("1050"),
            high=Decimal("1050"),
            low=Decimal("1050"),
            close=Decimal("1050"),
            volume=1000,
        ),
    ]
    m = _metrics(
        current_price=Decimal("1000"),
        average_purchase_price=Decimal("800"),
        bars=bars,
        basis_date=basis,
    )
    assert m.insufficient_data_reason is None
    assert m.peak_price_since_entry == Decimal("1050")


def test_a1_case_b_pre_and_post_basis_extreme_highs_only_post_used() -> None:
    """basis_date前・当日いずれの極端な高値も使わず、basis_date翌営業日以降の
    通常の高値のみを採用する。
    """
    basis = dt.date(2026, 3, 2)
    bars = [
        PriceBar(
            date=basis - dt.timedelta(days=60),
            open=Decimal("5000"),
            high=Decimal("5000"),
            low=Decimal("5000"),
            close=Decimal("5000"),
            volume=1000,
        ),
        PriceBar(
            date=basis,
            open=Decimal("800"),
            high=Decimal("4000"),
            low=Decimal("800"),
            close=Decimal("800"),
            volume=1000,
        ),
        PriceBar(
            date=_CALENDAR.next_business_day(basis),
            open=Decimal("1000"),
            high=Decimal("1010"),
            low=Decimal("990"),
            close=Decimal("1000"),
            volume=1000,
        ),
        PriceBar(
            date=dt.date(2026, 4, 1),
            open=Decimal("1050"),
            high=Decimal("1050"),
            low=Decimal("1050"),
            close=Decimal("1050"),
            volume=1000,
        ),
    ]
    m = _metrics(
        current_price=Decimal("1000"),
        average_purchase_price=Decimal("800"),
        bars=bars,
        basis_date=basis,
    )
    assert m.insufficient_data_reason is None
    assert m.peak_price_since_entry == Decimal("1050")


def test_a1_case_c_only_basis_date_bar_exists_is_insufficient() -> None:
    """basis_date当日のバーしか存在しない場合、basis_date当日は除外される
    ためDATA_INSUFFICIENTとする。
    """
    basis = dt.date(2026, 3, 2)
    bars = [
        PriceBar(
            date=basis,
            open=Decimal("800"),
            high=Decimal("1200"),
            low=Decimal("800"),
            close=Decimal("800"),
            volume=1000,
        )
    ]
    m = _metrics(
        current_price=Decimal("1000"),
        average_purchase_price=Decimal("800"),
        bars=bars,
        basis_date=basis,
        as_of_date=basis,
    )
    assert m.insufficient_data_reason is not None


# --- price history coverage(コードレビュー対応2026-08、指摘A-3) ---


def test_a3_case_h_bar_on_next_business_day_is_normal() -> None:
    """basis_date翌営業日のbarがあれば正常に評価できる(_bars()の既定構成で
    暗黙に確認済みだが、明示的にも確認する)。"""
    m = _metrics(Decimal("1227"))
    assert m.insufficient_data_reason is None


def test_a3_case_i_long_gap_right_after_basis_date_is_insufficient() -> None:
    """basis_date直後から長期間価格データが欠落している場合はDATA_INSUFFICIENT
    とする(basis_date翌営業日に対応するbarが無い)。
    """
    basis = dt.date(2026, 6, 1)
    bars = [
        PriceBar(
            date=basis,
            open=Decimal("1000"),
            high=Decimal("1000"),
            low=Decimal("1000"),
            close=Decimal("1000"),
            volume=1000,
        ),
        # basis_date翌営業日〜6/29まで欠落し、6/30から再開する。
        PriceBar(
            date=dt.date(2026, 6, 30),
            open=Decimal("1454.5"),
            high=Decimal("1454.5"),
            low=Decimal("1454.5"),
            close=Decimal("1454.5"),
            volume=1000,
        ),
    ]
    m = _metrics(
        current_price=Decimal("1227"),
        bars=bars,
        basis_date=basis,
    )
    assert m.insufficient_data_reason is not None


def test_a3_case_j_friday_to_monday_is_normal() -> None:
    """basis_dateが金曜、次のbarが月曜(土日は営業日ではないため欠損扱いしない)
    の場合は正常に評価できる。"""
    friday = dt.date(2026, 3, 6)
    assert friday.weekday() == 4  # 金曜であることを確認
    monday = _CALENDAR.next_business_day(friday)
    assert monday == dt.date(2026, 3, 9)
    bars = [
        PriceBar(
            date=friday,
            open=Decimal("900"),
            high=Decimal("2000"),  # basis_date当日(除外されるべき)
            low=Decimal("900"),
            close=Decimal("900"),
            volume=1000,
        ),
        PriceBar(
            date=monday,
            open=Decimal("1000"),
            high=Decimal("1454.5"),
            low=Decimal("1000"),
            close=Decimal("1000"),
            volume=1000,
        ),
    ]
    m = _metrics(
        current_price=Decimal("1227"),
        average_purchase_price=Decimal("920"),
        bars=bars,
        basis_date=friday,
    )
    assert m.insufficient_data_reason is None
    assert m.peak_price_since_entry == Decimal("1454.5")


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
            high=Decimal("1010"),  # basis_date当日(除外されるべき)
            low=Decimal("990"),
            close=Decimal("1000"),
            volume=1000,
        ),
        # basis_date翌営業日(coverage確認用)。
        PriceBar(
            date=_CALENDAR.next_business_day(_BASIS_DATE),
            open=Decimal("1000"),
            high=Decimal("1005"),
            low=Decimal("995"),
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
    late_bars = [b for b in _bars_with_pre_and_post_basis_highs() if b.date > _BASIS_DATE]
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
    """basis_date以降の価格データが取得できない場合はスキップする。"""
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
    """価格データがbasis_dateまで遡れない場合、真の最高値を見逃す可能性がある
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
        business_calendar=_CALENDAR,
    )
    assert m.candidate_signal is False
    assert m.strong_signal is False


# --- 指摘1対応: evaluation_barsの空チェック順序(コードレビュー対応2026-08) ---


def test_evaluation_bars_empty_clear_reason() -> None:
    """evaluation_bars(basis_date < date <= as_of_date)が1件も無い場合、
    「基準日より後の価格データが取得できない」という明確な理由でスキップする
    (コードレビュー対応2026-08、指摘1: 空チェックを先行させて理由を区別する)。
    """
    # basis_dateが2026-08-14(as_of_date)と同じか後の場合、basis_dateより後の
    # barが存在しようがない。
    basis = dt.date(2026, 8, 14)
    bars = [
        PriceBar(
            date=basis,
            open=Decimal("1000"),
            high=Decimal("1000"),
            low=Decimal("1000"),
            close=Decimal("1000"),
            volume=1000,
        )
    ]
    m = _metrics(
        current_price=Decimal("1227"),
        bars=bars,
        basis_date=basis,
        as_of_date=basis,
    )
    assert m.insufficient_data_reason is not None
    assert "基準日より後の価格データが取得できない" in m.insufficient_data_reason


def test_evaluation_bars_not_empty_but_missing_first_business_day() -> None:
    """evaluation_bars は0件ではなく1件以上あるが、basis_date翌営業日の
    barが欠落している場合は、「基準日直後の営業日から価格データが欠落」という
    別の理由でスキップする(コードレビュー対応2026-08、指摘1: 理由の明確化)。
    """
    basis = dt.date(2026, 6, 1)  # 月曜
    bars = [
        PriceBar(
            date=basis,
            open=Decimal("1000"),
            high=Decimal("1000"),
            low=Decimal("1000"),
            close=Decimal("1000"),
            volume=1000,
        ),
        # next_bd(翌営業日)をスキップし、その後のbarのみを含める。
        # これはbasis_date < date <= as_of_dateを満たすため
        # evaluation_barsは0件ではなく1件以上。
        PriceBar(
            date=dt.date(2026, 6, 3),  # 水曜、翌営業日から更に1日後
            open=Decimal("1454.5"),
            high=Decimal("1454.5"),
            low=Decimal("1454.5"),
            close=Decimal("1454.5"),
            volume=1000,
        ),
    ]
    m = _metrics(
        current_price=Decimal("1227"),
        bars=bars,
        basis_date=basis,
        as_of_date=dt.date(2026, 8, 14),
    )
    assert m.insufficient_data_reason is not None
    assert "基準日直後の営業日から価格データが欠落" in m.insufficient_data_reason
