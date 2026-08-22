"""3銘柄のbefore/afterレポート生成(要求仕様20節)。

「before」は既存の保存済みRecommendation/AuditLogEntry(旧ロジックによる
最後の判定結果)、「after」は現在のロジックをその場で再実行した結果を指す。
LINE送信はせず、Markdownとして出力しユーザーが直接確認する。
"""

from __future__ import annotations

import datetime as dt
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.line_notification_service import render_notification_preview
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalService


@dataclass(frozen=True)
class BeforeAfterEntry:
    stock_code: str
    holding: Holding | None
    before_recommendations: list[Recommendation]
    before_audit_entries: list[AuditLogEntry]
    after_profit_taking: Recommendation | None
    after_sell_signal: Recommendation | None
    after_error: str | None = None
    not_held_note: str | None = None


@dataclass(frozen=True)
class BeforeAfterReport:
    basis_date: dt.date
    entries: list[BeforeAfterEntry] = field(default_factory=list)


class BeforeAfterReportService:
    def __init__(
        self,
        providers: ProviderBundle,
        config: AppConfig,
        recommendation_repository: RecommendationRepository | None = None,
        audit_log_repository: AuditLogRepository | None = None,
        holding_repository: HoldingRepository | None = None,
    ) -> None:
        self._providers = providers
        self._config = config
        self._recommendation_repo = recommendation_repository or RecommendationRepository()
        self._audit_repo = audit_log_repository or AuditLogRepository()
        self._holding_repo = holding_repository or HoldingRepository()
        # after判定はレポート専用の使い捨てローカル監査ログに記録し、beforeの取得元
        # (本番DynamoDB等)へ意図せず新規の監査ログを書き込まないようにする
        self._scratch_audit_dir = Path(tempfile.mkdtemp(prefix="jstock_before_after_"))
        self._scratch_audit = AuditService(
            repository=AuditLogRepository(store_dir=self._scratch_audit_dir)
        )

    def build_entry(self, stock_code: str, now: dt.datetime) -> BeforeAfterEntry:
        before_recommendations = self._recommendation_repo.list_by_stock(stock_code)
        before_audit_entries = self._audit_repo.list_by_stock(stock_code)
        # M3(保有銘柄オーナー機能): HoldingRepositoryのPKはholding_id。本レポートは
        # owner別の切り替えUIを持たないため、既定owner(DEFAULT_OWNER)の保有のみを
        # 対象とする。
        holding = self._holding_repo.get(build_holding_id(DEFAULT_OWNER, stock_code))

        if holding is None:
            return BeforeAfterEntry(
                stock_code=stock_code,
                holding=None,
                before_recommendations=before_recommendations,
                before_audit_entries=before_audit_entries,
                after_profit_taking=None,
                after_sell_signal=None,
                not_held_note="保有銘柄として登録されていないため、afterの再判定は実行していません",
            )

        profit_taking_outcome = ProfitTakingService(
            providers=self._providers, config=self._config, audit_service=self._scratch_audit
        ).analyze(holding, now)
        sell_signal_outcome = SellSignalService(
            providers=self._providers, config=self._config, audit_service=self._scratch_audit
        ).analyze(holding, now)

        return BeforeAfterEntry(
            stock_code=stock_code,
            holding=holding,
            before_recommendations=before_recommendations,
            before_audit_entries=before_audit_entries,
            after_profit_taking=profit_taking_outcome.recommendation,
            after_sell_signal=sell_signal_outcome.recommendation,
            after_error=profit_taking_outcome.data_error or sell_signal_outcome.data_error,
        )

    def build_report(self, stock_codes: list[str], now: dt.datetime) -> BeforeAfterReport:
        entries = [self.build_entry(code, now) for code in stock_codes]
        return BeforeAfterReport(basis_date=now.date(), entries=entries)

    def render_markdown(self, report: BeforeAfterReport) -> str:
        lines = [
            f"# before/afterレポート({report.basis_date.isoformat()}基準)",
            "",
            "根本原因修正・24節仕様の再設計を適用する前(before)と適用後(after)で、"
            "同一銘柄の判定がどう変化したかを比較する。LINE送信は行わず、本ファイルのみに出力する。",
            "",
        ]
        for entry in report.entries:
            lines.extend(self._render_entry(entry))
        return "\n".join(lines)

    def _render_entry(self, entry: BeforeAfterEntry) -> list[str]:
        lines = [f"## {entry.stock_code}", ""]

        if entry.not_held_note:
            lines.append(f"- {entry.not_held_note}")
            lines.append("")

        lines.append("### Before(旧ロジックによる最後の判定)")
        lines.append("")
        if not entry.before_recommendations:
            lines.append("保存済みのRecommendationはありません。")
        else:
            latest = entry.before_recommendations[-1]
            lines.extend(self._render_recommendation_summary(latest))
        lines.append("")
        lines.append(
            f"監査ログ件数: {len(entry.before_audit_entries)}件"
            + (
                f"(直近: {entry.before_audit_entries[-1].decision_type} "
                f"{entry.before_audit_entries[-1].timestamp.isoformat()})"
                if entry.before_audit_entries
                else ""
            )
        )
        lines.append("")

        lines.append("### After(現行ロジックでの再判定)")
        lines.append("")
        if entry.after_error:
            lines.append(f"データ取得エラー: {entry.after_error}")
        elif entry.holding is None:
            pass
        else:
            if entry.after_profit_taking is not None:
                lines.append("**利確判定:**")
                lines.extend(self._render_recommendation_summary(entry.after_profit_taking))
                lines.append("")
                lines.append("```")
                lines.append(render_notification_preview(entry.after_profit_taking))
                lines.append("```")
            else:
                lines.append("利確判定: HOLD相当(通知対象外)")
            lines.append("")
            if entry.after_sell_signal is not None:
                lines.append("**投資前提悪化判定:**")
                lines.extend(self._render_recommendation_summary(entry.after_sell_signal))
                lines.append("")
                lines.append("```")
                lines.append(render_notification_preview(entry.after_sell_signal))
                lines.append("```")
            else:
                lines.append("投資前提悪化判定: HOLD相当(通知対象外)")
        lines.append("")
        return lines

    @staticmethod
    def _render_recommendation_summary(recommendation: Recommendation) -> list[str]:
        lines = [
            f"- 判定: {recommendation.recommendation_type.value}",
            f"- 現在値: {recommendation.price_at_recommendation}円",
        ]
        if recommendation.fair_value_at_recommendation is not None:
            lines.append(f"- 適正価格: {recommendation.fair_value_at_recommendation}円")
        if recommendation.total_yield_pct_at_recommendation is not None:
            lines.append(f"- 総合利回り: {recommendation.total_yield_pct_at_recommendation:.2f}%")
        sp = recommendation.sell_prices
        if sp is not None:
            if sp.partial_profit_start_price:
                lines.append(f"- 一部利確開始価格: {sp.partial_profit_start_price.price}円")
            if sp.recommended_limit_price:
                lines.append(f"- 利確推奨価格: {sp.recommended_limit_price.price}円")
            if sp.full_profit_consideration_price:
                lines.append(f"- 全株利確検討価格: {sp.full_profit_consideration_price.price}円")
            if sp.reevaluation_price_upside:
                lines.append(f"- 再評価価格(上昇時): {sp.reevaluation_price_upside.price}円")
            if sp.stop_review_price:
                lines.append(f"- 売却目安価格: {sp.stop_review_price.price}円")
        if recommendation.reasons:
            lines.append("- 根拠: " + " / ".join(recommendation.reasons))
        lines.append(f"- 信頼度: {recommendation.confidence.value}")
        lines.append(f"- rule_version: {recommendation.rule_version}")
        lines.append(f"- recommendation_id: {recommendation.recommendation_id}")
        return lines
