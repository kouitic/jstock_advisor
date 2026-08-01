"""LINE通知サービス(要求仕様3節 line_notification_service、16〜19節)。

推奨種別ごとにメッセージを整形し、以下のいずれかに該当する場合のみLINEへ送信する
(要求仕様16節の再通知条件のうち機械的に判定可能なものを実装。決算発表・価格到達・
重要度上昇による再通知は将来の拡張ポイント)。
  - 当該銘柄・通知種別について過去に通知履歴が無い
  - 前回通知時から判定区分(recommendation_type)が変化した
  - 前回通知時から代表価格が設定閾値(%)以上変化した
  - 前回通知からresend_after_days日(暦日)以上経過した
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.data_quality_alert import DataQualityAlert
from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    BuyAction,
    CandidateSource,
    ConfidenceLevel,
    DividendComparisonOutcome,
    EligibilityBlockCategory,
    NotificationContext,
    NotificationStatus,
    NotificationType,
    RecommendationType,
    RecordDateUnknownReason,
    SourceType,
    buy_action_label,
)
from jstock_advisor.domain.entities.evaluation_audit import SUMMARY_CATEGORIES
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.jst import format_jst
from jstock_advisor.domain.signals.watchlist_screening import describe_matched_criteria
from jstock_advisor.infrastructure.line.client import LineClient
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.data_quality_service import DataQualityIssueSeverity, detect_anomalies
from jstock_advisor.services.recommendation_consistency_validator import validate_recommendation
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningResult

logger = logging.getLogger(__name__)

# notify_buy_candidates_digest()のチャンク単位送信結果(統合BUY候補パイプライン
# 2026-07で追加)。外部LINE APIとDynamoDBを1つのトランザクションにはできないため、
# 「LINE送信は成功したがログ保存に失敗した」中間状態を明示的に区別する。
BuyDigestSendOutcome = Literal["SENT_AND_RECORDED", "SENT_LOG_FAILED", "SEND_FAILED"]

_RECOMMENDATION_TO_NOTIFICATION_TYPE: dict[RecommendationType, NotificationType] = {
    RecommendationType.BUY: NotificationType.DAILY_BUY_CANDIDATES,
    RecommendationType.WATCH_BUY: NotificationType.WATCHLIST_BUY_SIGNAL,
    RecommendationType.WATCH: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.PARTIAL_PROFIT_TAKE: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.FULL_PROFIT_TAKE: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.SELL: NotificationType.SELL_SIGNAL,
    RecommendationType.URGENT_REVIEW: NotificationType.SELL_SIGNAL,
    # --- 決算直前・直後ルール(要求仕様14節)で追加 ---
    # 2026-07仕様レビュー対応: WATCH_BEFORE_EARNINGSは利確判定エンジンのWATCH抑制
    # 専用として使う(買い候補側のrecommend_earnings_aware_actionは実際には未接続の
    # ため競合しない)。買い候補向けのフォーマット(予想配当利回り等)を保有銘柄の
    # 通知に誤って使わないよう、PROFIT_TAKING_SIGNALへ送る。
    RecommendationType.WATCH_BEFORE_EARNINGS: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.PARTIAL_RISK_REDUCTION: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.REVIEW_AFTER_EARNINGS: NotificationType.PROFIT_TAKING_SIGNAL,
    # --- 売却判定エンジンの再設計(2026-07仕様)で追加 ---
    RecommendationType.REVIEW: NotificationType.SELL_SIGNAL,
    RecommendationType.MANUAL_REVIEW_REQUIRED: NotificationType.MANUAL_REVIEW_REQUIRED,
    # --- 利確判定エンジン再レビュー対応(2026-07)で追加(要求仕様§4) ---
    RecommendationType.REVIEW_BEFORE_EARNINGS: NotificationType.PROFIT_TAKING_SIGNAL,
    RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW: NotificationType.PROFIT_TAKING_SIGNAL,
}

_DISCLAIMER = "※最終的な投資判断は利用者が行ってください。"

# 購入候補まとめ通知(notify_buy_candidates_digest)を分割する目安の文字数
# (LINEのメッセージ長上限に対して余裕を持たせた値。BUYパイプライン第2次修正
# 2026-07で追加。要求仕様17節)。
_BUY_DIGEST_MAX_CHARS = 4500

# バッチサマリーの内訳区分(要求仕様§13)。domain/entities/evaluation_audit.pyの
# SUMMARY_CATEGORIESと同じキー集合を使う。
_BATCH_SUMMARY_CATEGORIES = SUMMARY_CATEGORIES

_WATCHLIST_SCREENING_POLICY_LABELS: dict[str, str] = {
    "high_dividend_financial_health": "高配当・財務健全性",
}

# LINEの文字数上限を考慮し、追加銘柄の詳細表示は上位10件までとする(要求仕様§12)。
_WATCHLIST_ADDITION_DETAIL_LIMIT = 10


def _yen(value: Decimal | int | float | str | None) -> str:
    """金額を円単位の整数・カンマ区切りで表示する(要求仕様レビュー対応: 小数点以下は表示しない)。

    Decimalが指数表記(例: Decimal('5.5E+2'))を内部的に保持している場合、
    to_integral_value()だけでは指数表記が残り "5.5E+2円" のように表示されてしまうため、
    Python組み込みのintへ変換して指数表記を確実に解消してから整形する。
    """
    if value is None:
        return "不明"
    amount = int(Decimal(str(value)).to_integral_value(rounding=ROUND_HALF_UP))
    return f"{amount:,}円"


# 判定区分の表示ラベル(要求仕様レビュー対応)。
# RecommendationTypeは業務ロジック上13種類に分かれるが、通知で読む側にとって重要なのは
# 「買い候補」「保有継続(様子見)」「一部売却を検討」「全部売却を検討」という
# おおまかな3区分+買い候補で、英語の生の列挙値(PARTIAL_PROFIT_TAKE等)は意味が伝わらない。
# 括弧内に元のニュアンス(決算前縮小・至急確認等)を残しつつ、基本語彙を3つに絞る。
_RECOMMENDATION_TYPE_LABELS: dict[RecommendationType, str] = {
    RecommendationType.BUY: "買い推奨",
    RecommendationType.WATCH_BUY: "買い候補(監視)",
    RecommendationType.HOLD: "保有継続",
    RecommendationType.WATCH: "保有継続(監視)",
    # 2026-07仕様レビュー対応: WATCH_BEFORE_EARNINGSは利確判定エンジン専用のため
    # 「保有継続」系の語彙を使う(以前の「買い候補」表記は買い候補通知向けの
    # 誤った流用だった)。
    RecommendationType.WATCH_BEFORE_EARNINGS: "保有継続(決算待ち)",
    RecommendationType.REVIEW: "要確認",
    RecommendationType.REVIEW_AFTER_EARNINGS: "要確認(決算後)",
    RecommendationType.REVIEW_BEFORE_EARNINGS: "要確認(決算前)",
    RecommendationType.MANUAL_REVIEW_REQUIRED: "人的確認が必要",
    RecommendationType.PARTIAL_PROFIT_TAKE: "一部売却を検討",
    RecommendationType.PARTIAL_RISK_REDUCTION: "一部縮小を検討",
    RecommendationType.FULL_PROFIT_TAKE: "全部売却を検討",
    RecommendationType.SELL: "売却を検討",
    RecommendationType.URGENT_REVIEW: "至急確認",
    RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW: "保有比率を確認",
}


def _recommendation_type_label(recommendation_type: RecommendationType) -> str:
    return _RECOMMENDATION_TYPE_LABELS.get(recommendation_type, recommendation_type.value)


_RECORD_DATE_UNKNOWN_REASON_LABELS: dict[RecordDateUnknownReason, str] = {
    RecordDateUnknownReason.SOURCE_NOT_FOUND: "未登録(要ユーザー登録)",
    RecordDateUnknownReason.PARSE_ERROR: "取得データの解析に失敗",
    RecordDateUnknownReason.CORPORATE_ACTION_UNRESOLVED: "企業行動未反映のため保留",
    RecordDateUnknownReason.DATA_PROVIDER_MISSING: "データ提供元が非対応(恒久的)",
    RecordDateUnknownReason.NOT_APPLICABLE: "該当なし",
}

_DIVIDEND_COMPARISON_OUTCOME_LABELS: dict[DividendComparisonOutcome, str] = {
    DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT: "減配(実績確定)",
    DividendComparisonOutcome.FORECAST_DIVIDEND_CUT: "予想減配(未確定)",
    DividendComparisonOutcome.SPLIT_ADJUSTMENT_ONLY: "分割調整のみ(実質的な減配ではない)",
    DividendComparisonOutcome.DIVIDEND_MAINTAINED: "配当維持",
    DividendComparisonOutcome.DIVIDEND_INCREASE: "増配",
    DividendComparisonOutcome.COMPARISON_NOT_POSSIBLE: "比較不可",
}

# データ品質アラートの対応内容(要求仕様18節): check_nameごとに「何を確認・実施すべきか」を明示する
_RECOMMENDED_ACTION_BY_CHECK: dict[str, str] = {
    "full_take_extreme_margin": (
        "全株利確検討価格が現在値から極端に乖離しています。"
        "適正価格算出の入力データ(企業行動調整・財務データ等)に誤りがないか確認してください。"
    ),
    "full_take_no_price_guidance": (
        "全株利確判定なのに指値候補が算出されていません。データ取得状況を確認してください。"
    ),
    "watch_recommends_immediate_sell": (
        "監視判定なのに即時執行目安の価格が提示されています。判定ロジックの不整合の"
        "可能性があるため、該当銘柄の他の通知履歴と照合してください。"
    ),
    "three_or_more_equal_prices": (
        "3つ以上の価格フィールドが同一値になっています。適正価格・利確価格の"
        "算出元データを確認してください。"
    ),
    "reevaluation_unreasonably_above_full_take": (
        "再評価価格が全株利確検討価格より不合理に高くなっています。"
        "適正価格レンジの算出結果を確認してください。"
    ),
    "low_fair_value_confidence_full_take": (
        "適正価格の信頼度がLOWのまま全株利確判定が出ています。"
        "適正価格算出に使われた手法数・データ鮮度を確認してください。"
    ),
    "full_take_with_insufficient_gain_and_reasons": (
        "含み益率が全利確閾値未満なのに根拠が少数です。判定ロジックの条件充足状況を"
        "確認してください。"
    ),
    "sufficient_yield_full_take_on_yield_alone": (
        "総合利回りが基準以上なのに利回り低下のみを根拠に全株利確が出ています。"
        "配当・優待データを確認してください。"
    ),
    "price_equals_current_with_target_basis": (
        "価格フィールドが現在値と一致していますが、目標価格として扱われています。"
        "basis(算出根拠の種別)の設定を確認してください。"
    ),
    "fair_value_out_of_plausible_range": (
        "適正価格が現在株価から大きく外れた倍率になっています。"
        "財務データ(EPS/BPS等)の取得元・算出ロジックを確認してください。"
    ),
    "fair_value_changed_sharply": (
        "前回分析から適正価格が大きく変動しています。決算発表・企業行動(株式分割等)・"
        "データ取得エラーのいずれかが原因である可能性があります。CloudWatch Logsで"
        "直近の分析ログを確認してください。"
    ),
    "price_change_resembles_split_ratio": (
        "株価が前回から典型的な分割比率に近い倍率で変化しています。未反映の株式分割が"
        "ないか確認し、必要であれば企業行動レジストリに手動登録してください。"
    ),
    "full_profit_take_with_unrealized_loss": (
        "全株利確判定なのに含み損になっています。判定ロジックに重大な誤りがある"
        "可能性が高いため、優先的に確認してください。"
    ),
}
_DEFAULT_RECOMMENDED_ACTION = (
    "検出内容を確認し、必要に応じてデータの再取得や手動確認を行ってください。"
    "原因が分からない場合はCloudWatch Logsで該当銘柄のログを確認してください。"
)


def _build_recommended_action(check_names: list[str]) -> str:
    actions = dict.fromkeys(
        _RECOMMENDED_ACTION_BY_CHECK.get(name, _DEFAULT_RECOMMENDED_ACTION) for name in check_names
    )
    return " / ".join(actions) if actions else _DEFAULT_RECOMMENDED_ACTION


def _representative_price(recommendation: Recommendation) -> Decimal | None:
    if recommendation.buy_prices is not None and recommendation.buy_prices.standard is not None:
        return recommendation.buy_prices.standard.price
    if recommendation.sell_prices is not None:
        for level in (
            recommendation.sell_prices.recommended_limit_price,
            recommendation.sell_prices.stop_review_price,
            recommendation.sell_prices.partial_profit_start_price,
        ):
            if level is not None:
                return level.price
    return None


def _compute_content_hash(recommendation_type: RecommendationType) -> str:
    return hashlib.sha256(recommendation_type.value.encode()).hexdigest()[:16]


def compute_watchlist_addition_content_hash(
    stock_codes: list[str], evaluated_at: dt.datetime
) -> str:
    """ウォッチリスト自動追加通知の重複抑止に使うcontent hash(運用ハードニング
    第2弾2節: watchlist_batch_finalizer.pyがBatchRunsTable項目へ同じ値を
    永続化するために公開関数として切り出す。notify_watchlist_additions()の
    算出式と必ず一致させること、送信内容・dedup判定ロジック自体は変更しない)。
    """
    return hashlib.sha256(
        f"{evaluated_at.date().isoformat()}|{sorted(stock_codes)}".encode()
    ).hexdigest()[:16]


def _record_date_display(
    date: dt.date | None,
    reason: RecordDateUnknownReason | None,
    recurring_label: str | None = None,
) -> str:
    if date is not None:
        return date.isoformat()
    if recurring_label is not None:
        return recurring_label
    if reason is not None:
        return f"不明({_RECORD_DATE_UNKNOWN_REASON_LABELS[reason]})"
    return "不明"


# 基準日情報の情報区分表示(2026-07仕様レビュー対応)。SourceTypeの各値を
# 「会社公式情報」「手動登録データ」「データ提供元」の3区分いずれかへ写像する
# (Recommendation側は既存のSourceTypeをそのまま保持し、表示ラベルへの変換は
# ここに閉じ込める。統合ラベルにはせず5区分を明確に区別する)。
_RECORD_DATE_SOURCE_TYPE_LABELS: dict[SourceType, str] = {
    SourceType.COMPANY_IR: "会社公式情報",
    SourceType.TDNET_EDINET: "会社公式情報",
    SourceType.EXCHANGE: "会社公式情報",
    SourceType.MANUAL_REGISTRY: "手動登録データ",
    SourceType.CONTRACTED_PROVIDER: "データ提供元",
    SourceType.SECONDARY: "データ提供元",
    SourceType.OTHER_WEB: "データ提供元",
}


def _record_date_category_label(
    date: dt.date | None,
    recurring_label: str | None,
    source_type: SourceType | None,
) -> str | None:
    """基準日表示の情報区分(会社公式情報/手動登録データ/データ提供元/
    決算期末等からの推定)を返す。日付・推定ラベルのいずれも無ければNone
    (呼び出し側で「情報なし」の理由表示にフォールバックする)。
    """
    if date is None and recurring_label is None:
        return None
    if source_type is not None:
        return _RECORD_DATE_SOURCE_TYPE_LABELS[source_type]
    return "決算期末等からの推定"


def _record_date_lines(
    label: str,
    date: dt.date | None,
    reason: RecordDateUnknownReason | None,
    recurring_label: str | None,
    source_type: SourceType | None,
) -> list[str]:
    """基準日1件分の表示(要求仕様§1: 単に「不明」とだけ通知しない)。

    値に加えて、情報区分(会社公式情報/手動登録データ/データ提供元/決算期末等
    からの推定)、または情報が無い場合はその理由を明示する。
    """
    text = _record_date_display(date, reason, recurring_label)
    lines = [f"{label}:", text]
    category = _record_date_category_label(date, recurring_label, source_type)
    if category is not None:
        lines.append(f"情報区分：{category}")
    elif reason is not None:
        lines.append(f"理由：{_RECORD_DATE_UNKNOWN_REASON_LABELS[reason]}")
    return lines


def _confirmation_lines(recommendation: Recommendation) -> list[str]:
    """確認事項(要求仕様16節): 権利確定情報は情報区分・理由付き、配当比較は
    比較年度付きで表示する。

    正確な次回日付が不明でも、決算期末等から推定できる周期パターン
    (recurring_label)があれば単なる「不明」の代わりに表示する
    (要求仕様レビュー対応)。
    """
    lines = ["【確認事項】"]
    lines.extend(
        _record_date_lines(
            "配当権利確定日",
            recommendation.dividend_record_date,
            recommendation.dividend_record_date_unknown_reason,
            recommendation.dividend_record_date_recurring_label,
            recommendation.dividend_record_date_source_type,
        )
    )
    lines.extend(
        _record_date_lines(
            "優待権利確定日",
            recommendation.benefit_record_date,
            recommendation.benefit_record_date_unknown_reason,
            recommendation.benefit_record_date_recurring_label,
            recommendation.benefit_record_date_source_type,
        )
    )
    outcome = recommendation.dividend_comparison_outcome
    if outcome is not None:
        source_year = recommendation.dividend_comparison_source_fiscal_year or "不明"
        target_year = recommendation.dividend_comparison_target_fiscal_year or "不明"
        lines.append(
            f"配当比較({source_year} → {target_year}): "
            f"{_DIVIDEND_COMPARISON_OUTCOME_LABELS[outcome]}"
        )
    return lines


# 購入判断における業種別モデルの区分表示(2026-07 BUYパイプライン再設計。要求仕様12節・22節)。
_BUY_INDUSTRY_SECTOR_LABELS: dict[str, str] = {
    "BANK": "銀行",
    "LEASE_FINANCE": "リース・金融",
    "PHARMACEUTICAL": "医薬品",
    "AUTOMOTIVE_PARTS": "自動車部品",
    "CYCLICAL_MATERIALS": "素材(景気循環)",
    "UTILITY": "電気・ガス",
    "FOOD": "食品",
    "GENERAL_MANUFACTURING": "一般製造業",
    "SMALL_GROWTH": "小型成長株",
    "GENERAL": "一般事業会社",
    "UNKNOWN": "業種不明",
}


def _buy_industry_label(recommendation: Recommendation) -> str:
    sector = recommendation.buy_industry_sector
    key = sector.value if sector is not None else "UNKNOWN"
    return _BUY_INDUSTRY_SECTOR_LABELS.get(key, "業種不明")


def _dispersion_display(recommendation: Recommendation) -> str:
    ratio = recommendation.valuation_dispersion_ratio
    if ratio is None:
        return "算出不可"
    return f"{float(ratio):.2f}倍"


def _valuation_range_lines(recommendation: Recommendation) -> list[str]:
    """適正価格レンジ・購入判断基準価格(要求仕様9節・11節)。単一の「最終適正価格」を
    断定的に表示する旧設計は廃止した。

    --- BUYパイプライン第2次修正(2026-07)で変更。要求仕様9節・10節 ---
    下方外れ値(DCFの単年度キャッシュフロー歪み等)を除外した「決定用」レンジ
    (decision_valuation_min/max)を表示する。外れ値込みの全手法参考値
    (valuation_min/max)は監査ログのみで確認できるようにし、通知には表示しない。
    """
    lines = []
    if (
        recommendation.decision_valuation_min is not None
        and recommendation.decision_valuation_max is not None
    ):
        lines.append(
            f"適正価格レンジ: {_yen(recommendation.decision_valuation_min)}〜"
            f"{_yen(recommendation.decision_valuation_max)}"
        )
    if recommendation.valuation_anchor is not None:
        lines.append(f"購入判断基準価格: {_yen(recommendation.valuation_anchor)}")
    return lines


def _buy_price_levels_line(recommendation: Recommendation) -> str | None:
    bp = recommendation.buy_prices
    if bp is None or not (bp.entry and bp.standard and bp.strong):
        return None
    return (
        f"打診買い:{_yen(bp.entry.price)} 標準買い:{_yen(bp.standard.price)} "
        f"積極買い:{_yen(bp.strong.price)}"
    )


def _margin_adjustment_reasons_line(recommendation: Recommendation) -> str | None:
    """必要安全余裕を拡大した主な理由(要求仕様8節)。内部値すべては表示しない。

    カテゴリ内で不採用(superseded_by設定あり)になった調整はLINE本文には表示しない
    (監査ログには引き続きすべて保存される。BuySignalService.analyze()側の
    self._audit.record()呼び出しはrecommendation.margin_adjustmentsをそのまま
    渡しており、このフィルタは表示専用でaudit記録には影響しない)。
    """
    adopted_adjustments = [
        adjustment
        for adjustment in recommendation.margin_adjustments
        if adjustment.superseded_by is None
    ]
    if not adopted_adjustments:
        return None
    reasons = "\n".join(f"・{a.reason}" for a in adopted_adjustments)
    return f"必要安全余裕を拡大した理由:\n{reasons}"


_PRICE_CONDITION_LABELS: dict[BuyAction, str] = {
    BuyAction.STRONG_BUY: "積極買い価格以下",
    BuyAction.BUY: "標準買い価格以下",
    BuyAction.SMALL_ENTRY: "打診買い価格以下",
}


def _buy_candidate_digest_block(rank: int, recommendation: Recommendation) -> str:
    """購入候補まとめ通知(notify_buy_candidates_digest)の1銘柄分のブロック
    (BUYパイプライン第2次修正2026-07で追加。要求仕様17節・18節)。1銘柄1通では
    なく、最大5銘柄を1通(または分割時のみ複数通)にまとめるための本文断片。

    統合BUY候補パイプライン(2026-07)で気になる銘柄・保有銘柄の種別表示を追加。
    このダイジェストが実際の日次バッチ通知の本文そのものであるため、
    _format_buy_candidate_message(単独送信用)と同じ表示をここにも反映する。
    """
    action = recommendation.buy_action
    lines = [
        f"{rank}位 {recommendation.stock_code} {recommendation.stock_name}",
        f"判定: {buy_action_label(action) if action is not None else '購入候補'}",
    ]
    # candidate_source欠落の既存レコードは気になる銘柄として安全にフォールバックする。
    source = recommendation.candidate_source or CandidateSource.WATCHLIST
    lines.append(f"種別: {_CANDIDATE_SOURCE_LABELS[source]}")
    if source in (CandidateSource.HOLDING, CandidateSource.BOTH):
        if recommendation.holding_quantity is not None:
            lines.append(f"現在の保有: {recommendation.holding_quantity}株")
        if recommendation.average_acquisition_price is not None:
            lines.append(f"平均取得単価: {_yen(recommendation.average_acquisition_price)}")
        # 含み損益は参考情報としてのみ表示する(買い増し理由には使わない)。
        if recommendation.unrealized_profit_loss is not None:
            pct_part = (
                f"({recommendation.unrealized_profit_loss_pct:+.1f}%)"
                if recommendation.unrealized_profit_loss_pct is not None
                else ""
            )
            lines.append(
                f"評価損益(参考): {_yen(recommendation.unrealized_profit_loss)}{pct_part}"
            )
    lines.append(f"現在値: {_yen(recommendation.price_at_recommendation)}")
    price_line = _buy_price_levels_line(recommendation)
    if price_line is not None:
        lines.append(price_line)
    if action is not None and action in _PRICE_CONDITION_LABELS:
        lines.append(f"価格条件: {_PRICE_CONDITION_LABELS[action]}")
    if (
        recommendation.purchase_attractiveness_score is not None
        and recommendation.company_quality_score is not None
    ):
        lines.append(
            f"購入魅力度: {recommendation.purchase_attractiveness_score:.1f}点"
            f"　企業魅力度: {recommendation.company_quality_score:.1f}点"
        )
    if recommendation.total_yield_pct_at_recommendation is not None:
        lines.append(f"総合利回り: {recommendation.total_yield_pct_at_recommendation:.2f}%")
    lines.append(
        f"次回決算日: {recommendation.next_earnings_date}"
        if recommendation.next_earnings_date
        else "次回決算日: 不明"
    )
    lines.append(f"適正価格信頼度: {recommendation.confidence.value}")
    if recommendation.reasons:
        lines.append("主な理由: " + " / ".join(recommendation.reasons))
    if recommendation.counter_factors:
        lines.append("主なリスク: " + " / ".join(recommendation.counter_factors))
    return "\n".join(lines)


_CANDIDATE_SOURCE_LABELS: dict[CandidateSource, str] = {
    CandidateSource.WATCHLIST: "気になる銘柄",
    CandidateSource.HOLDING: "保有銘柄・買い増し候補",
    CandidateSource.BOTH: "保有銘柄・気になる銘柄",
}


def _format_buy_candidate_message(recommendation: Recommendation) -> str:
    """購入候補通知(STRONG_BUY/BUY/SMALL_ENTRY、2026-07 BUYパイプライン再設計。要求仕様22節。
    統合BUY候補パイプライン2026-07で気になる銘柄・保有銘柄の種別表示を追加)。
    """
    action = recommendation.buy_action
    title = buy_action_label(action) if action is not None else "購入候補"
    lines = [f"【{title}】{recommendation.stock_code} {recommendation.stock_name}"]
    # candidate_source欠落の既存レコードは気になる銘柄として安全にフォールバックする。
    source = recommendation.candidate_source or CandidateSource.WATCHLIST
    lines.append(f"種別: {_CANDIDATE_SOURCE_LABELS[source]}")
    if source in (CandidateSource.HOLDING, CandidateSource.BOTH):
        if recommendation.holding_quantity is not None:
            lines.append(f"現在の保有: {recommendation.holding_quantity}株")
        if recommendation.average_acquisition_price is not None:
            lines.append(f"平均取得単価: {_yen(recommendation.average_acquisition_price)}")
        # 含み損益は参考情報としてのみ表示する(買い増し理由には使わない。
        # 通知理由は現在値と適正価格・企業品質・購入魅力度・安全余裕・集中リスクに基づく)。
        if recommendation.unrealized_profit_loss is not None:
            pct_part = (
                f"({recommendation.unrealized_profit_loss_pct:+.1f}%)"
                if recommendation.unrealized_profit_loss_pct is not None
                else ""
            )
            lines.append(
                f"評価損益(参考): {_yen(recommendation.unrealized_profit_loss)}{pct_part}"
            )
    lines.append(f"現在値: {_yen(recommendation.price_at_recommendation)}")
    lines.extend(_valuation_range_lines(recommendation))
    price_line = _buy_price_levels_line(recommendation)
    if price_line is not None:
        lines.append(price_line)
    if recommendation.current_vs_entry_price_pct is not None:
        pct = float(recommendation.current_vs_entry_price_pct)
        direction = "上回る" if pct >= 0 else "下回る"
        lines.append(f"現在値と打診買い価格の差: 約{abs(pct):.1f}%{direction}")
    if recommendation.company_quality_score is not None:
        lines.append(f"企業魅力度: {recommendation.company_quality_score:.1f}点")
    if recommendation.purchase_attractiveness_score is not None:
        lines.append(f"購入魅力度: {recommendation.purchase_attractiveness_score:.1f}点")
    if recommendation.total_yield_pct_at_recommendation is not None:
        lines.append(f"総合利回り: {recommendation.total_yield_pct_at_recommendation:.2f}%")
    if recommendation.next_earnings_date:
        lines.append(f"次回決算予定日: {recommendation.next_earnings_date}")
    lines.append(f"適正価格信頼度: {recommendation.confidence.value}")
    lines.append(f"算出手法間のばらつき: {_dispersion_display(recommendation)}")
    applied = "適用" if recommendation.industry_model_applied else "未適用"
    lines.append(f"業種別モデル: {_buy_industry_label(recommendation)}({applied})")
    if recommendation.reasons:
        lines.append("主な評価理由: " + " / ".join(recommendation.reasons))
    if recommendation.counter_factors:
        lines.append("弱み: " + " / ".join(recommendation.counter_factors))
    margin_line = _margin_adjustment_reasons_line(recommendation)
    if margin_line is not None:
        lines.append(margin_line)
    lines.extend(_confirmation_lines(recommendation))
    if recommendation.data_sources:
        fetched_at = min(s.fetched_at for s in recommendation.data_sources)
        lines.append(f"データ取得日時: {format_jst(fetched_at)}")
    lines.append(f"通知ID: {recommendation.recommendation_id}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_watch_for_price_message(recommendation: Recommendation) -> str:
    """価格待ち通知(WATCH_FOR_PRICE/WATCH_BEFORE_EARNINGS、2026-07 BUYパイプライン
    再設計。要求仕様17節・22節)。「企業として魅力があること」と「現在価格で
    購入すべきこと」を分け、良い企業でも価格が高ければ購入候補には含めない。
    """
    action = recommendation.buy_action
    title = buy_action_label(action) if action is not None else "監視継続"
    lines = [f"【{title}】{recommendation.stock_code} {recommendation.stock_name}"]
    lines.append(f"現在値: {_yen(recommendation.price_at_recommendation)}")
    lines.extend(_valuation_range_lines(recommendation))
    price_line = _buy_price_levels_line(recommendation)
    if price_line is not None:
        lines.append(price_line)
    # 「打診買い価格まで」はあと何%の下落が必要かを示す指標であり、現在値が
    # entryを何%上回っているか(current_vs_entry_price_pct)とは意味が異なる
    # (BUYパイプライン第2次修正2026-07で修正。旧実装は現在値がentryを上回る
    # 割合をそのまま「まで」の文言で表示しており、数式と文言が矛盾していた)。
    if recommendation.required_decline_to_entry_pct is not None:
        pct = float(recommendation.required_decline_to_entry_pct)
        lines.append(f"打診買い価格まで: 約{abs(pct):.1f}%の下落が必要")
    if recommendation.company_quality_score is not None:
        lines.append(f"企業魅力度: {recommendation.company_quality_score:.1f}点")
    if recommendation.total_yield_pct_at_recommendation is not None:
        lines.append(f"総合利回り: {recommendation.total_yield_pct_at_recommendation:.2f}%")
    if recommendation.reasons:
        lines.append("企業評価: " + " / ".join(recommendation.reasons))
    if action == BuyAction.WATCH_BEFORE_EARNINGS:
        judgment = "購入判断: 次回決算が近いため、決算内容を確認してから再評価します。"
    else:
        judgment = (
            "購入判断: 現在値が打診買い価格を上回っているため、"
            "今回の購入候補には含めません。"
        )
    lines.append(judgment)
    if recommendation.next_earnings_date:
        lines.append(f"次回決算予定日: {recommendation.next_earnings_date}")
    lines.append(f"算出手法間のばらつき: {_dispersion_display(recommendation)}")
    lines.extend(_confirmation_lines(recommendation))
    lines.append(f"通知ID: {recommendation.recommendation_id}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


_BASIS_TYPE_LABELS: dict[str, str] = {
    "FAIR_VALUE_THRESHOLD": "バリュエーション基準",
    "PURCHASE_PRICE_RETURN_TARGET": "取得価格基準",
    "DIVIDEND_YIELD_THRESHOLD": "配当利回り基準",
    "TOTAL_YIELD_THRESHOLD": "総合利回り基準",
    "TECHNICAL_PRICE_LEVEL": "テクニカル基準",
    "USER_DEFINED_TARGET": "ユーザー設定目標",
}


def _basis_label(basis_type: object) -> str:
    if basis_type is None:
        return ""
    return _BASIS_TYPE_LABELS.get(getattr(basis_type, "value", str(basis_type)), "")


def _price_display(field: object) -> str:
    price = getattr(field, "price", None)
    low = getattr(field, "price_low", None)
    high = getattr(field, "price_high", None)
    if low is not None and high is not None:
        return f"{_yen(low)}〜{_yen(high)}"
    return _yen(price)


def _fair_value_range_lines(recommendation: Recommendation) -> list[str]:
    if (
        recommendation.fair_value_bear is None
        and recommendation.fair_value_neutral is None
        and recommendation.fair_value_bull is None
    ):
        return []
    lines = [
        "適正価格レンジ:",
        f"弱気：{_yen(recommendation.fair_value_bear)} ／ "
        f"中立：{_yen(recommendation.fair_value_neutral)} ／ "
        f"強気：{_yen(recommendation.fair_value_bull)}",
    ]
    used_methods = [m for m in recommendation.fair_value_methods if m.get("fair_value") is not None]
    if used_methods:
        lines.append("算出手法:")
        for m in used_methods:
            lines.append(f"・{m['method']}：{_yen(m['fair_value'])}")
    if recommendation.fair_value_spread_ratio is not None:
        lines.append(f"手法間乖離(強気/弱気): {recommendation.fair_value_spread_ratio:.2f}倍")
    if recommendation.fair_value_overall_confidence is not None:
        # 2026-07仕様レビュー対応: 判定全体の信頼度(Recommendation.confidence)と
        # 混同されないよう、「何の信頼度か」を明示するラベルにする。
        lines.append(f"適正価格算出の信頼度: {recommendation.fair_value_overall_confidence.value}")
    return lines


def _is_fair_value_dispersion_large(
    recommendation: Recommendation, large_spread_ratio_threshold: float
) -> bool:
    """適正価格の信頼度がLOW、または手法間乖離が閾値以上かどうか(要求仕様§7)。

    このいずれかに該当する場合、「強気シナリオの想定範囲内」等、強気価格を
    根拠に保有継続を支持するような楽観的な文言を抑止する。
    """
    ratio = recommendation.fair_value_spread_ratio
    return recommendation.fair_value_overall_confidence == ConfidenceLevel.LOW or (
        ratio is not None and ratio >= large_spread_ratio_threshold
    )


def _current_price_position_lines(
    recommendation: Recommendation, large_spread_ratio_threshold: float
) -> list[str]:
    """現在株価が中立/強気適正価格に対してどの位置にあるかを表示する(要求仕様§1)。

    以前は監視開始価格(閾値ベースの価格)を使って割高率を計算しており、どの銘柄でも
    ほぼ同じ%になる不具合があった。必ず実際の現在株価とfair_value_neutral/bullの
    比率から算出する。

    2026-07仕様レビュー対応: 適正価格の信頼度がLOW、または手法間乖離が大きい
    場合は「強気シナリオの想定範囲内」という楽観的な補足を付けない(手法間の
    推定差が大きいことの注意書きは別途_fair_value_dispersion_warning_linesが担う)。
    """
    lines: list[str] = []
    neutral_pct = recommendation.current_price_vs_neutral_fair_value_pct
    bull_pct = recommendation.current_price_vs_bull_fair_value_pct
    dispersion_large = _is_fair_value_dispersion_large(recommendation, large_spread_ratio_threshold)
    if neutral_pct is not None:
        if neutral_pct >= 0:
            lines.append(f"・中立適正価格を{neutral_pct:.1f}%上回る")
        else:
            lines.append(f"・中立適正価格を{abs(neutral_pct):.1f}%下回る")
    if bull_pct is not None:
        if bull_pct >= 0:
            lines.append(f"・強気適正価格を{bull_pct:.1f}%上回る")
        elif dispersion_large:
            lines.append(f"・強気適正価格を{abs(bull_pct):.1f}%下回る")
        else:
            lines.append(f"・強気適正価格を{abs(bull_pct):.1f}%下回る(強気シナリオの想定範囲内)")
    return lines


def _dividend_increase_lines(recommendation: Recommendation) -> list[str]:
    lines: list[str] = []
    years = recommendation.consecutive_actual_dividend_increase_years
    if years is not None and years > 0:
        lines.append(f"実績で{years}期連続増配")
    if recommendation.forecast_dividend_increase is True:
        rate = recommendation.forecast_dividend_increase_rate
        rate_text = f"(前期比+{rate:.1f}%)" if rate is not None else ""
        lines.append(f"今期も増配予想{rate_text}")
    elif recommendation.forecast_dividend_increase is False:
        lines.append("今期の予想配当は前期実績を下回る")
    return lines


_WATCH_TITLE_EARNINGS_PENDING = "適正価格超過・決算後に再評価"
_WATCH_TITLE_DISPERSION_LARGE = "適正価格のばらつき大・継続監視"
_WATCH_TITLE_DEFAULT = "割高水準を監視"
_WATCH_TITLE_DATA_INSUFFICIENT = "保有継続・データ確認待ち"


def _resolve_watch_profit_taking_title(
    recommendation: Recommendation, large_spread_ratio_threshold: float
) -> str:
    """WATCH(監視)判定通知の見出しを状態別に出し分ける(要求仕様§4)。

    以前は「割高水準を監視」に固定されており、決算待ちで暫定判定であることや
    適正価格のばらつきが大きく信頼度が低いことが見出しから分からなかった。
    recommendation_type等の構造化フィールドのみで判定し、文字列一致は使わない。
    """
    if recommendation.recommendation_type == RecommendationType.WATCH_BEFORE_EARNINGS:
        return _WATCH_TITLE_EARNINGS_PENDING
    has_fair_value = any(
        v is not None
        for v in (
            recommendation.fair_value_bear,
            recommendation.fair_value_neutral,
            recommendation.fair_value_bull,
        )
    )
    if not has_fair_value:
        return _WATCH_TITLE_DATA_INSUFFICIENT
    if _is_fair_value_dispersion_large(recommendation, large_spread_ratio_threshold):
        return _WATCH_TITLE_DISPERSION_LARGE
    return _WATCH_TITLE_DEFAULT


def _fair_value_dispersion_warning_lines(
    recommendation: Recommendation, large_spread_ratio_threshold: float
) -> list[str]:
    """適正価格の信頼度がLOW、または手法間乖離が大きい場合、どの手法が突出して
    強気価格を作っているかを補足する(要求仕様§7)。

    特定の手法名(DCF等)をハードコードせず、実際にfair_value_methods内で
    最大値を持つ手法を動的に特定する。手法数が少ない、または最大値が他の
    手法から突出していない場合は無理に表示しない。
    """
    methods = [m for m in recommendation.fair_value_methods if m.get("fair_value") is not None]
    if len(methods) < 3:
        return []
    if not _is_fair_value_dispersion_large(recommendation, large_spread_ratio_threshold):
        return []
    values = sorted((Decimal(str(m["fair_value"])), str(m["method"])) for m in methods)
    max_value, max_method = values[-1]
    rest_values = [v for v, _ in values[:-1]]
    if not rest_values or max_value <= max(rest_values):
        return []
    rest_min, rest_max = min(rest_values), max(rest_values)
    price = recommendation.price_at_recommendation
    if price > rest_max:
        direction = "上回っています"
    elif price < rest_min:
        direction = "下回っています"
    else:
        direction = "範囲内です"
    return [
        "適正価格に関する注意:",
        "・手法間の推定差が大きいため、強気価格だけを根拠に保有継続を判断できません",
        f"・{max_method}を除く適正価格は{_yen(rest_min)}〜{_yen(rest_max)}で、"
        f"現在値{_yen(price)}はその{direction}",
    ]


_DEFAULT_FAIR_VALUE_LARGE_SPREAD_RATIO = 2.0


def _format_watch_profit_taking_message(
    recommendation: Recommendation,
    large_spread_ratio_threshold: float = _DEFAULT_FAIR_VALUE_LARGE_SPREAD_RATIO,
) -> str:
    """WATCH(監視)判定専用のフォーマット(要求仕様レビュー対応)。

    即時執行を意味する価格は表示しない。適正価格レンジ・保有継続を支持する要因・
    直ちに利確しない理由・監視条件を明示する。
    """
    title = _resolve_watch_profit_taking_title(recommendation, large_spread_ratio_threshold)
    lines = [f"【{title}】{recommendation.stock_code} {recommendation.stock_name}", ""]
    lines.append("保有状況:")
    shares = recommendation.shares_at_recommendation
    avg = recommendation.average_purchase_price_at_recommendation
    price = recommendation.price_at_recommendation
    lines.append(f"{shares}株／平均取得{_yen(avg)}")
    lines.append(f"現在値{_yen(price)}")
    if shares is not None and avg is not None:
        gain = (price - avg) * shares
        gain_pct = float(price / avg - 1) * 100 if avg > 0 else 0.0
        lines.append(f"含み益{_yen(gain)}({gain_pct:+.1f}%)")
    lines.append("")
    is_earnings_pending = (
        recommendation.recommendation_type == RecommendationType.WATCH_BEFORE_EARNINGS
    )
    if is_earnings_pending:
        # 2026-07仕様レビュー対応(§5): 決算待ちで最終判定ではないことを明示する。
        # RecommendationType/判定ロジック自体は変更せず、表示のみ「暫定判定」に区別する。
        lines.append("暫定判定:")
        lines.append(_recommendation_type_label(recommendation.recommendation_type))
        lines.append("")
        days = recommendation.business_days_to_earnings
        if days is not None:
            lines.append(
                f"判断保留理由: 次回決算まで{days}営業日のため、決算内容確認後に再評価"
            )
            lines.append("")
    else:
        lines.append("判定:")
        lines.append(_recommendation_type_label(recommendation.recommendation_type))
        lines.append("")

    fv_lines = _fair_value_range_lines(recommendation)
    if fv_lines:
        lines.extend(fv_lines)
        lines.append("")

    sp = recommendation.sell_prices
    position_lines = _current_price_position_lines(recommendation, large_spread_ratio_threshold)
    if position_lines:
        lines.append("現在株価の位置:")
        lines.extend(position_lines)
        lines.append("")
    elif sp is not None and sp.partial_profit_start_price is not None:
        lines.append("割高懸念:")
        lines.append(f"監視開始水準({_price_display(sp.partial_profit_start_price)})に到達")
        lines.append("")

    dispersion_lines = _fair_value_dispersion_warning_lines(
        recommendation, large_spread_ratio_threshold
    )
    if dispersion_lines:
        lines.extend(dispersion_lines)
        lines.append("")

    if recommendation.counter_factors:
        lines.append("保有継続を支持する要因:")
        lines.extend(f"・{f}" for f in recommendation.counter_factors)
        lines.append("")

    if recommendation.not_yet_action_reasons:
        lines.append("直ちに利確しない理由:")
        lines.extend(f"・{r}" for r in recommendation.not_yet_action_reasons)
        lines.append("")

    if recommendation.next_review_conditions:
        lines.append("監視条件:")
        lines.extend(f"・{c}" for c in recommendation.next_review_conditions)
        lines.append("")

    if sp is not None and sp.full_profit_consideration_price is not None:
        p = sp.full_profit_consideration_price
        label = _basis_label(p.basis_type) or "参考"
        lines.append(f"{label}の全株利確目標:")
        full_gain_pct = float(p.price / avg - 1) * 100 if avg is not None and avg > 0 else None
        suffix = f"(含み益+{full_gain_pct:.0f}%)" if full_gain_pct is not None else ""
        lines.append(f"{_price_display(p)}{suffix}")
        lines.append("")

    lines.extend(
        _record_date_lines(
            "配当基準日",
            recommendation.dividend_record_date,
            recommendation.dividend_record_date_unknown_reason,
            recommendation.dividend_record_date_recurring_label,
            recommendation.dividend_record_date_source_type,
        )
    )
    lines.append("")
    lines.extend(
        _record_date_lines(
            "優待基準日",
            recommendation.benefit_record_date,
            recommendation.benefit_record_date_unknown_reason,
            recommendation.benefit_record_date_recurring_label,
            recommendation.benefit_record_date_source_type,
        )
    )
    lines.append("")
    # 「適正価格算出の信頼度」(_fair_value_range_lines)とは別軸の、この保有継続
    # 判定自体の信頼度であることをラベルで明示する(要求仕様§6、旧「信頼度:」の
    # ラベル無し表示を解消)。
    lines.append(f"保有継続判定の信頼度: {recommendation.confidence.value}")
    lines.append(f"通知ID: {recommendation.recommendation_id}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_earnings_suppressed_message(recommendation: Recommendation) -> str:
    """決算直前のためPARTIAL/FULL_PROFIT_TAKE提案を保留した場合の通知
    (要求仕様§4)。通常のPARTIAL/FULL向けの価格提案は表示しない
    (sell_prices自体がサービス層で空にされている)。
    """
    lines = [
        f"【要確認・決算直前】{recommendation.stock_code} {recommendation.stock_name}",
        "判定:",
        _recommendation_type_label(recommendation.recommendation_type),
        "",
        f"【保有状況】{recommendation.shares_at_recommendation}株 / "
        f"平均取得 {_yen(recommendation.average_purchase_price_at_recommendation)} → "
        f"現在 {_yen(recommendation.price_at_recommendation)}",
        "",
    ]
    if recommendation.not_yet_action_reasons or recommendation.reasons:
        lines.append("理由:")
        for r in recommendation.not_yet_action_reasons or recommendation.reasons:
            lines.append(f"・{r}")
        lines.append("")
    if recommendation.next_earnings_date:
        lines.append(f"次回決算: {recommendation.next_earnings_date}")
    lines.append(
        "判断: 適正価格上は割高な可能性がありますが、決算内容を確認後に再評価します。"
    )
    lines.extend(_confirmation_lines(recommendation))
    lines.append(f"判定の信頼度: {recommendation.confidence.value}")
    lines.append(f"通知ID: {recommendation.recommendation_id}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_portfolio_concentration_message(recommendation: Recommendation) -> str:
    """ポートフォリオ集中リスク通知(要求仕様§14)。

    企業価値判断(sell_signal/profit_taking)とは独立した通知であることを明示し、
    「売却シグナルはない」ことを明確に述べたうえで保有比率の高さのみを伝える。
    """
    lines = [
        f"【保有比率を確認】{recommendation.stock_code} {recommendation.stock_name}",
        "",
        "企業評価上の売却シグナルはありませんが、ポートフォリオ内の保有比率が"
        "高くなっています。",
        "",
    ]
    if recommendation.portfolio_weight_pct is not None:
        lines.append(f"時価ベースの保有比率: {recommendation.portfolio_weight_pct:.1f}%")
    if recommendation.portfolio_acquisition_cost_weight_pct is not None:
        lines.append(
            f"取得価格ベースの保有比率: {recommendation.portfolio_acquisition_cost_weight_pct:.1f}%"
        )
    if recommendation.reasons:
        lines.append("")
        lines.append("検出内容:")
        lines.extend(f"・{r}" for r in recommendation.reasons)
    lines.append("")
    lines.append(f"通知ID: {recommendation.recommendation_id}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_profit_taking_message(
    recommendation: Recommendation,
    large_spread_ratio_threshold: float = _DEFAULT_FAIR_VALUE_LARGE_SPREAD_RATIO,
) -> str:
    if recommendation.recommendation_type in (
        RecommendationType.WATCH,
        RecommendationType.WATCH_BEFORE_EARNINGS,
    ):
        return _format_watch_profit_taking_message(recommendation, large_spread_ratio_threshold)
    if recommendation.recommendation_type == RecommendationType.REVIEW_BEFORE_EARNINGS:
        return _format_earnings_suppressed_message(recommendation)
    if recommendation.recommendation_type == RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW:
        return _format_portfolio_concentration_message(recommendation)

    lines = [
        f"【利確検討】{recommendation.stock_code} {recommendation.stock_name}",
        f"【保有状況】{recommendation.shares_at_recommendation}株 / "
        f"平均取得 {_yen(recommendation.average_purchase_price_at_recommendation)} → "
        f"現在 {_yen(recommendation.price_at_recommendation)}",
        f"判定: {_recommendation_type_label(recommendation.recommendation_type)}",
    ]
    if recommendation.reasons:
        lines.append("利確を検討する理由: " + " / ".join(recommendation.reasons))
    if recommendation.counter_factors:
        lines.append("反対材料: " + " / ".join(recommendation.counter_factors))
    dividend_lines = _dividend_increase_lines(recommendation)
    if dividend_lines:
        lines.append("配当動向: " + " / ".join(dividend_lines))
    fv_lines = _fair_value_range_lines(recommendation)
    lines.extend(fv_lines)
    sp = recommendation.sell_prices
    next_decision_lines: list[str] = []
    if sp is not None:
        if sp.partial_profit_start_price:
            p = sp.partial_profit_start_price
            suffix = "(即時執行目安)" if p.basis.name == "IMMEDIATE_EXECUTION_REFERENCE" else ""
            label = _basis_label(p.basis_type)
            lines.append(f"一部利確開始価格({label}): {_price_display(p)}{suffix}")
        if sp.recommended_limit_price:
            p = sp.recommended_limit_price
            suffix = "(即時執行目安)" if p.basis.name == "IMMEDIATE_EXECUTION_REFERENCE" else ""
            label = _basis_label(p.basis_type)
            lines.append(f"利確推奨価格候補({label}): {_price_display(p)}{suffix}")
        elif recommendation.recommendation_type in (
            RecommendationType.PARTIAL_PROFIT_TAKE,
            RecommendationType.FULL_PROFIT_TAKE,
        ):
            lines.append("利確推奨価格(指値候補): 算出不能(総合利回り低下のみが根拠のため)")
        if sp.full_profit_consideration_price:
            p = sp.full_profit_consideration_price
            label = _basis_label(p.basis_type)
            lines.append(f"{label}の全株利確目標: {_price_display(p)}")
            next_decision_lines.append(f"全株利確目標({_price_display(p)})到達時に再検討")
        if sp.immediate_execution_price:
            p = sp.immediate_execution_price
            lines.append(f"即時執行目安価格: {_price_display(p)}")
        if sp.reevaluation_price_upside:
            p = sp.reevaluation_price_upside
            label = _basis_label(p.basis_type)
            lines.append(f"{label}の再評価水準(上昇時): {_price_display(p)}")
            next_decision_lines.append(f"{label}の再評価水準({_price_display(p)})到達時に再検討")
    if next_decision_lines:
        lines.append("次の判断条件: " + " / ".join(next_decision_lines))
    if recommendation.next_earnings_date:
        lines.append(f"次回決算予定日: {recommendation.next_earnings_date}")
    lines.extend(_confirmation_lines(recommendation))
    if recommendation.data_sources:
        fetched_at = min(s.fetched_at for s in recommendation.data_sources)
        lines.append(f"データ取得日時: {format_jst(fetched_at)}")
    lines.append(f"判定の信頼度: {recommendation.confidence.value}")
    lines.append(f"通知ID: {recommendation.recommendation_id}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_sell_message(recommendation: Recommendation) -> str:
    label = _recommendation_type_label(recommendation.recommendation_type)
    lines = [
        f"【{label}】{recommendation.stock_code} "
        f"{recommendation.stock_name}(投資前提悪化の可能性)",
        f"判定: {label}",
        f"【保有状況】{recommendation.shares_at_recommendation}株 / "
        f"平均取得 {_yen(recommendation.average_purchase_price_at_recommendation)} → "
        f"現在 {_yen(recommendation.price_at_recommendation)}",
    ]
    if recommendation.reasons:
        lines.append("悪化懸念(投資前提が悪化した理由): " + " / ".join(recommendation.reasons))
    if recommendation.counter_factors:
        lines.append("反対材料: " + " / ".join(recommendation.counter_factors))
    if recommendation.recommended_action_summary:
        lines.append("判定内容: " + recommendation.recommended_action_summary)
    sp = recommendation.sell_prices
    if sp is not None and sp.immediate_execution_price:
        lines.append(f"即時執行目安価格: {_yen(sp.immediate_execution_price.price)}")
    if sp is not None and sp.stop_review_price:
        lines.append(f"売却目安価格: {_yen(sp.stop_review_price.price)}")
    if recommendation.next_review_conditions:
        lines.append("次の判断条件: " + " / ".join(recommendation.next_review_conditions))
    if recommendation.holding_risks:
        lines.append("保有を継続する場合のリスク: " + " / ".join(recommendation.holding_risks))
    if recommendation.data_sources:
        fetched_at = min(s.fetched_at for s in recommendation.data_sources)
        lines.append(f"データ取得日時: {format_jst(fetched_at)}")
    lines.append(f"判定の信頼度: {recommendation.confidence.value}")
    lines.append(f"通知ID: {recommendation.recommendation_id}")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def _format_message(
    recommendation: Recommendation,
    notification_type: NotificationType,
    large_spread_ratio_threshold: float = _DEFAULT_FAIR_VALUE_LARGE_SPREAD_RATIO,
) -> str:
    # BUYパイプライン再設計(2026-07)以降のRecommendationはbuy_actionを持つ。
    # notification_type(recommendation_type由来)より先にこちらで分岐することで、
    # RecommendationType.WATCH_BEFORE_EARNINGS(利確判定エンジンのWATCH抑制専用)との
    # 意味の衝突を避ける(buy_action is not Noneなレコードのrecommendation_typeは
    # 常にBUY/WATCH_BUYのまま、呼び出し元の文脈を保持するだけの値)。
    if recommendation.buy_action is not None:
        if recommendation.buy_action in BUY_FAMILY_ACTIONS:
            return _format_buy_candidate_message(recommendation)
        if recommendation.buy_action in {
            BuyAction.WATCH_FOR_PRICE,
            BuyAction.WATCH_BEFORE_EARNINGS,
        }:
            return _format_watch_for_price_message(recommendation)
    if notification_type in (
        NotificationType.DAILY_BUY_CANDIDATES,
        NotificationType.WATCHLIST_BUY_SIGNAL,
    ):
        return _format_buy_candidate_message(recommendation)
    if notification_type == NotificationType.PROFIT_TAKING_SIGNAL:
        return _format_profit_taking_message(recommendation, large_spread_ratio_threshold)
    return _format_sell_message(recommendation)


def render_notification_preview(recommendation: Recommendation) -> str:
    """実際に送信されるLINE通知本文と同じ内容を、送信せずに生成する(before/afterレポート用)。"""
    notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[recommendation.recommendation_type]
    return _format_message(recommendation, notification_type)


@dataclass(frozen=True)
class NotificationOutcome:
    """notify_recommendation_with_statusの戻り値(要求仕様§12・§13)。

    data_quality_blockedは、整合性検証・異常値検知でBLOCKING相当の問題が検出され
    (DATA_QUALITY_ALERTまたはMANUAL_REVIEW_REQUIREDへ切り替わった)場合にTrueとなる。
    この場合、たとえ手動確認メッセージ自体はLINEへ送信されていても(sent=True)、
    評価結果としては「要確認」区分として扱う(呼び出し側の責務)。
    """

    status: NotificationStatus
    sent: bool
    data_quality_blocked: bool = False


class LineNotificationService:
    def __init__(
        self,
        line_client: LineClient,
        notification_log_repository: NotificationLogRepository,
        recommendation_repository: RecommendationRepository,
        config: AppConfig,
        audit_service: AuditService | None = None,
    ) -> None:
        self._client = line_client
        self._log_repo = notification_log_repository
        self._recommendation_repo = recommendation_repository
        self._config = config
        self._audit = audit_service or AuditService()

    def notify_recommendation(self, recommendation: Recommendation, now: dt.datetime) -> bool:
        """再通知条件を満たす場合のみLINEへ送信する。送信した場合Trueを返す。

        単一の合流点(要求仕様11節・17節): 送信前に判定/価格の整合性検証と異常値検知を
        必ず実行し、いずれかでBLOCKING相当の問題を検出した場合は通常の推奨通知を送らず
        DATA_QUALITY_ALERTへ切り替える。
        """
        return self.notify_recommendation_with_status(recommendation, now).sent

    def notify_recommendation_with_status(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> NotificationOutcome:
        """notify_recommendationと同じ処理を行い、送信有無だけでなく送信しなかった
        理由(NotificationStatus)まで返す(要求仕様§12・§13: バッチサマリーの内訳集計に使う)。
        """
        outcome = self.evaluate_notification_status(recommendation, now)
        if outcome.status != NotificationStatus.SENT or outcome.data_quality_blocked:
            return outcome
        self.send_recommendation_notification(recommendation, now)
        return NotificationOutcome(status=NotificationStatus.SENT, sent=True)

    def evaluate_notification_status(
        self,
        recommendation: Recommendation,
        now: dt.datetime,
        context: NotificationContext = NotificationContext.DEFAULT,
    ) -> NotificationOutcome:
        """notify_recommendation_with_statusと同じ判定を行うが、実際の送信
        (send_recommendation_notification)は行わない(2026-07仕様追加: 買い候補の
        優先度付け通知のように、一旦すべての候補を評価してから上位N件だけ送信したい
        呼び出し元向け)。

        status==SENTかつdata_quality_blocked=Falseの場合のみ、呼び出し側が
        send_recommendation_notificationを呼んで実際に送信する責任を持つ。
        データ品質アラート・要手動確認メッセージは例外的な安全経路のため、
        従来通りここで即時送信する(優先度付けの対象外)。

        --- BUYパイプライン第2次修正(2026-07)で追加。要求仕様13節・16節 ---
        buy_action(BUYパイプライン由来のRecommendationにのみ設定される。
        SELL/利確系のRecommendationでは常にNone)がBUY_FAMILY_ACTIONS以外の
        場合、監視継続・購入見送り・要確認・データ不足・対象外はLINE通知
        しない(分析結果・監査ログへの記録は`BuySignalService.analyze()`側で
        既に完了している)。データ品質アラート・要手動確認の安全弁チェックより
        前にこのゲートを置くことで、BUY系以外のbuy_actionでは重い整合性検証・
        異常値検知を実行せず、要手動確認LINE送信(notify_manual_review_required)
        も発生させない(MANUAL_REVIEWはLINE通知しないという要求仕様6節と一致)。

        --- BUYパイプライン第3次修正(2026-07)で追加 ---
        context=BUY_CANDIDATE_BATCHの場合、データ品質アラートがrequires_manual_review
        (要手動確認)相当であっても`notify_manual_review_required`によるLINE送信は
        行わない(「今日、現在の株価で実際に購入条件を満たした銘柄だけを通知する」
        というBUY候補バッチの方針を、要手動確認の安全弁が上書きしてしまうのを防ぐ)。
        異常自体は`_check_data_quality`内で監査ログへ記録済みのため、ここでは
        data_quality_blocked=Trueのみ返して黙って除外する。SELL/HOLDING_REVIEW/
        DEFAULTでは従来通りLINE送信する。
        """
        if (
            recommendation.buy_action is not None
            and recommendation.buy_action not in BUY_FAMILY_ACTIONS
        ):
            return NotificationOutcome(status=NotificationStatus.NOT_REQUIRED, sent=False)

        notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[recommendation.recommendation_type]
        previous = self._previous_recommendation(recommendation.stock_code, notification_type)

        alert, requires_manual_review = self._check_data_quality(
            recommendation, previous, notification_type, now
        )
        if alert is not None:
            if requires_manual_review and context is not NotificationContext.BUY_CANDIDATE_BATCH:
                sent = self.notify_manual_review_required(recommendation, alert, now)
                return NotificationOutcome(
                    status=NotificationStatus.SENT if sent else NotificationStatus.NOT_REQUIRED,
                    sent=sent,
                    data_quality_blocked=True,
                )
            if not requires_manual_review:
                self.notify_data_quality_alert(alert, now)
            return NotificationOutcome(
                status=NotificationStatus.NOT_REQUIRED, sent=False, data_quality_blocked=True
            )

        status = self._notification_status_for_send(recommendation, previous, now)
        return NotificationOutcome(status=status, sent=False)

    def check_data_quality_eligibility(
        self,
        recommendation: Recommendation,
        now: dt.datetime,
        context: NotificationContext = NotificationContext.DEFAULT,
    ) -> NotificationEligibility:
        """統合BUY候補パイプライン(2026-07)向け。データ品質(整合性検証・異常値検知)
        による通知可否だけを判定する読み取り専用メソッド。

        BUY_CANDIDATE_BATCHコンテキストではnotify_manual_review_requiredを呼ばない
        (evaluate_notification_statusと同じ規約)。notify_data_quality_alertは
        ログのみでLINE送信・NotificationLog書き込みを行わないため、読み取り専用の
        枠内で呼んでも副作用にならない。再送防止(前回通知からの経過)の判定は
        含まない(check_resend_eligibilityが別途行う。買い増し固有リスクの
        チェックより前にデータ品質だけを先に確認するための分離)。
        """
        notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[recommendation.recommendation_type]
        previous = self._previous_recommendation(recommendation.stock_code, notification_type)
        alert, requires_manual_review = self._check_data_quality(
            recommendation, previous, notification_type, now
        )
        if alert is None:
            return NotificationEligibility(eligible=True)
        if not requires_manual_review:
            self.notify_data_quality_alert(alert, now)
        return NotificationEligibility(
            eligible=False,
            block_category=EligibilityBlockCategory.DATA_QUALITY,
            block_reason="DATA_QUALITY_BLOCKED",
        )

    def check_resend_eligibility(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> NotificationEligibility:
        """統合BUY候補パイプライン(2026-07)向け。再送防止(前回通知からの経過日数・
        価格変動)による通知可否だけを判定する読み取り専用メソッド(NotificationLogを
        書き込まない)。
        """
        notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[recommendation.recommendation_type]
        previous = self._previous_recommendation(recommendation.stock_code, notification_type)
        status = self._notification_status_for_send(recommendation, previous, now)
        if status == NotificationStatus.SENT:
            return NotificationEligibility(eligible=True)
        return NotificationEligibility(
            eligible=False,
            block_category=EligibilityBlockCategory.RECENTLY_NOTIFIED,
            block_reason=status.value,
        )

    def send_recommendation_notification(
        self, recommendation: Recommendation, now: dt.datetime
    ) -> None:
        """recommendationの通知メッセージを条件判定なしで送信する。

        呼び出し前にevaluate_notification_statusでstatus==SENTかつ
        data_quality_blocked=Falseであることを確認していること
        (このメソッド自体は再通知抑止・データ品質チェックを一切行わない)。
        """
        notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[recommendation.recommendation_type]
        message = _format_message(
            recommendation,
            notification_type,
            self._config.notification.fair_value_large_spread_ratio,
        )
        self._client.push_message(message)
        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=notification_type,
                stock_code=recommendation.stock_code,
                content_hash=_compute_content_hash(recommendation.recommendation_type),
                sent_at=now,
                related_recommendation_id=recommendation.recommendation_id,
            )
        )

    def notify_buy_candidates_digest(
        self, winners: list[Recommendation], now: dt.datetime
    ) -> dict[str, BuyDigestSendOutcome]:
        """購入候補(BUY_FAMILY_ACTIONS)の上位銘柄を1通(長すぎる場合のみ複数通)に
        まとめて送信する(BUYパイプライン第2次修正2026-07で追加。要求仕様17節・18節。
        統合BUY候補パイプライン2026-07でチャンク単位の3状態管理に変更)。

        1銘柄1通ずつ`send_recommendation_notification`を呼ぶ旧方式を廃止し、
        優先順位順に並んだ購入候補だけをまとめて1回のバッチで送信する。
        個別のNotificationLogは引き続き銘柄ごとに1件ずつ保存し、再送判定の
        粒度(銘柄単位)は変えない。呼び出し前にwinnersの各要素が
        評価済みであることを呼び出し側が保証すること(このメソッド自体は
        再通知抑止を行わない)。

        戻り値は銘柄コードごとの送信結果。LINE送信(push_message)自体が例外を
        投げた場合、そのチャンク以降はすべて"SEND_FAILED"とし、以降のチャンクの
        送信を中断する(未送信のため次回バッチで通常の再送防止ロジックに従って
        自然に再評価される)。push_messageは成功したがNotificationLog保存が
        例外を投げた場合、LINEは既に届いているため"SEND_FAILED"にはせず
        "SENT_LOG_FAILED"とする(二重送信を避けるため、このバッチ内では
        再送しない)。呼び出し側はSENT_LOG_FAILEDが1件でもあればLambda呼び出しを
        失敗させ、CloudWatch Logsで運用検知できるようにすること。
        """
        if not winners:
            return {}

        blocks = [
            _buy_candidate_digest_block(rank, rec) for rank, rec in enumerate(winners, start=1)
        ]
        footer = [
            "",
            f"対象: 最大{len(winners)}銘柄",
            f"評価日時: {format_jst(now)}",
            _DISCLAIMER,
        ]

        pairs = list(zip(winners, blocks, strict=True))
        chunks: list[list[tuple[Recommendation, str]]] = []
        current_chunk: list[tuple[Recommendation, str]] = []
        current_len = 0
        for pair in pairs:
            block_len = len(pair[1]) + 2  # 区切りの空行分
            if current_chunk and current_len + block_len > _BUY_DIGEST_MAX_CHARS:
                chunks.append(current_chunk)
                current_chunk = []
                current_len = 0
            current_chunk.append(pair)
            current_len += block_len
        if current_chunk:
            chunks.append(current_chunk)

        total_chunks = len(chunks)
        results: dict[str, BuyDigestSendOutcome] = {}
        for index, chunk in enumerate(chunks, start=1):
            header = (
                "【本日の購入候補】"
                if total_chunks == 1
                else f"【本日の購入候補】({index}/{total_chunks})"
            )
            message = "\n\n".join([header, *(block for _, block in chunk)])
            if index == total_chunks:
                message = "\n\n".join([message, "\n".join(footer)])

            try:
                self._client.push_message(message)
            except Exception:  # noqa: BLE001 - LINE送信失敗は未送信として扱い処理を継続する
                logger.exception(
                    "notify_buy_candidates_digest: push_message failed chunk=%d/%d",
                    index,
                    total_chunks,
                )
                for remaining_chunk in chunks[index - 1 :]:
                    for rec, _ in remaining_chunk:
                        results[rec.stock_code] = "SEND_FAILED"
                break

            for recommendation, _ in chunk:
                notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[
                    recommendation.recommendation_type
                ]
                try:
                    self._log_repo.save(
                        NotificationLog(
                            notification_id=str(uuid.uuid4()),
                            notification_type=notification_type,
                            stock_code=recommendation.stock_code,
                            content_hash=_compute_content_hash(recommendation.recommendation_type),
                            sent_at=now,
                            related_recommendation_id=recommendation.recommendation_id,
                        )
                    )
                    results[recommendation.stock_code] = "SENT_AND_RECORDED"
                except Exception:  # noqa: BLE001 - LINEは届いているため未送信扱いにしない
                    logger.exception(
                        "notify_buy_candidates_digest: NotificationLog save failed "
                        "stock_code=%s (LINE送信自体は成功済み)",
                        recommendation.stock_code,
                    )
                    results[recommendation.stock_code] = "SENT_LOG_FAILED"
        return results

    def _check_data_quality(
        self,
        recommendation: Recommendation,
        previous: Recommendation | None,
        notification_type: NotificationType,
        now: dt.datetime,
    ) -> tuple[DataQualityAlert | None, bool]:
        del notification_type  # 将来process名の出し分けに使う可能性があるため引数として保持
        contradictions: list[str] = []
        suppressed_values: dict[str, str] = {}
        check_names: list[str] = []

        consistency_result = validate_recommendation(
            recommendation,
            self._config.data_validation.consistency_validation,
            buy_decision_config=self._config.buy_decision,
        )
        for violation in consistency_result.violations:
            contradictions.append(f"[{violation.check_name}] {violation.description}")
            check_names.append(violation.check_name)

        anomalies = detect_anomalies(
            recommendation.stock_code,
            recommendation,
            previous,
            self._config.data_validation.anomaly_detection,
        )
        for issue in anomalies:
            if issue.severity != DataQualityIssueSeverity.BLOCKING:
                continue
            contradictions.append(f"[{issue.check_name}] {issue.description}")
            suppressed_values.update(issue.suppressed_values)
            check_names.append(issue.check_name)

        if not contradictions:
            return None, False

        alert = DataQualityAlert(
            stock_code=recommendation.stock_code,
            stock_name=recommendation.stock_name,
            detected_at=now,
            process="notify_recommendation",
            contradictions=contradictions,
            suppressed_values=suppressed_values,
            recalculation_result=None,
            action_required=True,
            recommended_action=_build_recommended_action(check_names),
        )

        self._audit.record(
            decision_type="notify_recommendation_quality_alert",
            stock_code=recommendation.stock_code,
            input_values={"recommendation_id": recommendation.recommendation_id},
            calculation_formulas={},
            output_values={"contradictions": contradictions},
            data_sources=list(recommendation.data_sources),
            rule_version=recommendation.rule_version,
            timestamp=now,
            consistency_validation_result={
                "passed": consistency_result.passed,
                "violations": [
                    {"check_name": v.check_name, "description": v.description}
                    for v in consistency_result.violations
                ],
            },
            suppressed_rules=[issue.check_name for issue in anomalies],
            notification_values={
                "suppressed_values": suppressed_values,
                "recommended_action": alert.recommended_action,
            },
        )
        return alert, consistency_result.requires_manual_review

    def notify_data_quality_alert(self, alert: DataQualityAlert, now: dt.datetime) -> bool:
        """判定/価格の矛盾または異常値を検出した際、通常の推奨通知の代わりに呼ばれる。

        LINEへは個別送信しない(要求仕様: 個別のデータ品質アラートはチャットへ
        配信せず、バッチ全体のサマリー通知(notify_batch_summary)に集約する)。
        検出内容・対応内容はCloudWatch Logsおよび監査ログ(_check_data_quality内で
        AuditServiceへ記録済み)で追跡できる。LINEへは何も送信していないためFalseを返す
        (戻り値は「メッセージを送信したか」を表す既存の意味を保つ)。
        """
        del now
        name_part = f" {alert.stock_name}" if alert.stock_name else ""
        logger.warning(
            "data_quality_alert stock_code=%s%s process=%s contradictions=%s recommended_action=%s",
            alert.stock_code,
            name_part,
            alert.process,
            alert.contradictions,
            alert.recommended_action,
        )
        return False

    def notify_manual_review_required(
        self, recommendation: Recommendation, alert: DataQualityAlert, now: dt.datetime
    ) -> bool:
        """SELL/URGENT_REVIEW等の自動判定が安全条件(独立根拠件数・一次情報確認等)を
        満たさない場合、自動通知の代わりに手動確認を促すメッセージを送信する
        (要求仕様§15・§16)。DATA_QUALITY_ALERTと異なり、これは実際にLINEへ配信する
        (根拠不足のSELL/URGENT_REVIEWは自動で確定させず、必ず人間の確認を経由させるため)。
        """
        lines = [
            f"【要手動確認】{recommendation.stock_code} {recommendation.stock_name}",
            "自動売却判定の根拠に、業種別評価未対応・独立根拠不足・一次情報未確認の"
            "いずれかの項目が含まれています。",
            "検出内容:",
            *[f"・{c}" for c in alert.contradictions],
            f"自動判定結果: {_recommendation_type_label(recommendation.recommendation_type)}",
            "自動売却推奨: 停止(手動確認が完了するまで自動での売却推奨は行いません)",
            "確認事項:",
        ]
        if recommendation.reasons:
            lines.append(f"・検出された懸念事項: {' / '.join(recommendation.reasons)}")
        lines.append("・一次情報(EDINET/TDnet等)での事実確認")
        if recommendation.next_earnings_date:
            lines.append(f"・次回決算({recommendation.next_earnings_date})の内容")
        lines.append(_DISCLAIMER)
        self._client.push_message("\n".join(lines))

        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=NotificationType.MANUAL_REVIEW_REQUIRED,
                stock_code=recommendation.stock_code,
                content_hash=_compute_content_hash(recommendation.recommendation_type),
                sent_at=now,
                related_recommendation_id=recommendation.recommendation_id,
            )
        )
        return True

    def notify_disclosure_risk(
        self,
        stock_code: str,
        disclosure_title: str,
        disclosure_summary: str | None,
        matched_keywords: list[str],
        published_at: dt.datetime,
        now: dt.datetime,
        stock_name: str | None = None,
    ) -> bool:
        """適時開示からリスクキーワードが検出された場合に速報として送信する。

        同一開示(published_at+タイトルで識別)は再送しない。
        """
        content_hash = hashlib.sha256(
            f"{stock_code}|{published_at.isoformat()}|{disclosure_title}".encode()
        ).hexdigest()[:16]
        latest = self._log_repo.latest_by_stock_and_type(
            stock_code, NotificationType.IMPORTANT_DISCLOSURE
        )
        if latest is not None and latest.content_hash == content_hash:
            return False

        name_part = f" {stock_name}" if stock_name else ""
        lines = [
            f"【重要開示検知】{stock_code}{name_part}",
            f"検出キーワード: {', '.join(matched_keywords)}",
            f"開示タイトル: {disclosure_title}",
        ]
        if disclosure_summary:
            lines.append(f"概要: {disclosure_summary[:300]}")
        lines.append("対応内容: 開示内容を確認し、投資前提に影響がないか確認してください。")
        lines.append(f"開示日時: {format_jst(published_at)}")
        lines.append(_DISCLAIMER)
        self._client.push_message("\n".join(lines))

        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=NotificationType.IMPORTANT_DISCLOSURE,
                stock_code=stock_code,
                content_hash=content_hash,
                sent_at=now,
                related_recommendation_id=None,
            )
        )
        return True

    def notify_data_error(
        self, stock_code: str, message: str, now: dt.datetime, stock_name: str | None = None
    ) -> bool:
        """データ取得エラーが発生した際に呼ばれる。

        LINEへは個別送信しない(要求仕様: 個別のデータ取得エラーはチャットへ配信せず、
        バッチ全体のサマリー通知(notify_batch_summary)に集約する)。詳細はCloudWatch
        Logsで追跡できる。LINEへは何も送信していないためFalseを返す。
        """
        del now
        name_part = f" {stock_name}" if stock_name else ""
        logger.warning("data_error stock_code=%s%s message=%s", stock_code, name_part, message)
        return False

    def notify_batch_summary(
        self,
        process_name: str,
        total: int,
        category_counts: dict[str, int],
        now: dt.datetime,
        data_insufficient_stock_codes: list[str] | None = None,
        failed_stock_codes: list[str] | None = None,
        buy_candidates_sent_count: int | None = None,
        send_empty_summary: bool = True,
    ) -> bool:
        """銘柄単位ファンアウト(lambda_handlers/_fanout.py)の全件処理完了後に1回だけ送る、
        全体件数・区分別内訳のサマリー通知(要求仕様§13)。個別のデータ取得エラー・
        データ品質アラートはこれに集約され、個別には送信しない。

        category_countsは"sent"/"hold"/"review"/"data_insufficient"/"suppressed"/"failed"
        をキーとする内訳件数。合計が対象銘柄数(total)と一致するか整合性チェックし、
        一致しない場合は警告ログを出したうえで、通知本文にもその旨を明記する
        (件数の不整合自体を隠さない)。

        buy_candidates_sent_countは買い候補分析(2026-07 BUYパイプライン再設計)専用。
        0が渡され、かつsend_empty_summary=False(BUYパイプライン第2次修正2026-07で
        追加。要求仕様16節)の場合、このメソッドは何も送信せずFalseを返す
        (購入候補が1件も無い日に「該当なし」の完了通知すら送らない設定)。
        send_empty_summary=True(既定値、他のバッチ処理からの呼び出しはこちらの
        まま)の場合は従来どおり「今回の購入候補: 該当なし」を明示する。

        ファンアウトの起動元(スケジューラ・手動実行)が何らかの理由で二重ディスパッチ
        された場合、独立した2つのbatch_idがそれぞれ完了を検知してこのメソッドを
        呼び出しうる。それぞれのbatch_idの完了自体は正しい検知だが、結果として
        まったく同一内容のサマリーがLINEへ二重送信されることを防ぐため、同一日付・
        同一内容(件数)の通知が既に送信済みの場合は送信をスキップする。
        """
        if buy_candidates_sent_count == 0 and not send_empty_summary:
            logger.info(
                "batch_summary suppressed (no buy candidates, send_empty_summary=False) "
                "process_name=%s total=%d",
                process_name,
                total,
            )
            return False

        counts = {
            category: category_counts.get(category, 0) for category in _BATCH_SUMMARY_CATEGORIES
        }
        counts_sum = sum(counts.values())
        is_consistent = counts_sum == total
        if not is_consistent:
            logger.warning(
                "batch_summary category count mismatch process_name=%s total=%d "
                "counts_sum=%d counts=%s",
                process_name,
                total,
                counts_sum,
                counts,
            )

        pseudo_stock_code = f"__batch__:{process_name}"
        content_hash = hashlib.sha256(
            f"{process_name}|{now.date().isoformat()}|{total}|"
            f"{sorted(counts.items())}".encode()
        ).hexdigest()[:16]
        latest = self._log_repo.latest_by_stock_and_type(
            pseudo_stock_code, NotificationType.BATCH_SUMMARY
        )
        if latest is not None and latest.content_hash == content_hash:
            logger.info(
                "batch_summary duplicate suppressed process_name=%s total=%d counts=%s",
                process_name,
                total,
                counts,
            )
            return False

        # 2026-07仕様レビュー対応(§10): 「判定結果」(保有継続/要確認等)と「通知処理の
        # 結果」(送信した/抑止した等)が同じ並びで表示され意味が伝わりにくいという
        # 指摘を受け、「処理結果:」見出し+「・」箇条書きへ整理する。集計方法
        # (category_countsの分類自体)は変更しない。
        lines = [
            f"【{process_name}完了】",
            "",
            "処理結果:",
            f"・個別通知送信：{counts['sent']}件",
            f"・通知不要（保有継続）：{counts['hold']}件",
            f"・要確認：{counts['review']}件",
            f"・データ不足：{counts['data_insufficient']}件",
            f"・再通知抑止：{counts['suppressed']}件",
            f"・処理失敗：{counts['failed']}件",
        ]
        # 買い候補分析(2026-07 BUYパイプライン再設計)専用: 購入候補・価格待ちの
        # いずれもシグナル自体は成立したが、1回あたりの通知上限により今回は通知を
        # 見送った件数。他のバッチ(保有銘柄・適時開示等)ではこれらのカテゴリを
        # 使わない(常に0)ため、0件のときは表示しない。
        # 「優先順位の高いN件」という表現は使わず、購入候補/価格待ちを明示して区別する。
        if counts["candidate_not_ranked"] > 0:
            lines.append(f"・買い候補(通知上限により見送り)：{counts['candidate_not_ranked']}件")
        if counts["watch_not_ranked"] > 0:
            lines.append(f"・価格待ち(通知上限により見送り)：{counts['watch_not_ranked']}件")
        lines.append("")
        lines.append(f"対象銘柄：{total}件")
        if buy_candidates_sent_count == 0:
            lines.append("")
            lines.append("【今回の購入候補】")
            lines.append("該当なし")
            lines.append("現在価格で安全余裕を満たす銘柄はありませんでした。")
        if not is_consistent:
            lines.append("")
            lines.append(f"※内訳合計({counts_sum}件)が対象銘柄数と一致していません。")
        if data_insufficient_stock_codes:
            lines.append("")
            lines.append("データ不足：")
            lines.extend(f"・{code}" for code in data_insufficient_stock_codes)
        if failed_stock_codes:
            lines.append("")
            lines.append("処理失敗：")
            lines.extend(f"・{code}" for code in failed_stock_codes)
        lines.append("")
        lines.append(f"評価日時：{format_jst(now)}")
        lines.append(_DISCLAIMER)
        self._client.push_message("\n".join(lines))
        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=NotificationType.BATCH_SUMMARY,
                stock_code=pseudo_stock_code,
                content_hash=content_hash,
                sent_at=now,
                related_recommendation_id=None,
            )
        )
        return True

    def notify_watchlist_additions(
        self,
        added_items: list[WatchlistItem],
        results_by_code: dict[str, WatchlistScreeningResult],
        policy_name: str,
        evaluated_at: dt.datetime,
    ) -> bool:
        """週次スクリーニングでウォッチリストへ実際に追加された銘柄の通知
        (ウォッチリスト自動追加機能、要求仕様§12)。

        表示対象はWatchlistRepository.add_if_new()が実際にTrueを返した銘柄のみ
        (スコア合格でも上限超過/既登録/Repository失敗だった銘柄は表示しない)。
        追加が1件も無い場合は送信しない。
        """
        if not added_items:
            return False

        ranked = sorted(
            added_items,
            key=lambda item: (-results_by_code[item.stock_code].total_score, item.stock_code),
        )

        pseudo_stock_code = "__batch__:watchlist_auto_addition"
        content_hash = compute_watchlist_addition_content_hash(
            [item.stock_code for item in ranked], evaluated_at
        )
        latest = self._log_repo.latest_by_stock_and_type(
            pseudo_stock_code, NotificationType.WATCHLIST_AUTO_ADDITION
        )
        if latest is not None and latest.content_hash == content_hash:
            logger.info("watchlist_auto_addition duplicate suppressed count=%d", len(ranked))
            return False

        policy_label = _WATCHLIST_SCREENING_POLICY_LABELS.get(policy_name, policy_name)
        lines = [
            "【ウォッチリスト追加】",
            "",
            "週次スクリーニングにより",
            f"新たに{len(ranked)}銘柄を追加しました。",
            "",
            "評価ポリシー：",
            policy_label,
        ]

        detail_items = ranked[:_WATCHLIST_ADDITION_DETAIL_LIMIT]
        for rank, item in enumerate(detail_items, start=1):
            result = results_by_code[item.stock_code]
            display_name = item.stock_name or item.stock_code
            lines.append("")
            lines.append(f"{rank}. {display_name}（{item.stock_code}）")
            lines.append(f"・総合スコア：{result.total_score:.0f}点")
            lines.extend(f"・{label}：{value}" for label, value in result.main_metrics.items())
            lines.append(f"・追加理由：{describe_matched_criteria(result.matched_criteria)}")

        remaining = len(ranked) - len(detail_items)
        if remaining > 0:
            lines.append("")
            lines.append(f"ほか{remaining}銘柄を追加しました。")

        lines.append("")
        lines.append("評価日時：")
        lines.append(format_jst(evaluated_at))

        self._client.push_message("\n".join(lines))
        self._log_repo.save(
            NotificationLog(
                notification_id=str(uuid.uuid4()),
                notification_type=NotificationType.WATCHLIST_AUTO_ADDITION,
                stock_code=pseudo_stock_code,
                content_hash=content_hash,
                sent_at=evaluated_at,
                related_recommendation_id=None,
            )
        )
        return True

    def _previous_recommendation(
        self, stock_code: str, notification_type: NotificationType
    ) -> Recommendation | None:
        latest_log = self._log_repo.latest_by_stock_and_type(stock_code, notification_type)
        if latest_log is None or latest_log.related_recommendation_id is None:
            return None
        return self._recommendation_repo.get(latest_log.related_recommendation_id)

    def _notification_status_for_send(
        self,
        recommendation: Recommendation,
        previous: Recommendation | None,
        now: dt.datetime,
    ) -> NotificationStatus:
        """送信するかどうかに加えて、送信しない場合の理由も返す(要求仕様§12)。"""
        notification_type = _RECOMMENDATION_TO_NOTIFICATION_TYPE[recommendation.recommendation_type]
        latest_log = self._log_repo.latest_by_stock_and_type(
            recommendation.stock_code, notification_type
        )
        if latest_log is None:
            return NotificationStatus.SENT
        if previous is None:
            return NotificationStatus.SENT

        if previous.recommendation_type != recommendation.recommendation_type:
            return NotificationStatus.SENT

        prev_price = _representative_price(previous)
        new_price = _representative_price(recommendation)
        price_comparable = prev_price is not None and new_price is not None and prev_price > 0
        if price_comparable:
            change_pct = abs(float(new_price / prev_price - 1) * 100)  # type: ignore[operator]
            if change_pct >= self._config.notification.price_change_resend_threshold_pct:
                return NotificationStatus.SENT

        days_elapsed = (now.date() - latest_log.sent_at.date()).days
        if days_elapsed >= self._config.notification.resend_after_days:
            return NotificationStatus.SENT

        # 判定区分・価格いずれも実質的に変化していない、まったく同一内容の再送とみなせる
        # 場合はDUPLICATE_SUPPRESSED、価格を比較できたが閾値未満だった場合は
        # PRICE_CHANGE_BELOW_THRESHOLD、価格を比較できず日数のみで判断した場合は
        # RESEND_INTERVAL_NOT_REACHEDとする。
        if price_comparable:
            return NotificationStatus.PRICE_CHANGE_BELOW_THRESHOLD
        if prev_price is None and new_price is None:
            return NotificationStatus.DUPLICATE_SUPPRESSED
        return NotificationStatus.RESEND_INTERVAL_NOT_REACHED
