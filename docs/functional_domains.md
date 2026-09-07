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

全 47 機能。各表の列は次を表す。

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
| F-16 | 分散実行(dispatcher / worker / 回収) | `lambda_handlers/watchlist_dispatcher_handler.py` `lambda_handlers/watchlist_worker_handler.py` `lambda_handlers/watchlist_batch_reconciler_handler.py` `lambda_handlers/watchlist_terminal_failure_handler.py` | `schedule.yaml` | `WatchlistCandidateProgressTable` `WatchlistScreeningRotationStateTable` `WatchlistRotationDispatchLeaseTable` | D4 / D9 |
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
| F-45 | CI・品質ゲート | `.github/workflows/ci.yml` `.github/workflows/pii-metadata-audit.yml` `scripts/` | — | なし | D9 |
| F-47 | batch finalize recovery | `lambda_handlers/_finalize_recovery.py` `services/watchlist_batch_finalizer.py` | — | `BuyCandidateBatchCompletionTable`(読み取り) | D9 / D4 / D1 / D3 |

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
| S-17 | 永続化ストア層 | `infrastructure/collection_store.py` `infrastructure/local_repository/json_store.py` `infrastructure/aws/dynamodb_store.py` `infrastructure/record_failure_policy.py` | 全領域 | 全 repository(通知 / 推奨 / 保有 / 監視 / 評価 / 監査)+ migrations + EDINET cache |
| S-18 | 所有者と holding_id 規約 | `domain/entities/owner.py` | D3 / D5 / D6 | `portfolio_service` `conversation_service` `csv_import_service` `transaction_history_service` `holding_repository` `holdings_owner_migration` ほか(src 内 15 module) |
| S-19 | 価格レンジ共通 | `domain/signals/_price_range_shared.py` | D1 / D2 | `entry_price_range`(F-03) `exit_price_range`(F-09) |

`SHARED_ID` は再利用しない。

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
