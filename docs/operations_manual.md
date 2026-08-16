# jstock_advisor 運用手順書

本書は、AWSへのデプロイ前にローカル環境で本システムを日常運用する場合の操作手順を
まとめたものです。実際に構築済みのCLIコマンドのみを記載しており、
`config/schedule.yaml` に定義はあるが未実装の処理は「未実装」と明記しています。

最終的な投資判断は、本システムの出力にかかわらず必ず利用者自身が行ってください。

---

## 1. システム概要

- 対象: 日本株の長期・高配当・株主優待重視の売買支援(REIT・ETFは対象外)
- 現状: ローカルCLIとAWS(Lambda/DynamoDB/EventBridge Scheduler/API Gateway)の両方に対応
  (`infrastructure/collection_store.py`が実行環境を自動判定してストレージを切り替える)。
  AWSへのデプロイ手順は[infra/README.md](../infra/README.md)を参照
- データ保存先: ローカル実行時は`data/local_store/*.json`、AWS実行時はDynamoDB(リポジトリ層のインターフェースは同一)
- 判断の原則:
  - Pythonが数値計算・判定を行い、あいまいな推測はしない(データが無ければ「取得不可」として扱う)
  - ルール変更(config/*.yaml)は`rules`コマンド経由の提案→人間承認を経ないと本適用されない
  - 実際の売買可否は必ず利用者が最終判断する

---

## 2. 事前準備

### 2.1 Python環境

```bash
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
```

以降、本書のコマンドは `jstock <サブコマンド>` の形式で記載します
(`.venv\Scripts\jstock.exe` が有効化されている前提。有効化していない場合は
`.venv\Scripts\python.exe -m jstock_advisor.cli.main <サブコマンド>` で代替可能)。

### 2.2 環境変数(.env)

```powershell
Copy-Item .env.example .env
```

`.env` に以下を設定します(未設定の項目は該当機能がドライラン/取得不可扱いになります)。

| 変数 | 用途 | 未設定時の挙動 |
|---|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_USER_ID` | LINE通知の送信 | 標準出力へのドライラン表示のみ(送信されない) |
| `EDINET_API_KEY` | 配当クロスバリデーション・適時開示(臨時報告書)の取得 | EDINET由来のデータが常に取得不可扱いになる |

### 2.3 設定ファイル(config/\*.yaml)

すべてユーザーが直接編集可能な閾値・ルール設定です。主なファイル:

| ファイル | 内容 |
|---|---|
| `screening_rules.yaml` | 一次スクリーニング条件(総合利回り下限・財務健全性等) |
| `valuation_rules.yaml` | 適正価格算出方法・推奨買値の算出比率 |
| `profit_taking_rules.yaml` | 利確判定の閾値・緩和要因 |
| `sell_rules.yaml` | 投資前提悪化売却ルール |
| `scoring_weights.yaml` | 買い候補スコアの重み付け |
| `schedule.yaml` | 実行スケジュール定義(AWS移行時にEventBridge化を想定) |
| `notification_rules.yaml` | LINE再通知条件 |
| `data_validation_rules.yaml` | データ出典間の乖離許容閾値 |
| `evaluation_rules.yaml` | 定点評価のラベル判定閾値 |
| `review_improvement.yaml` | 週次改善レビューの対象期間・改善候補の基準・GitHub Issue自動起票の有効/無効(5.1節) |
| `decision_evaluation.yaml` | 判定精度向上機能・自己評価基盤(Phase A)がDecisionSnapshotの成績集計対象とみなす営業日ホライズン(5.2節) |

これらの値を変更する場合は、原則として第7節の「ルール改善承認フロー」を経てください
(直接編集での即時反映も技術的には可能ですが、変更履歴・根拠が記録されなくなります)。

---

## 3. 初回セットアップ(データ登録)

自動取得できないデータは、運用開始前に手動で登録する必要があります。

### 3.1 保有銘柄の登録

```bash
jstock holdings add 8136 --shares 100 --price 3775 --account-type NISA
# または
jstock holdings import-csv holdings.csv
```

### 3.2 ウォッチリストの登録

```bash
jstock watchlist add 7203 --priority HIGH
```

### 3.3 株主優待の登録(★必須・自動取得非対応)

株主優待は自動取得できる公式データ源が存在しないため、**保有銘柄・ウォッチリスト
銘柄のうち株主優待がある銘柄は、必ず利用者が会社発表等の一次情報を確認のうえ
登録してください**。未登録の場合、その銘柄の総合利回り計算・優待廃止検知は
機能しません。

```bash
jstock shareholder-benefit add 2914 \
  --min-shares-required 100 --frequency-per-year 1 \
  --category CASH_EQUIVALENT --description "クオカード1000円分" \
  --min-shares-for-tier 100 --estimated-value 1000
# または
jstock shareholder-benefit import-csv benefits.csv
```

CSVを用意しただけでは反映されません。必ず`import-csv`を実行し(ローカル運用は
そのまま、AWS本番環境へ反映する場合は`AWS_LAMBDA_FUNCTION_NAME`環境変数を
設定した状態で同じコマンドを実行)、`jstock shareholder-benefit list`で
登録件数を確認してください。取込漏れは4節の起動時ログ(`WARNING`)でも検知できます。

### 3.4 ウォッチリスト自動追加の候補銘柄一覧(2026-08-01・候補ユニバース本格対応で全面変更)

候補ユニバース本格対応(2026-08-01)により、固定CSV(`data/universe/candidate_universe.csv`)
から、東証(JPX)・日本経済新聞社が公開する銘柄一覧を毎週自動取得する方式へ変更しました。
`config/watchlist_screening_rules.yaml`の`candidate_universe.provider`を`"jpx"`(既定)に
設定していれば、事前の候補登録作業は不要です。CSV方式(`"csv"`)は小規模な動作検証用に
残しています。

**キャッシュの取得元とローカル管理コマンド**: 取得したデータはS3(本番)またはローカル
ファイル(`data/cache/candidate_universe/`)へキャッシュされます。`WatchlistDispatcherFunction`
が起動のたびに自動で取得・検証・更新するため、**通常運用では以下のコマンドを使う必要は
ありません**。ローカルでの事前確認・リハーサル用の任意ツールとして提供しています(常に
ローカルキャッシュのみを読み書きし、本番S3には一切アクセスしません)。

```bash
jstock candidate-universe refresh   # ローカルキャッシュを取得・検証・更新
jstock candidate-universe status    # ローカルキャッシュの現在の状態(source_date・件数等)を表示
```

**本番S3キャッシュを定例スケジュール外で手動更新したい場合**: ローカルCLIからは行えません。
`WatchlistDispatcherFunction`を直接手動起動してください(4.1節参照)。

---

## 4. 日次運用

`config/schedule.yaml` の日次ジョブと、対応するCLIコマンド・AWS Lambda関数の対応表です。
AWSデプロイ後はEventBridge Schedulerが下表のLambda関数を自動実行します(時刻はJST、
`infra/template.yaml`の`ScheduleExpressionTimezone: Asia/Tokyo`により変換不要)。
ローカル運用のみの場合は、利用者自身がタスクスケジューラ等にCLIコマンドを登録するか
手動で実行してください。

| 時刻 | schedule.yamlのジョブ | 対応コマンド | 対応Lambda関数 | 備考 |
|---|---|---|---|---|
| 08:00 | `daily_buy_candidates_analysis` | `jstock analyze buy-candidates <銘柄コード...> --source real --notify` | `BuyCandidatesFunction` | ウォッチリスト+保有銘柄を統合して買い判定(新規購入・買い増し)を行う(2026-07-31改訂)。全上場銘柄の自動スクリーニングではない |
| 08:00 | `daily_holdings_watchlist_analysis` | `jstock analyze holdings --source real --notify` | `HoldingsWatchlistFunction` | 保有銘柄の利確・売却判定、ポートフォリオ集中チェック(2026-07-31改訂: 16:30から08:00へ変更。買い候補分析と処理条件・通知タイミングを揃えるため)。保有銘柄は全件自動対象 |
| 10:00/12:30/15:30 | `disclosure_check` | `jstock analyze disclosure-check --source real --notify` | `DisclosureCheckFunction` | 保有銘柄の新規開示にリスクキーワードが検出された場合のみ速報通知する |
| 18:00 | `point_in_time_evaluation` | `jstock evaluation run --source real` | `EvaluationFunction` | 評価期限(営業日数)を迎えた推奨のみ処理。通知機能は無く、結果はコンソール/CloudWatch Logs表示のみ |

### 実行結果の確認ポイント

- `[DATA_ERROR]` と表示された銘柄は、データ取得に失敗し判定を出せなかったことを示します(推測での補完はしていません)
- 買い候補が無い日は「本日買いを検討すべき銘柄はありませんでした。」と表示されます(異常ではありません)
- `--notify` 時、前回と同一内容の推奨は再送されません(「前回と同内容のため通知をスキップしました」)
- `BuyCandidatesFunction`・`HoldingsWatchlistFunction`は同じ08:00起動でも別々のLambda関数として完全に独立しており、それぞれ別の「まとめ通知」を送ります。保有銘柄側のまとめ通知に買い候補の結果が含まれないのは意図した設計です(2026-07-31確認)。買い候補が0件の日は既定でまとめ通知自体を送信しません(`notification_rules.yaml`の`send_empty_summary`)
- 両関数とも起動時に株主優待レジストリの読み込み件数をCloudWatch Logsへ`INFO`で常時記録し、`notification_rules.yaml`の`operations.shareholder_benefit_registry_min_expected_entries`(既定1)未満の場合は`WARNING`を追加で出します(2026-07-31追加。CSV取込漏れ等の運用ミスを検知するため。バッチ処理自体は止めません)

---

## 4.1 ウォッチリスト自動追加(2026-08-01追加・候補ユニバース本格対応で全面改訂・2026-08-16平日毎日起動化)

| 時刻 | schedule.yamlのジョブ | 対応コマンド | 対応Lambda関数 |
|---|---|---|---|
| 平日(月曜〜金曜)06:00(2026-08-16改訂。旧: 毎週土曜07:00。祝日判定なし) | (未登録。`infra/template.yaml`の`WeekdayMorning` ScheduleV2にcron直書き) | `jstock watchlist-screening run` | `WatchlistDispatcherFunction`(`job_type=NEW_CANDIDATE_SCREENING`) |
| 毎時 | (未登録。`infra/template.yaml`にcron直書き) | ― | `WatchlistBatchReconcilerFunction` |

**WATCHLIST_MAINTENANCEに独立したScheduleは存在しない(2026-08-16改訂)**:
旧・毎週日曜07:00の独立実行(`SundayMaintenanceReview`)は廃止した。現在は、
同日のNEW_CANDIDATE_SCREENINGが業務finalizeを正常完了した直後の後続処理
としてのみ起動する(詳細は4.1.1節「起動方式の平日毎日化」参照)。

候補ユニバース本格対応(2026-08-01)で、単一Lambdaの自己再帰fan-outから、
4つのLambda関数+SQSキューによる構成へ全面的に作り直しました。

| Lambda関数 | 役割 |
|---|---|
| `WatchlistDispatcherFunction` | 平日毎日06:00のEventBridge起動、またはWATCHLIST_MAINTENANCEの自己invoke起動。候補ユニバースの取得(Downloader)・確定・銘柄ごとの進捗行作成・SQSへの投入のみを行う |
| `WatchlistWorkerFunction` | メインキュー(`WatchlistScreeningQueue`)のトリガー。1メッセージ=1銘柄を評価する |
| `WatchlistTerminalFailureHandlerFunction` | メインキューで3回失敗したメッセージの移動先(`WatchlistTerminalFailureQueue`)のトリガー。該当銘柄をFAILED確定する |
| `WatchlistBatchReconcilerFunction` | 毎時起動。長時間RUNNINGのまま/DISPATCHINGのままのバッチのタイムアウト検知・終端確定を行う |

CLIでの手動実行・dry-run確認方法は変更ありません。

```bash
jstock watchlist-screening run --dry-run   # 登録・通知・監査ログ記録を一切行わず結果のみ表示
jstock watchlist-screening run             # 実際にウォッチリストへ登録・LINE通知
```

**バッチの状態遷移**: DynamoDBの`jstock-batch_runs`テーブルの`status`属性で
確認できます。

```
DISPATCHING → RUNNING → FINALIZING → COMPLETED (execution_result=NORMAL)
     ↓                                   ↘ ABORTED (execution_result=HIGH_THROTTLE_RATE)
DISPATCH_FAILED                       FINALIZING → FINALIZE_FAILED

RUNNING → TIMEOUT_FINALIZING → TIMED_OUT
              ↘ TIMEOUT_FINALIZE_FAILED → (Reconcilerが毎時自動で再試行)
```

- **`COMPLETED`**: 通常の正常完了。`execution_result=NORMAL`。
- **`ABORTED`**(`execution_result=HIGH_THROTTLE_RATE`): 全銘柄の処理完了後、
  データ取得元(Yahoo Finance)へのアクセス集中が疑われた件数の割合が閾値
  (既定20%、`high_throttle_rate_threshold_pct`)を超えた場合。ウォッチリスト
  追加・LINE通知は行われません(合否判定自体の結果は監査用に保持されます)。
- **`DISPATCH_FAILED`**: 候補ユニバースの取得・進捗行の作成に失敗した、または
  `WatchlistDispatcherFunction`自体が`batch_processing_timeout_hours`(既定24時間)
  以内に応答しなかった場合。候補リスト自体が確定していないため、この状態から
  finalize処理は一切行われません。**自動的な再開はしません**。次回のスケジュール
  起動(NEW_CANDIDATE_SCREENINGは翌平日06:00)が新しい`batch_id`で最初からやり直します。
- **`FINALIZE_FAILED`**: 全銘柄の評価は完了したが、集計処理(ウォッチリストへの
  実登録・LINE通知・実行結果の記録)自体が例外で失敗した場合。`finalize_error_message`
  にエラー概要、`finalize_failed_at`に失敗時刻が記録されます。自動復旧の仕組みは
  なく、次回のスケジュール起動を待つか、`jstock watchlist-screening run`で
  手動実行してください(新しい`batch_id`で最初からやり直す形になります)。
- **`TIMED_OUT`**: 処理開始から`batch_processing_timeout_hours`(既定24時間)
  以内に全銘柄の評価が終わらなかった場合。`WatchlistBatchReconcilerFunction`が
  毎時のチェックで検知し、未完了銘柄をまとめてFAILED確定します。**この場合、
  途中まで合格していた銘柄も含めてウォッチリストへの追加・LINE通知は一切
  行いません**(全銘柄評価が終わっていない状態のランキングは実際の実力順とは
  限らないため)。途中結果・完了率(`completion_rate`)はAuditLogに記録されます。
- **`TIMEOUT_FINALIZE_FAILED`**: タイムアウト確定処理自体が想定外の理由で
  失敗した一時的な状態。`WatchlistBatchReconcilerFunction`が次回(1時間後)の
  実行で自動的に再試行するため、通常は運用者の対応は不要です。長時間
  (数時間以上)この状態のままの場合はCloudWatch Logsのエラー内容を確認してください。

**候補銘柄数の上限について**: 旧仕様にあった評価対象件数の上限(300件)は、
候補ユニバース本格対応でSQSベースの銘柄単位処理へ全面的に作り直したことに伴い
撤廃しました。約3,122銘柄の全件処理には数時間規模の時間がかかります(1銘柄
あたり30〜45秒 ÷ 同時実行数3)。

**段階導入(全件処理へ移行する前の実測)**: `config/watchlist_screening_rules.yaml`の
`staged_rollout`で、評価対象を一時的に絞り込めます。

```yaml
staged_rollout:
  candidate_limit: 100          # 先頭100件のみ評価(nullで無制限)
  market_segment_filter: null   # 例: ["プライム（内国株式）"]で市場区分を絞り込み
```

100→500→プライム市場のみ→全件、の順に実測し、以下をすべて満たすことを
確認してから全件(両方`null`)へ戻すことを推奨します(実測値は`record_batch_audit`の
出力値、またはCloudWatch Logsで確認できます)。

- 429疑い率(`rate_limit_suspected_rate_pct`)が5%未満
- データ取得失敗率(`data_error_rate_pct`)が5%未満
- p95処理時間(`p95_processing_duration_ms`)がWorkerのLambda Timeout(180秒)以内
- `batch_processing_timeout_hours`(既定24時間)以内に95%以上完了
- Terminal Failure率(`terminal_failure_rate_pct`)が5%未満

**`TransactionConflictException`について(2026-08-07追加)**: `WatchlistWorkerFunction`の
CloudWatch Logsで`TransactionConflictException`(`batch_tracker.py`の
`try_finalize_if_ready`等)が稀に記録されることがありますが、これは複数の
Worker(同時実行数`WatchlistReservedConcurrentExecutions`、既定3)がほぼ同時に
`jstock-batch_runs`テーブルの同一項目(`batch_id`)を更新しようとした際の
DynamoDB側の一時的な競合であり、`ConditionalCheckFailedException`と同様に
想定内の競合として捕捉・無視する扱いに修正済みです(2026-08-07修正)。
該当銘柄の評価結果自体は例外発生前に確定保存されているため失われず、SQSの
再送により数分以内に自動回復します。この文字列でCloudWatch Logsを検索して
頻発している場合のみ、`WatchlistReservedConcurrentExecutions`を下げるなどの
対応を検討してください。

**銘柄ごとのウォッチリスト登録結果の確認**: `decision_type=
watchlist_auto_addition_repository_result`のAuditLogに、`batch_id`ごとに
各銘柄が実際に追加された(`added`)・既に登録済みで見送られた(`skipped_existing`)・
追加件数上限外で見送られた(`skipped_over_limit`)・削除後の再追加クールダウン中
のため見送られた(`skipped_cooldown`、2026-08-15追加)・書き込みに失敗した
(`repository_failed`)のいずれかが記録されます。同じ`batch_id`の
`decision_type=watchlist_auto_addition_candidate_evaluation`(スクリーニング
評価結果)と突き合わせることで、ある銘柄がなぜ追加されなかったのかを追跡できます。

### 4.1.1 永続ローテーション・自動メンテナンス(2026-08-15追加)

**永続ローテーション**: 毎回固定300銘柄(先頭側のみ)しか評価していなかった
問題を解消するため、前回どこまで評価したかを`WatchlistScreeningRotationState`
テーブル(単一行、`rotation_id=default`)へ永続化し、次回はその続きから
評価する巡回方式に変更した。現在の巡回状況(何周目か・概算進捗・現在の
カーソル位置・次回選択プレビュー)は以下で確認できる。

```bash
jstock watchlist-screening rotation-status
```

`config/watchlist_screening_rules.yaml`の`rotation.enabled`を`false`にすると、
巡回を行わず旧来の固定300件スライス方式へフォールバックできる(移行時の
安全弁)。ローテーションの前進(コミット)は、その回の候補銘柄に対する
ランキング・ウォッチリスト追加・通知までの業務処理が確定した時点
(`_finish_batch()`到達時)にのみ行われる。個別銘柄の評価エラー(poison
stock)はローテーションの前進を妨げないが、finalize処理自体(ランキング
計算・ウォッチリスト書き込み)が技術的に失敗した場合(`FINALIZE_FAILED`)は
その回のローテーションは前進しない(次回同じwindowから再開する)。

**本番検証(2026-08-15)で発覚・修正した不具合**: 上記のローテーション前進
(`try_commit_rotation_advance`/`_commit_dynamodb`)は、`WatchlistScreeningRotationState`
テーブルの実際の保存形式(1項目全体を単一の`data`属性(JSON文字列)へ保存する、
`infrastructure/aws/dynamodb_store.py`の`DynamoDbCollectionStore`方式)と、
更新処理側が前提としていたスキーマ(`pointer_version`等を項目の
トップレベル属性として直接更新)が一致しておらず、DynamoDB上では
`ConditionExpression`が常に`ConditionalCheckFailedException`となり
**commitが恒久的に失敗していた**(=巡回が一度も前進していなかった)。
`data`属性全体の一致を条件とする条件付き更新へ修正し、既存の本番state
(移行不要)のままそのまま前進できるようにした。

あわせて、Dispatcherがほぼ同時に2回起動された場合、両方が同じ未前進の
cursorを読み同一rotation windowを二重にdispatchできる問題も本番検証で
確認された(rotation cursorのCASは「cursorの二重前進」は防ぐが「同じ
windowの二重選択・二重dispatch」自体は防げないため)。これを防ぐため、
`job_type="NEW_CANDIDATE_SCREENING"`かつ`rotation.enabled=true`の場合のみ、
候補選択前に専用の軽量lease(`WatchlistRotationDispatchLeaseTable`、
`infrastructure/aws/watchlist_rotation_dispatch_lease.py`、
`trade_detection_run_locks`テーブルと同じ単一行・条件付き更新パターン)を
取得するようにした。取得できなかった場合、Dispatcherは候補選択・SQS投入を
一切行わず`{"skipped": "rotation_dispatch_in_progress"}`を返し、監査ログへ
`block_reason=ROTATION_DISPATCH_ALREADY_IN_PROGRESS`として記録する。この
leaseはバッチが正常/異常いずれの終端状態(COMPLETED/COMPLETED_WITH_
NOTIFICATION_FAILURE/ABORTED/DISPATCH_FAILED/TIMED_OUT)に至った場合も
解放されるが、万一解放されなかった場合(Lambda異常終了等)も
`batch_processing_timeout_hours`(既定24時間)経過で自動的に失効し、次回の
取得を妨げない。rotation cursorのCAS(前進の排他制御)とこのdispatch lease
(同一windowの二重評価防止)は別責務であり、どちらか一方を欠かすと不具合が
再発するため両方を維持している。`WATCHLIST_MAINTENANCE`はこのleaseの対象外
(候補選択がrotation windowに依存しないため)。

**自動メンテナンス(自動削除)**: `registration_source=AUTO_SCREENING`の
銘柄のみを対象に再評価し、以下の条件に該当する銘柄を自動でウォッチリスト
から削除する(手動登録銘柄は対象外)。起動タイミングは2026-08-16改訂で
「毎週日曜07:00の独立実行」から「同日のNEW_CANDIDATE_SCREENINGの後続処理」
へ変更した(詳細は次項「起動方式の平日毎日化」参照)。

- **即時削除**: REIT/ETFへの分類変更・債務超過・継続企業の前提への重大な
  疑義のいずれか1つでも該当すれば1回の再評価で削除する。
- **3回連続非該当+最低継続期間**: 上記以外の理由による非該当が3回連続し、
  かつ最初に非該当となってから`minimum_not_qualified_span_days`(既定28日)
  以上経過した場合にのみ削除する(件数条件・期間条件は独立したAND条件)。
  2026-08-16の起動方式変更により実行頻度が週1回から平日毎日へ変わった
  ため、「3回連続」が実質的に意味する期間は従来の約3週間から最短で約3
  営業日相当へ短縮されている(閾値自体は変更していない。GitHub Issue
  「自動maintenance削除基準の閾値再評価」で実データ蓄積後の再評価を管理)。
- **長期確認不能**: データ取得エラー等で`maximum_unconfirmed_days`(既定
  180日)を超えて再評価できない場合、削除はせず`decision_type=
  watchlist_auto_removal`のAuditLogとCloudWatch Logsの警告記録に留める。

削除は`decision_type=watchlist_auto_removal`のAuditLogに理由とともに
記録される(LINE通知は行わない)。削除から`readd_cooldown_days`(既定30日)は
`WatchlistRemovalHistoryTable`(DynamoDB Native TTLで自動失効)により同一
銘柄の自動再追加をスキップする。この自動メンテナンスジョブは新規候補
スクリーニングと同じDispatcher/Worker/SQSキュー/毎時Reconcilerを共用しており
(SQSメッセージ本文の`job_type`で分岐)、専用のランキング・ウォッチリスト
書き込み・通知フェーズは持たず、`WatchlistScreeningRotationState`も一切
変更しない(ローテーションの前進は`job_type=NEW_CANDIDATE_SCREENING`
専用)。

**起動方式の平日毎日化(2026-08-16改訂・同日再修正)**: NEW_CANDIDATE_SCREENINGの
スケジュールを毎週土曜07:00から平日(月曜〜金曜)06:00へ変更した
(`infra/template.yaml`の`WeekdayMorning` ScheduleV2、`cron(0 6 ? * MON-FRI *)`、
日本の祝日は考慮しない)。これに伴いWATCHLIST_MAINTENANCEの独立した
定期実行(旧`SundayMaintenanceReview`)は廃止し、同日のNEW_CANDIDATE_SCREENING
バッチが**信頼できる状態で正常finalizeした場合のみ**、後続処理として
自動的に起動する方式へ変更した。

トリガーは`maybe_trigger_maintenance(batch_id, batch_item, now, config,
final_status)`が担い、`_finish_batch()`が`_maybe_commit_rotation()`の直後に
呼び出す。**再修正(High、2026-08-16)**: 当初は`_finish_batch()`へ到達した
かどうか(=`mark_watchlist_batch_completed()`が呼ばれたかどうか)のみで
起動可否を判定しており、`ABORTED`(429率・スコア項目欠損率等の閾値超過による
安全側の見送り判断)を含む全終端状態でトリガーされ得る不整合があった。
これはデータ品質が疑わしい状態のまま自動削除判定へ流れてしまう恐れがあった
ため、`final_status`を明示的な引数として受け取り、`WatchlistBatchStatus.
COMPLETED`/`COMPLETED_WITH_NOTIFICATION_FAILURE`の2状態のみを起動対象とする
よう修正した。`final_status`は、`_finish_batch()`側では
`batch_tracker.resolve_watchlist_batch_completion_status(execution_result,
notification_permanently_failed)`(`mark_watchlist_batch_completed()`自身が
使う判定ロジックと同一実装を共有)で、その回の`execution_result`から都度
計算した値を渡す(finalize処理の途中で取得した古い`batch_item`のstatus
フィールドは一切参照しない)。`ABORTED`・`DISPATCH_FAILED`・`TIMED_OUT`・
`FINALIZE_FAILED`のいずれも起動対象外(`DISPATCH_FAILED`/`TIMED_OUT`/
`FINALIZE_FAILED`は`_finish_batch()`へ構造的に到達しないためそもそも
呼ばれないが、`maybe_trigger_maintenance()`自体もこれらの`final_status`を
渡された場合は起動しない防御的なガードを持つ)。個別銘柄の評価エラー
(`FAILED_REQUIRED`/`FAILED_NO_TARGET_TYPE`/`NOT_FOUND`)は、その回の
業務finalizeが`COMPLETED`として正常完了する限りトリガーを妨げない。

`maybe_trigger_maintenance()`は`MaintenanceTriggerOutcome`(`TRIGGERED`/
`NOT_APPLICABLE`/`SKIPPED_LEASE_UNAVAILABLE`/`SKIPPED_LOCAL_EXECUTION`/
`CONFIGURATION_ERROR`/`INVOKE_FAILED`)を返す。運用監視・GitHub Issue #8の
観測でこの戻り値を使う。

**重複起動防止(exactly-once相当)**: `BatchRunsTable`の該当バッチ項目へ
`maintenance_trigger_status`(`NOT_TRIGGERED`→`TRIGGERING`→`TRIGGERED`)・
`maintenance_batch_id`(`f"watchlist-maint-{親batch_id}"`、決定論的に算出)・
`maintenance_trigger_lease_expires_at`を持たせ、`batch_tracker.
try_acquire_maintenance_trigger()`が既存の`try_acquire_dispatch_lease`/
`try_acquire_rotation_dispatch_lease`と同じ「lease期限切れなら再取得可」
条件付き更新パターンで起動権利を排他的に取得する(この段階に到達するのは
`final_status`が起動対象の場合のみで、`ABORTED`等では`maintenance_trigger_
status`自体が一切書き込まれない)。取得成功後、`boto3` Lambda `invoke()`
(`InvocationType="Event"`、非同期)で`WatchlistDispatcherFunction`自身を
`{"job_type": "WATCHLIST_MAINTENANCE", "batch_id": <maintenance_batch_id>,
"triggered_by_batch_id": <親batch_id>, "trigger_type":
"POST_NEW_CANDIDATE_SCREENING"}`ペイロードで自己invokeする。invoke成功後は
`mark_maintenance_triggered()`で`TRIGGERED`へ恒久確定し、以後同じ親バッチ
から二度と起動されない(戻り値`TRIGGERED`)。invoke自体が失敗した場合は
`TRIGGERING`のまま(lease期限120秒)残し(戻り値`INVOKE_FAILED`)、毎時
`WatchlistBatchReconcilerFunction`が`list_stale_maintenance_triggers()`経由で
lease失効を検知し`maybe_trigger_maintenance()`を再試行する(処理の消失を
防止)。子バッチ側の`batch_id`が親から決定論的に算出されるため、万一起動
権利の排他制御をすり抜けて`invoke()`が二重に発生しても、2回目は子バッチ
自身の`try_acquire_dispatch_lease`で棄却される二重の安全策になっている。
親バッチ側にも`maintenance_batch_id`・`maintenance_triggered_at`が記録
されるため、`get_watchlist_batch(親batch_id)`で子バッチへの追跡ができる
(子バッチ側は`triggered_by_batch_id`/`trigger_type`で親を追跡)。

ローカルCLI実行時は`running_on_lambda()`(`AWS_LAMBDA_FUNCTION_NAME`環境
変数の有無で判定)が偽になるため、起動権利の取得までは行うが実際の
Lambda `invoke()`は行わない(戻り値`SKIPPED_LOCAL_EXECUTION`。誤って本番
Lambdaを起動しない安全策。この場合`TRIGGERED`へは確定せず、`TRIGGERING`の
まま次回Reconcilerパスの対象になるため、ローカル検証後に本番実行すれば
正しく起動できる)。

**Reconciler再試行件数の計測(Medium修正、2026-08-16再修正・同日再々修正)**:
毎時Reconcilerの戻り値`maintenance_trigger_retried`は、`list_stale_
maintenance_triggers()`で取得した各バッチについて`maybe_trigger_
maintenance()`を呼んだ回数の単純カウントではなく、その戻り値
(`MaintenanceTriggerOutcome`)を見て**「実際にLambda invoke()を試行した
(=`TRIGGERED`/`INVOKE_FAILED`)」ケースのみ**を数える。4つのカウンタの
意味は以下のとおり(いずれも新規の永続DynamoDBカウンタは追加せず、
Reconciler実行1回分のin-memory集計のみをログ・戻り値(GitHub Issue #8の
ロールアウト観測で使用)に残す設計)。

- `maintenance_trigger_retried`: 実際にLambda invoke()を試行した回数
  (`TRIGGERED`+`INVOKE_FAILED`)
- `maintenance_trigger_retry_failed`: そのうちinvoke()自体が失敗した回数
  (`INVOKE_FAILED`のみ)
- `maintenance_trigger_retry_skipped`: lease競合(`SKIPPED_LEASE_
  UNAVAILABLE`、他の主体が先にleaseを再取得済み)・`NOT_APPLICABLE`等で
  invoke()を試行しなかった回数
- `maintenance_trigger_retry_configuration_error`: leaseの再取得には
  成功したが、起動先関数名の環境変数(`WATCHLIST_DISPATCHER_FUNCTION_
  NAME`)未設定等の設定不備によりLambda invoke()呼び出し自体に到達しな
  かった回数(`CONFIGURATION_ERROR`)

**再々修正(2026-08-16)**: 当初`CONFIGURATION_ERROR`も`maintenance_trigger_
retried`(および`retry_failed`)へ含めていたが、`CONFIGURATION_ERROR`は
invoke()呼び出し前に終了するケースであり、「実際にinvoke()を試行した
件数」という`retried`の定義と矛盾していたため、専用カウンタ
(`maintenance_trigger_retry_configuration_error`)へ分離した。

**スクリーニング高速化(計測のみ、2026-08-15追加)**: `WatchlistCandidateProgressTable`の
各行へ`data_fetch_duration_ms`/`scoring_duration_ms`(データ取得・判定計算の
所要時間)を記録するようになり、`record_batch_audit`のfinalize集計へ
p50/p95・平均値が追加された。判定に必要な最小限の項目のみ取得する軽量版
Provider(`LightweightScreeningDataProvider`)も実装済みだが、
`config/watchlist_screening_rules.yaml`の`screening_data_provider`の本番既定値は
引き続き`stock_snapshot`のまま(`lightweight`への切替は同値性検証後に別途判断)。

---

## 5. 週次・月次・四半期レビュー

**2026-08 振り返り機能改修**: 従来の週次・月次の全期間合算成績レポート自動送信、
四半期の固定リマインド送信は廃止した。詳細はdocs/functional_spec.md 12.4節参照。

| 頻度 | schedule.yamlのジョブ | 対応Lambda関数 | 挙動 |
|---|---|---|---|
| 週次(月19:00) | `weekly_review` | `WeeklyReviewFunction` | 前週(月〜日 JST)に確定した7暦日評価を分析し、改善候補を検出。GitHub Issue作成成功時のみLINE通知(5.1節) |
| 月次(第1土10:00) | `monthly_review` | `MonthlyReviewFunction` | 内部記録(ログ)のみ。LINE送信なし |
| 四半期(1,4,7,10月第1土11:00) | `quarterly_logic_review` | `QuarterlyReviewFunction` | 内部記録(ログ)のみ。LINE送信なし |

全期間合算の成績を手動で確認したい場合は、引き続き
`jstock review report --notify`(LINE送信)または`jstock review report`
(標準出力のみ)を使う。必要に応じ`jstock performance summary --horizon <N>`で
特定ホライズンのみ確認できる。

ローカルCLIには「当月第1土曜日か」を判定する処理はありません。AWS Lambda版
(`MonthlyReviewFunction`/`QuarterlyReviewFunction`)は毎週土曜に起動したうえで、
`lambda_handlers/_scheduling.py`が当月第1土曜日かどうかを内部判定し、
戻り値(`is_monthly_review_day`/`is_quarterly_review_day`)に含めるが、
いずれの場合もLINE送信は行わない。`QuarterlyReviewFunction`はルール改善提案
(リスク影響・過学習リスク評価等の自由記述を要する)を自動生成しない
(要求仕様45節の人間承認必須の原則のため)。実際の`rules backtest`/
`rules propose`は利用者が手動で実行すること(第7節参照)。

### 5.1 GitHub Issue自動起票の設定(振り返り機能改修、2026-08追加)

週次改善レビュー(`WeeklyReviewFunction`)は、改善候補が十分な証拠とともに
検出された場合にGitHub Issueを自動作成する。この機能を有効化するには、
以下の手順が必要(**GitHub App本体の作成・インストールは本システムが
代行できないため、必ず利用者自身がGitHub UI上で行うこと**)。

1. GitHub Developer Settingsで新しいGitHub Appを作成する。権限は最小限
   (`Repository permissions > Issues: Read and write`、
   `Repository permissions > Metadata: Read-only`)のみ付与する
   (`Contents`等の書き込み権限は不要。本機能はコードを書き換えない)。
2. 対象リポジトリへこのGitHub Appをインストールする(Installation IDが
   発行される)。
3. GitHub Appの秘密鍵(.pemファイル)を生成・ダウンロードする。
4. AWS Secrets Managerへ、以下のJSON形式でシークレットを作成する
   (キー名は固定):
   ```bash
   aws secretsmanager create-secret \
     --name jstock/github-app \
     --secret-string '{"app_id":"<App ID>","installation_id":"<Installation ID>","private_key":"<.pemファイルの中身をそのまま>"}'
   ```
5. 作成したシークレットのARNを`infra/samconfig.toml`の
   `parameter_overrides`へ`GithubAppSecretArn="<ARN>"`として追加し、
   対象リポジトリ("owner/repo"形式)を`GithubRepository="<owner>/<repo>"`
   として追加する。
6. `config/review_improvement.yaml`の`issue_creation_enabled`を`false`から
   `true`へ変更する。
7. `sam build && sam deploy`で再デプロイする(**重要**:
   `config/review_improvement.yaml`はLambda Layer経由で配布される静的設定
   ファイルであり、YAML編集だけでは反映されない。必ず再デプロイが必要)。

上記1〜7が完了するまでの間は、`issue_creation_enabled=false`のままで安全に
運用できる(GitHub API・Secrets Managerへは一切アクセスせず、改善候補の検出・
内部記録のみ継続する。エラー扱いにも運用エラー通知にもならない)。

**動作確認・トラブルシューティング**: 改善候補・Issue対応状況はDynamoDBへ
直接記録される(CLIは今回未整備)。
```bash
# その週に検出された改善候補一覧
aws dynamodb scan --table-name jstock-improvement_candidates

# candidate_key単位のGitHub Issue対応状況(status: CANDIDATE/
# SKIPPED_NOT_CONFIGURED/CONFIGURATION_ERROR/ISSUE_CREATING/ISSUE_CREATED/
# ISSUE_CREATION_FAILED)
aws dynamodb scan --table-name jstock-improvement_tasks

# 週次の実績集計(Candidateの有無に関わらず毎週保存される)
aws dynamodb scan --table-name jstock-weekly_review_metrics
```
`status=CONFIGURATION_ERROR`が継続する場合、Secrets Managerの値
(app_id/installation_id/private_keyの3項目すべて)・GitHub App権限
(Issues: Read and write)・`GithubRepository`パラメータの"owner/repo"形式を
確認すること。`status=ISSUE_CREATION_FAILED`はGitHub API側の一時的な障害
(5xx・タイムアウト・レート制限等)の可能性が高く、翌週の週次レビューで
自動的に再試行される。

### 5.2 判定精度向上機能・自己評価基盤(Phase A)の運用(2026-08追加)

買い候補・売却・保有判断・利益確定の各判定が確定するたびに、その時点の
最終判断値をDecisionSnapshotとして自動記録する(詳細はdocs/functional_spec.md
12.5節参照)。運用者が個別に設定・起動する必要はなく、既存の
`BuyCandidatesFunction`/`HoldingsWatchlistFunction`(またはローカル実行時は
`jstock analyze buy-candidates`等)の実行に付随して自動的に動作する。

**記録された判断の成績確認**:
```bash
# 記録済み全DecisionSnapshotの成績(件数・成功率・平均/中央値リターン・平均MFE/MAE)
jstock decision-performance summary

# 特定ホライズン(営業日数)のみに絞り込む場合
jstock decision-performance summary --horizon 60
```
本コマンドが集計する対象は、既存の振り返り機能(12.1節)が既に算出済みの
EvaluationResultのうち、`config/decision_evaluation.yaml`の
`horizons_business_days`(既定5・20・60・120・250営業日)に含まれる行のみ。
専用の振り返り処理を別途動かすものではないため、本コマンドを実行しても
新たな株価取得・LINE通知は発生しない。

**スコア別の詳細分析(2026-08追加、functional_spec.md 12.10節)**:
```bash
# 過去バリュエーション比較スコアをカテゴリ・信頼度・カバレッジ・
# model_version別に分析(--horizonは必須)
jstock decision-performance segments --score historical_valuation --horizon 60

# 2つのスコア範囲グループの成績を比較(範囲が重複する場合はエラー終了)
jstock decision-performance compare --score timing \
  --label-a "TAILWIND寄り" --min-a 20 \
  --label-b "HEADWIND寄り" --max-b -20 --horizon 60
```
`--score`には`historical_valuation`/`timing`/`earnings_surprise`/
`earnings_trend`/`market`/`sector`/`environment`のいずれかを指定する。
分析は各DecisionSnapshotに保存された「判定当時に実際に使用した設定値」
のみを使い、現在の設定・現在のカテゴリ定義では再解釈しない。

```bash
# 市場全体の地合いスコアをカテゴリ・信頼度・カバレッジ・model_version別に分析
jstock decision-performance segments --score market --horizon 60

# 所属セクターの地合いスコア(functional_spec.md 12.12節)。sector_etf_mapに
# 対応が無い業種(NOT_APPLICABLE)・データ不足で今回は算出できなかった業種
# (NOT_EVALUATED)はいずれも自動的に対象dimensionから除外される
jstock decision-performance segments --score sector --horizon 60

# 市場+セクターを統合したEnvironment Composite Score
jstock decision-performance segments --score environment --horizon 60
```
`environment`スコアも独自のcoverage閾値(`min_coverage_required`/
`coverage_high_threshold`/`coverage_medium_threshold`)を持つため
(コードレビュー対応、2026-08)、`segments --score environment`の
coverage tier別分析が実際に機能する。本番運用では`sector_etf_map`が
未整備のため所属セクターのスコアは全銘柄でNOT_APPLICABLEとなり、
Environment Compositeは実質的にMarketのみのcoverageで判定され続ける
点に留意すること(functional_spec.md 12.12節「既知の制約」参照)。

**保存失敗時の確認方法**: DecisionSnapshotの保存に失敗した場合、
CloudWatch Logsに固定イベントキー`decision_snapshot_save_failed`
(`stock_code`/`recommendation_id`/`decision_type`付き)でWARNINGログが
記録される(`BuyCandidatesFunction`/`HoldingsWatchlistFunction`のロググループを
このキーで検索・メトリクスフィルタ可能)。

**記録の不変性(2026-08再レビュー対応)**: DecisionSnapshotは一度保存されたら
後から絶対に上書きされない(insert-only)。同じ判定の保存処理が偶然もう一度
走った場合、記録内容が完全に同一であれば何もしない(正常な冪等再実行)。
万一、同じ判定のはずなのに記録内容が食い違う異常なケース(想定される原因は
ほぼ無いが、不正なデータ操作等)を検知した場合、既存の記録をそのまま保持し
(新しい値では上書きしない)、CloudWatch Logsに固定イベントキー
`decision_snapshot_conflict`(`stock_code`/`recommendation_id`/`decision_id`/
`decision_type`付き)でWARNINGログを残す。`decision_snapshot_save_failed`
(ストレージ障害等の予期しない失敗)とは原因が異なるため、イベントキーを
分けて検索できるようにしてある。いずれの場合も既存の買い候補判定・売却判定・
保有判断・利益確定判定やLINE通知には一切影響しない。

**成績集計側の異常データ検知**: `jstock decision-performance summary`の集計対象は
「1件の判定につきDecisionSnapshotは常に1件」を前提としている。万一この前提に
反するデータが混入した場合、集計結果が不安定にならないよう該当の判定は集計から
除外され、CloudWatch Logsに固定イベントキー`decision_performance_duplicate_snapshot`
(`recommendation_id`付き)でWARNINGログが残る。

**既存機能への影響について**: 本機能はShadow計測基盤であり、
(1) LINE通知の内容・頻度には一切変更がなく通知件数も増えない、
(2) DecisionSnapshotの保存に失敗しても、買い候補判定・売却判定・
保有判断・利益確定判定やLINE通知の送信は一切ブロックされない
(失敗は上記CloudWatchログにのみ記録される)。

---

## 6. 実売買記録(随時)

推奨に基づいて実際に売買した場合、または見送った場合に記録します
(この記録は保有銘柄の自動更新とは独立しています。保有銘柄自体は引き続き
`jstock holdings add` 等で別途更新してください)。

```bash
jstock transactions buy-executed 2914 100 3400 --recommendation-id <推奨ID>
jstock transactions sell-executed 2914 50 4600 --recommendation-id <推奨ID>
jstock transactions skip-recommendation <推奨ID> --reason WAITED_FOR_EARNINGS
jstock transactions list
```

### 6.1 LINEチャットからの登録(AWSデプロイ時のみ)

AWSデプロイ後、LINE Webhookを設定していれば、CLIを開かずLINEのトーク画面から
以下のCSV形式のメッセージを送るだけで売買記録・ウォッチリスト登録ができます
(誤登録を防ぐため固定フォーマットのみ対応。自由文解析は行いません)。

```
買付,2914,100,3400
売却,2914,50,4600
ウォッチ,7203
```

送信すると同じトークに結果が返信されます。本人(LINE_USER_IDに設定したアカウント)
以外からのメッセージは無視されます。推奨IDとの紐付けは行われないため
(CLIの`--recommendation-id`相当の機能は無い)、推奨との価格差分析が必要な場合は
引き続きCLIの`transactions buy-executed`/`sell-executed`を使用してください。

「買付」「売却」は売買記録(実行結果ログ)の登録に加えて、保有銘柄データ
(`holdings`/購入ロット)も同時に更新します。売却はFIFO(購入日が古いロットから)
で消費され、保有株数を超える売却は登録前に拒否されます。

---

## 7. ルール改善承認フロー(四半期レビュー等で発生)

改善提案から実際の設定反映までは、必ず以下の順序で人間の承認を経ます。
**どの段階でも自動適用は行われません。**

```bash
# 1. 感応度分析(対応: screening.total_yield.min_total_yield_pct のみ。現行値→提案値の方向は「厳しくする」向きのみ対応)
jstock rules backtest screening.total_yield.min_total_yield_pct 3.5 4.0

# 2. 改善提案の作成(評価件数が閾値未満だとエラーになります: 閾値変更60件/それ以外30件)
jstock rules propose screening.total_yield.min_total_yield_pct 3.5 4.0 \
  --reason "..." --risk-impact "..." --overfitting-risk "..." --rollback-condition "..."

# 3. 提案の承認申請・承認(人間の判断)
jstock rules submit-proposal <proposal_id>
jstock rules approve-proposal <proposal_id>

# 4. 新ルールバージョンの作成・承認・有効化(人間の判断)
jstock rules create-version v2-mvp --description "..." --reason "..." --previous-version v1-mvp
jstock rules submit-version v2-mvp
jstock rules approve-version v2-mvp --approved-by <承認者名>
jstock rules activate-version v2-mvp

# 5. ★config/*.yamlへの実際の値の反映は自動化されていません。手動で編集してください
#    (例: screening_rules.yaml の min_total_yield_pct を 4.0 に変更)
```

---

## 8. 監査ログ・振り返り

```bash
jstock audit show <銘柄コード>                 # 判定の入力値・計算式・出力値・出典を確認
jstock evaluation list --recommendation-id <推奨ID>  # 定点評価結果の確認(暦日7日評価も同コマンドで確認できる)
jstock feedback add --recommendation-id <推奨ID> --satisfaction-score 4
```

週次改善レビュー(5.1節)は`decision_type=weekly_improvement_review`として
AuditLogへ毎週1件記録される(対象件数・joinできた件数・
`weekly_review_recommendation_missing_count`等の欠損件数・検出したCandidate数・
GitHub連携の結果内訳を含む)。`jstock audit show`は銘柄コード単位の検索のため、
週次レビューの監査ログはDynamoDB(`jstock-audit_log`テーブル)を
`decision_type`でフィルタするか、直接スキャンして確認すること。

---

## 9. 既知の制約事項

| 項目 | 制約 |
|---|---|
| 適時開示(決算短信) | TDnet専用のためEDINETからは取得不可。取得できるのはEDINET臨時報告書(代表者異動・特定子会社異動・財務コベナンツ等)のみ |
| 適時開示チェックの対象範囲 | 保有銘柄のみが対象(ウォッチリストは対象外)。EDINET臨時報告書のみで、TDnet速報自体は取得不可 |
| 株主優待 | 自動取得不可。必ず手動/CSV登録が必要 |
| バックテスト | `screening.total_yield.min_total_yield_pct`のみ対応。それ以外のターゲットは「データ不足」扱い。かつ閾値を緩める方向は生存バイアスにより検証不可 |
| 定点評価のtotal_return | 配当・優待込みの正確な総合リターンは未算出(株価ベースのリターンのみ) |
| EvaluationLabelの自動付与 | LATE / PROFIT_TAKE_TOO_LATE は自動付与されない(推奨前の価格推移データを保持していないため) |
| `--source real`時の全銘柄スキャン | `analyze buy-candidates --source real` は対象銘柄コードの指定が必須(mock時のみ全銘柄自動) |
| 月次・四半期の「第1土曜日」判定 | CLI側では未実装(Lambda版は`_scheduling.py`で判定)。CLIで手動実行する場合は実行タイミングを利用者が判断 |
| `BuyCandidatesFunction`のスキャン対象 | 全上場銘柄の自動スクリーニングではなく、ウォッチリスト登録銘柄のみを対象とする(市場全体をスキャンする実データ取得元が未接続のため) |
| `QuarterlyReviewFunction` | ルール改善提案の自動生成は行わない(人間承認が必須な自由記述項目があるため)。レビュー時期のLINEリマインドのみ |
| LINEチャット登録(6.1節) | 推奨ID・手数料・税額・メモ等は指定不可(最小限の項目のみ)。詳細な記録はCLIを使用 |
| 決算発表の実施確認(2026-08-06追加、2026-08-07改訂×3) | 無償データ(yfinance)のみを利用しており、TDnet等の有償APIは導入していないため、決算が実際に発表されたかどうかをシステムが自動で確定することはできない(常にUNCONFIRMED相当)。内部区分`EarningsDateStatus.CONFIRMED`は「取得できた決算予定日が過去日ではない」という意味であり、「発表が確認された」という意味ではない。財務データの更新有無から間接的に推定するのみで、その際`FinancialSummary.fiscal_period_end`は年次決算の期末日を表す(直近四半期の期末日ではない)ため、四半期反映確認には四半期実績データ(取得できる場合)を優先し、取得できない銘柄でのみ年次決算期末日を代替とする。ただし`recent_quarters`という名称だけでは四半期データ由来とは限らず、データ提供元から四半期単位のデータを取得できない場合は年次決算データへの振り替えである場合がある(`FinancialSummary.recent_periods_source`/監査ログの`financial_period_end_source`で区別可能。年次振り替えの場合、期中決算の反映は検知できない既知の制約が残る)。財務データの取得時刻(fetched_at)は取得元へのAPI呼び出し時刻に過ぎず、それだけでは「更新済み」と判定しない。財務期間を確認できない場合は安全側に「確認待ち」の状態のままとする。不明な由来を四半期データと推測することはしない。監査ログの`financial_period_end_source=UNKNOWN`、またはCloudWatch Logs上の`financial_period_source_inconsistent`という警告ログが確認された場合は、通常運用では起こらないデータ不整合(四半期実績データと年次代替のいずれとも判別できない状態)を示すため、データ整合性の確認対象とする |

---

## 10. 保有判断スコア方式の運用(2026-08-06追加)

保有銘柄の「投資した前提が崩れていないか」の判定を、従来方式
(`SellSignalService`)から新方式(保有判断スコア、`HoldingDecisionService`)へ
段階的に切り替えるための運用手順です(判定方式自体の考え方は
[機能仕様書6.9節](functional_spec.md)を参照)。利益確定(利確)判定は対象外で、
常に従来どおりです。

### 10.1 RuntimeConfigの初回作成

新方式の稼働モード(`mode`)・kill switch(`notification_enabled`)は、
再デプロイ不要で切り替えられるよう専用のRuntimeConfigレコード(DynamoDB、
ローカル運用時はJSONファイル)で管理します。**運用開始前に必ず1回だけ**
初期化してください(2回目以降はエラーになります)。

```bash
jstock holding-decision init-runtime-config --changed-by <あなたの名前> --mode legacy
# 本番(AWS)環境に対して初期化する場合は --target aws を追加
jstock holding-decision init-runtime-config --changed-by <あなたの名前> --mode legacy --target aws
```

既定は`mode=legacy`(現行と完全同一動作)・`notification_enabled=False`
(kill switch ON相当)です。現在の設定は次のコマンドで確認できます。

```bash
jstock holding-decision show-runtime-config --target aws
```

### 10.2 Shadow運用手順(新旧を並行計算し、通知は旧方式のみ)

```bash
jstock holding-decision set-mode shadow --changed-by <あなたの名前> \
  --reason "新方式の並行検証を開始" --target aws
```

`mode=shadow`にすると、実際のLINE通知は引き続き旧方式のみが行いますが、
新方式(`HoldingDecisionService`)も毎回計算・保存されるようになります
(`HoldingDecisionResult`)。数日〜数週間このモードで運用し、10.6節の
`compare`コマンドで新旧の判定差分を定期的に確認してください。

### 10.3 Active切替手順(新方式が実際の通知を担当する)

Shadow運用で新旧の乖離に問題が無いことを確認できたら、本稼働へ切り替えます。

```bash
jstock holding-decision set-mode active --changed-by <あなたの名前> \
  --reason "Shadow検証完了、本稼働へ切替" --target aws
```

`mode=active`にすると、一般事業会社の銘柄は新方式が実際の通知を担当し、
旧方式(`SellSignalService`)は通知を出さなくなります(判定自体は行われなく
なります)。**銀行・保険・証券などの金融業銘柄は、`mode=active`に切り替えた
後も自動的に旧方式のまま**です(10.5節)。

ロールバックは`set-mode legacy`の1コマンドで即座に行えます(再デプロイ不要)。

```bash
jstock holding-decision set-mode legacy --changed-by <あなたの名前> \
  --reason "問題を確認したため旧方式へ戻す" --target aws
```

### 10.4 kill switch運用(緊急停止)

`mode`とは独立して、保有銘柄分析に関するLINE通知を即座に停止できる緊急
スイッチです。`mode`を切り替えずに「今すぐ通知だけ止めたい」場合に使います。

```bash
jstock holding-decision kill-switch on --changed-by <あなたの名前> \
  --reason "誤判定の疑いがあるため一時停止" --target aws
# 解除
jstock holding-decision kill-switch off --changed-by <あなたの名前> \
  --reason "原因を確認し再開" --target aws
```

**`kill-switch on` ↔ `notification_enabled`の対応関係**(取り違えやすいため明記):

| CLI指定 | 内部値 | 意味 |
|---|---|---|
| `kill-switch on` | `notification_enabled=False` | 通知停止 |
| `kill-switch off` | `notification_enabled=True` | 通知許可(既定) |

**停止対象(コードレビュー対応で全経路へ適用範囲を拡張、2026-08版)**:

- 旧売却通知(SellSignalService)
- 新保有判断通知(HoldingDecisionService)
- 利確通知(ProfitTakingService)
- ポートフォリオ集中リスク通知(PORTFOLIO_CONCENTRATION_REVIEW)
- 保有銘柄分析バッチ完了サマリー通知

**停止しないもの**: 判定処理そのもの・Recommendation/HoldingDecisionResultの
保存・監査ログの記録。空振りにはならず、kill switch中でも通常どおり
Recommendationは作成・保存されます(LINE送信だけが行われません)。

`mode`等の他の設定値は、DynamoDBの読み取り頻度を抑えるため60秒
(`runtime_config_cache_ttl_seconds`)キャッシュされますが、**kill switchの
状態だけはこのキャッシュを経由せず、判定のたびに必ず最新値を取得します**
(緊急停止操作が最大60秒遅れて反映される事態を避けるため)。切り替え後は
次回の判定サイクルから確実に反映されます。

**kill switch抑止状態の可観測性の制約**: kill switchにより送信を見送った
事実そのものは、CloudWatch Logsの構造化ログ(`kill_switch_suppressed: ...`)
以外には永続化されません。`NotificationLog`は実送信成功時にのみ書き込まれる
既存仕様のため、`backtest`コマンドのhistory replayでは「送信ログが無い」
ケースを`UNKNOWN`としてしか判定できず、「kill switchにより抑止された」と
断定することはできません(抑止か記録漏れかを過去データから区別する手段が
現状無いため)。将来この区別をhistory replayで確定表示したい場合は、
専用の`NotificationAttempt`/監査テーブルの新設が別途必要です(現時点では
未実装、残課題)。

### 10.5 金融業移行手順

銀行・保険・証券・その他金融業の銘柄は、`BankRegulatoryMetrics`(自己資本
比率規制等)を評価する専用データソース・専用モデルが未実装のため、
`mode=active`に切り替えた後も**当面は自動的に旧方式のまま**通知を継続します
(`config/industry_scoring_policy.yaml`の`financial_industry_policy`が正の
設定元)。新方式はこれらの銘柄についてもshadow相当で計算・保存は継続し、
将来のモデル検証データとして蓄積されます。

金融業を新方式へ移行するには、以下がすべて完了している必要があります
(現時点ではいずれも未着手です)。

1. 専用データソースの実装(`BankRegulatoryMetrics`の実データ取得)
2. 専用スコアリングモデルの実装・`financial_model_version`の採番
3. 最低1四半期程度のshadow運用相当での試験運用・人間レビュー
4. `config/industry_scoring_policy.yaml`の該当カテゴリの`deferred: false`への
   変更(コード変更を伴うためデプロイが必要)

**緊急退避**: 何らかの理由で金融業の判定に問題が疑われる場合、
`financial_policy_override`を`FORCE_DEFER_ALL`にすると、YAML側の設定に
関わらず全金融業カテゴリを即座に旧方式へ退避させられます(再デプロイ不要)。

```bash
jstock holding-decision init-runtime-config --changed-by <あなたの名前> \
  --mode active --financial-policy-override FORCE_DEFER_ALL --target aws
```

既に初期化済みの場合は`set-mode`と同様、`get-config`で現在値を取得してから
`update_config`相当の操作が必要です(現状CLIに`financial-policy-override`
単体を変更するコマンドは無く、`init-runtime-config`の初回作成時のみ指定
可能です。運用中に変更したい場合はPythonから直接
`HoldingDecisionRuntimeConfigService.update_config()`を呼び出してください)。

### 10.6 compare実行方法(Shadow比較レポート)

Shadow運用中に新旧の判定差分を確認するためのコマンドです。指定銘柄
(または全保有銘柄)を現在のデータで両エンジンにかけ、判定・score・
通知差分に加えて、coverage・ハードゲート・主な加点/減点理由を表示します。

```bash
jstock holding-decision compare --stock-code 2914 --stock-code 8306
# 保有銘柄すべてを対象にする場合は --stock-code を省略
jstock holding-decision compare
# 実データで比較する場合(既定はmock)
jstock holding-decision compare --source real
# CSVへ出力
jstock holding-decision compare --csv compare_result.csv
```

**列名の意味(コードレビュー対応で改名、2026-08版)**: `legacy_should_notify`/
`new_should_notify`は「実際に通知したか」ではなく「通知条件に該当するか」を
表します(compareはliveモードのみで何も送信しないため)。`should_notify_diff`は
`MATCH`(一致)/`DIFFERENT`(不一致)/`NOT_COMPARABLE`(比較不能)の三値です。
非保有銘柄では旧方式を評価しないため`legacy_should_notify`が`None`となり、
その場合`should_notify_diff`は必ず`NOT_COMPARABLE`になります(bool比較による
誤った差分表示を避けるための設計)。

出力の「差分」欄が「一致」以外(旧のみ検討/新のみ検討)の銘柄は、判定根拠
(「主な減点要因」「保有を支持する要因」)を確認し、必要であれば
`config/holding_decision_rules.yaml`等の閾値調整を検討してください
(調整自体は本書7節のルール改善承認フローに準じ、根拠を残しながら行うことを
推奨します)。

### 10.7 バックテスト手順

過去に実際に保存された判定結果を再生する`backtest`コマンドです。**このシステムは
財務・配当・優待データを現在値としてのみ保持しており、過去の任意時点の
財務スナップショットは保存していないため、真の意味での過去時点シミュレーション
はできません**(Phase0前提)。

```bash
# liveモード(--start-date省略時): 指定銘柄を現在のデータで新旧比較
jstock holding-decision backtest --stock-code 2914

# replayモード(--start-date指定時): 過去に保存された評価結果を期間指定で再生
jstock holding-decision backtest --start-date 2026-08-01 --end-date 2026-08-31

# 保有銘柄すべてを対象にCSV出力
jstock holding-decision backtest --csv backtest_result.csv
```

replayモードは`mode=shadow`で運用した蓄積データが無い期間を指定すると、
推測で埋め合わせず素直に「該当するデータがありません」と表示します。
運用開始直後で蓄積が無い場合は、まずliveモードで現状の判定を確認してください。

**非保有銘柄の扱い**: liveモードの非保有銘柄は、旧方式(SellSignalService)を
評価しません(架空の取得単価・保有期間による誤評価を防ぐため)。
`legacy_recommendation_type=NOT_EVALUATED_NON_HOLDING`と表示されます。
新方式は取得単価等を入力に使わないため非保有銘柄でも評価されます。
単一銘柄指定時に限り、以下のオプションをすべて指定することで旧方式も
評価できます(一部のみの指定・複数銘柄指定・replayモードとの併用はエラーに
なります)。

```bash
jstock holding-decision backtest --stock-code 2914 \
  --purchase-price 1500 --purchase-date 2024-01-15 --shares 100
```

この仮の保有データはどのRepositoryへも保存されません(検証専用)。

**history replayの対応付け(コードレビュー対応で全面再設計、2026-08版)**:

旧方式のRecommendationにはHoldingDecisionResultから参照できるFK(ID)が
存在しないため、以下の優先順位で対応付けます。

1. 近接時刻(評価時刻との差が5分以内)による対応付け(`NEAREST_TIMESTAMP`)
2. 同一日(JST基準の暦日)による対応付け(`SAME_DAY_FALLBACK`。**既定では
   無効**。`--allow-same-day-fallback`を指定した場合のみ有効になり、信頼度は
   中程度として扱われます。有効化した場合、`SAME_DAY_FALLBACK`行はActive
   移行判断の集計から除外してください)
3. いずれも一意に定まらない場合は対応付けを行わず`AMBIGUOUS_MATCH`とする
   (近接時刻内・同一日のいずれかに複数候補がある場合。最も近い1件を
   自動採用することはしません)

対応付け候補が全く見つからない場合(`NO_MATCH`)、`execution_plan_reason`から
「旧方式がそもそも実行されなかった」(`mode=active`の一般事業会社)ことが
分かる場合のみ`legacy_should_notify=False`と確定します。旧方式が実行予定
だった(`legacy/shadow`モード等)にもかかわらず候補が見つからない場合は、
`HoldingEvaluationAudit`(実行完了の証跡)が永続化されていないため過去データ
から実行完了を証明できず、`legacy_recommendation_type=UNKNOWN_NO_MATCH`
として「HOLDだった」と断定しません。

新方式側は`HoldingDecisionResult.recommendation_id`という明示的なFKがある
ため対応付けは決定論的ですが、IDが設定されていてもRecommendationの保存が
失敗・欠落している可能性があるため実在確認まで行います。
`RECOMMENDATION_ID_MISSING`(レコード欠落または銘柄コード不一致)、
`RECOMMENDATION_ID_TYPE_MISMATCH`(recommendation_typeが新方式の想定型と
不一致)という2種類のデータ不整合を区別して表示します。

**Recommendation作成と通知実績の分離**: `*_recommendation_created`(作成有無)
と`*_notification_sent`(実送信成功有無)は別の概念です。`NotificationLog`は
実送信成功時にのみ書き込まれるため、Recommendationは作成されたが送信ログが
無い場合は`*_notification_status=UNKNOWN`とし、`False`(未送信と確定)とは
判定しません(kill switch抑止・記録漏れ等を過去データから区別できないため)。
liveモードは何も永続化・送信しないため、`*_recommendation_created`は常に
`False`、`*_notification_status=NOT_EXECUTED_LIVE_MODE`となります。

---

## 11. `infra/template.yaml`へDynamoDBテーブルを追加する際の注意(2026-08-14追加)

`BuyCandidatesFunction`/`HoldingsWatchlistFunction`のように多数のテーブルへ
アクセスするLambda関数へ`Policies:`で`DynamoDBCrudPolicy`/`DynamoDBReadPolicy`
をテーブルごとに個別指定すると、SAMはエントリごとに個別のインラインIAM
ポリシー(`AWS::IAM::Policy`)を生成します。テーブルが増えるたびにロールへ
新規インラインポリシーが積み上がり、IAMロールのインラインポリシー合計
サイズ上限(10240バイト、拡張不可のハード上限)を超過すると、
`sam deploy`が`UPDATE_ROLLBACK_COMPLETE`で失敗します
(`HandlerErrorCode: ServiceLimitExceeded`)。`BuyCandidatesFunction`/
`HoldingsWatchlistFunction`/`WatchlistDispatcherFunction`/
`WatchlistWorkerFunction`/`WatchlistBatchReconcilerFunction`(2026-08-15、
rotation dispatch leaseテーブル追加を機に集約)は既に個別指定をやめ、
同じアクション集合(CRUD/Read)のテーブル群を`Statement:`の`Resource`配列へ
集約する形へ変更済みです。**今後これらの関数へ新しいテーブルへのアクセスを
追加する場合は、新規に`DynamoDBCrudPolicy`等のエントリを追加するのではなく、
既存の集約Statement(`DynamoDbCrudAccess`/`DynamoDbReadOnlyAccess`)の
`Resource`配列へ`!GetAtt <Table>.Arn`と`!Sub "${<Table>.Arn}/index/*"`を
追記してください**(付与するアクション自体は変更しない)。
`WatchlistTerminalFailureHandlerFunction`はテーブル数がまだ少ないため
個別指定のままですが、今後大きく増える場合は同様の集約が必要になります。
`sam deploy --no-execute-changeset`でchangesetを事前作成し、実行前に
`Replacement`列がすべて`False`であることを確認してから
`aws cloudformation execute-change-set`で適用する運用を徹底してください
(この事故は`sam deploy`の対話的confirm_changesetをバイパスせず、
changesetの中身を人間が確認していれば防げた種類の問題ではなく、
IAM側のサイズ上限はCloudFormation実行時まで判明しないため、事前の
`sam validate`だけでは検知できません)。
