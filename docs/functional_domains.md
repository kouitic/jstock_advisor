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

## 0. 発効状態

```
CURRENT_WIP_RULE = DOMAIN_WIP_RULE_V1
EFFECTIVE_FROM   = 2026-09-06 02:27 JST(2026-09-05T17:27:01Z)
```

領域ベース WIP は既に発効している。発効の手順と、発効時点で進行中だった
作業の扱いは [development_workflow.md](development_workflow.md) 2.6.10 が正本である。

発効状態は運用の中で変わりうる(試行の結果、人間の判断で従来ルールへ戻すことも
ありうる)。**本書のような静的な文書を、変わりうる状態の唯一の根拠にしない。**
現在の発効状態を確認する必要がある場合の参照先は
[development_workflow.md](development_workflow.md) 2.6.10 の
`ACTIVATION_STATE_SSOT` に従う。上記の値は本節を改訂した時点のものである。

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

全 48 機能。各表の列は次を表す。

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
| F-04 | 見送り理由と整合性検証 | `services/recommendation_consistency_validator.py` | `confidence_rules.yaml` | `SkippedRecommendationsTable` | D1 / D2 / D3 |
| F-05 | 買い候補・保有銘柄の表示整形 | `services/buy_candidate_target_view_service.py` `services/stock_analysis_view_service.py` | — | なし(読み取りのみ) | D1 / D3 / D5 |

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
| F-11 | 投資仮説の管理と採点 | `services/investment_thesis_service.py` `domain/signals/investment_thesis_scoring.py` `cli/baseline_repair.py` | `investment_thesis_template.yaml` | `InvestmentThesesTable` `InvestmentThesisBaselinesTable` `InvestmentThesisBaselineSequencesTable` `InvestmentThesisBaselinePointersTable` | D3 |
| F-12 | 保有判断の実行時設定 | `services/holding_decision_runtime_config_service.py` | — | `HoldingDecisionRuntimeConfigTable` | D3 / D9 |
| F-13 | 取引停止・クールダウン | `services/trading_pause_service.py` `services/trade_cooldown_service.py` `infrastructure/aws/trading_pause_config.py` | — | `TradingPauseConfigTable` `TradeEventRecordsTable`(Issue #71 F-C11 Phase 1) | D3 / D1 / D2 |
| F-14 | 保有スナップショット | `services/holdings_view_service.py` `domain/entities/holdings_snapshot.py` `services/stock_snapshot_service.py` | — | `HoldingsSnapshotTable` | D3 / D6 |
| F-46 | 保有監視日次バッチ | `lambda_handlers/holdings_watchlist_handler.py` `lambda_handlers/_fanout.py` | `schedule.yaml` | `RecommendationsTable` `DecisionSnapshotsTable` `HoldingDecisionResultsTable` `HoldingEvaluationRecordsTable` `NotificationLogTable` | D3 / D1 / D2 / D4 / D5 / D9 / S |

```
★ F-46 は B節「実行単位(Lambda)を領域の境界にしない」の実例そのものである。

  B節は本 handler を「買い・売り・利確・保有判断・監視状態・通知の 6 領域の
  サービスを呼んでいる」実例として挙げているが、機能一覧に行が無いため
  L節の手順(参照元を本書の表で引く)が成立しなかった(Issue #209)。

  影響領域は呼び出し先の実測による。
    buy_signal_service                       -> D1
    sell_signal_service / profit_taking_service -> D2
    holding_decision_service                 -> D3(PRIMARY)
    watch_state_service                      -> D4
    LineNotificationService                  -> D5
    batch tracker / audit                    -> D9
    decision_snapshot_service / stock_snapshot_service /
    shareholder_benefit_registry_service     -> S(S-02 / S-05 / S-15)

  B節の「6 領域」はサービスの数え方であり、売りと利確がともに D2 のため
  領域としては D1〜D5 の 5 つになる。本行はそれに D9 と S を加えた実測値である。

  保有台帳(D6)は **読み取りのみで書き込まない**ため影響領域に含めない
  (F-10 保有継続判断が holdings を読みながら D3 / S に留めているのと同じ扱い)。
  株主優待も判定利用側であり D6 ではなく S-15 として数える(B節)。
```

### D4 WATCHLIST

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-15 | 監視候補スクリーニング | `domain/signals/watchlist_screening.py` `domain/screening/rules.py` `services/watchlist_screening_service.py` `services/watchlist_screening_audit.py` | `watchlist_screening_rules.yaml` `screening_rules.yaml` | `WatchlistTable` | D4 / S |
| F-16 | 分散実行(dispatcher / worker / 回収) | `lambda_handlers/watchlist_dispatcher_handler.py` `lambda_handlers/watchlist_worker_handler.py` `lambda_handlers/watchlist_batch_reconciler_handler.py` `lambda_handlers/watchlist_terminal_failure_handler.py` `lambda_handlers/_watchlist_execution_mode.py` | `schedule.yaml` | `WatchlistCandidateProgressTable` `WatchlistScreeningRotationStateTable` `WatchlistRotationDispatchLeaseTable` | D4 / D9 |
| F-17 | 監視状態遷移・営業日カウント | `services/watch_state_service.py` `domain/signals/near_buy.py` | `notification_rules.yaml` | `WatchStateTable` `ValidationWatchStateTable` | D4 / D1 / D5 |
| F-18 | 監視銘柄の登録・削除・維持 | `services/watchlist_service.py` `services/watchlist_maintenance_service.py` `services/watchlist_csv_import_service.py` | — | `WatchlistTable` `WatchlistRemovalHistoryTable` | D4 |
| F-19 | 監視データ cache | `services/watchlist_data_cache.py` | — | `WatchlistPriceCacheTable` `WatchlistFinancialCacheTable` | D4 / D8 |
| F-20 | 監視結果の表示・要約整形 | `services/watchlist_view_service.py` `services/watchlist_judgment_summary_formatter.py` `services/watchlist_score_detail.py` `services/watchlist_addition_summary_builder.py` | — | なし | D4 / D5 |

### D5 NOTIFICATION

| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |
|---|---|---|---|---|---|
| F-21 | LINE 通知送信 | `services/line_notification_service.py` `infrastructure/line/` | `notification_rules.yaml` | `NotificationLogTable` | D5 |
| F-22 | メッセージ整形 | `domain/notification/message_formatter.py` `domain/notification/recommendation_adapter.py` `domain/notification/notification_intent.py` `services/holding_decision_notification_builder.py` | `notification_rules.yaml` | なし | D5 |
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
| F-45 | CI・品質ゲート | `.github/workflows/ci.yml` `.github/workflows/pii-metadata-audit.yml` `scripts/` `docs/policy_registry.yaml` | — | なし | D9 |
| F-47 | batch finalize recovery | `lambda_handlers/_finalize_recovery.py` `services/watchlist_batch_finalizer.py` | — | `BuyCandidateBatchCompletionTable`(読み取り) | D9 / D4 / D1 / D3 |
| F-48 | 判断の安全条件のshadow計測 | `domain/signals/judgment_safety_shadow_config.py` `domain/signals/judgment_safety.py` `services/judgment_safety_shadow_service.py` `services/judgment_safety_shadow_report.py` `infrastructure/aws/audit_shadow_reader.py` `cli/judgment_safety_shadow.py` | `judgment_safety_shadow.yaml`(専用loader。AppConfigへは載せない) | `AuditLogTable`(`decision_type=judgment_safety_shadow`。PR-3[#457]から。shadowがOFF[既定]なら書かない。新規Table・IAMなし。集計CLIは読み取りのみ[#458]) | D1 / D2 / D3 / D9 |

```
★ F-47 の影響領域は「生産側」と「消費側」の両方から成る。

  生産側  watchlist_batch_reconciler_handler(F-16 / D4)が
          build_finalize_only_payload() で recovery の payload を組み立てる
  消費側  buy_candidates_handler(F-01 / D1)と
          holdings_watchlist_handler(F-46 / D3 ほか)が受け取って finalize する

  したがって本部品を変更すると、停滞 batch の復旧経路を通じて
  D1 と D3 の判定結果まで届く。D9 を PRIMARY としたのは、
  `_execution_mode.py` / `_scheduling.py`(F-42)と同じく
  **実行基盤側の共通部品**であるためである。
```

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
| S-05 | バリュエーション | `domain/valuation/` | D1 / D2 / D4 / D5 / D6 / D7 | `entry_price_range` `exit_price_range` `profit_taking` `buy_signal_service` `stock_snapshot_service` `screening_data_provider`(F-15) `stock_analysis_view_service`(F-05) `watchlist_judgment_summary_formatter`(F-20) `shareholder_benefit_registry_service`(F-29) `valuation_shadow_analysis`(F-33) |
| S-06 | 信頼度スコア | `domain/signals/confidence_scoring.py` `config/confidence_rules.yaml` | D1 / D2 / D8 | `valuation_confidence` `sell_signal_service` `profit_taking_service` `financial_freshness_integration` |
| S-07 | 銘柄・業種分類 | `domain/classification/` `config/stock_classification_rules.yaml` `config/industry_scoring_policy.yaml` | D1 / D2 / D3 / D4 | 買い / 利確 / 財務 / 正規化の各分類 |
| S-08 | 監視接近判定 | `domain/signals/near_buy.py` | D1 / D4 / D5 | `buy_candidates_handler` `watch_state_service` `line_notification_service` `recommendation_adapter` |
| S-09 | スコアリング基盤 | `domain/scoring/` `config/scoring_weights.yaml` `domain/signals/risk_deduction_scoring.py` `domain/signals/momentum.py` `domain/signals/timing_score.py` `domain/entities/momentum.py` `domain/entities/timing_score.py` | D1 / D3 / D4 | 全スコア算出 |
| S-10 | 財務系列・鮮度 | `domain/financial_series.py` `domain/financial_freshness.py` `domain/price_freshness.py` | D1 / D2 / D3 / D8 | 判定系全般 + 品質監視 |
| S-11 | 決算イベント | `domain/signals/earnings_surprise.py` `domain/signals/earnings_trend.py` `domain/signals/earnings_window.py` | D1 / D2 / D3 | 各判定 |
| S-12 | 市場・セクター環境 | `domain/signals/market_environment.py` `domain/signals/sector_environment.py` `domain/signals/_environment_shared.py` | D1 / D2 / D4 | 各判定 |
| S-13 | 設定ロードとスキーマ | `config/loader.py` `config/models.py` | 全領域 | すべての config 読み込み |
| S-14 | provider 契約 | `interfaces/` | D1 / D2 / D3 / D4 / D8 | provider 実装と全利用側 |
| S-15 | 優待の判定利用 | `domain/valuation/shareholder_benefit_matching.py` `domain/signals/record_date_resolution.py` | D1 / D2 / D3 / D4 | 買い / 売り / 保有 / 監視 / スコア / 投資仮説 |
| S-16 | 共通 enum・基底 | `domain/entities/enums.py` `domain/entities/common.py` `domain/entities/base.py` | 全領域 | 全域 |
| S-17 | 永続化ストア層 | `infrastructure/collection_store.py` `infrastructure/local_repository/json_store.py` `infrastructure/aws/dynamodb_store.py` `infrastructure/record_failure_policy.py` | 全領域 | 全 repository(通知 / 推奨 / 保有 / 監視 / 評価 / 監査)+ migrations + EDINET cache |
| S-18 | 所有者と holding_id 規約 | `domain/entities/owner.py` | D3 / D5 / D6 | `portfolio_service` `conversation_service` `csv_import_service` `transaction_history_service` `holding_repository` `holdings_owner_migration` ほか(src 内 15 module) |
| S-19 | 価格レンジ共通 | `domain/signals/_price_range_shared.py` | D1 / D2 | `entry_price_range`(F-03) `exit_price_range`(F-09) |
| S-20 | 観測・Shadow計測の隔離 | `domain/shadow_observation.py` | D1 / D2 / D3 / D4 / D5 / D6 / D9 | `stock_snapshot_service`(PR-3の8箇所+PR-8のEarnings Surprise/Trend Phase C前提4箇所)`holdings_watchlist_handler` `holding_decision_notification_builder` `buy_signal_service`(PR-6で8箇所を隔離済み)・`sell_signal_service`(PR-7でexit_price_range算出自体を隔離済み)・`profit_taking_service`(同左)は統合済み。ファイル単位ではなくcall-site単位での再sweep(USER決定、#384)は別途実施予定 |
| S-21 | LINE通知clientの実行時構築 | `infrastructure/line/client.py` | D1 / D2 / D3 / D4 / D5 / D7 / D8 / D9 | `buy_candidates_handler`(F-01) `holdings_watchlist_handler`(F-46) `disclosure_check_handler`(F-37) `line_webhook_handler`(F-24) `weekly_review_handler`(F-31) `watchlist_dispatcher_handler` / `watchlist_worker_handler` / `watchlist_terminal_failure_handler` / `watchlist_batch_reconciler_handler`(F-16) `cli/analyze.py` `cli/review.py` `cli/watchlist_screening.py`。Issue #117 Phase B1aで`build_live_line_client_from_env()`を追加。★ 切替済み: `line_webhook_handler`(B1b-1) / `watchlist_dispatcher_handler`(B1b-2) / `buy_candidates_handler`(B1b-3b)/ `holdings_watchlist_handler`(B1b-3c)/ `disclosure_check_handler`(B1b-3d)(後3者は実行モード別の`build_line_client_for_run(dry_run=...)`)/ `watchlist_worker_handler`(B1b-4a。strict版`build_live_line_client_from_env()`をNEW_CANDIDATE_SCREENING検出時のみ構築)/ `watchlist_terminal_failure_handler`(B1b-4b。workerと同じprescan方式、job_type欠損時の既定はNEW_CANDIDATE_SCREENING)/ `watchlist_batch_reconciler_handler`(B1b-4c。認証情報欠落は構築の失敗でなく送信時の失敗として扱い、登録は継続・欠落は全処理の後に送出)/ `weekly_review_handler`(B1b-4d。認証情報欠落は`service.run`の前に失敗させるfail-early)(★ Lambda handlerの切替は9本すべて完了。残る3 CLI(`cli/analyze.py` `cli/review.py` `cli/watchlist_screening.py`)は、CLI専用として意図的に旧`build_line_client_from_env()`のまま。ただし`cli/watchlist_screening.py`の4コマンド(run / retry-finalize / retry-notification / retry-stock)には`--notify`が無く、送信されないまま「送信済み」と記録される不具合がある: Issue #434) |
| S-22 | 市場休場日gate | `lambda_handlers/_market_holiday.py` | D1 / D5 / D9 | `buy_candidates_handler`(親)・`holdings_watchlist_handler`(親)・`watchlist_dispatcher_handler`(NEW_CANDIDATE_SCREENINGのみ)。営業日判定は`domain/business_calendar.py`(S-04)へ委譲し、新しい判定を作らない。recovery/child/worker/reconciler・適時開示・評価・週次月次四半期は対象外。VALIDATION限定のbypass(`allow_market_closed`) |

`SHARED_ID` は再利用しない。

### S-17 の読み取り API(2026-09-08 / Issue #279 で 1 つ追加)

```
既存(変更していない)
  list_all / iter_all / get / get_consistent / find /
  query_by_index / get_many / get_raw_data
  -> いずれも `list[T]` / `T | None` を返し、**decode の成否を返さない**

追加  find_with_outcome(predicate) -> DecodeOutcome[T]
  `find()` と同じ絞り込みに、decode の成否(`undecidable` / `failures`)を添える。
  `RecordFailurePolicy.FAIL_SAFE_SUPPRESS` の核心である「判定不能」を
  呼び出し側へ渡す口がどの読み取り API にも無かったため追加した(Issue #279)。
  実装は既存の `decode_records()` を経由する。
```

★ **既存 API の signature も挙動も変えていない。** 宣言していない collection は
既定の `STRICT` のままであり、本メソッドを呼ばない限り何も変わらない。

### S-17 の実測(2026-09-06 / main = 6ae201bc)

```
build_collection_store() の呼び出し   78 箇所
呼び出す module                        39 個
扱う collection(literal な file_name)  28 種
```

呼び出し元の領域は D1〜D9 のすべてに及ぶ。

```
D1  buy_candidate_evaluation_record / latest_buy_candidate_batch_pointer
D2  recommendation(売却・利確も同一 collection)
D3  holding_decision_result / holding_decision_runtime_config /
    holding_evaluation_record / investment_thesis / baseline_pointer /
    baseline_sequence / trading_pause_config
D4  watchlist / watch_state / watchlist_removal_history /
    watchlist_rotation_state / watchlist_data_cache
D5  notification_log / notification_claim / daily_notification_priority
D6  holding / holdings_snapshot / transaction / corporate_action_registry /
    shareholder_benefit_registry / holdings_owner 系 migrations
D7  evaluation / weekly_review_metrics / improvement_candidate / feedback /
    rule_version
D8  disclosure_finder / document_finder / document_list_cache /
    stock_name_override
D9  audit_log
```

```
★ 「lock する領域 = 全領域」は、この層の**実装を変更する場合**の既定である
  (K節冒頭のとおり LOCK_LEVEL_2 以上が対象)。
  本カタログの行そのものを直す等の docs 変更は D9 に閉じる。

  なお LOCK_LEVEL_1(ADDITIVE_AND_BACKWARD_COMPATIBLE)に該当することを
  5 つの compatibility evidence で示せる変更は、その限りではない
  (判定表は development_workflow.md 2.6.5 が正本)。
  既定 STRICT のまま失敗ポリシー機構を追加する等がこれにあたりうる(Issue #63)。
```

```
S-16 と S-17 の境界

  S-16  何を検証するか(entity の型・enum・extra="forbid" 等の基底設定)
  S-17  いつ・どの単位で検証するか(全件読み込み / per-record / 失敗時の扱い)

  両者は隣接するが別部品である。S-16 を変えると全 entity の契約が変わり、
  S-17 を変えると全 collection の読み書き経路が変わる。
```

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
5  管理者レビュー
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

### M.5 網羅性の不変条件(COVERAGE_INVARIANT)

```
C1  `src/jstock_advisor/` 配下の全 module(`__init__.py` を除く)は、
    いずれかの F 行または S 行の「主要 source」に属していなければならない

C2  属し方は 2 通りある。どちらも有効である
      ファイル指定      `services/stock_snapshot_service.py`
      ディレクトリ指定  `domain/valuation/`(配下の全 module を覆う)

C3  どちらにも属さない module は、下の UNCATALOGED 一覧へ
    「割り当て予定の F/S 行」を添えて載せる。これは一時的な許容であり、
    解消は Issue #212 Phase D で行う

C4  「主要 source」に書かれた path は実在しなければならない
    (削除・改名した module への参照を残さない)

C5  C1〜C4 は CI job `catalog-coverage` が機械的に検査する(Phase C。未実装)。
    検査は本書の**全文ではなく F 行 / S 行の主要 source 列**に対して行う
```

```
★ C2 を明記する理由

  何をもって「属する」とするかを文書側で先に決めないと、CI の実装が
  baseline を勝手に決めてしまう。実際、Phase A の実測はディレクトリ指定を
  数えなかったため未登録を 183 件と算出したが、C2 を適用すると 108 件である
  (差の 75 件は `domain/valuation/` 等のディレクトリ指定で既に覆われている)。

  ディレクトリ指定を数えるのは、本書の目的が L節すなわち
  「変更する module から lock 範囲を引く」ことだからである。
  S-05 の主要 path が `domain/valuation/` であれば、配下のどのファイルを
  変更する作業者もその 1 行で lock 範囲を確定できる。目的を果たしている。
```

### 「主要 source」列の意味(SOURCE_COLUMN_SEMANTICS)

この列は**代表例ではなく網羅**である。

```
本書は 2 通りに使われる

  top-down   機能から実装を探す                    -> 代表例で足りる
  bottom-up  L節。変更する module から lock 範囲を引く
                                                   -> **網羅でなければ成立しない**
```

L節は後者を要求する。表に無い module に当たった作業者は fail-closed で
`LOCK_LEVEL_3` となり、そこで作業が止まる(Issue #201 / #209 / #135 で実際に発生した)。

したがって「主な実装はこれ」という書き方をしない。その機能・共通部品に属する
module を**すべて**挙げる(ディレクトリ単位でまとめてよい。M.5 C2)。

### UNCATALOGED 一覧(Issue #212 / baseline)

```
BASELINE_AT      = main a3dca50a7d3aaeb2575983f415155e5ae9944686
MODULE_TOTAL     = 324(`__init__.py` を除く)
COVERED_BY_FILE  = 147
COVERED_BY_DIR   = 75
UNCATALOGED      = 102
DEAD_REFERENCE   = 0
```

下表の module は、まだ F 行 / S 行の主要 source に属していない。
**「割り当て予定」は提案であり、確定は Phase D の各 PR で行う。**

```
★ 本一覧は Phase D の作業表を兼ねる。
  割り当てが済んだ行は一覧から削る。**一覧が空になった時点で C1 が成立する。**
  一覧に残っているのに実は covered、という状態も Phase C の CI が FAIL させる
  (一覧そのものが陳腐化しないようにするため)。
```
#### `analysis/`  2 件

| module | 割り当て予定 |
|---|---|
| `analysis/valuation_shadow_analysis.py` | F-33 |
| `analysis/valuation_shadow_hypotheses.py` | F-33 |

#### `domain/entities/`  33 件

| module | 割り当て予定 |
|---|---|
| `domain/entities/_legacy_migration.py` | 要判断 |
| `domain/entities/audit.py` | F-43 |
| `domain/entities/buy_candidate_batch_pointer.py` | F-01 |
| `domain/entities/buy_candidate_evaluation_record.py` | F-32 |
| `domain/entities/buy_decision.py` | F-02 |
| `domain/entities/buy_evaluation_target.py` | F-02 |
| `domain/entities/classification.py` | S-07 |
| `domain/entities/corporate_action.py` | F-30 |
| `domain/entities/data_quality_alert.py` | F-38 |
| `domain/entities/earnings_surprise.py` | S-11 |
| `domain/entities/earnings_trend.py` | S-11 |
| `domain/entities/entry_price_range.py` | F-03 |
| `domain/entities/environment.py` | S-12 |
| `domain/entities/evaluation.py` | F-32 |
| `domain/entities/evaluation_audit.py` | F-32 |
| `domain/entities/exit_price_range.py` | F-09 |
| `domain/entities/feedback.py` | F-25 |
| `domain/entities/financial_input_provenance.py` | F-38 |
| `domain/entities/historical_valuation.py` | F-33 |
| `domain/entities/holding_decision.py` | F-10 |
| `domain/entities/holding_evaluation_record.py` | F-14 |
| `domain/entities/improvement.py` | F-34 |
| `domain/entities/market_environment.py` | S-12 |
| `domain/entities/momentum.py` | S-09 |
| `domain/entities/notification.py` | F-23 |
| `domain/entities/rule_version.py` | F-34 |
| `domain/entities/sector_environment.py` | S-12 |
| `domain/entities/timing_score.py` | S-09 |
| `domain/entities/trading_pause.py` | F-13 |
| `domain/entities/transaction.py` | F-27 |
| `domain/entities/valuation.py` | S-05 |
| `domain/entities/watch_state.py` | F-17 |
| `domain/entities/watchlist.py` | F-18 |

#### `domain/`  2 件

| module | 割り当て予定 |
|---|---|
| `domain/evaluation_rules.py` | F-32 |
| `domain/ranking.py` | F-05 |

#### `domain/signals/`  11 件

| module | 割り当て予定 |
|---|---|
| `domain/signals/add_on_risk.py` | F-10 |
| `domain/signals/dividend_cut_analysis.py` | F-06 |
| `domain/signals/environment.py` | S-12 |
| `domain/signals/eps_normalization.py` | F-02 |
| `domain/signals/historical_valuation.py` | F-33 |
| `domain/signals/momentum.py` | S-09 |
| `domain/signals/portfolio_concentration.py` | F-14 |
| `domain/signals/risk_deduction_scoring.py` | S-09 |
| `domain/signals/simple_roe.py` | S-01 |
| `domain/signals/timing_score.py` | S-09 |
| `domain/signals/trading_unit_feasibility.py` | F-03 |

#### `infrastructure/aws/`  8 件

| module | 割り当て予定 |
|---|---|
| `infrastructure/aws/baseline_pointer.py` | F-11 |
| `infrastructure/aws/baseline_sequence.py` | F-11 |
| `infrastructure/aws/conversation_commit.py` | F-24 |
| `infrastructure/aws/dynamodb_transaction.py` | S-17 |
| `infrastructure/aws/holding_replacement_commit.py` | F-26 |
| `infrastructure/aws/improvement_task_tracker.py` | F-34 |
| `infrastructure/aws/watchlist_rotation_dispatch_lease.py` | F-16 |
| `infrastructure/aws/watchlist_rotation_state.py` | F-16 |

#### `infrastructure/`  1 件

| module | 割り当て予定 |
|---|---|
| `infrastructure/external_value_parser.py` | F-36 |

#### `infrastructure/github/`  1 件

| module | 割り当て予定 |
|---|---|
| `infrastructure/github/client.py` | F-34 |

#### `infrastructure/local_repository/`  26 件

| module | 割り当て予定 |
|---|---|
| `infrastructure/local_repository/audit_log_repository.py` | F-43 |
| `infrastructure/local_repository/buy_candidate_evaluation_record_repository.py` | F-32 |
| `infrastructure/local_repository/corporate_action_registry_repository.py` | F-30 |
| `infrastructure/local_repository/daily_notification_priority_repository.py` | F-23 |
| `infrastructure/local_repository/decision_snapshot_repository.py` | S-02 |
| `infrastructure/local_repository/evaluation_repository.py` | F-32 |
| `infrastructure/local_repository/feedback_repository.py` | F-25 |
| `infrastructure/local_repository/holding_decision_result_repository.py` | F-10 |
| `infrastructure/local_repository/holding_decision_runtime_config_repository.py` | F-12 |
| `infrastructure/local_repository/holding_evaluation_record_repository.py` | F-14 |
| `infrastructure/local_repository/holding_repository.py` | F-26 |
| `infrastructure/local_repository/holdings_snapshot_repository.py` | F-14 |
| `infrastructure/local_repository/improvement_candidate_repository.py` | F-34 |
| `infrastructure/local_repository/investment_thesis_baseline_repository.py` | F-11 |
| `infrastructure/local_repository/investment_thesis_repository.py` | F-11 |
| `infrastructure/local_repository/latest_buy_candidate_batch_pointer_repository.py` | F-01 |
| `infrastructure/local_repository/notification_claim_repository.py` | F-23 |
| `infrastructure/local_repository/notification_log_repository.py` | F-23 |
| `infrastructure/local_repository/rule_version_repository.py` | F-34 |
| `infrastructure/local_repository/shareholder_benefit_registry_repository.py` | F-29 |
| `infrastructure/local_repository/stock_name_override_repository.py` | F-20 |
| `infrastructure/local_repository/transaction_repository.py` | F-27 |
| `infrastructure/local_repository/watch_state_repository.py` | F-17 |
| `infrastructure/local_repository/watchlist_removal_history_repository.py` | F-18 |
| `infrastructure/local_repository/watchlist_repository.py` | F-18 |
| `infrastructure/local_repository/weekly_review_metrics_repository.py` | F-31 |

#### `migrations/`  8 件

| module | 割り当て予定 |
|---|---|
| `migrations/baseline_migration.py` | 要判断(F 行新設 or 恒久例外) |
| `migrations/conversions.py` | 要判断(F 行新設 or 恒久例外) |
| `migrations/holdings_owner_migration.py` | 要判断(F 行新設 or 恒久例外) |
| `migrations/holdings_owner_preflight.py` | 要判断(F 行新設 or 恒久例外) |
| `migrations/holdings_owner_reclassification.py` | 要判断(F 行新設 or 恒久例外) |
| `migrations/legacy_shapes.py` | 要判断(F 行新設 or 恒久例外) |
| `migrations/target.py` | 要判断(F 行新設 or 恒久例外) |
| `migrations/v2_entities.py` | 要判断(F 行新設 or 恒久例外) |

#### `providers/`  1 件

| module | 割り当て予定 |
|---|---|
| `providers/mock_fixtures.py` | F-35 |

#### `providers/news/`  1 件

| module | 割り当て予定 |
|---|---|
| `providers/news/mock_impl.py` | F-40 |

#### `services/`  8 件

| module | 割り当て予定 |
|---|---|
| `services/csv_import_service.py` | F-26 |
| `services/holding_decision_compare_service.py` | F-33 |
| `services/latest_batch_records_provider.py` | F-43 |
| `services/performance_metrics_service.py` | F-32 |
| `services/screening_data_provider.py` | F-15 |
| `services/watchlist_candidate_collector.py` | F-39 |
| `services/watchlist_display_name.py` | F-20 |
| `services/write_plan.py` | 要判断 |

```
★ `migrations/` 8 件の扱いは未確定である(要判断)。

  一回限りの移行スクリプトであり恒常的な機能ではない。新規 F 行を起こすか、
  UNCATALOGED の恒久例外とするかを Phase D の着手時に決める。
  `holdings_owner_*` は保有台帳(D6)と保有判断(D3)に触れるため、
  lock 範囲を引けない状態のまま実行するのは危険である。

★ `domain/entities/_legacy_migration.py` と `services/write_plan.py` も
  用途の実測が要る(要判断)。
```

---

## 変更履歴

| 日付 | 変更概要 |
|---|---|
| 2026-09-06 | 新規作成(Issue #177)。担当者単位の code WIP 制限が、互いに無関係な機能領域まで直列化する一方で、共通 module 経由の semantic conflict(衝突の型 2・型 3)を防げていなかったため、並行して安全な範囲を判定するための材料を正本化した。領域 D1〜D9 と SHARED 層、機能 F-01〜F-45、共通部品 S-01〜S-16 を、呼び出し元の実測に基づいて定義している。**実行単位(Lambda)・ディレクトリを領域の境界にしない**(1 つの handler が 6 領域のサービスを呼ぶ実測があるため)。株主優待は「登録・取り込み側(D6)」と「判定利用側(S-15)」で性質が割れるため独立領域にしない。D2 SELL と D3 HOLDING は config・永続契約が分かれているため分離する(Human 承認 H1)。あわせて新規機能・新規領域・廃止時のカタログ維持契約と、陳腐化防止の 3 段ゲートを定めた。**運用ルール本文は development_workflow.md 2.6節が正本であり本書へ複製していない。** 本書の作成時点で `DOMAIN_WIP_MODEL_ACTIVE = NO` であり、有効な WIP ルールは #122(担当者単位 code WIP = 1)のままである。判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない |
| 2026-09-06 | §0 を現在の発効状態へ同期した(Issue #184)。本書は作成時点で `DOMAIN_WIP_MODEL_ACTIVE = NO` と記していたが、2026-09-06 02:27 JST に人間の承認により領域ベース WIP が発効しており、記述が現況と矛盾していた。`CURRENT_WIP_RULE = DOMAIN_WIP_RULE_V1` / `EFFECTIVE_FROM` へ更新し、あわせて**静的な文書を変わりうる状態の唯一の根拠にしない**ことを明記した(発効状態は試行の結果として人間の判断で戻ることもありうるため、確認が必要な場合の参照先は development_workflow.md 2.6.10 の `ACTIVATION_STATE_SSOT` に従う)。**領域カタログ・機能一覧・共通部品一覧・維持契約の内容は変更していない。** コード・Production 挙動の変更なし |
| 2026-09-06 | 役割名の製品非依存化に伴う参照の更新(Issue #190)。本文中の "ChatGPT" 1 か所を「管理者」へ改めた。**領域カタログ・機能一覧・共通部品一覧・維持契約の内容は変更していない。** governance docs の改称は catalog の主要 path 欄に現れないため、F-45 を含む行の更新も不要である(D9 の主要 source は `.github/workflows/ci.yml` と `scripts/`)。変更履歴の過去エントリも書き換えていない。コード・Production 挙動の変更なし |
| 2026-09-06 | F-45(CI・品質ゲート)の主要 source へ `.github/workflows/pii-metadata-audit.yml` を追加(Issue #131)。公開される GitHub metadata(Issue / PR の本文・タイトル、コメント、label、branch 名)の PII 監査を、既存の required job へ GitHub API 依存を持ち込まないため `ci.yml` とは別 workflow として追加したことによる。M.1(新規機能の追加時に主要 source を更新する維持契約)に基づく更新であり、**領域カタログ・機能一覧・共通部品一覧・維持契約の内容は変更していない**(新機能ではなく既存 F-45 の source 追加であるため F 番号は増やしていない)。commit message 走査(`scripts/scan_commit_messages_pii.py`)と `ci.yml` の `pii-scan-commit-messages` job は既存の主要 source(`scripts/` と `ci.yml`)に含まれるため行の更新を要しない。判定ロジック・通知内容・保存データ形式・Production 挙動の変更なし |
| 2026-09-06 | 永続化ストア層を SHARED 部品 S-17 として追加(Issue #201)。`collection_store.py` / `json_store.py` / `dynamodb_store.py` は全 9 領域の repository が経由するにもかかわらず、本書に 1 度も現れていなかった(実測 0 件)。`domain/entities/base.py` は S-16 に登録されているのに、その 1 段下で実際に読み書きを担う層がカタログに無い状態であり、**L 節の手順(参照元を本書の表で引く)が成立しなかった**。判定できない場合は fail-closed で `LOCK_LEVEL_3` となるため、この層を変更する Issue #63(永続データの耐障害性 / P1)が lock 対象をカタログから導出できずにいた。呼び出し元を実測(`build_collection_store()` 78 箇所 / 39 module / 28 collection)し、lock する領域を全領域と定めたうえで、S-16(何を検証するか)と S-17(いつ・どの単位で検証するか)の境界、および docs 変更は D9 に閉じることを明記した。M.1(共通部品を追加・変更した場合はカタログを更新する維持契約)に基づく追記である。**領域一覧・機能一覧・既存の S-01〜S-16 の行・維持契約の内容は変更していない。** lock ルール本文は development_workflow.md 2.6節が正本であり本書へ複製していない。判定ロジック・通知内容・保存データ形式・Production 挙動の変更なし |
| 2026-09-06 | S-17(永続化ストア層)の主要 source へ `infrastructure/record_failure_policy.py` を追加(Issue #63 / A-U1a)。per-record のデコード失敗をコレクション単位のポリシー(STRICT 既定 / LENIENT / FAIL_SAFE_SUPPRESS)で扱う機構を新規 module として追加したことによる。M.1(共通部品を追加・変更した場合はカタログを更新する維持契約)に基づく更新である。**本 module は追加のみであり、既存の呼び出し元を 1 つも変更していない**(`src/` 内で本 module を import する module は実測 0 件)ため `LOCK_LEVEL_1`(ADDITIVE_AND_BACKWARD_COMPATIBLE)として領域 WIP を取得せずに実施した(2.6.5「新しい関数・モジュールの追加 — 既存の呼び出し元をひとつも変更しない場合に限り LOCK_LEVEL_1」)。`json_store.py` / `dynamodb_store.py` / `collection_store.py` を本機構へ差し替えるのは PR-2(A-U1b)であり、その時点で `LOCK_LEVEL_2` として全領域を取得する。**領域一覧・機能一覧・既存の S-01〜S-16 の行・S-17 の lock する領域(全領域)・維持契約の内容は変更していない。** 判定ロジック・通知内容・保存データ形式・Production 挙動の変更なし |
| 2026-09-06 | 機能一覧へ F-46(保有監視日次バッチ)と F-47(batch finalize recovery)を追加(Issue #209)。`lambda_handlers` 配下 16 ファイルを全件走査したところ、`holdings_watchlist_handler.py` と `_finalize_recovery.py` の 2 件が F 行に無く、**L 節の手順(参照元がどの機能に属するかを本書の表で引く)が成立しない**状態だった。前者は B 節が「1 つの handler が 6 領域のサービスを呼ぶ」実例として名指ししているファイルでありながら行が無く、後者は本書に 1 度も現れていなかった。判定できない場合は fail-closed で `LOCK_LEVEL_3` となるため、この 2 ファイルを変更する Issue #70(execution context の伝播と fail-close 統一)が影響領域をカタログから導出できずにいた。呼び出し先を実測し、F-46 は `D3 / D1 / D2 / D4 / D5 / D9 / S`(保有台帳 D6 は**読み取りのみで書き込まないため含めない**。F-10 が holdings を読みながら D3 / S に留めているのと同じ扱い。株主優待は判定利用側のため S-15 として数える)、F-47 は `D9 / D4 / D1 / D3`(生産側 = reconciler / 消費側 = buy・holdings の両 handler)とした。F-47 の PRIMARY を D9 としたのは `_execution_mode.py` / `_scheduling.py`(F-42)と同じ実行基盤側の共通部品であるためである。あわせて E-L 節の「全 45 機能」を「全 47 機能」へ更新した(行追加により本文と表が食い違うため)。M.1(新しい機能を追加したときのカタログ維持契約)に基づく追記であり、**領域一覧・既存の F-01〜F-45 の行・K 節の共通部品一覧・維持契約の内容は変更していない。** lock ルール本文は development_workflow.md 2.6節が正本であり本書へ複製していない。判定ロジック・通知内容・保存データ形式・Production 挙動の変更なし |
| 2026-09-07 | 網羅性の不変条件(M.5)・「主要 source」列の意味・UNCATALOGED 一覧を追加した(Issue #212 Phase B)。本書は機能の列挙(top-down)で作られ主要 source は代表ファイルのみだったが、L節は「変更する module を表から引く」bottom-up の使い方を要求しており、**作り方と使い方が噛み合っていなかった**。表に無い module に当たると fail-closed で `LOCK_LEVEL_3` となり作業が止まる(#201 の永続化ストア層 / #209 の handler / #135 の owner.py で実際に発生)。維持契約 M.1〜M.4 はいずれもイベント駆動(機能を足したら行を足す)であり、**初期作成時の抜けを検出する不変条件が無かった**。M.5 として「src 配下の全 module は F 行 / S 行の主要 source に属する」を定め、属さないものは UNCATALOGED 一覧へ割り当て予定つきで載せることとした。**属し方はファイル指定とディレクトリ指定の 2 通りをどちらも有効とする**(M.5 C2)。L節の目的は lock 範囲を引くことであり、`domain/valuation/` のようなディレクトリ指定でも配下の全 module についてその目的を果たすためである。この扱いにより baseline は 183 件ではなく **102 件**になる(Phase A の実測 183 はディレクトリ指定を数えていなかった。差の 75 件は既に覆われている)。あわせて本 PR で 6 件を割り当てまで済ませた: `services/stock_snapshot_service.py` を F-14 へ(#208 で判明した保有側の適正価格集約)、`services/watchlist_screening_audit.py` を F-15 へ / `services/watchlist_batch_finalizer.py` を F-47 へ(いずれも #62 で判明)、`services/holding_decision_notification_builder.py` を F-22 へ、`domain/entities/owner.py` を新規 S-18(所有者と holding_id 規約 / lock D3 D5 D6 / src 内 importer 15 module)へ、`domain/signals/_price_range_shared.py` を新規 S-19(価格レンジ共通 / lock D1 D2 / F-03 と F-09 の両方が使う)へ。S-18 は #135(ログへの個人識別情報)の実装が lock 範囲を引けずにいた直接の原因である。M.1 N4(2 領域以上が読む共通部品は K節へ)に基づく追加であり、**領域一覧・既存の F 行 / S-01〜S-17 の lock する領域・維持契約 M.1〜M.4 の内容はいずれも変更していない**(F-14 / F-15 / F-22 / F-47 は主要 source へ追記したのみで影響領域を変えていない)。UNCATALOGED 一覧は Phase D の作業表を兼ね、割り当てが済んだ行を削り、空になった時点で C1 が成立する。CI(`catalog-coverage`)による機械検査は Phase C であり本 PR には含まない。**docs のみの変更であり、判定ロジック・通知内容・保存データ形式・Production 挙動はいずれも変更していない** |
| 2026-09-08 | S-17(永続化ストア層)の読み取り API へ `find_with_outcome()` を 1 つ追加した(Issue #279)。既存の読み取り API(list_all / iter_all / get / get_consistent / find / query_by_index / get_many / get_raw_data)はいずれも `list[T]` / `T | None` を返すため、**`RecordFailurePolicy.FAIL_SAFE_SUPPRESS` の核心である「判定不能」を呼び出し側へ渡す口がどこにも無かった**。notification_log の再送判定では、skip すると過去の送信実績を見落として重複送信になり、例外にすると通知が出せなくなるため、どちらでもない第三の答え(判定できなかった)を返せる必要がある。実装は既存の `decode_records()` を経由し、失敗の記録・開示レベルの適用・走査単位の集計ログはすべて既存の機構と同一である。**既存 API の signature も挙動も 1 つも変えていない**(宣言していない collection は既定の STRICT のままで、本メソッドを呼ばない限り何も変わらない)。領域一覧・機能一覧・S-01〜S-19 の lock する領域・維持契約 M.1〜M.5 はいずれも変更していない |
| 2026-09-08 | F-16(分散実行)の主要 source へ `lambda_handlers/_watchlist_execution_mode.py` を追加した(Issue #286)。watchlist 系 4 handler が `execution_mode` / `notification_mode` を黙殺していた問題(#70 F-B4)の是正で追加した private module であり、指定を検出して**明示的に例外で止める**(対応はしない)。M.5 C1(src 配下の全 module は F 行 / S 行の主要 source に属する)を満たすための追記である。★ **共通部品(S 行)としては登録していない。** import しているのは F-16 の 4 handler だけであり、2 領域以上が読む共通部品(M.1 N4)に当たらないためである。★ 既存の実行文脈の共通部品 `lambda_handlers/_execution_mode.py`(F-42 / 全領域)は**1 行も変更していない**。あちらへ手を入れると lock が全領域へ広がるうえ、watchlist だけの「対応しない」という判断を共通部品へ持ち込むことになるため、F-16 側の private module として分けた。**領域一覧・既存の F 行 / S 行の影響領域・維持契約はいずれも変更していない。**判定ロジック・通知内容・保存データ形式の変更なし |
| 2026-09-09 | F-11(投資仮説の管理と採点)の主要 source へ `cli/baseline_repair.py` を追加した(Issue #272)。baseline pointer の不整合を検出・修復する運用 CLI であり、**共通部品(K 節)の追加・変更は無い**。影響領域は F-11 のまま D3。判定ロジック・通知内容・保存データ形式・Production 挙動の変更なし |
| 2026-09-09 | 実測に合わせて 3 行を是正した(Issue #212)。★ **S-05(バリュエーション)の影響領域を D1 / D2 / D4 -> D1 / D2 / D4 / D5 / D6 / D7 へ拡張**した。`domain.valuation` を import する src は実測 19 ファイルで、カタログが挙げていた 5 件のほかに stock_analysis_view_service(F-05) / watchlist_judgment_summary_formatter(F-20) / shareholder_benefit_registry_service(F-29) / valuation_shadow_analysis(F-33) が消費しており、狭い記載は **lock 漏れを生む**(カタログは L 節の fail-closed な入力である)。★ **過去の lock 宣言は遡及しない**(#221 / #253 / #62 はいずれも「S-05 は参照するだけで変更しない」と宣言しており lock 不要であった。誤りではない)。★ **S-09(スコアリング基盤)の構成列へ 5 ファイルを追加**した(risk_deduction_scoring / momentum / timing_score の signals・entities)。ファイル索引はこれらを S-09 としていたのに構成列が取り込んでおらず、**2 つの入口が違う答えを返す**状態だった。★ **F-05 の名称を「買い候補・保有銘柄の表示整形」へ改め、影響領域へ D3 を追加**した(stock_analysis_view_service.py:1099 の build_holding_analysis_text を conversation_service.py:321/329 が呼んでおり保有側の経路が実在するが、機能名からその行へ辿り着けなかった)。**領域一覧・既存の F 行/S 行のその他・K 節の共通部品一覧・維持契約の内容は変更していない。** stock_analysis_view_service.py がファイル索引に行を持たない件は本改訂では扱わず #212 本体(catalog-coverage)の対象とする。docs のみの変更であり、コード・Production 挙動の変更なし |
| 2026-09-09 | **S-09（スコアリング基盤）の `domain/signals/risk_deduction_scoring.py` の coverage 算出を変更した**（Issue #269）。同 module は `coverage_ratio=1.0` を**無条件に返して**おり、「評価できなかった（NOT_EVALUATED）」を「該当しない」と同じ扱いで捨てていた。企業品質・投資ストーリー維持と**同じ形**（evaluated_weight / available_weight。NOT_EVALUATED は分母に残り分子から外れる）へ揃え、`RiskDeductionCategoryDetail.status` も実態を書くようにした。★ **S-09 の主要 source・lock する領域（D1 / D3 / D4）・維持契約はいずれも変更していない**（構成ファイルの追加・削除は無く、既存 1 ファイルの内部実装のみ）。★ enum 値の追加も無い（`EvidenceCoverageStatus` の既存 3 値の範囲内）。M.1（共通部品を変更した場合の維持契約）に基づく記録である。判定ロジック（final_score / category）・通知内容・保存データ形式は変更していない（変わるのは coverage と、それに依存する confidence の値のみ） |
| 2026-09-12 | F-45(CI・品質ゲート)の主要 source へ `docs/policy_registry.yaml` を追加した(Issue #337)。操作 -> 読むべき正本の節の索引であり、`scripts/policy_check.py` が読む。M.1 の維持契約に基づく source 追加であり、★ **新機能ではないため F 番号を増やしていない**(2026-09-06 に `pii-metadata-audit.yml` を追加したときと同じ扱い)。★ **registry は規則本文を持たず pointer だけを持つ**ため、本書と同じく「ルール本文を複製しない」性格の文書である。**領域カタログ・機能一覧・共通部品カタログ・維持契約の内容は変更していない。** 判定ロジック・通知内容・保存データ形式・Production 挙動の変更なし |
| 2026-09-14 | **S-13(設定ロードとスキーマ)の `config/models.py` へ field を 1 つ追加した**(Issue #234)。`WatchlistScreeningRulesConfig.universe_failure_notification_enabled`(既定 True)であり、候補一覧の取得に失敗した日の要約通知だけを対象とする kill switch である。既存の `notification_enabled` が「追加を知らせない」と「取得の失敗を知らせない」を兼ねており、**知らせたい日ほど届かない**状態になっていたため分けた。★ **S-13 の主要 source・lock する領域(全領域)・維持契約はいずれも変更していない**(構成ファイルの追加・削除は無く、既存 1 ファイルへの field 追加のみ)。★ **共通部品の追加も F 番号の追加も無い**(新機能ではなく既存 F-15 / F-47 の送信可否の判定を 1 箇所変えたもの)。★ 既定値つきの追加であり `EXISTING_SERIALIZATION_COMPATIBLE` / `EXISTING_VALIDATION_COMPATIBLE` は満たすが、★ **`EXISTING_CONSUMER_BEHAVIOR_UNCHANGED = NO`**(失敗日の outcome が SKIPPED -> SENT へ変わる)であるため `LOCK_LEVEL_1` を主張せず、`LOCK_LEVEL_2` として S-13 の lock する領域(全領域)を取得して実施した。M.1(共通部品を変更した場合の維持契約)に基づく記録である。**領域一覧・機能一覧・既存の S 行・維持契約の内容は変更していない。** 判定ロジック(スコア計算・合否基準)・通知の文面・保存データ形式は変更しておらず、変わるのは「送ってよいか」の判定だけである |
| 2026-09-18 | **新規共通部品 S-20(観測・Shadow計測の隔離)を追加した**(Issue #384、PR-1)。`services/buy_signal_service.py`の private helper `_isolated_shadow_observation()`(Issue #22 C2で新設)を、`domain/shadow_observation.py`の`isolated_shadow_observation()`として振る舞い変更なしのrefactorで抽出した(既存テスト51件+新規5件で固定。buy_signal_service.py側の呼び出し箇所・出力値はいずれも不変)。同型の隔離未実施箇所が`sell_signal_service.py` / `profit_taking_service.py` / `stock_snapshot_service.py` / `holdings_watchlist_handler.py`にも見つかっている(Issue #384本体、#371 FINDING F1の残り)ため、私物化(buy_signal_service.py固有)のままでは4ファイル分のヘルパー複製が生じることから、USER承認(SHARED_COMPONENT_CREATION_APPROVED=YES)を得て共有部品として抽出した。★ **既存のS-02(判定スナップショット)・S-05(バリュエーション)への統合はしていない**(USER判断。責務混在を避けるため独立entryとした)。lock する領域はPR-1〜4で対象となる5ファイルの領域を本書のF行(F-02/F-06/F-07/F-14/F-46)からunionして導出し、D1 / D2 / D3 / D4 / D5 / D6 / D9とした(★ 初版はholdings_watchlist_handler.pyのF-46タグ全体[D3 / D1 / D2 / D4 / D5 / D9 / S]のうちD4・D5を取りこぼしており、レビュー指摘により訂正した。取りこぼしの間、本PRのcode WIP宣言自体はD1のみを取得しており実害は無い)。**領域一覧・機能一覧・既存のF行・S-01〜S-19の内容は変更していない。** 判定ロジック・通知内容・保存データ形式・Production挙動の変更なし(PR-2〜4での他ファイルへの適用は別途記録する) |
| 2026-09-18 | S-20(観測・Shadow計測の隔離)の構成へ`sell_signal_service.py` / `profit_taking_service.py`を追加した(Issue #384、PR-2、PR #398でmerge済み)。両ファイルのShadow計測(それぞれ9個の`*_metrics`算出)を、PR-1で抽出済みの`isolated_shadow_observation()`で隔離した。lock する領域はPR-1決定時点の`D1 / D2 / D3 / D4 / D5 / D6 / D9`のunionに既に収まっており拡張していない。レビュー(サブちゃん)で、SELL側の例外注入テストが実際には到達しない5ファイルの通常fixtureのみで検証されており、`SellSignalService`を使う全15ファイルで見ると32件の既存テストが同経路へ到達することが指摘され、`test_issue_21_sell_fair_value_usability_snapshot.py`の`_canned_sell_result()`パターンを使って再現性のある到達を確認したうえで固定した(反映済み)。**領域一覧・機能一覧・既存のF行・S-01〜S-19・S-20の主要 source 列以外は変更していない。** 判定ロジック・閾値・保存スキーマ・正常系の通知内容は変更していない。一方で、Shadow計測の想定外例外時については、当該サービスの判定処理全体を失敗させる挙動から、当該Shadow結果のみ`COMPUTATION_FAILED`として記録し後続処理を継続する挙動へ意図的に変更している(PR-3のstock_snapshot_service.pyと同型。#384の目的そのものであり、正常系の投資判断変更ではない) |
| 2026-09-18 | S-20(観測・Shadow計測の隔離)の構成へ`stock_snapshot_service.py`を追加した(Issue #384、PR-3)。`build_stock_snapshot()`内の8箇所の`evaluate_*()`呼び出し(historical_valuation / timing / earnings_surprise / earnings_trend / entry_price_range / market_environment / sector_environment / environment)を隔離した。これらはPR-1/PR-2と異なりdictではなくdomain object(Result型)を返すため、既存の`isolated_shadow_observation()`(dict専用契約)を流用できず、**新規に`isolated_shadow_computation[T](observation_name, build, on_failure)`を`domain/shadow_observation.py`へ追加した**(PEP 695 generic。既存の`isolated_shadow_observation()`の実装・契約は変更しない、追加のみ)。fallbackは各Result型が既に持つ`NOT_EVALUATED` state + `reason_codes`へ`SHADOW_COMPUTATION_FAILED:<例外型名>`をタグ付けする形で構築し、0.0/空値への偽装を避けた(8型すべての必須fieldをclass定義から実測し、`model_version`は各`config.<subconfig>.model_version`から取得。`entry_price_range`のみ`current_price`も必須のため追加供給)。★ **新規関数の追加であり既存の呼び出し元を1つも変更していないため`LOCK_LEVEL_1`(ADDITIVE_AND_BACKWARD_COMPATIBLE)として実施した**(`domain/shadow_observation.py`のimporterは実測3ファイルのみで、いずれも本PRでは変更しない)。lock する領域はPR-1決定時点の`D1 / D2 / D3 / D4 / D5 / D6 / D9`のunionに既に収まっており拡張していない。8箇所すべてへの例外注入で`build_stock_snapshot()`自体が失敗しないことと、隔離が算出ごとに独立していることを新規テスト2件で固定し、mutation testing(隔離前の実装へ戻して新規テストが実際に落ちることを確認後、復元)で検証済み。**領域一覧・機能一覧・既存のF行・S-01〜S-19・S-20の主要 source 列以外は変更していない。** 判定ロジック・閾値・保存スキーマ・正常系の通知内容は変更していない。一方で、Shadow計測の想定外例外時については、`build_stock_snapshot()`全体を失敗させる挙動から、当該Shadow結果のみ`NOT_EVALUATED`として記録し、後続処理を継続する縮退継続(degraded continuation。失敗はwarning log/state/reason_codesで可視のまま残るため、development_workflow.md DoD項目5が禁じるfail-open[失敗が見えなくなること]ではない)へ意図的に変更している。これはIssue #384の目的そのものであり、正常系の投資判断変更ではない(具体的には、変更前は当該銘柄1件のRecommendation全体が失敗扱いになり得たが、変更後は実際の投資判断は失われず継続する) |
| 2026-09-18 | S-20(観測・Shadow計測の隔離)の構成へ`holdings_watchlist_handler.py`を追加した(Issue #384、PR-4。対象1箇所を統合。同型sweepで`holding_decision_notification_builder.py`の未隔離Shadow整形が追加で確認されたため、#384は後続PR-5へ継続する)。`_notify_holding_decision_and_build_result()`内のinline呼び出し`evaluate_exit_price_range()`(戻り値`ExitPriceRangeResult`、dict以外のdomain object)を、PR-3で追加済みの`isolated_shadow_computation[T]`で隔離した。**新規helperの追加は不要**(`domain/shadow_observation.py`への変更なし。既存4 consumerに次ぐ5番目の利用のみ)。fallbackはPR-3のentry_price_rangeと同一パターン(`NOT_EVALUATED` state + `SHADOW_COMPUTATION_FAILED:<例外型名>`のreason_codesタグ)。lock する領域は`functional_domains.md`のF-46タグ全体(D1/D2/D3/D4/D5/D9。D6は読み取り専用のためF-46自体が除外)を取得し、PR-1決定時点のS-20 unionに収まっている。正常系(should_notify判定・Recommendation内容)は変更していない。一方で、Shadow計測の想定外例外時については、当該holding 1件のHoldingDecision通知全体が失われ得た挙動から、当該Shadow結果のみ`NOT_EVALUATED`として記録し後続処理(通知・Recommendation保存)を継続する縮退継続(degraded continuation。失敗はwarning log/state/reason_codesで可視のまま残るため、development_workflow.md DoD項目5が禁じるfail-open[失敗が見えなくなること]ではない)へ意図的に変更している。新規テスト(`test_shadow_exit_price_range_failure_does_not_break_holding_decision_notification`)で固定し、mutation testing(隔離前の実装へ戻して新規テストが実際に落ちることを確認後、復元)で検証済み。**領域一覧・機能一覧・既存のF行・S-01〜S-19・S-20の主要 source 列以外は変更していない。** 判定ロジック・閾値・保存スキーマ・正常系の通知内容は変更していない |
| 2026-09-18 | S-20(観測・Shadow計測の隔離)の構成へ`holding_decision_notification_builder.py`を追加した(Issue #384、PR-5)。`build_holding_decision_recommendation()`内、`Recommendation(...)`構築のinline引数として置かれていた9箇所の`*_to_metrics()`呼び出し(historical_valuation / timing_score / earnings_surprise / earnings_trend / entry_price_range / exit_price_range / market_environment / sector_environment / environment)を隔離した。9関数はいずれも`dict[str, object]`を返すため、PR-1で抽出済みの`isolated_shadow_observation()`(dict専用契約)をそのまま利用できた。**新規helperの追加は不要**(`domain/shadow_observation.py`への変更なし)。命名・fallback形式は、PR-2(`sell_signal_service.py`/`profit_taking_service.py`)が同一9関数へ既に適用済みのパターンをそのまま踏襲した(fallbackは`isolated_shadow_observation()`の既存固定形`{"shadow_state": "COMPUTATION_FAILED", "error_type": <例外型名>}`)。lock する領域は`functional_domains.md`のF-22タグ(D5)のみ取得し、PR-1決定時点のS-20 unionに収まっている。到達性は呼び出し元(`holdings_watchlist_handler.py`のみ)・到達testファイル(5ファイル・計142件)を全数grepで確認したうえで着手した。正常系(v1判定・Recommendation内容)は変更していない。異常系は、9箇所いずれかの例外で`build_holding_decision_recommendation()`全体が失敗しHoldingDecision通知が失われ得た挙動から、当該metricsのみ`COMPUTATION_FAILED`として記録し後続処理を継続する縮退継続(degraded continuation。development_workflow.md DoD項目5が禁じるfail-openではない)へ意図的に変更している。新規テスト2件(9箇所同時失敗/1箇所ずつの隔離独立性)で固定し、mutation testing(隔離前の実装へ戻して新規テストが実際に落ちることを確認後、復元)で検証済み。★ **訂正(PR #401レビュー、サブちゃんFINAL VERDICT対応)**: 当初ここに「`src/`全体を`Shadow計測`/`DecisionSnapshot記録専用`でgrepし、算出関数自身のコメント以外に未隔離のinline呼び出しが残っていないことを確認したうえで、Issue #384を完了とする」と記載していたが誤りだった。ファイル単位で「PR-1/PR-2で既に対応済み」と判断し、ファイル内の個別call siteをASTレベルで再確認していなかったため、`buy_signal_service.py`(8箇所)・`sell_signal_service.py`(1箇所)・`profit_taking_service.py`(1箇所)に同型の未隔離Shadow整形が計10箇所残っていることを見落としていた(サブちゃんの指摘を実装コードで独立に確認済み)。**Issue #384は本PRでは完了しない**(#384延長でのPR-6化等はUSER判断待ち)。**領域一覧・機能一覧・既存のF行・S-01〜S-19・S-20の主要 source 列以外は変更していない。** 判定ロジック・閾値・保存スキーマ・正常系の通知内容は変更していない |
| 2026-09-18 | F-13(取引停止・クールダウン)のDynamoDB Table列へ`TradeEventRecordsTable`を追加した(Issue #71 F-C11 Phase 1)。`trade_cooldown_service.py`が検知した売買イベント(TradeEvent)を、`HoldingsSnapshotEntry`更新より前に耐久性のある形で記録する新規collection(sparse GSI `pending-marker-index`を持つ)。lock する領域はF-13の既存タグ(D3 / D1 / D2)のまま変更していない(新規repository・エンティティの追加のみで、既存の呼び出し元[buy_candidates_handler.py/holdings_watchlist_handler.py]は1つも変更していないため)。**領域一覧・機能一覧・既存のF行(F-13のTable列以外)・共通部品カタログは変更していない。** 判定ロジック・通知内容・保存データ形式(既存collection)の変更なし。pending-event consumption(WatchState終了)はPhase 2のスコープであり本変更には含まない |
| 2026-09-18 | S-20(観測・Shadow計測の隔離)の構成へ`buy_signal_service.py`を追加した(Issue #384、PR-6)。`analyze()`内、`Recommendation(...)`構築のinline引数として置かれていた8箇所の`*_to_metrics()`呼び出し(historical_valuation / timing_score / earnings_surprise / earnings_trend / entry_price_range / market_environment / sector_environment / environment)を隔離した。8関数はいずれも`dict[str, object]`を返すため、PR-1で本ファイル自身が抽出した`isolated_shadow_observation()`(dict専用契約。既存3箇所[common_quality_shadow等]で利用中)をそのまま利用できた。**新規helperの追加は不要**(`domain/shadow_observation.py`への変更なし)。サブちゃんのPR #401レビュー(FINAL VERDICT)で実測された箇所と一致していることを実装コードで再確認したうえで着手した。lock する領域は`functional_domains.md`のF-02タグ(D1)のみ取得し、PR-1決定時点のS-20 unionに収まっている。到達性は本ファイルを参照する全12テストファイル(計397件、新規2件を含め399件)を全数で回帰確認した。正常系(v1判定・buy_action・company_quality_score)は変更していない。異常系は、8箇所いずれかの例外で`analyze()`全体が失敗し当該銘柄のBUY判定結果全体が失われ得た挙動から、当該metricsのみ`COMPUTATION_FAILED`として記録し後続処理を継続する縮退継続(degraded continuation。development_workflow.md DoD項目5が禁じるfail-openではない)へ意図的に変更している。新規テスト2件(8箇所同時失敗/1箇所ずつの隔離独立性)で固定し、mutation testing(隔離前の実装へ戻して新規テストが実際に落ちることを確認後、復元)で検証済み。残るsell_signal_service.py/profit_taking_service.py各1箇所(evaluate_exit_price_range)はPR-7で対応予定であり、#384は継続する。**領域一覧・機能一覧・既存のF行・S-01〜S-19・S-20の主要 source 列以外は変更していない。** 判定ロジック・閾値・保存スキーマ・正常系の通知内容は変更していない |
| 2026-09-18 | S-20(観測・Shadow計測の隔離)の構成へ`sell_signal_service.py`/`profit_taking_service.py`のexit_price_range算出自体を追加した(Issue #384、PR-7)。両ファイルとも、`evaluate_exit_price_range()`のinline呼び出し(既存の`exit_price_range_metrics`の入力にもなっている)を`isolated_shadow_computation()`(PR-3で追加、dict以外のdomain objectを返す算出用)で隔離した。返り値`ExitPriceRangeResult`はdict以外のdomain objectのため、dict専用の`isolated_shadow_observation()`ではなくこちらを使用(stock_snapshot_service.pyのEntryPriceRangeResult隔離と同型パターン)。fallbackは`state=NOT_EVALUATED`・5価格すべてNone・`reason_codes`へ`SHADOW_COMPUTATION_FAILED:<例外型名>`を積む形とし、既存のstate=NOT_EVALUATED(業務上の未評価)と同じ不変条件を満たす。★ **訂正**: 従来「exit_price_range自体はsell_prices/買付価格帯の実判定に使われるため対象外」としていたコメントは誤りだった。実装コードを実測したところ、両ファイルとも`sell_prices`(profit_taking_service.pyは`effective_sell_prices`)はexit_price_range算出より前に確定済みであり、代入順序上exit_price_rangeへ依存しない(USER決定、Issue #384のコメント参照)。lock する領域は`functional_domains.md`のF-06/F-07タグ(D2)のみ取得し、PR-1決定時点のS-20 unionに収まっている。到達性は両サービスを参照する全15テストファイル(計456件、新規2件を含む)を全数で回帰確認した。正常系(recommendation_type・sell_prices等のv1判定)は変更していない。異常系は、exit_price_range算出の例外で判定処理全体が失敗しRecommendation全体が失われ得た挙動から、`exit_price_range_state`等のフィールドのみNOT_EVALUATEDとして記録し後続処理(下流のexit_price_range_metrics算出含む)を継続する縮退継続(degraded continuation。development_workflow.md DoD項目5が禁じるfail-openではない)へ意図的に変更している。新規テスト2件(sell_signal_service.py/profit_taking_service.py各1件)で固定し、mutation testing(隔離前の実装へ戻して新規テストが実際に落ちることを確認後、復元。両ファイルで実施)で検証済み。これにより#384のS-20統合対象10箇所(PR-6の8箇所+本PRの2箇所)はすべて隔離済みとなったが、**#384自体は完了としない**(USER決定により、PR-7完了後にファイル単位ではなくcall-site単位での全src再sweepが必須)。**領域一覧・機能一覧・既存のF行・S-01〜S-19・S-20の主要 source 列以外は変更していない。** 判定ロジック・閾値・保存スキーマ・正常系の通知内容は変更していない |
| 2026-09-18 | S-20(観測・Shadow計測の隔離)の構成へ`stock_snapshot_service.py`のEarnings Surprise/Trend Score(Phase C)前提解決4箇所を追加した(Issue #384、PR-8)。サブちゃんのPR #406レビュー(F1節)が、`*_to_metrics()`/`evaluate_*()`という名前ではないため従来の名前軸sweep(grep)では見つからない、Shadow計測専用の未隔離input(`resolved_period` `release_confirmation_state` `decision_relevance` `earnings_surprise_history`)を「コメント軸」(「Shadow計測」直後の呼び出しを走査)で発見した。4箇所とも出力先はearnings_surprise/earnings_trend(既存のisolated_shadow_computation)のみであることを全数grepで確認済み。`resolve_latest_financial_period_end()`/`resolve_earnings_release_confirmation()`/`resolve_earnings_decision_relevance()`は`isolated_shadow_computation()`で個別に隔離し、fallbackはそれぞれ`ResolvedFinancialPeriodEnd(period_end=None, source=FinancialPeriodEndSource.UNAVAILABLE)`・`EarningsReleaseConfirmationState.NOT_APPLICABLE`(phase_c_earnings_blockedをFalse側へ倒す安全側の値)・`EarningsDecisionRelevance.UNKNOWN`(既存の業務上の値)とした。`earnings_surprise_history`(外部I/O、`providers.financial_data.get_earnings_surprise_history()`)も同様に隔離し、fallbackは既存の「phase_c_earnings_blocked時」と同じ空list(`[]`)とした。サブちゃんの反証(release_confirmation_state例外→175 failed、外部I/O例外→160 failed、いずれもBUY/SELL/ProfitTaking/builderの全経路)に対する固定。lock する領域は`functional_domains.md`のF-14タグ(D3/D6)を取得し、PR-1決定時点のS-20 unionに収まっている。到達性は`build_stock_snapshot()`を参照する全18テストファイル(計537件passed、5件skipped)を全数で回帰確認した。正常系(v1判定)は変更していない。異常系は、4箇所いずれかの例外で`build_stock_snapshot()`全体が失敗し当該銘柄のBUY/SELL/ProfitTaking判定結果全体が失われ得た挙動から、後続処理(下流のearnings_surprise/earnings_trend算出含む)を継続する縮退継続(degraded continuation。development_workflow.md DoD項目5が禁じるfail-openではない)へ意図的に変更している。新規テスト2件(Phase C前提3箇所同時失敗/外部I/O単独失敗)で固定し、mutation testing(隔離前の実装へ戻して新規テストが実際に落ちることを確認後、復元)で検証済み。★ サブちゃんのレビューは、call-site単位の再sweepを名前軸だけで行うと本件のような形をまた見落とす、という方法論上の指摘もしている(#384のclose条件である再sweep実施時に踏まえる必要がある)。**領域一覧・機能一覧・既存のF行・S-01〜S-19・S-20の主要 source 列以外は変更していない。** 判定ロジック・閾値・保存スキーマ・正常系の通知内容は変更していない |
| 2026-09-18 | PR-8(上記)のサブちゃんのPR #408レビューF1/F2対応を追加した(Issue #384、同一PR)。★ **F1**: fallback値(NOT_APPLICABLE/UNKNOWN/空list等)は「正常時にも起こりうる値そのもの」であり、bare enum/dataclass/listはreason_codesを持たないため、失敗時のearnings_surprise/earnings_trendのstate/reason_codesが正常時と区別できなかった(特に外部I/O失敗時はANALYST_CONSENSUS_UNAVAILABLEという別の業務事実として記録されていた)。4箇所いずれかの失敗を`_phase_c_shadow_errors`へ記録し、下流のearnings_surprise/earnings_trend(PR-3から既にある`isolated_shadow_computation`)のbuild()内で該当する失敗を再送出することで、既存の`SHADOW_COMPUTATION_FAILED:<例外型名>`タグ付きon_failureをそのまま再利用する形とした(新しいtagging方式は導入していない)。★ **F2**: fallback値(NOT_APPLICABLE/UNKNOWN)そのものがテストで固定されていなかった(F1対応後もphase_c_earnings_blockedのgate判定には引き続き使われるため、外部I/O呼び出しの有無を通じて固定可能)。新規テスト2件(release_confirmation_state/decision_relevanceのfallback値をそれぞれ個別にmutationして固定)を追加し、mutation testing(値を「抑止する」側へ変異させて外部I/Oが呼ばれなくなることを確認後、復元)で検証済み。到達性は同じ全18テストファイル(計539件passed、5件skipped)を全数で回帰確認した。正常系は変更していない。**領域一覧・機能一覧・既存のF行・S-01〜S-19・S-20の主要 source 列以外は変更していない。** 判定ロジック・閾値・保存スキーマ・正常系の通知内容は変更していない |
| 2026-09-18 | **新規共通部品 S-21(LINE通知clientの実行時構築)を追加した**(Issue #117 Phase B1a)。`infrastructure/line/client.py`の`build_line_client_from_env()`(LINE_CHANNEL_ACCESS_TOKEN/LINE_USER_ID未設定時にConsoleLineClientへ黙ってフォールバックする関数。CLI 3箇所が想定利用者)は、Lambda実行で同じフォールバックが起きると通知未送信のままLambda呼び出しが正常終了して見える不可視の障害を生む(2026-09-01 Production露出事象の根本Issue #117)。★ **本PRは新規関数`build_live_line_client_from_env()`の追加のみであり、既存`build_line_client_from_env()`の関数本体は1行も変更していない**(git diffで実測)。既存9 handler/3 CLIは引き続き旧関数を呼び続けるため挙動不変であり、`LOCK_LEVEL_1`(ADDITIVE_AND_BACKWARD_COMPATIBLE)として実施した(新関数の既存呼び出し元は定義上0件)。★ 2026-09-07のUSER承認時点の共通部品名は「S-20」だったが、その後Issue #384が同番号を先に使用したため`S-21`へ付け替えた(MANAGER判断。承認の実体・スコープには影響しない)。★ 段階分割方針(MANAGER判断): 本PR(a)は新規関数の追加のみとし、既存9 handler(D1/D2/D3/D4/D5/D7/D8/D9)/3 CLIを1つずつ新関数へ切り替える作業は後続の小PR群(b)で行う(#384のPR-1〜7と同型の分割)。**領域一覧・機能一覧・既存のF行・S-01〜S-20の内容は変更していない。** 判定ロジック・通知内容・保存データ形式・Production挙動の変更なし(既存呼び出し元は1つも変更していない) |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)の消費側として`lambda_handlers/line_webhook_handler.py`(F-24 / D5)を新関数`build_live_line_client_from_env()`へ切り替えた(Issue #117 Phase B1b-1。MANAGER承認の段階分割(b)の1件目)。LINE_CHANNEL_ACCESS_TOKEN欠落時、従来はConsoleLineClient(標準出力のみ)へ黙って落ち、ユーザーへの返信が届かないのにLambdaは正常終了していた。切替後は`LineCredentialsMissingError`を伝播しLambda呼び出しが失敗する(Errorsメトリクスで検知可能)。既存のLINE_CHANNEL_SECRET/LINE_USER_ID未設定時の500返却は変更していない。lock領域はD5のみ(F-24)。`infrastructure/line/client.py`(S-21)は変更していない。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし(認証情報が正常に設定されている通常運用の挙動は不変) |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)の消費側として`lambda_handlers/watchlist_dispatcher_handler.py`(F-16 / D4・D9)を新関数`build_live_line_client_from_env()`へ切り替えた(Issue #117 Phase B1b-2。段階分割(b)の2件目)。★ 単純な関数名の置換ではない: 通知サービスの構築は従来、dispatch lease取得・BatchRuns行/進捗行作成の**後**にあり、strict版へ置換するとbatchがDISPATCHINGのままleaseを保持した中途状態でLambdaが失敗する。構築をlease取得より前(同ファイルの既存ゲートと同じ「開始前に中止する」位置)へ移し、認証情報欠落は状態作成前に失敗させる。また通知サービスを使うのはNEW_CANDIDATE_SCREENINGのfinalizeのみのため、構築はその場合に限り、maintenanceが不要な認証情報で新たに失敗しないようにした。lock領域はD4/D9(F-16)、対象は当該handler 1ファイルのみ。`infrastructure/line/client.py`(S-21)は変更していない。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし(認証情報が正常な通常運用の挙動は不変) |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)へ`build_line_client_for_run(*, dry_run: bool)`を追加した(Issue #117 Phase B1b-3a)。buy_candidates / holdings_watchlist / disclosure_check の3 handlerは実行モード(NORMAL / VALIDATION+SEND / DRY_RUN)を持つため、外部送信が起きない実行(DRY_RUN)は認証情報が無くても検証できる従来関数、外部送信が起きうる実行(NORMAL・VALIDATION+SEND)はstrict関数、と切り分ける必要がある。clientはdomainのExecutionContextをimportしないため、呼び出し側が`execution_context.is_dry_run`を渡す。★ **追加のみ**: 既存の`build_line_client_from_env()` / `build_live_line_client_from_env()`は1行も変更しておらず、新関数の呼び出し元は本PRで0件のため`LOCK_LEVEL_1`(D9のみ)として実施した。3 handlerの置換は後続PRで1本ずつ行う。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式・Production挙動の変更なし(呼び出し元が無いため) |
| 2026-09-19 | `HoldingEvaluationRecord`(F-14 / D3・D6)へ`profit_taking_audit_log_id`(optional、既定None)を追加し、`holdings_watchlist_handler.py`(F-46)の純粋HOLD経路で`pt_outcome.audit_id`を保存、`stock_analysis_view_service.py`(F-05)の「利確判定の状況」でその監査記録から比率(含み益率・適正価格との位置)を表示するようにした(理由リストは、最終HOLDでは常に空で、かつ金額を含み得るため表示しない。PR #414 F1/M2)(Issue #369、MANAGER判断で案(α)。従来のentity docstringの「エンジン別のaudit idは持たない」方針を、authoritativeでないエンジンのRecommendationを持たない実行結果の証跡に限り反転した。理由と範囲はdocstringへ記載)。PRIMARY D3 / LOCKED D1・D2・D3・D4・D5・D6・D9 / SHARED_TOUCHED なし。**領域一覧・機能一覧・既存のF行・S-01〜S-21は変更していない。** 永続entityへのoptional field追加のため、新fieldを持つrecordは旧版readerが読み飛ばす(TTL 90日)。判定ロジック・通知内容の変更なし |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)の消費側として`lambda_handlers/buy_candidates_handler.py`(F-01 / D1・D5・D9)を`build_line_client_for_run(dry_run=execution_context.is_dry_run)`へ切り替えた(Issue #117 Phase B1b-3b。段階分割(b)の3 handler切替の1本目)。外部送信が起きうる実行(NORMAL / VALIDATION+SEND)は認証情報欠落を`LineCredentialsMissingError`で顕在化させ、外部送信が起きない実行(DRY_RUN)は従来どおり認証情報なしで検証できる。構築位置はhandler()冒頭で、構築前にlease・保存等の状態変更が無い(位置の移動は不要)。lock領域はD1・D5・D9(F-01)、対象は当該handler 1ファイルのみ。`infrastructure/line/client.py`(S-21)は変更していない。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし(認証情報が正常な通常運用の挙動は不変) |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)の消費側として`lambda_handlers/holdings_watchlist_handler.py`(F-46 / D1・D2・D3・D4・D5・D9)を`build_line_client_for_run(dry_run=execution_context.is_dry_run)`へ切り替えた(Issue #117 Phase B1b-3c。3 handler切替の2本目、buy_candidatesと同型)。外部送信が起きうる実行(NORMAL / VALIDATION+SEND)は認証情報欠落を`LineCredentialsMissingError`で顕在化させ、外部送信が起きない実行(DRY_RUN)は従来どおり認証情報なしで検証できる。構築位置はhandler()冒頭で、構築前に状態変更が無い。子Lambdaへは親がexecution_mode/notification_modeを既に伝播しており、親子でモードが食い違わない。lock領域はF-46の領域タグ全体、対象は当該handler 1ファイルのみ。`infrastructure/line/client.py`(S-21)は変更していない。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし(認証情報が正常な通常運用の挙動は不変) |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)の消費側として`lambda_handlers/disclosure_check_handler.py`(F-37 / D5・D8)を`build_line_client_for_run(dry_run=execution_context.is_dry_run)`へ切り替えた(Issue #117 Phase B1b-3d。3 handler切替の3本目、buy_candidates・holdings_watchlistと同型)。外部送信が起きうる実行(NORMAL / VALIDATION+SEND)は認証情報欠落を`LineCredentialsMissingError`で顕在化させ、外部送信が起きない実行(DRY_RUN)は従来どおり認証情報なしで検証できる。構築位置はhandler()冒頭で、構築前に状態変更が無い(EDINET cacheの書込みは構築後のcheck_holdings内)。lock領域はD5・D8(F-37)、対象は当該handler 1ファイルのみ。`infrastructure/line/client.py`(S-21)は変更していない。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし(認証情報が正常な通常運用の挙動は不変) |
| 2026-09-19 | `profit_taking_service.py`(F-07 / D2)の利確判定の監査記録(`output_values`)へ、純粋HOLDの「利確を見送った根拠」の事実として`gain_watch_threshold_pct`/`upside_pct`/`independent_condition_count`/`fair_value_action_usable`/`fair_value_action_block_reason_code`/`fair_value_unusable_reason_code`を追加し、`stock_analysis_view_service.py`(F-05 / D1・D3・D5)の`_profit_taking_hold_audit_lines`が**許可リストのキーだけ**を読んで固定文言で表示するようにした(Issue #419、#369から分割)。値は比率・件数・bool・codeに限り、金額・株数を運ぶfieldを持たない。表示が読むキーを限ることで、「金額・数量を表示しない」を別moduleの不変条件へ依存させず表示側だけで保証する(PR #414 F1/M2の指摘への恒久的な対処)。`ProfitTakingResult`(domain)は変更していない(必要な事実は既存のresult/config/fv_rangeに揃っていたため)。PRIMARY D2 / LOCKED D1・D2・D3・D5 / SHARED_TOUCHED なし。**領域一覧・機能一覧・既存のF行・S-01〜S-21は変更していない。** 判定ロジック・閾値・通知内容の変更なし。`output_values`は自由dictで旧readerは新キーを無視する |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)の消費側として`lambda_handlers/watchlist_worker_handler.py`(F-16 / D4・D9)を、strict版`build_live_line_client_from_env()`へ切り替えた(Issue #117 Phase B1b-4a)。★ 単純な関数名の置換ではない: workerは1回の呼び出しで複数のjob_typeのSQSメッセージを処理しうるが、LINE通知サービスを使うのはNEW_CANDIDATE_SCREENINGのfinalizeだけである。そこで新設の`lambda_handlers/_watchlist_notification_prescan.py`でメッセージ本文を**状態変更(リース取得・完了記録)より前**に走査し、NEW_CANDIDATE_SCREENINGを含む場合だけstrictな構築を行う。認証情報欠落は状態変更前に`LineCredentialsMissingError`で失敗し(SQS再配信で中途状態を残さない)、通知に使わないWATCHLIST_MAINTENANCEの呼び出しは、不要な認証情報で新たに失敗しない。prescanは本処理と同じ`resolve_watchlist_job_type()`を使い、解釈できないメッセージは判定から外して例外を出さない(不正メッセージの扱いは従来どおり本処理が送出)。lock領域はD4/D9(F-16)、対象はworker 1 handlerと新設の走査モジュール(terminal_failureでも再利用予定)。`infrastructure/line/client.py`(S-21)は変更していない。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし(認証情報が正常な通常運用の挙動は不変) |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)の消費側として`lambda_handlers/watchlist_terminal_failure_handler.py`(F-16 / D4・D9)を、strict版`build_live_line_client_from_env()`へ切り替えた(Issue #117 Phase B1b-4b。workerと同じprescan方式)。認証情報欠落は**終端記録(状態変更)より前**に`LineCredentialsMissingError`で失敗する(記録後に失敗すると、再配信では「既に終端」となりfinalizeが呼ばれない中途状態になるため)。通知に使わないWATCHLIST_MAINTENANCEのみ・未知job_type(finalizeをskip)の呼び出しは、不要な認証情報で新たに失敗しない。★ job_type欠損時の既定は本処理と同じNEW_CANDIDATE_SCREENING(workerの既定=Noneと異なる)で、prescanへ引数として渡す。あわせて、#425のPhase 1条件F1・F2に対応し、prescanと本処理の判定の一致を退行防止のテストで固定した(F1: 既定値の取り違えで赤くなる、F2: 乖離時は通知欠落でなく`RuntimeError`)。lock領域はD4/D9(F-16)、対象はterminal failure 1 handlerとテスト。`infrastructure/line/client.py`(S-21)は変更していない。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし(認証情報が正常な通常運用の挙動は不変) |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)の消費側として`lambda_handlers/watchlist_batch_reconciler_handler.py`(F-16 / D4・D9)を、strict版`build_live_line_client_from_env()`へ切り替えた(Issue #117 Phase B1b-4c。案B、USER承認)。★ worker/terminal_failureのprescan方式とは異なる: reconcilerは複数の独立した回復処理を担う「最後の安全網」であり、LINE送信が必要なのはfinalizerのPhase 3(通知)の1点だけである。認証情報の欠落を通知サービスの**構築の失敗**として扱うと、その手前のPhase 1/2(ランキング確定・ウォッチリスト登録)や通知と無関係な回復処理まで止まり、24時間を超えるとTIMED_OUT(部分結果は登録しない)で候補が失われる。そこで欠落は**実際の送信時の失敗**として扱う: 認証情報が無ければ、送信メソッドが必ず`LineCredentialsMissingError`を送出する`_CredentialDeferredLineClient`(handler内に閉じる。決して成功を返さない)を通知サービスへ渡す。finalizerのPhase 3が例外を捕捉してNOTIFICATION_FAILEDとして記録し(ウォッチリスト登録は保持)、通知だけが既存のretry_notification()・COMPLETED_WITH_NOTIFICATION_FAILUREの仕組みに載る(finalizer・batch_tracker・line_notification_service・S-21は無変更)。Phase 3の例外捕捉によりLambdaが成功扱いで欠落が不可視にならないよう、「欠落のまま送信が試みられた」事実を保持し、全処理の完了後に`LineCredentialsMissingError`を送出する。認証情報欠落以外の例外は握りつぶさず、同一視もしない。★ 契約: credential復旧前に既存のretry上限(max_notification_retry_attempts)へ達した場合は自動通知されない(COMPLETED_WITH_NOTIFICATION_FAILURE。手動のretry-notificationが必要)。lock領域はD4/D9(F-16)、対象はreconciler 1 handlerとテスト。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし(認証情報が正常な通常運用の挙動は不変) |
| 2026-09-19 | F-45(CI・品質ゲート、`scripts/`)へ`scripts/human_gate_preflight.py`を追加した(Issue #332 Unit 1-B)。Human Gateの承認要求(APPROVAL_REQUEST)と受領証(RECEIPT)を、実行者がgated actionの直前に呼んで検査する**read-only**のpreflight checkerで、GitHubのGET(`gh api`)だけを行う。三値(PASS / FAIL / UNKNOWN)を厳格に区別し、UNKNOWNをPASSへ倒さない(exit code 0 / 1 / 3。2はargparse)。**PASSは承認の真正性の証拠ではない**(形式の検査のみ)。USER_DIRECT_TURN・EXPLICIT_APPROVAL_INTENTは機械では検査できず、「必要なMANAGER scope check」(v3が未定義)と標準TTLの上限比較(未決定)は未実装として、結果に明示する。hook・CI・policy_checkへは接続していない(Unit 3は別Issue)。`docs/policy_registry.yaml`は変更していない(発効前の規則を「読むべき条文」として提示しないため)。**発効しない**(発効状態の正本はIssue #332の最新のdurableな記録)。PRIMARY D9 / SHARED_TOUCHEDなし。**領域一覧・機能一覧・既存のF行・S-01〜S-21は変更していない。**判定ロジック・通知内容・保存データ形式・Production挙動の変更なし |
| 2026-09-19 | `services/investment_thesis_service.py`(F-11 / D3)のVALIDATION用INFOログが`baseline_id`(`{holding_id}:v{version}`。所有者名を含む)を生のまま出していたため、`holding_ref`(log_ref済み)と`version`(整数)を出す形へ改めた(Issue #416。案a)。あわせて`infrastructure/local_repository/investment_thesis_baseline_repository.py`の`save()`の`ValueError` messageが`baseline_id`を生で埋め込んでいた潜在箇所(src内の呼び出し元0件)も同じ方針で伏せた(例外の型は変えない)。再発防止として`tests/unit/test_issue_135_no_pii_in_logs.py`のguardの名前集合へ`baseline_id`/`holding_evaluation_id`を加え(既存の部分文字列判定に足す形。AST判定への切り替えはしていない)、raise側の検査対象へbaseline repositoryを加えた(#256の既知の1件[migrations/conversions.py]は別Issueのため対象外)。`activate_baseline`のVALIDATION経路を通すcaplogテストを追加した(従来の`get_or_create_thesis`経路のテストはこのINFOを通っていなかった)。PRIMARY D3 / LOCKED D3 / SHARED_TOUCHEDなし。**領域一覧・機能一覧・既存のF行・S-01〜S-21は変更していない。** 判定・保存・通知の挙動の変更なし(ログ文言と例外messageのみ。このINFOはIssue #413の修正までCloudWatch Logsへ出ないため、Productionで観測できる変化は無い) |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)の消費側として`lambda_handlers/weekly_review_handler.py`(F-31 / D5・D7)を、strict版`build_live_line_client_from_env()`へ切り替えた(Issue #117 Phase B1b-4d。fail-early)。認証情報の欠落は、集計・メトリクス保存・候補検出(`service.run`)の**前**に`LineCredentialsMissingError`で失敗する。送信時に失敗させる方式にすると、候補を保存した後に失敗し、再実行時は「既存候補」(`is_new`が立たない)となって通知が永久に失われうるため。mode(VALIDATION/DRY_RUN)の概念を持たないので、`build_line_client_for_run`ではなくstrict版を直接使う。これまで存在しなかったhandlerのテストを新設した。★ 副作用(承認済み): 認証情報が欠落した週は、通知が不要な週でも週次レビュー自体が実行されない(Errorsで可視化)。★ **#117 stage (b)のLambda handler切替は本件で9本すべて完了した**(webhook / dispatcher / buy_candidates / holdings_watchlist / disclosure_check / worker / terminal_failure / reconciler / weekly_review)。CLI 3本(analyze / review / watchlist_screening)は、CLI専用として意図的に旧`build_line_client_from_env()`のまま(#117 H-14。MANAGER判断で承認)。ただし`cli/watchlist_screening.py`の4コマンド(run / retry-finalize / retry-notification / retry-stock)には`--notify`が無く、認証情報が無いと送信されないまま「送信済み」と表示・記録されるため、別Issue #434とした。lock領域はD5/D7(F-31)、対象はweekly_review 1 handlerとテスト。`infrastructure/line/client.py`(S-21)は変更していない。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし(認証情報が正常な通常運用の挙動は不変) |
| 2026-09-19 | S-21(LINE通知clientの実行時構築)の`infrastructure/line/client.py`で、`build_line_client_from_env()`へ関数docstringを追加した(Issue #117。**docstringのみ・挙動不変**。LOCK_LEVEL 1、D9)。この関数が「CLI専用」であり、認証情報が無い場合は**この関数自身が**ConsoleLineClient(標準出力のみ・送信しない)へ黙ってフォールバックすること、Lambda handlerでは使わず`build_live_line_client_from_env()`または`build_line_client_for_run(dry_run=...)`を使うこと、CLIでも`--notify`の無い経路(`cli/watchlist_screening.py`の4コマンド。Issue #434)では「送信済み」と誤って表示・記録されうることを、関数自身の契約として明記した。従来この区別は`LineCredentialsMissingError`と`build_live_line_client_from_env()`のdocstringが**間接的に**述べているのみで、関数自体には未記載だった(PR #433の本文の記述が不正確だったことの是正)。関数シグネチャ・本体の実行コード・呼び出し元は不変(ASTからdocstringを除いた比較で同一)。**領域一覧・機能一覧・既存のF行・S-01〜S-20は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし |
| 2026-09-19 | F-04(見送り理由と整合性検証)から、Production判定経路から到達不能だった`domain/signals/judgment_safety_ladder.py`とそのテスト`tests/unit/test_judgment_safety_ladder.py`を削除した(Issue #160 Q-E。USER決定 #160 issuecomment-5741496634、先行の独立PR-A)。ladderの公開名(`max_allowed_strength` / `cap_judgment_strength` / `JudgmentSafetyInputs`)の参照元は、ladder自身とそのテストのみ(src / tests / scripts / docs / infraを全件検索して0件を確認)。9条件はすべて「移管済み・不要(USER決定)・新安全機構として別途実装(G1〜G5)」に分類済みで、cap規則は移管しない(#160 issuecomment-5738713711)。★ 本PRに**含めていない**もの: `config/confidence_rules.yaml`の`judgment_safety_ladder`ブロックと`config/models.py`の`JudgmentSafetyLadderConfig`(共通部品S-13=全領域のため、config field削除はLOCK_LEVEL_2=全領域lock)、`JudgmentStrength`(S-16=全領域)。これらは、ladder削除後にfreshで参照0を確認したうえで、全領域lockを伴う最後の独立PR(PR-B)で扱う。**領域一覧・機能一覧・F-04以外の行・S-01〜S-21は変更していない。** 判定ロジック・通知内容・保存データ形式・設定の挙動は不変(削除したのは未使用のmoduleとそのテストのみ) |
| 2026-09-19 | 共通部品S-22(市場休場日gate)を追加し、F-01(買い候補日次バッチ)・F-46(保有監視日次バッチ)・F-16(分散実行のdispatcher)の3 entryが、東証休場日に何も行わず正常終了するようにした(Issue #440)。`lambda_handlers/_market_holiday.py`を新設(営業日判定は既存のS-04へ委譲)。★ 新機能ではないためF番号は増やしていない。領域一覧・既存のF行の領域割当は変更していない。判定ロジック・通知内容・保存データ形式の変更なし(休場日に実行しないのみ) |
| 2026-09-20 | 機能一覧へ F-48(判断の安全条件のshadow計測)を追加した(Issue #160 PR-0)。shadow = 判定・通知・保存を変えずに「安全条件を適用していたら何件がどうなったか」を観測する機構で、PR-0は**設定(`config/judgment_safety_shadow.yaml`と専用の設定モデル・loader)のみ**であり、判定経路のどこにも接続していない(参照元0件をテストで固定)。mode既定はOFFで、ファイルが無い・不正な場合もOFFへ縮退する(fail-closed)。★ **S-13(`config/models.py` / `loader.py`)は変更していない**(AppConfigへ載せず専用loaderにしたため、全領域lockを増やしていない)。既存のF行・S行・領域一覧・維持契約は変更していない。判定ロジック・通知内容・保存データ形式・Production挙動の変更なし。後続PR(PR-1〜PR-4)で主要sourceを更新する |
| 2026-09-19 | `services/audit_service.py`(F-43 / D9)・`lambda_handlers/_finalize_recovery.py`・`services/watchlist_batch_finalizer.py`(F-47 / D9・D4・D1・D3)の3 moduleが、module直下で`logger.setLevel(logging.INFO)`を宣言し、書かれていたINFOがCloudWatch Logsへ出力されるようにした(Issue #413 PR-2。Lambdaのroot loggerの既定はWARNINGで、宣言の無いmoduleのINFOは出力されていなかった)。有効化の時点で、3 moduleのINFOの全call-siteの引数を確認し、生のowner・holding_id・baseline_id・holding_evaluation_idを出していないことを確認した(audit_serviceのVALIDATION用INFOはdecision_type・stock_code・audit_id。audit_idの生成元の入力にもownerは含まれない)。PR #436のguard(`tests/unit/test_issue_413_logger_level_declared.py`)のallowlistから3件を外した(11件→8件)。挙動の変化は「INFOが出力される」のみ(判定・保存・通知・LINE送信は変えない)。PRIMARY D9 / LOCKED D9・D4・D1・D3 / SHARED_TOUCHEDなし。**領域一覧・機能一覧・既存のF行・S-01〜S-22は変更していない。** |
| 2026-09-20 | `services/recommendation_evaluation_service.py`(F-32 / D7)・`services/weekly_improvement_review_service.py`(F-31 / D7・D5)の2 moduleが、module直下で`logger.setLevel(logging.INFO)`を宣言し、書かれていたINFOがCloudWatch Logsへ出力されるようにした(Issue #413 PR-3。Lambdaのroot loggerの既定はWARNINGで、宣言の無いmoduleのINFOは出力されていなかった)。有効化の時点で、2 moduleのINFOの全call-site(7箇所)の引数を確認し、生のowner・holding_id・baseline_id・holding_evaluation_idを出していないことを確認した(出すのは件数・経過時間・recommendation_id〔uuid4由来〕・axis・horizon・semantics_version・evaluated_at・週ごとの件数)。PR #436のguardのallowlistから2件を外した(8件→6件)。挙動の変化は「INFOが出力される」のみ(判定・保存・通知・LINE送信は変えない)。PRIMARY D7 / LOCKED D7・D5 / SHARED_TOUCHEDなし。**領域一覧・機能一覧・既存のF行・S-01〜S-22は変更していない。** |
| 2026-09-20 | F-48(判断の安全条件のshadow計測)の主要sourceへ`domain/signals/judgment_safety.py`を追加した(Issue #160 PR-1)。安全条件G1(決算日不明)・G2(BUYの財務STALE)・G3(利確FULLの緩和要因不明)・G4(株式分割・併合の未解決)を評価する**純関数**(`evaluate_safety_conditions`)と、非永続のfacts・finding・reason codeの型である。**判定経路のどこにも接続していない**(本番コードからの参照0件をテストで固定。挙動不変)。入力が無い条件は「評価していない」として、該当なしと区別する(FalseやNoneを不明と推測しない)。G5と条件8は既存validatorとの整理を伴うため本PRには含めない。新機能ではなく既存F-48のsource追加であるためF番号は増やしていない。既存のF行・S行・領域一覧・維持契約は変更していない。判定ロジック・通知内容・保存データ形式・Production挙動の変更なし |
| 2026-09-20 | F-02(買いシグナル判定)の`services/buy_signal_service.py`が、財務鮮度のverdictを**判定に入る前の事実**として非永続の`SafetyFacts`へ載せるようにした(Issue #160 shadow計測 PR-2a)。`BuyAnalysisOutcome`へ末尾の任意field(`safety_facts`。既定None・等価比較とreprから除外)を1つ追加し、推奨が生成された経路の最終returnでのみ設定する。STALE -> True / FRESH -> False / UNKNOWN -> None(評価していない)。**評価関数`evaluate_safety_conditions`は本流から呼ばない**(呼ぶのはPR-3以降)。既存の警告・反対材料・confidence・保存・監査ログは変更していない。F-48の主要sourceは変更していない(F-02側の変更)。既存のF行・S行・領域一覧・維持契約は変更していない。判定ロジック・通知内容・保存データ形式・Production挙動の変更なし |
| 2026-09-20 | F-07(利確判定)の`services/profit_taking_service.py`が、緩和要因のうち**UNKNOWN(None)を事実として識別できる2項目**(`continuous_dividend_increase_years` / `is_progressive_or_doe_policy`)の実値を、判定に入る前の事実として非永続の`SafetyFacts`へ載せるようにした(Issue #160 shadow計測 PR-2b)。`ProfitTakingOutcome`へ末尾の任意field(`safety_facts`。既定None・等価比較とreprから除外)を1つ追加し、推奨が生成された最終returnでのみ設定する(data_error・HOLD等はNone)。`MitigatingFactorInputs`の構築・判定式は変更していない(構築箇所の実値を並行して転記するのみ)。測定不能の3項目(`fair_value_rising_with_earnings_growth` / `long_term_holding_benefit_imminent` / `few_reinvestment_alternatives`)は載せない。**評価関数`evaluate_safety_conditions`は本流から呼ばない**(PR-3以降)。F-48の主要sourceは変更していない(F-07側の変更)。既存のF行・S行・領域一覧・維持契約は変更していない。判定ロジック・通知内容・保存データ形式・Production挙動の変更なし |
| 2026-09-20 | `services/watch_state_service.py`(F-17 / D4・D1・D5)が、module直下で`logger.setLevel(logging.INFO)`を宣言し、書かれていたINFOがCloudWatch Logsへ出力されるようにした(Issue #413 PR-4。Lambdaのroot loggerの既定はWARNINGで、宣言の無いmoduleのINFOは出力されていなかった)。INFOは1箇所(評価側の更新が競合し、別実行が先に終了させていた場合のみ。出す値はwatch_id〔銘柄コード:監視種別〕と終了理由)。有効化の時点で、全call-site(INFO 1・WARNING 2)の引数を確認し、生のowner・holding_id・保有数量・取得単価・状態の中身(価格・距離)を出していないことを確認した。この機能は売買イベント(owner・holding_id・数量・取得単価を持つ)を`end_for_trade_events()`で受け取るが、読むのは銘柄コードだけである(テストで構造として固定した)。PR #436のguardのallowlistから1件を外した(6件→5件)。挙動の変化は「INFOが出力される」のみ(判定・保存・通知・LINE送信は変えない)。PRIMARY D4 / LOCKED D4・D1・D5 / SHARED_TOUCHEDなし。**領域一覧・機能一覧・既存のF行・S-01〜S-22は変更していない。** |
| 2026-09-20 | 通知文面のgoldenテストを追加した(Issue #255。**テストのみ・`src/`は1行も変更していない**)。利用者に届くLINE本文を、架空値のfixtureから生成してスナップショット(`tests/unit/golden/notification/*.txt`)で固定する`tests/unit/test_notification_message_golden.py`を新設した(F-22 / F-21 / F-20 = D5)。対象は5種類: 銘柄分析(BUY判定の詳細 / 保有の利確判定の状況〔#222 N-5〕/ 保有継続の事実〔#222 N-3〕の3スナップショット)・利確判定・売却判断(いずれも実送信の本文)・監視銘柄の追加・候補一覧の取得失敗日(#234)。差分は読める形(unified diff)で出し、意図した変更のときの更新手順を`tests/unit/golden/notification/README.md`に残した(`UPDATE_GOLDEN=1`)。fixtureは架空値のみで、日付・時刻は固定している。整形部品・文言・設定値を変えると該当のスナップショットが落ちることを、実装への変異と、#222の変更の巻き戻しで確認した。PRIMARY D5 / LOCKED D5 / SHARED_TOUCHEDなし。**領域一覧・機能一覧・既存のF行・S-01〜S-22は変更していない。** 判定ロジック・通知内容・保存データ形式の変更なし |
| 2026-09-20 | F-48(判断の安全条件のshadow計測)の`domain/signals/judgment_safety.py`で、**G4の契約を是正した**(Issue #455、#160 USER決定 U8)。PR-1(#445)の変更履歴・型は、G4を「株式分割・併合(SPLIT / REVERSE_SPLIT)の未解決」と表現していたが、既存の`check_split_consistency()`は分割か併合かの区別を返さない(4種の`check_name`のいずれかを返す)ため、前提が誤っていた。是正後のG4は「**既存の株式分割・併合整合性検査が未解決の問題を検出した状態**」であり、`CorporateActionFacts.unresolved_checks`が実際の`check_name`(4種)を保持し、reason codeは`CORPORATE_ACTION_UNRESOLVED:<check_name>`である。**向きの推定・新しい分類ロジックは実装していない。** MERGER・株式交換・株式移転・上場廃止等へ対象を広げていない。評価関数は本流から呼ばれず(呼び出し元0件)、shadow modeはOFFのままで、Production挙動は変わらない。既存のF行・S行・領域一覧・維持契約は変更していない |
| 2026-09-20 | F-07(利確判定)の`services/profit_taking_service.py`が、企業行動のshadow facts(G4)を供給するようにした(Issue #456、#160 PR-2c。USER決定 U7 / U8)。(1) 企業行動eventsの取得は**既存の1回のみ**(追加のprovider呼び出し0)で、取得開始日を`min(profit_protection_basis_date, lookback_start)`へ**shadow modeに依存せず**広げた(yfinance providerは取得後にローカルで`since`より古いeventsを除外するため外部通信は増えない)。★ **既存Profit Protectionの観測窓は変更していない**(取得済みeventsを従来どおり`effective_date >= basis_date`で再filterする)。(2) shadow modeがSHADOWかつ保有の`FULL_PROFIT_TAKE`のときだけ、既存の`check_split_consistency()`の結果を`CorporateActionFacts`(#455の契約)へ写す。評価は`isolated_shadow_computation`(S-20)で隔離し、失敗は`COMPUTATION_FAILED`になるだけで既存の業務結果を変えない。未知のcheck_nameも`COMPUTATION_FAILED`(黙って捨てない)。mode=OFF(既定・出荷config)では`check_split_consistency`を実行しない。`ProfitTakingOutcome.safety_facts.corporate_action`へ載せる(非永続)。**評価関数は本流から呼ばれず、shadow modeはOFFのままで、Production挙動は変わらない。** `data_quality_service.py`・provider・handler・BUY経路・S-13は変更していない。既存のF行・S行・領域一覧・維持契約は変更していない |
| 2026-09-20 | `docs/operations_manual.md`へ24節「DLQの滞留の確認手順」を追加した(Issue #349 ⑤。**docsのみ・`src/`と`infra/`は変更していない**)。SQSのDLQ(ウォッチリスト評価の終端失敗DLQ・非同期invoke失敗DLQ。#396の反映後は新規2本も)にメッセージが溜まっていないかを、観測用roleでCloudWatchのSQSメトリクスから確認する手順を定めた。★ 限界を明記した: 手動の確認は「見に行かなければ気づかない」ため、気づく仕組み(Alarm・通知・起票。Issue #349 ①・#132)の代替にならない。SQSのAPIは観測用roleに許可されておらず、キューの属性は確認できない。メトリクスのデータ点が無い日を「0」と読んではならない。見つかった場合のredrive・削除・purgeはProductionの書き込み(Human Gateの対象)で、本手順の範囲外とした。「誰が・いつ確認するか」は決めていない(未決定)。PRIMARY D9 / LOCKED D9 / SHARED_TOUCHEDなし。**領域一覧・機能一覧・既存のF行・S-01〜S-22は変更していない。** 判定ロジック・通知内容・保存データ形式・Productionの挙動の変更なし |
| 2026-09-20 | F-07(利確判定)の「上限価格(ceiling_price)が使えない原因」ごとのテストを追加した(Issue #467、#254 経路4の分離。**tests-only・`src/`は変更していない**)。`domain/signals/profit_taking.py::_fair_value_action_usable`が`False`になる原因(レンジなし・レンジが使えない・bullなし・業種区分の未指定・bearなし/0以下・手法数不足・スプレッド超過・決算未反映/判定不能・決算直前・含み損/損益ゼロ)を、基準入力(FULLへ到達する入力)から1つだけ変える1変数テストで固定し、各境界(手法数・スプレッド`<=`・決算までの営業日数・含み益`>0`)を閾値ちょうどと外側の対で固定した。理由コードが構造化されているのはスプレッド超過のみという現状も観測として固定した(理由コードの拡張=#471で、意図した変更として更新する)。判定ロジック・理由コードは変更していない |
| 2026-09-20 | 買い候補・保有銘柄の子handler(`lambda_handlers/buy_candidates_handler.py` / `holdings_watchlist_handler.py`)の**正常系の終端ログ2行**へ、末尾に`batch_id=%s`を追加した(Issue #362。案1)。これまで親のdispatch・集計行と子の失敗分岐にだけ`batch_id`が出ており、「うまくいった1件」をログから run へ結びつけられなかった(2026-09-11の保有銘柄バッチで、`batch_id`を含む行2件・子の監査行27件)。既存の抽出手順を壊さないよう、先頭のprefixと各フィールドの並びは変えず末尾へ足した。`batch_id`は親が生成する識別子(時刻+乱数)で、owner・holding_idを含まない。判定・通知・保存・監査レコードは変更していない(ログ行のみ)。案2(logging共通機構)・案3(保有系の監査レコード)は対象外 |
| 2026-09-20 | F-10(保有判断)の新方式(保有判断スコア)へ、財務データの鮮度(#52 B3)を接続した(Issue #468。USER決定 U17 = OPTION_B_CONFIDENCE_CAP / Q1 = W2)。(1) `domain/signals/holding_decision_score.py::combine_holding_decision`へ keyword-only の`financial_stale`(既定False)を追加し、STALEのとき既存のcoverage由来の上限の**後**にconfidenceのHIGHを許可しない(HIGH→MEDIUMのみ。MEDIUM以下は不変)。score・component score・coverage・coverage gate・通知判定は変更しない。(2) `services/holding_decision_service.py`が共通部品`assess_financial_freshness`(SELL・利確と同じ)を呼び、監査へ共通10項目と最終confidenceを記録する。(3) `services/holding_decision_notification_builder.py`がSTALEのとき既存の警告を`key_risks`へ入れ、`services/line_notification_service.py`の保有判断の本文へ、`key_risks`が空でないときだけの「留意事項」節を追加した(空なら本文は不変)。共通部品(`financial_freshness_integration.py`・`domain/financial_freshness.py`)・config・永続schema・BUY/SELL/利確は変更していない。呼び出し元ガード(`test_production_call_sites_are_limited_to_the_current_phase`)の許可リストへ保有判断の2ファイルを意図的に追加した。現在のProductionは`mode=shadow`で新方式は通知に未到達。ACTIVE切替の前提(切替は別Human Gate) |
| 2026-09-20 | `services/shareholder_benefit_registry_service.py`(F-29 / D6)が、module直下で`logger.setLevel(logging.INFO)`を宣言し、書かれていたINFOがCloudWatch Logsへ出力されるようにした(Issue #493〔#413のatomic分割 U-1〕。Lambdaのroot loggerの既定はWARNINGで、宣言の無いmoduleのINFOは出力されていなかった)。INFOは1箇所(`check_registry_health()`が登録件数を常時記録する行。BUY・保有の2 handlerが1回の実行につき1回呼ぶ)。有効化の時点で、全call-site(INFO 1・WARNING 1・exception 1)の引数を確認し、出すのは登録件数だけで、優待の内容・銘柄・所有者を出していないことを確認した(架空の優待を実際に読まれるデータへ登録して、出力に現れないことをテストで固定し、内容を出す行を注入するとテストが赤になることを確認した)。既存のWARNING・exceptionの行の文面・levelは変更していない。**機能一覧・共通部品・領域・既存のF行/S行は変更していない**。判定・通知・保存データの形式・IAM・スケジュールの変更なし |
| 2026-09-20 | `providers/dividend_data/cross_validating_impl.py`(F-36 / D8)が、module直下で`logger.setLevel(logging.WARNING)`を**意図した静音として明示**した(Issue #494〔#413のatomic分割 U-2〕。当初設計〔#413 issuecomment-5738498397〕どおり、INFOは有効化しない)。この moduleのINFO(5か所)は候補ごとの条件付きで高頻度になりうるため出さない。返り値のDividendInfo.validation_statusに残るのは状態(VALIDATED / NOT_YET_VALIDATABLE)だけで、NOT_YET_VALIDATABLEになった4つの理由(暦年フォールバック・共通決算期なし・決算期内の分割・推定期間での乖離)の区別は残らない(理由はINFOの文面にだけあった。INFOは従来もProductionでは出力されておらず、理由の区別が新たに失われるわけではない)。真の乖離のWARNING(共通決算期で正規化後も乖離した場合)は、これまでどおり出力される。5つのINFO経路を実際に通してもINFOが出ないこと(rootをINFOにしても出ない)、WARNINGの文面が不変であることをテストで固定した。**機能一覧・共通部品・領域・既存のF行/S行は変更していない**。判定・通知・保存データの形式・IAM・スケジュールの変更なし。Productionのログ出力の増減もない |
| 2026-09-20 | F-48(判断の安全条件のshadow計測)の評価と監査記録を、handlerの合流点へ接続した(Issue #160 PR-3 = #457。USER決定 U13 = OPTION_C)。新規`services/judgment_safety_shadow_service.py`が、強い判定(買い系 / 利確のFULL_PROFIT_TAKE)について純関数`evaluate_safety_conditions`を評価し、既存の`AuditLogTable`へ`decision_type=judgment_safety_shadow`の1件を記録する(決定的な`audit_id`と`record_if_absent`で冪等。findingが0件でも強い判定なら記録する[分母])。呼び出しは`buy_candidates_handler._process_single_candidate`と`holdings_watchlist_handler._analyze_one_holding`の**保存(Recommendation・DecisionSnapshot)が完了した後**の1か所ずつ(利確側は通知の前)。VALIDATIONは既存のifが除外する。**shadowが既定のOFF(`mode: "OFF"`)のときは、事実の取得・評価・記録のいずれも行わない**。SHADOWでも評価・記録の失敗は隔離され(S-20)、判定・通知・保存・返り値は変わらない。記録する内容は許可した項目の列挙のみで、holding_id・owner・価格・数量・銘柄名・理由文を含まない。ON/OFFのgoldenテストで本流の不変を固定した。SHADOWの有効化は別Human Gate。Production enforcementなし。新規Table・IAM・config・永続schemaの変更なし(両関数の実行ロールが`jstock-audit_log`へPutItemできることをread-onlyで再確認済み)。読み取り・集計は#458 |
| 2026-09-21 | `services/watchlist_data_cache.py`(F-19 / D4)が、module直下で`logger.setLevel(logging.WARNING)`を**意図した静音として明示**した(Issue #495〔#413のatomic分割〕。当初設計〔#413 issuecomment-5738498397 §2〕どおり、INFOは有効化しない)。この moduleのINFO(cacheのhit / miss〔期限切れ〕/ miss〔不在〕の3か所)は候補ごとに最大9行で高頻度になるため出さない。集計はhandler側のCacheStatsログ(`watchlist worker cache stats hit=… miss=…`)で既に出ており、候補ごとの詳細を出さなくても取得できる。calibration用の高粒度な計測(呼び出し種別・所要時間)は別責務(#5)で、通常運用のログでは行わない。cacheの判定・TTL・cache key・quality_status・統計の集計は変更していない。#413のguardのallowlistは3件から2件になった。判定・通知・保存データ・IAM・スケジュールへの影響なし |
| 2026-09-21 | F-48(判断の安全条件のshadow計測)に、shadow監査記録の**読み取り専用の集計CLI**を追加した(Issue #160 PR-4 = #458。USER決定 U13 = OPTION_C)。`jstock judgment-safety-shadow report`が、`AuditLogTable`の`decision_type=judgment_safety_shadow`(schema_version 1)を集計する(条件別G1〜G4の件数・率・reason code別・重なり・日別[JST暦日]・「適用していたら何件が抑止されたか」。`not_evaluated`は件数へ含めず別掲。G3は下限値・G4は保有のFULL_PROFIT_TAKEのみ、の注記を常に出す)。あわせて表の増加量・scanメトリクス(ItemCount / TableSizeBytes[概算]、Scanのページ数・ScannedCount・消費した読み取りユニット・推定コスト、decision_type別の内訳、baselineとの比較)を出す。**閾値の判定・専用Tableへの移行の提案はしない**(判断はUSER / MANAGER)。既定は`--source local`で**Productionを既定で読まない**。`--source dynamodb`は`scan` / `describe_table`だけを通すallowlistのproxy経由でread-onlyに読み、書き込み系の呼び出しは構造的に例外になる(テストとASTで固定)。新規`services/judgment_safety_shadow_report.py`(純関数)・`infrastructure/aws/audit_shadow_reader.py`・`cli/judgment_safety_shadow.py`をF-48の主要sourceへ追加し、領域へD9(CLI・reader)を加えた。**既存の呼び出し元・判定・通知・保存・IAM・Table・configは変更していない**(変更した既存ファイルは`cli/main.py`のサブコマンド登録の1行のみ)。Productionへのdeploy・実行は行っていない。既存のS行・領域一覧・維持契約は変更していない |
| 2026-09-21 | `services/watchlist_display_name.py`(F-20 / D4・D5)が、module直下で`logger.setLevel(logging.INFO)`を宣言し、Lambdaで**INFOが出力される**ようにした(Issue #496〔#413のatomic分割〕)。この moduleのINFOは「JPX銘柄名mapの読み込み件数」の1行(`JPX stock name map loaded count=N`)だけで、銘柄名・stock_codeは出さない(架空の値を実際に読まれるmapへ置いて、出力に現れないことをテストで確認した)。読み込みは成功キャッシュが無いときだけで、コンテナ生存期間中に1回(cold startごとに最大1行)。既存のWARNING 5か所の文面・levelは変更していない。名称解決の判定・cache・negative cache・判定・通知・保存データ・IAM・スケジュールへの影響なし。#413のguardのallowlistは2件から1件になった |
| 2026-09-21 | F-10(保有継続判断)の優待条件(`domain/signals/investment_thesis_scoring.py`・`services/holding_decision_service.py`)で、**baselineで優待あり + 現在の登録なしを「データ欠落」として不評価にした**(Issue #470。USER決定 U-A / U-B)。優待条件の状態を、1つの純関数`derive_benefit_condition_state`で導く(baselineの値・初回評価・現在の登録・明示的な改悪から、NOT_APPLICABLE / BASELINE_NOT_COMPARABLE / DATA_MISSING / DOWNGRADED / MAINTAINED)。`InvestmentThesisInputs`は、2つのbool(`has_shareholder_benefit` / `benefit_abolished_or_downgraded`)の代わりに、状態(`benefit_state`)を1つ受け取る。DATA_MISSINGは、不評価(NOT_EVALUATED)+ 理由コード`BENEFIT_DATA_MISSING`で、初回評価の`BASELINE_NOT_COMPARABLE`と区別し、スコアの分母から外す(coverageは下がる)。**共通enum S-16・永続schema(`ScoreItemDetail`)・config・IAMは変更していない**(`reason`の自由文字列に理由コードを1つ追加しただけ)。Issue #55 Phase A Decision 3(total_yieldの欠測は分母に残る)は変更していない。**明示的な廃止(`is_abolished`)は従来どおりNOT_APPLICABLE**(Issue #476が同じ関数の状態として改める)。baselineに優待なし + 現在は登録あり の場合は、以前は現在の状態で評価していたが、baselineの値でNOT_APPLICABLEになる(本番の該当は 0 件を実測済み)。現在は`mode=shadow`で通知に未到達のため、利用者への通知・判定の挙動は変わらない。既存のF行・S行・領域一覧・維持契約は変更していない |
