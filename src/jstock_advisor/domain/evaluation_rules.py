"""推奨の定点評価ラベル判定(要求仕様29〜36節)。

推奨後の株価実績(自社株・ベンチマーク)のみから、機械的に判定できる範囲で
EvaluationLabelを決定する。判定に必要なデータが揃わない場合は憶測せず
DATA_ISSUE/INCONCLUSIVEとする(要求仕様12節「推測で補完しない」原則)。

LATE/PROFIT_TAKE_TOO_LATEは、推奨"前"の株価推移(いつ本来売るべきだったか)が
必要になるため、現時点の実装では自動付与しない(将来、価格履歴の遡り取得に
対応した際の拡張ポイントとする)。

## RecommendationTypeの分類(Issue #270)

**すべてのRecommendationTypeは、次の4つのいずれか1つに必ず属する。**
分類漏れは`tests/unit/test_issue_270_evaluation_target_types.py`の網羅テストが
CIで落として知らせる(本Issueの欠陥は、新しい型が増えたのに
`_EXIT_TYPES`が追随せず**静かにINCONCLUSIVEになり続けた**ことだった)。

    _ENTRY_TYPES     株価の**上昇**がSUCCESSを意味する。7暦日後の超過リターンで測る
    _EXIT_TYPES      株価の**下落**がSUCCESSを意味する。同じくlabel判定の対象
    _EXCLUDED_TYPES  ★ **方向性を持たない「状態」**であり、
                     株価による成否の定義が業務上そもそも不適切な型。
                     常にINCONCLUSIVEであることが**仕様として妥当**であり、
                     週次改善レビューは「評価定義が未整備」として扱わない
                     (= GitHub Issueを自動起票しない)
    _EVALUATION_UNDEFINED_TYPES
                     ★ 評価基準が**まだ決まっていない**型。現状INCONCLUSIVEだが、
                     それは仕様ではなく**未整備**である。週次改善レビューは
                     従来どおり「評価定義が未整備」として改善候補を出す
                     (Issue #25が型ごとに要否を判断し、仕様として妥当と決まった型を
                      `_EXCLUDED_TYPES`へ移す)

★ `_EXCLUDED_TYPES`と`_EVALUATION_UNDEFINED_TYPES`は**どちらも常にINCONCLUSIVE**
  であり、`determine_evaluation_label()`の挙動は同一である。違うのは
  **週次改善レビューがそれを「直すべき未整備」とみなすかどうか**だけである。
"""

from __future__ import annotations

from jstock_advisor.config.models import EvaluationRulesConfig
from jstock_advisor.domain.entities.enums import EvaluationLabel, RecommendationType

_ENTRY_TYPES = (RecommendationType.BUY, RecommendationType.WATCH_BUY, RecommendationType.HOLD)
_EXIT_TYPES = (
    RecommendationType.PARTIAL_PROFIT_TAKE,
    RecommendationType.FULL_PROFIT_TAKE,
    RecommendationType.SELL,
    RecommendationType.URGENT_REVIEW,
    # WATCH(利確レベルの梯子でHOLDとPARTIAL_PROFIT_TAKEの間の監視段階)・
    # REVIEW(懸念1件のみでSELL/URGENT_REVIEWには不十分)は、いずれも実売買を
    # 伴わない警告にすぎないが、警告の正しさは「警告した事象(株価下落)が
    # 実際に起きたか」で測れるため、EXIT型と同じ基準を流用する
    # (Rule Improvement対応2026-08、Issue #9・#11)。
    RecommendationType.WATCH,
    RecommendationType.REVIEW,
    # Issue #270: 保有判断エンジンの売却系。上と同じ論理で、
    # 「売却を検討すべき」という警告の正しさは株価下落の発生で測れる。
    # ★ `SELL_LIKE_RECOMMENDATION_TYPES`(enums.py)で置き換えてはならない。
    #   あちらは通知側の概念で利確2型とWATCHを含まないため、置換すると
    #   PARTIAL_PROFIT_TAKE/FULL_PROFIT_TAKE/WATCHが評価対象から**外れ**、
    #   `_PROFIT_TAKE_TYPES`分岐(PROFIT_TAKE_TOO_EARLYへの唯一の経路)が
    #   **到達不能**になる。
    RecommendationType.SELL_CONSIDERATION,
    RecommendationType.STRONG_SELL_CONSIDERATION,
)
_PROFIT_TAKE_TYPES = (RecommendationType.PARTIAL_PROFIT_TAKE, RecommendationType.FULL_PROFIT_TAKE)

#: ★ 評価対象外であることが**仕様として妥当**な型(Issue #270)。
#: 常にINCONCLUSIVEになるが、それは欠陥ではないため、週次改善レビューは
#: 「評価定義が未整備」の改善候補としてGitHub Issueを自動起票しない。
#:
#: URGENT_HOLDING_REVIEW(ハードゲート発動「重大リスクのため緊急確認」)は、
#: 名称・意味ともに「**確認**」であって「売却」ではない。債務超過・継続企業の疑義
#: 等の発動を人間へ知らせる**状態**であり、「7暦日後に株価が下がったか」で
#: 妥当性を測ること自体が業務上不適切である
#: (WATCH_BEFORE_EARNINGSについて#10 / #241 / #25が確立した判断と同じ)。
#:
#: ★ ここへ型を足すのは**その型の評価定義を「不要」と決めた**ときだけである。
#:   「まだ決めていない」型は`_EVALUATION_UNDEFINED_TYPES`側であり、
#:   区別せずにここへ入れると**未整備が仕様として固定**されてしまう。
_EXCLUDED_TYPES = (RecommendationType.URGENT_HOLDING_REVIEW,)

#: ★ 評価基準が**まだ決まっていない**型(Issue #270 / #25)。
#: 現状INCONCLUSIVEだが、それは仕様ではなく未整備である。
#: 週次改善レビューは従来どおり改善候補(EVALUATION_CRITERIA_UNDEFINED)を出す。
#:
#: ★ Issue #25が型ごとに「評価すべきか / 仕様として対象外か」を判断し、
#:   後者と決まった型を`_EXCLUDED_TYPES`へ移す。本Issueでは**移さない**
#:   (#25の設計判断を先取りしないため)。
_EVALUATION_UNDEFINED_TYPES = (
    RecommendationType.WATCH_BEFORE_EARNINGS,
    RecommendationType.REVIEW_BEFORE_EARNINGS,
    RecommendationType.REVIEW_AFTER_EARNINGS,
    RecommendationType.MANUAL_REVIEW_REQUIRED,
    RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW,
    RecommendationType.PARTIAL_RISK_REDUCTION,
)


def is_evaluation_excluded_type(recommendation_type: RecommendationType) -> bool:
    """評価対象外であることが**仕様として妥当**な型かどうか(Issue #270)。

    週次改善レビューがこれをTrueと判定した型については、
    「評価定義が未整備」の改善候補をGitHub Issueへ起票しない
    (毎週同じIssueが立ち続けるノイズを止めるため)。

    ★ `is_performance_evaluated_type()`がFalseを返す型のうち、
      **「決めた結果、対象外」**なのがこちらで、
      **「まだ決めていない」**のが`_EVALUATION_UNDEFINED_TYPES`である。
      determine_evaluation_label()の挙動は両者で同一(INCONCLUSIVE)。
    """
    return recommendation_type in _EXCLUDED_TYPES


def is_performance_evaluated_type(recommendation_type: RecommendationType) -> bool:
    """determine_evaluation_label()がSUCCESS/ACCEPTABLE等の実質的な成績ラベルを
    付与しうる種別(=INCONCLUSIVE以外になりうる種別)かどうかを返す。振り返り
    機能改修の週次改善レビューが、成功率ベースの閾値判定(業績系)と
    評価定義未整備系(常にINCONCLUSIVE)のどちらの経路を使うか判定するために使う。
    """
    return recommendation_type in _ENTRY_TYPES or recommendation_type in _EXIT_TYPES


def is_entry_type(recommendation_type: RecommendationType) -> bool:
    """price_return_pctの上昇がSUCCESSを意味する種別(BUY/WATCH_BUY/HOLD)かどうか。

    週次改善レビューが、超過リターン(自社株リターン-ベンチマークリターン)ベースの
    悪化検知をENTRY型にのみ適用するために使う(EXIT型は下落がSUCCESSを意味する
    ため、超過リターンは方向が逆になり単純比較できない。2026-08-20、Issue #9・#11
    のコードレビュー対応)。
    """
    return recommendation_type in _ENTRY_TYPES


def is_exit_type(recommendation_type: RecommendationType) -> bool:
    """price_return_pctの下落がSUCCESSを意味する種別かどうか。is_entry_type()の対。"""
    return recommendation_type in _EXIT_TYPES


def determine_evaluation_label(
    recommendation_type: RecommendationType,
    price_return_pct: float | None,
    excess_return_pct: float | None,
    max_drawdown_pct: float | None,
    config: EvaluationRulesConfig,
) -> tuple[EvaluationLabel, str]:
    if price_return_pct is None:
        return EvaluationLabel.DATA_ISSUE, "評価時点の株価データが取得できませんでした"

    if recommendation_type in _ENTRY_TYPES:
        return _label_entry(price_return_pct, excess_return_pct, max_drawdown_pct, config)
    if recommendation_type in _EXIT_TYPES:
        return _label_exit(recommendation_type, price_return_pct, config)
    return EvaluationLabel.INCONCLUSIVE, f"{recommendation_type.value}は自動評価の対象外です"


def _label_entry(
    price_return_pct: float,
    excess_return_pct: float | None,
    max_drawdown_pct: float | None,
    config: EvaluationRulesConfig,
) -> tuple[EvaluationLabel, str]:
    if max_drawdown_pct is not None and max_drawdown_pct <= config.severe_decline_after_buy_pct:
        return (
            EvaluationLabel.RISK_UNDERESTIMATED,
            f"推奨後の最大下落率が{max_drawdown_pct:.1f}%に達し、想定リスクを超える下落が発生しました",
        )
    if price_return_pct > 0:
        if excess_return_pct is not None and excess_return_pct > 0:
            return (
                EvaluationLabel.SUCCESS,
                f"株価は{price_return_pct:.1f}%上昇し、ベンチマークを"
                f"{excess_return_pct:.1f}%上回りました",
            )
        return (
            EvaluationLabel.ACCEPTABLE,
            f"株価は{price_return_pct:.1f}%上昇しましたが、ベンチマーク対比では優位ではありませんでした",
        )
    return (
        EvaluationLabel.PRICE_TOO_HIGH,
        f"株価は{price_return_pct:.1f}%下落し、推奨価格が割高だった可能性があります",
    )


def _label_exit(
    recommendation_type: RecommendationType,
    price_return_pct: float,
    config: EvaluationRulesConfig,
) -> tuple[EvaluationLabel, str]:
    exit_cfg = config.exit_evaluation
    if price_return_pct <= exit_cfg.decline_confirms_good_call_pct:
        return (
            EvaluationLabel.SUCCESS,
            f"推奨後に株価は{price_return_pct:.1f}%下落しており、判断は妥当でした",
        )
    if price_return_pct >= exit_cfg.rally_flags_too_early_or_too_sensitive_pct:
        if recommendation_type in _PROFIT_TAKE_TYPES:
            return (
                EvaluationLabel.PROFIT_TAKE_TOO_EARLY,
                f"推奨後に株価はさらに{price_return_pct:.1f}%上昇しており、利確が早すぎた可能性があります",
            )
        return (
            EvaluationLabel.SELL_TOO_SENSITIVE,
            f"推奨後に株価は{price_return_pct:.1f}%上昇して回復しており、判定が過敏だった可能性があります",
        )
    return (
        EvaluationLabel.ACCEPTABLE,
        f"推奨後の株価変動は{price_return_pct:.1f}%にとどまり、明確な結論は得られませんでした",
    )
