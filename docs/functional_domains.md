# 機能領域カタログ

本書は次の 4 つの正本(SSoT)である。

```
DOMAIN_CATALOG            機能領域の一覧と境界
FUNCTION_CATALOG          機能の一覧
FUNCTION_DOMAIN_MAPPING   機能 -> 主領域 / 影響領域 / 主要資材
SHARED_COMPONENT_CATALOG  複数領域が共有する部品と、変更時に lock すべき領域
```

領域ベースの WIP 運用ルール(`DOMAIN_WIP_RULE_V1`)そのものは
[docs/development_workflow.md](development_workflow.md) 2.6節が正本である。
本書はその**判定材料**を提供する。ルール本文を本書へ複製しない。

---

## 0. 現在の発効状態

```
DOMAIN_WIP_MODEL_ACTIVE = NO
CURRENT_WIP_RULE        = ISSUE_122(担当者単位 code WIP = 1)
```

**本書が main に入っただけでは領域ベース WIP は有効にならない。**
発効は docs review -> PR -> CI -> 人間の merge 承認 -> merge -> main CI ->
周知 -> **人間による明示的な発効宣言**をすべて終えた後である
(development_workflow.md 2.6.10)。発効後も遡及適用しない。

---

## A. 目的と適用範囲

### 目的

担当者単位の WIP 制限は「同時に壊れる範囲の最小化」には有効だが、
互いに無関係な機能領域まで直列化する。一方で単純に並行数を増やすと、
Git では検出できない衝突が起きる。

```
衝突の型 1  同じファイルを同時に編集する
            -> Git が conflict として検出できる

衝突の型 2  異なるファイルだが、同じ判定契約・永続契約を同時に変える
            -> Git は無言で merge する。検出できない

衝突の型 3  共通 module を変えた結果、別の機能領域が壊れる
            -> PR 単体のレビューでは変更範囲しか見ないため見落としやすい
```

型 2 と型 3 を防ぐには「どのファイルを触るか」ではなく
**「どの機能領域の判定に効くか」**で並行可否を決める必要がある。本書はその
判定材料を提供する。

実例として Issue #140 は `domain/signals/company_quality_scoring.py` の
1 ファイルだけを変更した PR だが、この module の呼び出し元は
`services/buy_signal_service.py`(買い判定)と
`services/holding_decision_service.py`(保有判断)の 2 領域にまたがる。
ファイル重複だけを見ていると並行可能と誤判定する。

### 適用範囲

```
対象      本リポジトリの実装作業における code WIP の並行可否判断
対象外    Issue label の 4 軸(Issue Type / Priority / Release Blocker /
          Progress Status)。WIP は label とは別概念であり混ぜない
対象外    Production release の粒度。release は領域単位化しない
          (development_workflow.md 9節の grouped release が正本)
```

---

## B. 領域の境界を決める原則

領域は次の 4 条件を**すべて**満たす単位とする。

```
基準 1  独立した投資上の意思決定、または独立した運用上の役割を持つ
基準 2  主要な source / config / 永続契約が他領域と大きく重ならない
基準 3  その領域だけを壊しても、他領域の判定結果が変わらない
基準 4  1 人の作業者が 1 つの Issue で扱える大きさに収まる
```

基準 3 を満たさない資材は領域に属させず、**SHARED**(D節)として別に扱う。
これが衝突の型 2・型 3 への対策の中核である。

### 実行単位(Lambda)を領域の境界にしない

`lambda_handlers/holdings_watchlist_handler.py` 1 つが、買い・売り・利確・
保有判断・監視状態・通知の 6 領域のサービスを呼んでいる。Lambda を境界に
すると領域が巨大化し、担当者単位 WIP とほとんど変わらなくなる。

```
DOMAIN != LAMBDA
DOMAIN != DIRECTORY
```

ディレクトリも境界にしない。`domain/signals/` には単一領域のものと
複数領域から使われるものが混在している。**判定は参照関係の実測による。**

### 株主優待を独立領域にしない理由

株主優待関連は 35 ファイルに分散するが、性質が 2 つに割れている。

```
登録・取り込み側   レジストリ service / CSV 取り込み / provider / 専用 table
                 -> 判定を変えずに単独で変更できる。D6 PORTFOLIO の一機能

判定利用側        買いシグナル / 売りシグナル / 保有判断 / 監視スクリーニング /
                 スコアリング / 投資仮説 の 6 か所が参照
                 -> ここを変えると 4 領域の判定が同時に動く。SHARED(S-15)
```

1 領域にまとめると、lock を強くすれば「優待マスタへ 1 行足すだけ」の作業が
買い・売り・保有・監視をすべて止め、弱くすれば衝突の型 3 を素通しする。
分割したほうが安全かつ並行度が高いため、独立領域にしない。

---

## C. 領域一覧(DOMAIN_CATALOG)

| DOMAIN_ID | 名称 | 責務 |
|---|---|---|
| D1 | BUY | 買い候補の探索と買い判定 |
| D2 | SELL | 売却・利確・下落保護の判定 |
| D3 | HOLDING | 保有継続判断と投資仮説 |
| D4 | WATCHLIST | 監視銘柄の選定・分散実行・状態遷移 |
| D5 | NOTIFICATION | LINE 通知の送信・整形・抑止・受信応答 |
| D6 | PORTFOLIO | 保有・取引・優待・コーポレートアクションの台帳 |
| D7 | REVIEW | 判定の事後評価・レビュー・較正・改善提案 |
| D8 | DATA | 外部データ取得・cache・鮮度・品質 |
| D9 | PLATFORM | インフラ・実行基盤・監査・CLI・CI・開発運用ドキュメント |

`DOMAIN_ID` は再利用しない。領域の追加・分割・統合は G節の承認を要する。

### D2 SELL と D3 HOLDING を分ける根拠

どちらも保有銘柄を対象とするが、config・永続契約とも分かれている。

```
D2  sell_rules.yaml / profit_taking_rules.yaml      -> RecommendationsTable
D3  holding_decision_rules.yaml ほか 2 種            -> HoldingDecisionResultsTable
```

統合すると保有銘柄まわりの作業がすべて直列化し、保有判断の修正と下落保護の
修正を同時に進められなくなる。両方に効く変更では 2 領域を同時取得すれば足りる。

---

## D. SHARED 層

```
SHARED = 2 つ以上の領域の判定結果・永続表現を同時に変えうる資材
```

SHARED は**領域ではなく層**である。`SHARED` を primary domain として
宣言することはできない。SHARED を触る場合は、その部品が影響する領域の
code WIP を取得する(development_workflow.md 2.6.4)。

SHARED は置き場所ではなく**性質**である。`domain/` 配下にあっても単一領域から
しか使われないものは SHARED ではなく、`services/` 配下でも複数領域から
呼ばれていれば SHARED である。

---

## E-L. 機能一覧と mapping(FUNCTION_CATALOG / FUNCTION_DOMAIN_MAPPING)

機能の粒度は**「利用者から見て意味のある能力、または運用上独立して
差し替えられる単位」**とする。`1 関数 = 1 機能`にはしない。

全 45 機能。各表の列は次を表す。

```
ID                FUNCTION_ID。再利用しない(I節)
機能               FUNCTION_NAME
主要 source        MAJOR_SOURCE_PATHS(src/jstock_advisor/ からの相対。
                  D9 のみリポジトリ root からの相対)
主要 config        MAJOR_CONFIG_PATHS(config/ 配下)
永続契約           PERSISTED_CONTRACTS(DynamoDB table / 保存表現)
影響領域           AFFECTED_DOMAINS。先頭が PRIMARY_DOMAIN
```

共通事項。

```
MAJOR_TEST_PATHS  tests/unit/ 配下。Issue 起点の回帰は
                  tests/unit/test_issue_<番号>_*.py に集約されている
UPSTREAM          記載がない場合は D8 DATA(価格・財務・開示)
DOWNSTREAM        「影響領域」欄が下流を含む
SHARED_COMPONENTS 「影響領域」に S を含む機能は K節の該当 ID を参照する
```

### D1 BUY

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-01 | 買い候補日次バッチ | `lambda_handlers/buy_candidates_handler.py` `lambda_handlers/_fanout.py` | `schedule.yaml` | `BuyCandidateEvaluationRecordsTable` `BuyCandidateBatchCompletionTable` | D1 / D5 / D9 |
| F-02 | 買いシグナル判定 | `domain/signals/buy_signal.py` `domain/signals/buy_decision.py` `domain/signals/buy_consistency.py` `services/buy_signal_service.py` | `buy_decision_rules.yaml` `add_on_rules.yaml` | `RecommendationsTable` | D1 / S |
| F-03 | 買値レンジ算出 | `domain/signals/entry_price_range.py` `domain/valuation/buy_price_levels.py` `domain/valuation/buy_price_reliability.py` | `entry_exit_price_rules.yaml` | `EntryPriceRange`(Recommendation 内) | D1 / S |
| F-04 | 見送り理由と整合性検証 | `services/recommendation_consistency_validator.py` `domain/signals/judgment_safety_ladder.py` | `confidence_rules.yaml` | `SkippedRecommendationsTable` | D1 / D2 / D3 |
| F-05 | 買い候補の表示整形 | `services/buy_candidate_target_view_service.py` `services/stock_analysis_view_service.py` | — | なし(読み取りのみ) | D1 / D5 |

### D2 SELL

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-06 | 売却シグナル判定 | `domain/signals/sell_signal.py` `services/sell_signal_service.py` | `sell_rules.yaml` | `RecommendationsTable` | D2 / S |
| F-07 | 利確判定 | `domain/signals/profit_taking.py` `services/profit_taking_service.py` `domain/classification/profit_taking_industry.py` | `profit_taking_rules.yaml` | `RecommendationsTable` | D2 / S |
| F-08 | 下落保護 | `domain/signals/profit_protection.py` | `sell_rules.yaml` | `RecommendationsTable` | D2 |
| F-09 | 売値レンジ算出 | `domain/signals/exit_price_range.py` `services/sell_price_recommendation_service.py` | `entry_exit_price_rules.yaml` | `ExitPriceRange`(Recommendation 内) | D2 / S |

### D3 HOLDING

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-10 | 保有継続判断 | `domain/signals/holding_decision_score.py` `domain/signals/holding_decision_hard_gate.py` `domain/signals/holding_decision_execution_plan.py` `services/holding_decision_service.py` | `holding_decision_rules.yaml` `holding_decision_risk_rules.yaml` `holding_decision_ratio_rules.yaml` | `HoldingDecisionResultsTable` `HoldingEvaluationRecordsTable` | D3 / S |
| F-11 | 投資仮説の管理と採点 | `services/investment_thesis_service.py` `domain/signals/investment_thesis_scoring.py` | `investment_thesis_template.yaml` | `InvestmentThesesTable` `InvestmentThesisBaselinesTable` `InvestmentThesisBaselineSequencesTable` `InvestmentThesisBaselinePointersTable` | D3 |
| F-12 | 保有判断の実行時設定 | `services/holding_decision_runtime_config_service.py` | — | `HoldingDecisionRuntimeConfigTable` | D3 / D9 |
| F-13 | 取引停止・クールダウン | `services/trading_pause_service.py` `services/trade_cooldown_service.py` `infrastructure/aws/trading_pause_config.py` | — | `TradingPauseConfigTable` | D3 / D1 / D2 |
| F-14 | 保有スナップショット | `services/holdings_view_service.py` `domain/entities/holdings_snapshot.py` | — | `HoldingsSnapshotTable` | D3 / D6 |

### D4 WATCHLIST

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-15 | 監視候補スクリーニング | `domain/signals/watchlist_screening.py` `domain/screening/rules.py` `services/watchlist_screening_service.py` | `watchlist_screening_rules.yaml` `screening_rules.yaml` | `WatchlistTable` | D4 / S |
| F-16 | 分散実行(dispatcher / worker / 回収) | `lambda_handlers/watchlist_dispatcher_handler.py` `lambda_handlers/watchlist_worker_handler.py` `lambda_handlers/watchlist_batch_reconciler_handler.py` `lambda_handlers/watchlist_terminal_failure_handler.py` | `schedule.yaml` | `WatchlistCandidateProgressTable` `WatchlistScreeningRotationStateTable` `WatchlistRotationDispatchLeaseTable` | D4 / D9 |
| F-17 | 監視状態遷移・営業日カウント | `services/watch_state_service.py` `domain/signals/near_buy.py` | `notification_rules.yaml` | `WatchStateTable` `ValidationWatchStateTable` | D4 / D1 / D5 |
| F-18 | 監視銘柄の登録・削除・維持 | `services/watchlist_service.py` `services/watchlist_maintenance_service.py` `services/watchlist_csv_import_service.py` | — | `WatchlistTable` `WatchlistRemovalHistoryTable` | D4 |
| F-19 | 監視データ cache | `services/watchlist_data_cache.py` | — | `WatchlistPriceCacheTable` `WatchlistFinancialCacheTable` | D4 / D8 |
| F-20 | 監視結果の表示・要約整形 | `services/watchlist_view_service.py` `services/watchlist_judgment_summary_formatter.py` `services/watchlist_score_detail.py` `services/watchlist_addition_summary_builder.py` | — | なし | D4 / D5 |

### D5 NOTIFICATION

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-21 | LINE 通知送信 | `services/line_notification_service.py` `infrastructure/line/` | `notification_rules.yaml` | `NotificationLogTable` | D5 |
| F-22 | メッセージ整形 | `domain/notification/message_formatter.py` `domain/notification/recommendation_adapter.py` `domain/notification/notification_intent.py` | `notification_rules.yaml` | なし | D5 |
| F-23 | 重複抑止・優先度 | `domain/entities/notification_claim.py` `domain/entities/daily_notification_priority.py` `domain/entities/notification_eligibility.py` | `notification_rules.yaml` | `NotificationClaimsTable` `DailyNotificationPriorityTable` `ValidationDailyNotificationPriorityTable` | D5 |
| F-24 | LINE 受信と対話応答 | `lambda_handlers/line_webhook_handler.py` `services/line_event_router.py` `services/conversation_service.py` `infrastructure/aws/conversation_state_store.py` | — | `ConversationStatesTable` | D5 |
| F-25 | 利用者フィードバック収集 | `services/user_feedback_service.py` | — | `UserFeedbackTable` | D5 / D7 |

### D6 PORTFOLIO

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-26 | 保有・取得ロット管理 | `services/portfolio_service.py` `domain/entities/holding.py` | — | `HoldingsTable` `PurchaseLotsTable` | D6 |
| F-27 | 取引履歴の取り込み | `services/transaction_csv_import_service.py` `services/transaction_history_service.py` `services/csv_import_ledger.py` | — | `TransactionsTable` | D6 |
| F-28 | 取引イベント検知 | `domain/signals/trade_event_detection.py` `infrastructure/aws/trade_detection_lock.py` | — | `TradeDetectionRunLockTable` | D6 / D3 |
| F-29 | 株主優待レジストリ | `services/shareholder_benefit_registry_service.py` `services/shareholder_benefit_csv_import_service.py` `providers/shareholder_benefit/` | `shareholder_return_policies.yaml` | `ShareholderBenefitsTable` | D6 |
| F-30 | コーポレートアクション反映 | `services/corporate_action_service.py` `providers/corporate_action/` | — | `CorporateActionRegistryTable` `StockNameOverridesTable` | D6 / D8 |

### D7 REVIEW

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-31 | 週次・月次・四半期レビュー | `lambda_handlers/weekly_review_handler.py` `lambda_handlers/monthly_review_handler.py` `lambda_handlers/quarterly_review_handler.py` `services/review_report_service.py` `services/weekly_improvement_review_service.py` | `review_improvement.yaml` `schedule.yaml` | 週次レビュー指標の保存先 | D7 / D5 |
| F-32 | 判定の事後評価 | `lambda_handlers/evaluation_handler.py` `services/recommendation_evaluation_service.py` `services/decision_performance_service.py` | `evaluation_rules.yaml` `decision_evaluation.yaml` | `EvaluationResultsTable` | D7 |
| F-33 | 較正・バックテスト | `services/calibration_analysis_service.py` `services/calibration_dataset_service.py` `services/backtest_service.py` `services/holding_decision_backtest_service.py` `services/before_after_report_service.py` | — | なし(読み取り中心) | D7 |
| F-34 | 改善提案・ルール版管理 | `services/rule_proposal_service.py` `services/rule_version_service.py` `services/github_issue_service.py` `domain/improvement_rules.py` | `review_improvement.yaml` | `RuleVersionsTable` | D7 / D9 |

### D8 DATA

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-35 | 市場価格取得 | `providers/market_data/` `services/yfinance_rate_limit.py` `services/run_scoped_market_data.py` | — | なし | D8 |
| F-36 | 財務・配当データ取得 | `providers/financial_data/` `providers/dividend_data/` `domain/financial_series.py` `domain/financial_decomposition.py` | — | なし | D8 / S |
| F-37 | 開示情報取得 | `providers/disclosure/` `infrastructure/edinet/` `lambda_handlers/disclosure_check_handler.py` `services/disclosure_check_service.py` | `schedule.yaml` | `EdinetFilingCacheTable` `EdinetDisclosureCacheTable` `EdinetDailyDocumentListCacheTable` | D8 / D5 |
| F-38 | データ鮮度・品質監視 | `domain/price_freshness.py` `domain/financial_freshness.py` `services/data_quality_service.py` `services/financial_freshness_integration.py` | `data_validation_rules.yaml` | データ品質アラートの保存先 | D8 / S |
| F-39 | 銘柄ユニバース収集 | `services/candidate_universe_downloader.py` `services/jpx_industry_source.py` `providers/candidate_universe/` | — | なし | D8 / D1 / D4 |
| F-40 | provider 障害分類 | `providers/_failure.py` `services/provider_failure_classifier.py` `services/provider_factory.py` `services/provider_bundle.py` `interfaces/provider_errors.py` | — | なし | D8 / S |

### D9 PLATFORM

`主要 source` はリポジトリ root からの相対。

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-41 | インフラ定義・デプロイ | `infra/template.yaml` `infra/` | — | 全 table 定義 / IAM / Secrets 参照 | D9 / 全領域 |
| F-42 | 実行モード・スケジュール | `src/jstock_advisor/lambda_handlers/_execution_mode.py` `src/jstock_advisor/lambda_handlers/_scheduling.py` `src/jstock_advisor/domain/entities/execution_context.py` | `schedule.yaml` `holiday_calendar.json` | `BatchRunsTable` | D9 / 全領域 |
| F-43 | 監査ログ・実行追跡 | `src/jstock_advisor/services/audit_service.py` `src/jstock_advisor/services/evaluation_run_audit.py` `src/jstock_advisor/infrastructure/aws/batch_tracker.py` | — | `AuditLogTable` `BatchRunsTable` | D9 |
| F-44 | CLI 運用コマンド | `src/jstock_advisor/cli/` | — | なし | D9 / 全領域 |
| F-45 | CI・品質ゲート | `.github/workflows/ci.yml` `scripts/` | — | なし | D9 |

---

## K. 共通部品カタログ(SHARED_COMPONENT_CATALOG)

「lock する領域」は、その部品を `LOCK_LEVEL_2` 以上で変更する場合に
code WIP を取得すべき領域である(L節)。呼び出し元の実測に基づく。

| SHARED_ID | 共通部品 | 主要 path | lock する領域 | 実測した主な参照元 |
|---|---|---|---|---|
| S-01 | 企業品質スコア | `domain/signals/company_quality_scoring.py` | D1 / D3 | `buy_signal_service` `holding_decision_service` `simple_roe` |
| S-02 | 判定スナップショット | `domain/entities/decision_snapshot.py` `domain/decision_snapshot_builder.py` `services/decision_snapshot_service.py` | D1 / D3 / D4 / D7 | 買い候補 handler / 保有監視 handler / 較正 / 実績評価 |
| S-03 | 推奨エンティティ | `domain/entities/recommendation.py` `infrastructure/local_repository/recommendation_repository.py` | D1 / D2 / D3 / D4 / D5 / D7 | 全判定系 + 通知 + 評価 |
| S-04 | 営業日カレンダー | `domain/business_calendar.py` `domain/jst.py` `domain/market_session.py` `config/holiday_calendar.json` | D1 / D2 / D3 / D4 / D8 / D9 | screening / 環境 / 鮮度 / 決算窓 / handler / CLI |
| S-05 | バリュエーション | `domain/valuation/` | D1 / D2 / D4 | `entry_price_range` `exit_price_range` `profit_taking` `buy_signal_service` `stock_snapshot_service` |
| S-06 | 信頼度スコア | `domain/signals/confidence_scoring.py` `config/confidence_rules.yaml` | D1 / D2 / D8 | `valuation_confidence` `sell_signal_service` `profit_taking_service` `financial_freshness_integration` |
| S-07 | 銘柄・業種分類 | `domain/classification/` `config/stock_classification_rules.yaml` `config/industry_scoring_policy.yaml` | D1 / D2 / D3 / D4 | 買い / 利確 / 財務 / 正規化の各分類 |
| S-08 | 監視接近判定 | `domain/signals/near_buy.py` | D1 / D4 / D5 | `buy_candidates_handler` `watch_state_service` `line_notification_service` `recommendation_adapter` |
| S-09 | スコアリング基盤 | `domain/scoring/` `config/scoring_weights.yaml` | D1 / D3 / D4 | 全スコア算出 |
| S-10 | 財務系列・鮮度 | `domain/financial_series.py` `domain/financial_freshness.py` `domain/price_freshness.py` | D1 / D2 / D3 / D8 | 判定系全般 + 品質監視 |
| S-11 | 決算イベント | `domain/signals/earnings_surprise.py` `domain/signals/earnings_trend.py` `domain/signals/earnings_window.py` | D1 / D2 / D3 | 各判定 |
| S-12 | 市場・セクター環境 | `domain/signals/market_environment.py` `domain/signals/sector_environment.py` `domain/signals/_environment_shared.py` | D1 / D2 / D4 | 各判定 |
| S-13 | 設定ロードとスキーマ | `config/loader.py` `config/models.py` | 全領域 | すべての config 読み込み |
| S-14 | provider 契約 | `interfaces/` | D1 / D2 / D3 / D4 / D8 | provider 実装と全利用側 |
| S-15 | 優待の判定利用 | `domain/valuation/shareholder_benefit_matching.py` `domain/signals/record_date_resolution.py` | D1 / D2 / D3 / D4 | 買い / 売り / 保有 / 監視 / スコア / 投資仮説 |
| S-16 | 共通 enum・基底 | `domain/entities/enums.py` `domain/entities/common.py` `domain/entities/base.py` | 全領域 | 全域 |

`SHARED_ID` は再利用しない。

---

## L. 実質的な影響領域の決め方

`AFFECTED_DOMAINS` と SHARED の「lock する領域」は、**推測ではなく実測**で
決める。呼び出し元は検索で確認できる。

```
実測の最低手順

1  変更対象の module / 関数 / field の参照元を全件列挙する
2  各参照元がどの機能(F-xx)に属するかを本書の表で引く
3  その機能の PRIMARY_DOMAIN を集めたものが lock 対象の領域
4  列挙件数を DOMAIN_WIP_DECLARATION へ記録する
```

```
禁止事項

「たぶん影響しない」で lock 対象を減らす
ディレクトリ名や module 名だけで領域を判断する
本書の表を読んだだけで実測を省く(表は出発点であり、実測の代わりではない)
```

判定できない場合は fail-closed とする
(`SHARED_CLASSIFICATION_UNKNOWN` -> `LOCK_LEVEL_3`。
判定表は development_workflow.md 2.6.5 が正本)。

---

## M. カタログの維持契約(lifecycle)

本書が実体とずれると、WIP 判定の根拠が失われる。以下は本書の維持義務である。

### M.1 新しい機能を追加したとき(NEW_FUNCTION_RULE)

```
N1  新しい機能を実装する PR に、本書への行追加を同梱する

N2  追加行の必須項目

      FUNCTION_ID          連番。既存 ID を再利用しない
      FUNCTION_NAME
      PRIMARY_DOMAIN
      AFFECTED_DOMAINS
      MAJOR_SOURCE_PATHS
      MAJOR_CONFIG_PATHS   無ければ「—」
      PERSISTED_CONTRACTS  無ければ「なし」
      SHARED_COMPONENTS    参照する SHARED_ID。無ければ記載不要

N3  既存領域の中に収まる追加であれば、新たな人間承認は不要。
    実装 PR のレビューでカタログ行も一緒にレビューする

N4  新しい永続契約(table / field)を伴う場合、それを K節へ載せるかを判定する
      2 領域以上が読む -> K節へ SHARED として追加
      1 領域のみ       -> 機能行の「永続契約」欄のみ
```

### M.2 新しい領域を作りたいとき(NEW_DOMAIN_RULE)

領域の追加・分割・統合は**人間承認が必須**である。

```
理由: 領域は WIP の単位である。作業者が自分の都合で領域を増やせると、
      「自分の作業専用の領域」を宣言して lock を回避できてしまう。
```

```
手順

1  proposal            追加したい領域名と責務
2  boundary evidence   B節の基準 1〜4 それぞれを満たす根拠
                       (実測した path と参照関係)
3  migration impact    既存のどの機能が移るか
4  WIP lock impact     移動によって既存の LOCKED_DOMAINS 判定がどう変わるか
5  ChatGPT review
6  人間承認
7  docs 更新 PR
8  merge / main CI
9  必要なら明示的な発効
```

```
禁止  承認前に新領域名で DOMAIN_WIP_DECLARATION を掲示する
禁止  lock を回避する目的で領域を新設・分割する
```

### M.3 機能の廃止・領域の統廃合(DEPRECATION_RULE)

```
P1  機能を廃止する PR では、カタログの行を削除せず次を保持する

      DEPRECATED_AT          廃止日
      SUCCESSOR_FUNCTION_ID  後継。無ければ NONE

    過去の Issue / snapshot が参照する FUNCTION_ID を壊さないため

P2  FUNCTION_ID は再利用しない(欠番のままにする)。SHARED_ID / DOMAIN_ID も同じ

P3  領域の統廃合は M.2 と同じ承認経路を通る

P4  廃止によって SHARED でなくなった部品は K節から外し、
    外した理由(参照元が 1 領域になった実測)を変更履歴へ記す
```

### M.4 陳腐化を防ぐゲート

```
GATE_1  PR チェックリスト(人による。適用は本書の発効後)

    本 PR は新しい機能を追加したか            -> YES ならカタログ行を追加したか
    本 PR は SHARED を追加・変更したか         -> YES なら K節を更新したか
    本 PR の LOCKED_DOMAINS は宣言どおりだったか

GATE_2  CI による機械的検査(将来実装。本書の作成時点では未実装)

    カタログが参照する path がすべて実在すること
    カタログに載っていない lambda_handlers / config が無いこと
    FUNCTION_ID / DOMAIN_ID / SHARED_ID の重複が無いこと
    廃止済み ID の再利用が無いこと

GATE_3  定期棚卸し(最低 quarterly)

    K節の SHARED について参照元を実測し直し、増減を確認する
```

```
GATE_2 は「path の存在」など機械的に判定できる性質だけを対象とする。
「この機能の説明が正しいか」は機械では判定できないため CI に入れない。
そこは GATE_1 と GATE_3 が担う。

GATE_2 では「参照元が増えて SHARED になった」ことを検出できない。
これは GATE_3 の役割である。

CI ジョブの追加は Production の判定へ影響しないが、必須ジョブを増やすと
全 PR の merge 条件が変わる。導入は別 PR とする。
```

---

## 変更履歴

| 日付 | 変更概要 |
|---|---|
| 2026-09-06 | 新規作成(Issue #177)。担当者単位の code WIP 制限が、互いに無関係な機能領域まで直列化する一方で、共通 module 経由の semantic conflict(衝突の型 2・型 3)を防げていなかったため、並行して安全な範囲を判定するための材料を正本化した。領域 D1〜D9 と SHARED 層、機能 F-01〜F-45、共通部品 S-01〜S-16 を、呼び出し元の実測に基づいて定義している。**実行単位(Lambda)・ディレクトリを領域の境界にしない**(1 つの handler が 6 領域のサービスを呼ぶ実測があるため)。株主優待は「登録・取り込み側(D6)」と「判定利用側(S-15)」で性質が割れるため独立領域にしない。D2 SELL と D3 HOLDING は config・永続契約が分かれているため分離する(Human 承認 H1)。あわせて新規機能・新規領域・廃止時のカタログ維持契約と、陳腐化防止の 3 段ゲートを定めた。**運用ルール本文は development_workflow.md 2.6節が正本であり本書へ複製していない。** 本書の作成時点で `DOMAIN_WIP_MODEL_ACTIVE = NO` であり、有効な WIP ルールは #122(担当者単位 code WIP = 1)のままである。判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない |
