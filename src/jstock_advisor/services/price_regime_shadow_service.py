"""価格の経路の Shadow の受け手(Issue #877 PR-2b-ii)。★ exit_architecture へ繋がる唯一の module ★

`ProfitTakingOutcome.price_path_facts`(素の型 5 つ。PR-2b-i)を受け取り、L3 PRICE_REGIME の入力
(`PriceFacts`。PR-2a の `build_price_facts`)へ写す。**結果は戻り値だけ**(非永続)。判定・保存・
通知・SELL・LINE のどこにも反映しない。呼び出し側(`holdings_watchlist_handler`)は戻り値を使わない。

## 守ること(契約テストで固定する)

* ★ 閾値が供給されないとき(PR-2b では常にそう)は、facts の組み立ても分類も行わず None を返す ★
  (判定・通知・保存をしない)。閾値の供給元 `resolve_price_regime_thresholds` は PR-2b では常に
  None を返し、`RegimeThresholds(...)` を構築しない(PR-2a の契約: 承認された設定の loader を足す
  PR だけが更新する)
* ★ `classify_price_regime` を呼ばない ★: 信頼性(ReliabilityClass)の供給元が未決定のため。
  PR-2c では供給元を決めるまで分類を呼ばない(『とりあえず RELIABLE』を渡さない = 閾値を
  勝手に決めるのと同じ)
* ★ 例外を送出しない ★(どの内部の失敗も None・ログは型名だけ)。呼び出し側(handler)にも二重の網
* I/O・時計・repository・audit・queue・LINE を持たない(AST で固定)
* 本 module は #606 の網の外の名前(token・prefix に当たらない)。到達の保証は #606 の『局所許可』
  (別表 7 件 + 受け手の経由の固定 + サービスの閉包に exit_architecture が無いこと)であり、
  `_SHADOW_FRAME_EXEMPT`(免除 10 件)には足していない

設計: #877 PR-2 設計 rev3(…6182483078)・設計レビュー PASS(…6182584823)・USER 決定
(#122 …6182420023 の 1 = PR-2b に限定した Shadow 接続の承認)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from jstock_advisor.domain.exit_architecture.price_facts import build_price_facts
from jstock_advisor.domain.exit_architecture.price_regime import PriceFacts, RegimeThresholds
from jstock_advisor.domain.signals.price_path_facts import PricePathFacts

logger = logging.getLogger(__name__)


class _HasPricePathFacts(Protocol):
    @property
    def price_path_facts(self) -> PricePathFacts | None: ...


@dataclass(frozen=True)
class PriceRegimeObservation:
    """受け手の結果(非永続・戻り値だけ)。PR-2b は facts までで、分類(状態)は持たない。"""

    price_facts: PriceFacts


def resolve_price_regime_thresholds() -> RegimeThresholds | None:
    """閾値の供給元。★ PR-2b では常に None(承認された設定が無い)★。

    閾値 3 値は USER が保留している(事前登録 → shadow → replay → 承認)。ここで値を作らない・
    config を読まない。承認された設定の loader を足す PR だけが、この関数を意図して更新する。
    """
    return None


def observe_price_regime_shadow(
    outcome: _HasPricePathFacts,
    *,
    thresholds_provider: Callable[[], RegimeThresholds | None] = resolve_price_regime_thresholds,
) -> PriceRegimeObservation | None:
    """利確判定の outcome の価格の経路の事実を、regime の入力へ写す。★ 例外を送出しない ★。

    閾値が無い(None)・事実が無い(None)ときは何も組み立てず None。
    """
    try:
        thresholds = thresholds_provider()
        if thresholds is None:
            return None  # 閾値未指定: 組み立ても分類もしない(判定・通知・保存なし)
        path = outcome.price_path_facts
        if path is None:
            return None
        return PriceRegimeObservation(
            price_facts=build_price_facts(
                data_sufficient=path.data_sufficient,
                drawdown_from_peak_pct=path.drawdown_from_peak_pct,
                peak_gain_pct=path.peak_gain_pct,
                current_gain_pct=path.current_gain_pct,
                trend_is_down=path.trend_is_down,
            )
        )
    except Exception as error:  # noqa: BLE001 - shadowの失敗を本流へ伝播させない
        logger.warning("price regime shadow unavailable: error_type=%s", type(error).__name__)
        return None
