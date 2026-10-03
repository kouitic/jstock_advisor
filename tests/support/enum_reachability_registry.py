"""「名前で生成している箇所が src に無い」enum member の registry(Issue #274)。

`tests/unit/test_enum_reachability_guard.py` が、本 registry を **同じ AST の規則**
(`tests/support/enum_reachability.py`)で再計算した結果と突き合わせる。

## registry の意味

```
UNGENERATED_BY_NAME   src に、その member を名前で生成している箇所(`Enum.MEMBER` を値として
                      作る・渡す形)が 0 件
DYNAMIC_CLASS         同上。ただし、その class には `Enum(<値>)` の構築または iteration が src に
                      ある。★ この class の member は、名前で生成されていなくても、永続データや
                      入力文字列から構築されて到達しうる。**「到達不能」とは読まないこと**
```

★ **「到達不能」を意味しない。** AST で数えられるのは「名前による生成の有無」だけである。
  pydantic の field 検証による構築・getattr・文字列からの構築・fixture 経由の参照は視界外。
  実測: この registry の class の大半は、src の型注釈(pydantic の field の型を含む)に現れる。
  それらの member は、永続データの読み込み時の検証で構築されうるため、DYNAMIC_CLASS ではなくても
  「到達不能」とは読めない。

★ **enum member を削除してよいという意味でもない。** 永続レコード(約 6,000 件)があり、
  `extra="forbid"` かつ読み込み時にも検証が走るため、削除は実データの read-only 監査で不在を
  確認してから、別途判断する(Issue #274 の方針。受入条件 3: member を 1 つも削除しない)。
  guard は「削除候補」を表示しない。

## 更新のしかた

member に生成箇所が現れる(= 本 registry の宣言が古くなる)と guard が落ちる。その member を
本 registry から外す。新しい member を、生成箇所を持たないまま、本 registry に載せている class に
足した場合も guard が落ちる。登録するか、生成箇所を足す。
"""

from __future__ import annotations

UNGENERATED_BY_NAME = "UNGENERATED_BY_NAME"
DYNAMIC_CLASS = "DYNAMIC_CLASS"

#: 識別子は `Class.MEMBER`。クラス名が複数のファイルにある場合は `Class[ファイルパス].MEMBER`。
#: class ごとのコメントは、定義のあるファイル(src 相対)。
REGISTRY: dict[str, str] = {
    # AccountType(domain/entities/enums.py)
    "AccountType.SPECIFIC": DYNAMIC_CLASS,
    "AccountType.NISA": DYNAMIC_CLASS,
    # BacktestNotificationStatus(services/holding_decision_backtest_service.py)
    "BacktestNotificationStatus.NOT_EXECUTED_LIVE_MODE": UNGENERATED_BY_NAME,
    # BaselineOrigin(domain/entities/enums.py)
    "BaselineOrigin.HUMAN_APPROVED": UNGENERATED_BY_NAME,
    "BaselineOrigin.PURCHASE_SNAPSHOT": UNGENERATED_BY_NAME,
    "BaselineOrigin.HOLDING_REGISTRATION_SNAPSHOT": UNGENERATED_BY_NAME,
    "BaselineOrigin.HISTORICAL_RECONSTRUCTED": UNGENERATED_BY_NAME,
    "BaselineOrigin.COMMON_TEMPLATE": UNGENERATED_BY_NAME,
    # BaselineStatus(domain/entities/enums.py)
    "BaselineStatus.DRAFT": UNGENERATED_BY_NAME,
    "BaselineStatus.PROPOSED": UNGENERATED_BY_NAME,
    "BaselineStatus.REJECTED": UNGENERATED_BY_NAME,
    # BasisDateConsistency(services/corporate_action_service.py)
    "BasisDateConsistency.DETECTED": UNGENERATED_BY_NAME,
    # BatchFinalizeStatus(infrastructure/aws/batch_tracker.py)
    "BatchFinalizeStatus.RUNNING": UNGENERATED_BY_NAME,
    "BatchFinalizeStatus.FINALIZING": UNGENERATED_BY_NAME,
    "BatchFinalizeStatus.COMPLETED": UNGENERATED_BY_NAME,
    "BatchFinalizeStatus.FINALIZE_FAILED": UNGENERATED_BY_NAME,
    # BenefitUtilityCategory(domain/entities/enums.py)
    "BenefitUtilityCategory.CASH_EQUIVALENT": DYNAMIC_CLASS,
    "BenefitUtilityCategory.VERSATILE_POINT": DYNAMIC_CLASS,
    "BenefitUtilityCategory.IN_HOUSE_SERVICE": DYNAMIC_CLASS,
    "BenefitUtilityCategory.IN_HOUSE_PRODUCT": DYNAMIC_CLASS,
    "BenefitUtilityCategory.DISCOUNT_VOUCHER": DYNAMIC_CLASS,
    "BenefitUtilityCategory.LOTTERY_OR_COMMEMORATIVE": DYNAMIC_CLASS,
    # BuyIndustrySector(domain/entities/enums.py)
    "BuyIndustrySector.BANK": DYNAMIC_CLASS,
    "BuyIndustrySector.LEASE_FINANCE": DYNAMIC_CLASS,
    "BuyIndustrySector.PHARMACEUTICAL": DYNAMIC_CLASS,
    "BuyIndustrySector.AUTOMOTIVE_PARTS": DYNAMIC_CLASS,
    "BuyIndustrySector.CYCLICAL_MATERIALS": DYNAMIC_CLASS,
    "BuyIndustrySector.UTILITY": DYNAMIC_CLASS,
    "BuyIndustrySector.FOOD": DYNAMIC_CLASS,
    "BuyIndustrySector.GENERAL_MANUFACTURING": DYNAMIC_CLASS,
    # CliTarget(cli/trading_pause.py)
    "CliTarget.LOCAL": UNGENERATED_BY_NAME,
    "CliTarget.AWS": UNGENERATED_BY_NAME,
    # ConversationStateName(domain/entities/enums.py)
    "ConversationStateName.INPUT_WAITING": DYNAMIC_CLASS,
    "ConversationStateName.CONFIRM_WAITING": DYNAMIC_CLASS,
    # CorporateActionType(domain/entities/enums.py)
    "CorporateActionType.FREE_ALLOTMENT": UNGENERATED_BY_NAME,
    "CorporateActionType.SPINOFF": UNGENERATED_BY_NAME,
    "CorporateActionType.TICKER_CHANGE": UNGENERATED_BY_NAME,
    "CorporateActionType.MERGER": UNGENERATED_BY_NAME,
    "CorporateActionType.DELISTING": UNGENERATED_BY_NAME,
    "CorporateActionType.DIVIDEND_BASIS_CHANGE": UNGENERATED_BY_NAME,
    # EligibilityBlockCategory(domain/entities/enums.py)
    "EligibilityBlockCategory.EARNINGS_PROXIMITY": UNGENERATED_BY_NAME,
    "EligibilityBlockCategory.WATCH_OUT_OF_RANGE": UNGENERATED_BY_NAME,
    # EvaluationLabel(domain/entities/enums.py)
    "EvaluationLabel.EARLY": UNGENERATED_BY_NAME,
    "EvaluationLabel.LATE": UNGENERATED_BY_NAME,
    "EvaluationLabel.PRICE_TOO_LOW": UNGENERATED_BY_NAME,
    "EvaluationLabel.PROFIT_TAKE_TOO_LATE": UNGENERATED_BY_NAME,
    # ExclusionReason(domain/signals/watchlist_screening.py)
    "ExclusionReason.ALREADY_HELD": UNGENERATED_BY_NAME,
    "ExclusionReason.ALREADY_WATCHLISTED": UNGENERATED_BY_NAME,
    "ExclusionReason.RANK_OUTSIDE_ADDITION_LIMIT": UNGENERATED_BY_NAME,
    # ExecutionMode(domain/entities/enums.py)
    "ExecutionMode.VALIDATION": DYNAMIC_CLASS,
    # FailureClass(domain/notification/incident_signal.py)
    "FailureClass.HANDLED_FAILURE": DYNAMIC_CLASS,
    # FinancialIndustryCategory(domain/entities/enums.py)
    "FinancialIndustryCategory.BANKING": DYNAMIC_CLASS,
    "FinancialIndustryCategory.INSURANCE": DYNAMIC_CLASS,
    "FinancialIndustryCategory.SECURITIES": DYNAMIC_CLASS,
    # FinancialValueSourceType(domain/entities/financial_input_provenance.py)
    "FinancialValueSourceType.COMPANY_FORECAST": UNGENERATED_BY_NAME,
    "FinancialValueSourceType.ANALYST_ESTIMATE": UNGENERATED_BY_NAME,
    # HistoricalValuationEvaluationState(domain/entities/enums.py)
    "HistoricalValuationEvaluationState.NOT_APPLICABLE": UNGENERATED_BY_NAME,
    # ImprovementAction(domain/entities/enums.py)
    "ImprovementAction.REVIEW_LOGIC": UNGENERATED_BY_NAME,
    "ImprovementAction.INVESTIGATE_SEGMENT": UNGENERATED_BY_NAME,
    # ImprovementPriority(domain/entities/enums.py)
    "ImprovementPriority.NONE": UNGENERATED_BY_NAME,
    # IncidentGithubIssueStatus(infrastructure/aws/incident_state_tracker.py)
    "IncidentGithubIssueStatus.CREATING": UNGENERATED_BY_NAME,
    "IncidentGithubIssueStatus.CREATED": UNGENERATED_BY_NAME,
    "IncidentGithubIssueStatus.CONFIGURATION_ERROR": UNGENERATED_BY_NAME,
    "IncidentGithubIssueStatus.ISSUE_CREATION_FAILED": UNGENERATED_BY_NAME,
    # JudgmentStrength(domain/entities/enums.py)
    "JudgmentStrength.INFO": UNGENERATED_BY_NAME,
    "JudgmentStrength.WATCH": UNGENERATED_BY_NAME,
    "JudgmentStrength.REVIEW": UNGENERATED_BY_NAME,
    "JudgmentStrength.PARTIAL_ACTION": UNGENERATED_BY_NAME,
    "JudgmentStrength.FULL_ACTION": UNGENERATED_BY_NAME,
    "JudgmentStrength.URGENT_REVIEW": UNGENERATED_BY_NAME,
    # MigrationTarget(migrations/target.py)
    "MigrationTarget.LOCAL": UNGENERATED_BY_NAME,
    "MigrationTarget.AWS": UNGENERATED_BY_NAME,
    # NotificationContext(domain/entities/enums.py)
    "NotificationContext.HOLDING_REVIEW": UNGENERATED_BY_NAME,
    # NotificationMode(domain/entities/enums.py)
    "NotificationMode.DRY_RUN": DYNAMIC_CLASS,
    # NotificationType(domain/entities/enums.py)
    "NotificationType.DATA_ERROR": UNGENERATED_BY_NAME,
    "NotificationType.DATA_QUALITY_ALERT": UNGENERATED_BY_NAME,
    "NotificationType.WEEKLY_REVIEW": UNGENERATED_BY_NAME,
    "NotificationType.MONTHLY_REVIEW": UNGENERATED_BY_NAME,
    "NotificationType.QUARTERLY_LOGIC_REVIEW": UNGENERATED_BY_NAME,
    "NotificationType.OUTLIER_REVIEW": UNGENERATED_BY_NAME,
    "NotificationType.LOGIC_CHANGE_PROPOSAL": UNGENERATED_BY_NAME,
    # PeriodType(domain/entities/enums.py)
    "PeriodType.QUARTER": UNGENERATED_BY_NAME,
    "PeriodType.YTD": UNGENERATED_BY_NAME,
    # PortfolioValuationBasis(domain/entities/enums.py)
    "PortfolioValuationBasis.ACQUISITION_COST": UNGENERATED_BY_NAME,
    # PriceBasisType(domain/entities/enums.py)
    "PriceBasisType.USER_DEFINED_TARGET": UNGENERATED_BY_NAME,
    # PriceRangeEvaluationState(domain/entities/enums.py)
    "PriceRangeEvaluationState.NOT_APPLICABLE": UNGENERATED_BY_NAME,
    # Priority(domain/entities/enums.py)
    "Priority.HIGH": DYNAMIC_CLASS,
    "Priority.LOW": DYNAMIC_CLASS,
    # ProfitTakingIndustrySector(domain/entities/enums.py)
    "ProfitTakingIndustrySector.BANKING": UNGENERATED_BY_NAME,
    "ProfitTakingIndustrySector.LEASING_FINANCE": UNGENERATED_BY_NAME,
    "ProfitTakingIndustrySector.FOOD": UNGENERATED_BY_NAME,
    "ProfitTakingIndustrySector.CHEMICAL": UNGENERATED_BY_NAME,
    "ProfitTakingIndustrySector.GAS_UTILITY": UNGENERATED_BY_NAME,
    # RecommendationType(domain/entities/enums.py)
    "RecommendationType.MANUAL_REVIEW_REQUIRED": DYNAMIC_CLASS,
    # RecordDateUnknownReason(domain/entities/enums.py)
    "RecordDateUnknownReason.PARSE_ERROR": UNGENERATED_BY_NAME,
    "RecordDateUnknownReason.CORPORATE_ACTION_UNRESOLVED": UNGENERATED_BY_NAME,
    "RecordDateUnknownReason.NOT_APPLICABLE": UNGENERATED_BY_NAME,
    # RunLockStatus(infrastructure/aws/trade_detection_lock.py)
    "RunLockStatus.PROCESSING": UNGENERATED_BY_NAME,
    "RunLockStatus.COMPLETED": UNGENERATED_BY_NAME,
    # RuntimeConfigMode(domain/entities/enums.py)
    "RuntimeConfigMode.SHADOW": UNGENERATED_BY_NAME,
    "RuntimeConfigMode.ACTIVE": UNGENERATED_BY_NAME,
    # ShadowMode(domain/signals/judgment_safety_shadow_config.py)
    "ShadowMode[domain/signals/judgment_safety_shadow_config.py].SHADOW": UNGENERATED_BY_NAME,
    # ShadowMode(domain/signals/valuation_confidence_shadow_config.py)
    "ShadowMode[domain/signals/valuation_confidence_shadow_config.py].SHADOW": UNGENERATED_BY_NAME,
    # ShareholderReturnPolicyType(domain/entities/enums.py)
    "ShareholderReturnPolicyType.PROGRESSIVE": UNGENERATED_BY_NAME,
    "ShareholderReturnPolicyType.DOE": UNGENERATED_BY_NAME,
    "ShareholderReturnPolicyType.BOTH": UNGENERATED_BY_NAME,
    "ShareholderReturnPolicyType.NONE": UNGENERATED_BY_NAME,
    # SkipReason(domain/entities/enums.py)
    "SkipReason.PRICE_NOT_REACHED": UNGENERATED_BY_NAME,
    "SkipReason.INSUFFICIENT_FUNDS": UNGENERATED_BY_NAME,
    "SkipReason.PRIORITIZED_OTHER_STOCK": UNGENERATED_BY_NAME,
    "SkipReason.WAITED_FOR_EARNINGS": UNGENERATED_BY_NAME,
    "SkipReason.NOT_CONVINCED": UNGENERATED_BY_NAME,
    "SkipReason.MANUAL_JUDGMENT": UNGENERATED_BY_NAME,
    "SkipReason.OTHER": UNGENERATED_BY_NAME,
    # Source(cli/judgment_safety_shadow.py)
    "Source.DYNAMODB": UNGENERATED_BY_NAME,
    # SourceType(domain/entities/enums.py)
    "SourceType.COMPANY_IR": UNGENERATED_BY_NAME,
    "SourceType.EXCHANGE": UNGENERATED_BY_NAME,
    "SourceType.SECONDARY": UNGENERATED_BY_NAME,
    "SourceType.OTHER_WEB": UNGENERATED_BY_NAME,
    # ThesisConditionAttestationStatus(domain/entities/enums.py)
    "ThesisConditionAttestationStatus.MAINTAINED": UNGENERATED_BY_NAME,
    "ThesisConditionAttestationStatus.BROKEN": UNGENERATED_BY_NAME,
    "ThesisConditionAttestationStatus.UNCERTAIN": UNGENERATED_BY_NAME,
    # ValuationBasis(domain/entities/enums.py)
    "ValuationBasis.FORWARD": UNGENERATED_BY_NAME,
    # WatchlistBatchStatus(infrastructure/aws/batch_tracker.py)
    "WatchlistBatchStatus.DISPATCHING": DYNAMIC_CLASS,
    "WatchlistBatchStatus.RUNNING": DYNAMIC_CLASS,
    "WatchlistBatchStatus.FINALIZE_PREPARING": DYNAMIC_CLASS,
    "WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED": DYNAMIC_CLASS,
    "WatchlistBatchStatus.NOTIFICATION_PENDING": DYNAMIC_CLASS,
    "WatchlistBatchStatus.NOTIFICATION_SENT": DYNAMIC_CLASS,
    "WatchlistBatchStatus.NOTIFICATION_FAILED": DYNAMIC_CLASS,
    "WatchlistBatchStatus.DISPATCH_FAILED": DYNAMIC_CLASS,
    "WatchlistBatchStatus.FINALIZE_FAILED": DYNAMIC_CLASS,
    "WatchlistBatchStatus.TIMEOUT_FINALIZING": DYNAMIC_CLASS,
    "WatchlistBatchStatus.TIMED_OUT": DYNAMIC_CLASS,
    "WatchlistBatchStatus.TIMEOUT_FINALIZE_FAILED": DYNAMIC_CLASS,
    # WatchlistProgressStatus(infrastructure/aws/batch_tracker.py)
    "WatchlistProgressStatus.PENDING": UNGENERATED_BY_NAME,
    "WatchlistProgressStatus.PROCESSING": UNGENERATED_BY_NAME,
}

#: 「削除してはならない」と USER が決定した member(削除禁止)と、その理由。
#: guard は、これらが enum に残っていること、および「削除候補」として扱われていないことを確認する。
LEGACY_ONLY: dict[str, str] = {
    "NotificationType.DATA_QUALITY_ALERT": (
        "LEGACY_ONLY。過去の永続データに残りうるため削除しない(USER 決定。Issue #274)"
    ),
}
