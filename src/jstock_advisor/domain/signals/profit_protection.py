"""利益保全(Profit Protection)判定(2026-08、サンリオ8136の含み益吐き出し事例対応)。

Fair Valueベースの割高判定(profit_taking.pyのcondition_based_judgment等)とは
独立した軸として、「すでに十分な含み益を得ており、その利益を高値から相当量失い
始めている」ことを検出する。「割高だから売る」(Valuation-based)ではなく、
「大きな含み益を得た後、その利益を相当量失い始めたので一部を確定する」
(Profit-protection-based)という、Fair Valueの信頼度に依存しない独立した
売却理由を提供する。

peak_price_since_entryの算出は、株式分割・株式併合等で価格系列の調整基準が
一致しない場合に誤った値を出すより、判定自体をスキップする(安全側)。

peak探索の起点(basis_date)は「保有開始日」ではなく「現在のaverage_purchase_price
が成立した基準日(=最終購入日)」を使う(コードレビュー対応2026-08、指摘1)。
詳細はcompute_profit_protection_metrics()のdocstring参照。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import ProfitProtectionConfig
from jstock_advisor.interfaces.types import PriceBar


@dataclass(frozen=True)
class ProfitProtectionMetrics:
    """Profit Protection判定に用いる指標一式(要求仕様§2・§8: 判定理由の追跡可能性)。

    insufficient_data_reasonがNoneでない場合、他の数値フィールドは全てNoneであり、
    candidate_signal/strong_signalは両方Falseとする(誤ったPARTIALを出すより
    安全側にスキップする)。
    """

    insufficient_data_reason: str | None
    peak_price_since_entry: Decimal | None
    peak_gain_pct: float | None
    current_gain_pct: float | None
    drawdown_from_peak_pct: float | None
    gain_giveback_ratio_pct: float | None
    candidate_signal: bool
    strong_signal: bool

    @property
    def signal_label(self) -> str:
        """監査・通知向けの状態ラベル(要求仕様§8)。"""
        if self.insufficient_data_reason is not None:
            return "DATA_INSUFFICIENT"
        if self.strong_signal:
            return "STRONG"
        if self.candidate_signal:
            return "CANDIDATE"
        return "NONE"


def _insufficient(reason: str) -> ProfitProtectionMetrics:
    return ProfitProtectionMetrics(
        insufficient_data_reason=reason,
        peak_price_since_entry=None,
        peak_gain_pct=None,
        current_gain_pct=None,
        drawdown_from_peak_pct=None,
        gain_giveback_ratio_pct=None,
        candidate_signal=False,
        strong_signal=False,
    )


def compute_profit_protection_metrics(
    bars: list[PriceBar],
    current_price: Decimal,
    average_purchase_price: Decimal,
    basis_date: dt.date,
    as_of_date: dt.date,
    ratio_adjustment_event_since_basis: bool,
    config: ProfitProtectionConfig,
) -> ProfitProtectionMetrics:
    """peak_price_since_entry・peak_gain_pct・drawdown_from_peak_pct・
    gain_giveback_ratio_pctを算出し、candidate/strongシグナルの成立有無を判定する
    (要求仕様§2・§3・§9)。

    basis_dateは「現在のaverage_purchase_priceが成立した基準日」を表す
    (コードレビュー対応2026-08、指摘1)。単一購入の保有であれば保有開始日
    (first_purchase_date)と一致するが、買い増しがある場合はaverage_purchase_price
    が複数PurchaseLotの加重平均であるため、average_purchase_priceが実際に
    現在の値になった日(=最終購入日、holding.last_purchase_date)を渡すこと。
    保有開始日をそのまま使うと、買い増し前の(現在の平均取得単価とは無関係な)
    高値をpeakに含めてしまい、実際には存在しなかった含み益吐き出しを検出して
    しまう(過去のレビューで確認された不具合)。

    一部売却時にbasis_dateを再算出する必要が無い理由: 保有株数管理は
    FIFO(古いロットから消費)であり、売却は直近ロット(last_purchase_date)
    より新しい日付の取得原価を一切生成しない。そのため、売却後に
    average_purchase_priceが変化しても、その変化は常にbasis_date以前に
    存在した取得原価の再構成(古いロットの一部・全部消費)によるものであり、
    「basis_date以降の価格推移からpeakを見る」という前提はそのまま成立する
    (実証: test_portfolio_service.pyのFIFO売却テストでlast_purchase_dateが
    部分売却で変化しないことを確認済み)。

    peak_price_since_entryはbasis_date以降のPriceBar.high(日次高値)の
    最大値とする(momentum.pyのcompute_high_over_window()と同じ「日次高値の
    最大」基準に揃える)。

    データ品質ガード(要求仕様§9、誤ったPARTIALを出すよりスキップする):
    - basis_date以降に株式分割・株式併合・無償割当があった場合(raw価格系列
      (auto_adjust=False)と平均取得単価の調整基準が一致しない可能性がある)
    - basis_date以降の価格データが取得できない場合
    - 価格データがbasis_dateまで遡れない場合(真の最高値を見逃す可能性がある)
    """
    if average_purchase_price <= 0 or current_price <= 0:
        return _insufficient("平均取得単価または現在値が不正なため判定不能")

    if ratio_adjustment_event_since_basis:
        return _insufficient(
            "基準日以降に株式分割・併合等があり、価格系列の調整基準が一致しないため判定不能"
        )

    if bars and min(b.date for b in bars) > basis_date:
        return _insufficient(
            "基準日まで遡る価格データが無く、真の最高値を算出できないため判定不能"
        )

    entry_bars = [b for b in bars if basis_date <= b.date <= as_of_date]
    if not entry_bars:
        return _insufficient("基準日以降の価格データが取得できないため判定不能")

    peak_price = max(b.high for b in entry_bars)
    if peak_price <= 0:
        return _insufficient("最高値データが不正なため判定不能")

    peak_gain_pct = float(peak_price / average_purchase_price - 1) * 100
    current_gain_pct = float(current_price / average_purchase_price - 1) * 100
    drawdown_from_peak_pct = float(1 - current_price / peak_price) * 100

    # peak_gain_pct<=0(高値時点でも含み損だった)場合、giveback比率は定義できない
    # (分母が0以下になるため)。この場合はProfit Protectionのシグナル自体を
    # 成立させない(候補・strongいずれもFalse)。
    gain_giveback_ratio_pct = (
        (peak_gain_pct - current_gain_pct) / peak_gain_pct * 100 if peak_gain_pct > 0 else None
    )

    def _meets(
        min_current_gain_pct: float,
        min_drawdown_from_peak_pct: float,
        min_gain_giveback_ratio_pct: float,
    ) -> bool:
        return (
            gain_giveback_ratio_pct is not None
            and current_gain_pct >= min_current_gain_pct
            and drawdown_from_peak_pct >= min_drawdown_from_peak_pct
            and gain_giveback_ratio_pct >= min_gain_giveback_ratio_pct
        )

    candidate_signal = config.enabled and _meets(
        config.candidate.min_current_gain_pct,
        config.candidate.min_drawdown_from_peak_pct,
        config.candidate.min_gain_giveback_ratio_pct,
    )
    strong_signal = config.enabled and _meets(
        config.strong.min_current_gain_pct,
        config.strong.min_drawdown_from_peak_pct,
        config.strong.min_gain_giveback_ratio_pct,
    )

    return ProfitProtectionMetrics(
        insufficient_data_reason=None,
        peak_price_since_entry=peak_price,
        peak_gain_pct=peak_gain_pct,
        current_gain_pct=current_gain_pct,
        drawdown_from_peak_pct=drawdown_from_peak_pct,
        gain_giveback_ratio_pct=gain_giveback_ratio_pct,
        candidate_signal=candidate_signal,
        strong_signal=strong_signal,
    )
