"""推奨の定点評価結果(要求仕様29〜36節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import model_validator

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import EvaluationLabel

# 評価意味論の版(Issue #71 F-C12)。
#
# ★ この版は「同じ推奨・同じ軸・同じホライズンでも、**評価の定め方が違えば
#   別の評価である**」ことを表す。現行の定め方は次のとおりで、これを "v1" とする。
#
#     営業日ホライズン  起点 = recommended_at の **UTC 暦日**
#     暦日ホライズン    起点 = recommended_at の **JST 暦日**
#
#   この非対称は Issue #23 で**意図的に維持された既存仕様**であり
#   (functional_spec.md 2026-08-28「評価期間の起点日・評価対象日の算出方法自体は
#   変更していない」)、recommendation_evaluation_service.py の
#   `_evaluate_pending_work` にも「【意図的に変更しない】」と明記がある。
#
# ★ なぜ一意キーへ最初から版を入れるのか(後付けにしない理由)
#   #66-F-L3 は起点を JST 業務日へ変える案を検討中である。版を入れずに
#   一意キーを (推奨 ID, 軸, ホライズン) だけで定めると、F-L3 で定め方が
#   変わったとき、**同じキーが別意味論の評価と衝突する**。条件付き insert は
#   衝突を「既に評価済み」と解釈するため、★ 新しい意味論の評価が
#   **黙って保存されない**。版を持てば "v2" を足すだけで移行窓を表現できる。
EVALUATION_SEMANTICS_V1 = "v1"

# Issue #389(#66 F-L3): 営業日ホライズンの起点をJST暦日へ統一した意味論。
#
#     営業日ホライズン  起点 = recommended_at の **JST 暦日**(v1はUTC暦日)
#     暦日ホライズン    起点 = recommended_at の JST 暦日(変更なし。v1と同じ)
#
# 適用境界は recommendation_evaluation_service.V2_CUTOVER_AT を正本とする
# (この定数自体は「意味論の名前」のみを持ち、境界日時は持たない)。
EVALUATION_SEMANTICS_V2 = "v2"

# 一意キーの軸(営業日 / 暦日)。キー文字列に直接現れるため値は変更しない。
_AXIS_BUSINESS = "B"
_AXIS_CALENDAR = "C"

# 一意キーの区切り。★ recommendation_id は uuid4 文字列であり、この区切りを
# 含まない。含む値が将来入りうるなら、キーの組み立て自体を見直すこと。
_KEY_SEPARATOR = "#"


def build_evaluation_id(
    recommendation_id: str,
    *,
    horizon_business_days: int | None = None,
    horizon_calendar_days: int | None = None,
    semantics_version: str = EVALUATION_SEMANTICS_V1,
) -> str:
    """定点評価の一意キーを組み立てる(Issue #71 F-C12)。

    キー = 推奨 ID + 評価軸 + ホライズン + ★ 評価意味論の版。

    ★ この文字列がそのまま `EvaluationResult.evaluation_id`(= 永続層の
    パーティションキー)になる。`insert_if_absent()` の
    `attribute_not_exists(evaluation_id)` が効くのは、キーが**決定的**で
    あるときだけである。uuid4 のままでは、同じ評価を 2 回保存しても
    キーが違うため条件が成立してしまう。
    """
    if (horizon_business_days is None) == (horizon_calendar_days is None):
        raise ValueError(
            "horizon_business_daysとhorizon_calendar_daysはどちらか一方のみ指定してください"
        )
    if horizon_business_days is not None:
        axis, horizon = _AXIS_BUSINESS, horizon_business_days
    else:
        # 上の排他チェックを通っているため、ここでは必ず暦日側が入っている
        # (型の絞り込みのみを目的とした表明)。
        assert horizon_calendar_days is not None  # noqa: S101 - 直前の検証で保証済み
        axis, horizon = _AXIS_CALENDAR, horizon_calendar_days
    return _KEY_SEPARATOR.join((recommendation_id, axis, str(horizon), semantics_version))


class EvaluationResult(Entity):
    evaluation_id: str
    recommendation_id: str
    # 既存の営業日ベースホライズン(horizon_business_days)と、振り返り機能改修で
    # 追加したJST暦日ベースホライズン(horizon_calendar_days)は排他的であり、
    # 1レコードにつき必ずどちらか一方のみを設定する(_validate_horizon参照)。
    horizon_business_days: int | None = None
    horizon_calendar_days: int | None = None
    # evaluation_date: 評価基準日(ホライズンの到来日)。evaluated_at: 実際に処理が
    # 成功しこの結果が確定した日時。株価取得失敗等により両者はずれることがある
    # (振り返り機能改修で明確化)。
    # ★ 週次集計の軸は evaluation_date である(Issue #114 Phase B2 で
    #   evaluated_at から変更した)。evaluated_at は「いつ処理を走らせたか」しか
    #   表さず、遅延処理分が処理した週へ一括計上されて母数が歪むため。
    #   evaluation_date はホライズンから決定論的に定まり、遅れて処理しても
    #   評価値そのものは on-time 実行と一致する。
    evaluated_at: dt.datetime
    evaluation_date: dt.date

    @model_validator(mode="after")
    def _validate_horizon(self) -> EvaluationResult:
        business = self.horizon_business_days is not None
        calendar = self.horizon_calendar_days is not None
        if business == calendar:
            raise ValueError(
                "horizon_business_daysとhorizon_calendar_daysはどちらか一方のみ設定してください"
            )
        return self

    price_at_evaluation: Decimal
    price_return_pct: float
    buy_price_based_return_pct: float | None = None

    total_return_amount: Decimal | None = None
    total_return_pct: float | None = None

    max_gain_pct: float | None = None  # 推奨後の最高値ベース
    max_drawdown_pct: float | None = None  # 推奨後の最安値ベース

    reached_tentative_buy_price: bool | None = None
    reached_standard_buy_price: bool | None = None
    reached_aggressive_buy_price: bool | None = None
    business_days_to_reach_price: int | None = None

    # SELL側の利確目安到達判定(Issue #368)。BUY側と対称にhigh>=priceで判定する。
    # この3フィールドはhorizon_business_days(種別依存。1,5,20,60,120,250)と
    # horizon_calendar_days(常に7暦日固定)の両方の行に書かれ、horizonの種類を
    # 区別しない。利用時は必ずhorizon_business_days/horizon_calendar_daysと
    # 併せて読むこと(reached=Trueだけでは「どの窓で到達したか」が分からない)。
    #
    # horizon_business_days=1の行は「day-zero」と翌営業日の2営業日窓だが、
    # day-zeroの決め方はevaluation_semantics_versionでは判別できない
    # (resolve_business_day_zero(): v2〔recommended_at >= V2_CUTOVER_AT〕は
    # JST暦日=推奨当日。v1〔cutover前〕はrecommended_atのUTC暦日で、推奨時刻が
    # JST 00:00〜08:59だと実際のJST当日より1日前へずれうる。horizon_calendar_days
    # の行は軸自体が異なり〔常にJST暦日起点で当日を指す〕、常にevaluation_
    # semantics_version="v1"だがこのずれの対象ではない。判別条件は
    # `horizon_business_days is not None and recommended_at < V2_CUTOVER_AT`
    # であり、version単独では判別できない)。
    # reached=Falseはこの窓のどちらの日でも到達しなかったことを意味し当日
    # 未到達を含意するが、reached=Trueは窓内のどの日に到達したか(v1のずれた
    # 行では推奨より前の営業日を含むこともある)を区別できない(#368設計
    # R2/N1〜N3)。
    reached_partial_profit_start_price: bool | None = None
    reached_recommended_limit_price: bool | None = None
    reached_full_profit_consideration_price: bool | None = None
    business_days_to_reach_sell_price: int | None = None

    benchmark_symbol: str | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None

    evaluation_label: EvaluationLabel
    label_evidence: str
    notes: str | None = None

    # Issue #71 F-C12: 評価意味論の版。★ 既定値を持たせてあるのは、本変更より前に
    # 保存された行(このフィールドを持たない)が読めなくなると、
    # load_completed_horizon_index() が壊れて**全推奨が未評価扱いになる**ため。
    # 既存行はすべて現行の定め方で作られているので "v1" とみなしてよい。
    evaluation_semantics_version: str = EVALUATION_SEMANTICS_V1

    # --- 判定精度向上機能(Phase A)で追加。セクターETF proxy(config.sector_etf_map)
    # による指数比較用の予約フィールド。セクターproxy選定・安定取得可否の検証は
    # Phase D(Market/Sector Environment)で行うため、Phase Aでは常にNoneのまま
    # (推測で埋めない)。DecisionSnapshot/DecisionPerformanceServiceとは無関係で、
    # 既存の営業日/暦日ホライズン評価(benchmark_symbol/benchmark_return_pct/
    # excess_return_pctがTOPIX固定で使うのと同型のセクター版拡張)。 ---
    sector_benchmark_symbol: str | None = None
    sector_return_pct: float | None = None
    excess_return_vs_sector_pct: float | None = None
