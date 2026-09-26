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

### 3.5 買付余力(available cash)の参照・棚卸し更新(2026-09-26追加、Issue #594)

所有者(owner)単位の買付余力を、証券会社等の実額と照合して登録・確認します。
自動取得できないデータのため、実際の残高と乖離しないよう定期的に棚卸し
(reconcile)してください。**未登録owner(過去に一度も棚卸ししていない)と
0円(棚卸しした結果0円だった)は明示的に区別されます**(#584/#589の契約)。

```bash
jstock available-cash show --owner 本人
jstock available-cash reconcile --owner 本人 --amount 500000
```

`reconcile`は絶対値での上書きです(前回値との差額計算は行いません)。
負の金額は拒否されます。LINE経由の棚卸し操作は別Issue(#592)で開発中です。
本コマンドはローカル専用であり、Lambda/IAMを必要としません
(`AWS_LAMBDA_FUNCTION_NAME`環境変数の有無に関わらず常にローカルストアのみを
読み書きします)。

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
| 18:00 | `point_in_time_evaluation` | `jstock evaluation run --source real` | `EvaluationFunction` | 評価期限(営業日数)を迎えた推奨のみ処理。通知機能は無く、結果はコンソール/CloudWatch Logs表示のみ。Timeout 900秒(Issue #113。残時間に余裕を残して自主的に切り上げる) |

### 実行結果の確認ポイント

- `[DATA_ERROR]` と表示された銘柄は、データ取得に失敗し判定を出せなかったことを示します(推測での補完はしていません)
- 買い候補が無い日は「本日買いを検討すべき銘柄はありませんでした。」と表示されます(異常ではありません)
- `--notify` 時、前回と同一内容の推奨は再送されません(「前回と同内容のため通知をスキップしました」)
- `BuyCandidatesFunction`・`HoldingsWatchlistFunction`は同じ08:00起動でも別々のLambda関数として完全に独立しており、それぞれ別の「まとめ通知」を送ります。保有銘柄側のまとめ通知に買い候補の結果が含まれないのは意図した設計です(2026-07-31確認)。買い候補が0件の日も、保有銘柄側と同様に「今回の購入候補: 該当なし」を明示するまとめ通知を送信します(Issue #568で修正。`notification_rules.yaml`の`send_empty_summary`は既定true。falseへ変更すると0件の日にまとめ通知自体を送信しなくなるが、通常運用でこの既定値を変更する想定はない)
- 両関数とも起動時に株主優待レジストリの読み込み件数をCloudWatch Logsへ`INFO`で常時記録し、`notification_rules.yaml`の`operations.shareholder_benefit_registry_min_expected_entries`(既定1)未満の場合は`WARNING`を追加で出します(2026-07-31追加。CSV取込漏れ等の運用ミスを検知するため。バッチ処理自体は止めません)
- **Profit Protection ATTENTION(利益保全注意、2026-08-21追加)**: 保有継続(監視)のうち、利益保全判定のcandidate/strongシグナル(6.2節)を検出した銘柄のみ、例外的に「⚠️利益保全注意」としてLINE通知します(決算待ち等それ以外の理由による保有継続(監視)は引き続き通知しません)。同じ利益保全の局面(判定基準日・最高値・最高値を記録した日の組がすべて同じ)については、局面が変わるまで個別のLINE通知を再送しません
- 【保有株チェック完了】(内部の処理名は「保有銘柄分析」のまま、表示名のみ2026-08-21改称)の各件数(一部売却・全部売却・売却・緊急確認・利益保全注意)は、実際に個別LINE送信できた件数ではなく、その日の判定で有効なアクションとして検出された件数です。売買クールダウン・優先順位比較・再通知抑止・kill switchで個別のLINE通知だけが見送られても検出件数には残ります(データ品質チェックで判定自体が保留された場合のみ検出件数から除きます)。実際の送信件数(利益保全注意を含む)はCloudWatch Logsの`holdings_summary_action_counts`ログで確認できます(`batch_id`ごとに`{action}_detected`/`{action}_sent`を出力)
- kill switch(緊急停止)が有効な間は、個別のLINE通知・1日のまとめ通知のいずれも送信されません(判定・Recommendation保存自体は継続します)。緊急停止中も、まとめ通知の検出件数(一部売却・全部売却・売却・緊急確認・利益保全注意)に使うデータ品質チェックだけは通常時と同じ判定を読み取り専用で行います(2026-08-21再々改訂。以前は緊急停止中はこのチェック自体が行われず、緊急停止の開始/解除タイミング次第でデータ品質上「要確認」とすべき判定が検出件数へ混入しうる不備がありました)。LINE通知(個別・「要確認」通知を含む)が緊急停止中に送信されることはありません
- まとめ通知・個別通知(利益保全注意を含む)の重複送信防止は、2026-08-28改訂(Issue #17)で原子的なclaim方式へ強化しました。送信直前に「この送信決定」を表す記録をDynamoDBの条件付き書き込み(notification_claimsテーブル、保持期間30日は掃除専用)で確保し、確保できた1つの実行だけがLINE送信します。送信失敗時はclaimを取り消して再試行可能に戻し、送信成功後に履歴保存だけが失敗した場合は再試行がLINEを再送せず履歴のみ復元します。claimを確保したまま実行が異常終了した場合は20分経過後に後続実行が引き継ぎます。ただし厳密に1回だけ(exactly-once)の保証ではなく、LINE側受理済みの通信エラーや送信中の異常終了からの引き継ぎでは、ごく稀に二重送信があり得ます(通知の取りこぼし防止を優先する設計)。この重複抑止の「当日」判定は日本時間の暦日基準です(2026-08-21改訂。以前はシステム基盤の時刻(UTC)の暦日をそのまま使っており、日本時間の朝8時台〜9時台でUTC暦日とずれ、同じ日本時間の1日が別日として扱われる不備がありました)
- 売買イベント検知(新規購入・買い増し・一部売却・全部売却の推定)・クールダウン期限の算出・1日1回だけ検知処理を行うための排他制御も、同じ日本時間の暦日を基準に統一しています(2026-08-21再々改訂。以前はこの検知処理側がシステム基盤の時刻(UTC)のままで、上記の重複抑止・クールダウン判定側だけが先に日本時間基準へ変わっていたため、期限の算出と比較の基準日が1日分ずれる不備がありました)。クールダウンの日数設定・東証休業日/祝日を営業日として数えないルールは変更していません。日付・営業日判定の横断的な棚卸しはGitHub Issue #18で引き続き追跡しています

---

## 4.1 ウォッチリスト自動追加(2026-08-01追加・候補ユニバース本格対応で全面改訂・2026-08-16平日毎日起動化)

| 時刻 | schedule.yamlのジョブ | 対応コマンド | 対応Lambda関数 |
|---|---|---|---|
| 平日(月曜〜金曜)06:00(2026-08-16改訂。旧: 毎週土曜07:00。cron自体は祝日判定なし。★ 2026-09-19以降、東証休場日は起動後にskip: 本節末の「市場休場日のskip」) | (未登録。`infra/template.yaml`の`WeekdayMorning` ScheduleV2にcron直書き) | `jstock watchlist-screening run` | `WatchlistDispatcherFunction`(`job_type=NEW_CANDIDATE_SCREENING`) |
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

**finalize-only recovery で通常通知が抑止されることについて(Issue #211、2026-09-08追加)**:
`WatchlistBatchReconcilerFunction`は、全銘柄の評価が終わっているのに集計処理
(finalize)まで進んでいない停滞バッチを検知すると、子Lambdaへ
`recovery_action=FINALIZE_ONLY`のペイロードを送って集計だけをやり直させます
(銘柄評価・Recommendation再生成・fanout等は行いません)。

```
★ この経路では、重大リスク以外の通常のLINE通知は **送られません**。
  ブロック理由は `TRADE_DETECTION_IN_PROGRESS` として記録されます。
```

理由は、recoveryが**売買検知(`TradeCooldownService.detect_and_apply()`)を
この実行では走らせていない**ためです。売買を検知した銘柄には通常通知の
クールダウンが適用されますが、検知を走らせていない実行ではそのクールダウンが
未適用のままになります。この状態で通常通知を送ると、**本来は抑止されるべき
銘柄へ通知が出る**可能性があります。「検知完了を確認できていないなら送らない」
(fail-close)が本システムの方針です。

したがって運用上は次のように扱ってください。

- recovery が走った日は、その batch 由来の通常通知が出ないことは **正常**です。
  通知が来ないことを障害として扱わないでください。
- 集計・監査記録(AuditLog / DecisionSnapshot 等)は通常どおり残ります。
  判定結果を確認したい場合は `jstock audit show <銘柄コード>` を使ってください。
- 通知が必要な場合は、次回のスケジュール起動(新しい`batch_id`で最初から)を
  待ってください。recovery を再実行しても通知は出ません。
- 重大リスク(`is_critical_risk`)の通知はこのゲートを貫通するため、
  recovery 経路でも送られます。

```
★ 2026-09-08 より前は、この経路でも通常通知が送られていました
  (ペイロードに検知状態が載っておらず、受け取り側が既定値 True =
   「検知済み」として扱っていたため)。Issue #211 で fail-close へ是正しています。
```

**候補銘柄数の上限について**: 旧仕様にあった評価対象件数の上限(300件)は、
候補ユニバース本格対応でSQSベースの銘柄単位処理へ全面的に作り直したことに伴い
撤廃しました。約3,122銘柄の全件処理には数時間規模の時間がかかります(1銘柄
あたり30〜45秒 ÷ 同時実行数4)。

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
Worker(同時実行数`WatchlistReservedConcurrentExecutions`、既定4、2026-08-20に
Issue #4の実施基準充足確認を経て3から引き上げ)がほぼ同時に
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
cron自体は日本の祝日を考慮しない。休場日のskipは起動後のhandler側で行う: Issue #440)。これに伴いWATCHLIST_MAINTENANCEの独立した
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

### 4.1.2 市場休場日のskip(2026-09-19追加・Issue #440)

EventBridgeのcronは月〜金で固定であり、祝日・国民の休日を考慮しない。東証(JPX)の休場日
(判定は`BusinessCalendar.is_business_day`と同一。土日・祝日・国民の休日・
`config/holiday_calendar.json`の臨時休業)には、**市場依存の3 entryのみ**が
起動直後に何も行わず正常終了する(`lambda_handlers/_market_holiday.py`)。

| entry | 休場日の返却値 |
|---|---|
| BuyCandidates親(平日08:00) | `{"dispatched": 0, "skipped": "MARKET_CLOSED"}` |
| HoldingsWatchlist親(平日08:00) | `{"dispatched_holdings": 0, "skipped": "MARKET_CLOSED"}` |
| WatchlistDispatcher `NEW_CANDIDATE_SCREENING`(平日06:00) | `{"skipped": "MARKET_CLOSED"}` |

- **止めないもの**: 適時開示・評価・週次/月次/四半期・recovery(`FINALIZE_ONLY`等)・child・
  worker・reconciler・`WATCHLIST_MAINTENANCE`(今回はscope外)。
- **skipの位置**: eventとmodeのvalidationの後、最初の状態変更(売買検知・batch行・lease・SQS・
  fan-out・保有集中通知)の前。不正なeventは休場日でも従来どおりエラーになる(休場日で隠さない)。
- **ログ**: skip時にINFOを1件(`event=MARKET_CLOSED_SKIP handler=... business_date_jst=... execution_mode=...`)。
  PIIを含まない。CloudWatchで`MARKET_CLOSED_SKIP`を検索すると、いつ・どのentryがskipしたか確認できる。
  「起動したが何も起きていない」のは異常ではなく、この行があれば休場日のskipである。
- **VALIDATIONのbypass**: `execution_mode=VALIDATION`かつ`allow_market_closed=true`のときだけ
  休場日でも実行できる(使用時は`MARKET_CLOSED_BYPASS`を記録)。NORMALでtrue・真偽値以外はエラー。
- **★ WatchlistDispatcherは検証モードを持たない**(`execution_mode`を拒否する)ため、`allow_market_closed=true`は
  常にエラーとなり、**休場日に手動起動(例: S3キャッシュ更新目的)してもskipされる**。
  休場日に実行が必要な場合は、営業日まで待つか、別途Issueで対応を判断する。
- **連休明け**: 次の営業日は通常どおり起動する。連休中の売買は、前回保存した保有スナップショットとの
  差分として、その営業日に検知される(日数の連続性に依存しない。テストで確認済み)。
- **ロールバック**: 追加は返却値の`skipped`とログのみで、保存データ形式は変えていない。
  旧版へ戻すと休場日にも従来どおり起動する(直前営業日の値で判定・通知が出る従来の挙動)。

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

### 5.0 定点評価の監視とbacklog回復(Issue #113、2026-08-31追加)

#### run summaryの読み方

`EvaluationFunction`は実行のたびにCloudWatch Logsへ次の1行を出力する
(**予算切れで途中終了した場合も必ず出力される**)。

```
evaluation_handler done: evaluated=N (business=N calendar=N) skipped=N
  due_horizons=N already_evaluated=N pending_horizons=N pending_recommendations=N
  backlog_remaining=N budget_exhausted=<bool> recommendations_scanned=N
  missing=N provider_calls=N duration_ms=N
```

| 項目 | 意味 |
|---|---|
| `due_horizons` | 評価日が到来している(recommendation, horizon)の組の総数 |
| `already_evaluated` | そのうち既に評価済みの数 |
| `pending_horizons` | 未処理の数(= `due_horizons - already_evaluated`) |
| `backlog_remaining` | **この実行の後に残った未処理数**。0でなければ回復途中 |
| `budget_exhausted` | 残時間が尽きて自主的に切り上げたか |
| `provider_calls` | 外部株価APIへ実際に到達した回数(runスコープのキャッシュ後) |

**監視の要点**: `backlog_remaining`が**実行を重ねても減らない/増える**場合は、
1回あたりの処理能力が新規流入(推奨の増加ペース)を下回っている。
この場合はTimeout・providerレイテンシ・pending件数を確認すること。

途中経過は `evaluation scan done:` と `evaluation progress:`(500件ごと)で
追跡できる。以前は`START`〜`REPORT`の間にアプリケーションログが1行も出ず、
どこまで進んだか追跡できなかった。

#### CloudWatch Alarm

| Alarm | 条件 |
|---|---|
| `<stack>-evaluation-errors` | `Errors >= 1`(**Lambdaのタイムアウトもここに計上される**) |
| `<stack>-evaluation-duration` | `Duration >= 720,000ms`(Timeout 900秒の80%) |

★ **2026-09-24更新(Issue #504)**: 上記2本を含むLambda 12本すべてに`AlarmActions`(SNS Topic
`IncidentNotificationTopic`経由でLINEへ通知)が接続された。「通知先は設定していない」は
2026-09-23時点までの記述であり、現在は誤り。**最新の状態は29節・#132の最新の記録を読むこと**
(この節へ焼き込まない)。

#### run summaryの監査ログへの記録(Issue #114 Phase B1、2026-09-02追加)

上記のrun summaryは、CloudWatch Logsに加えて**監査ログへも保存される**。

```
保存先        jstock-audit_log
decision_type evaluation_run_summary
audit_id      evaluation_run_summary:<run開始時刻のISO8601>
```

CloudWatch Logsは検索が手間で保持期間の制約もあるため、
「いつの実行で、backlogがどこまで減ったか」を後から永続データとして
追跡できるようにしたもの。記録内容は上表のrun summary全項目に加え、
`run_started_at` / `run_completed_at` / `run_status`
(`COMPLETED` または `BUDGET_EXHAUSTED`)。

**保証範囲(重要)**

| 実行の終わり方 | 監査ログへの記録 |
|---|---|
| 正常完了 | される |
| 時間予算による自主終了 | される(`run_status=BUDGET_EXHAUSTED`) |
| **メモリ不足(OOM)・タイムアウト** | **されない場合がある** |

OOM・タイムアウトではLambdaのプロセスが強制終了され、記録処理まで到達できない。
**この検知は上記のCloudWatch Alarm(`<stack>-evaluation-errors`)の役割**である
(OOMもタイムアウトも`Errors`に計上される)。したがって
「監査ログに記録が無い」ことをもって「backlogが無い」と解釈してはならない。

**監査ログへの保存に失敗した場合**

評価そのもの(EvaluationResultの保存)は既に成功しているため、
**保存失敗でLambdaを失敗させない**(失敗させると自動リトライで
評価処理全体が不要に再実行されるため)。失敗時は次のように表れる。

```
ERRORログ         event=evaluation_run_summary_persist_failed ...
Lambdaの戻り値    "audit_persisted": false
```

`audit_persisted` が `false` の実行は、評価結果自体は正常だが
監査記録だけが欠けている状態である。

Lambdaが自動リトライした場合、各リトライは`run_started_at`が異なるため
**それぞれ別の実行として記録される**(最後の試行の状態を見ること)。

#### backlog回復期間中の注意

未処理分は**古い推奨から順に消化される**。評価値は基準日の株価から計算されるため
遅れて処理しても結果は変わらないが、**週次改善レビュー(5.1節)は
「前週に結果が確定した分」を集計する**ため、回復期間中はその週の集計件数が
一時的に大きく膨らむ。**回復期間中の週次レビュー結果を、通常の週と同じ意味で
比較しないこと**(`docs/functional_spec.md` 12.4節参照)。

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

### 6.1 (廃止)LINEチャットのCSVコマンドからの登録

**この登録方式(「買付,銘柄コード,株数,単価」「売却,銘柄コード,株数,単価」
「ウォッチ,銘柄コード」のCSVテキスト送信)は2026-08-29に廃止しました
(Issue #24)。** 現在、これらのテキストを送信しても登録は一切行われず、
メニュー操作を案内する返信のみが返ります。LINEからの売買記録・ウォッチリスト
登録は6.2節のメニューボタン(会話型UI)が正式かつ唯一の経路です。
推奨IDとの紐付けや手数料・税額・メモ等の詳細な記録が必要な場合は、従来どおり
CLI(`transactions buy-executed`/`sell-executed`等、6節冒頭)を使用してください。

(廃止理由の要点: リッチメニュー導入後は利用実態が無く、確認画面なしの即時
書き込み・売買記録と保有銘柄データの非原子的な2段階書き込み・保守停止
(TradingPause)の対象外・所有者が既定owner固定・ウォッチ登録時に既存設定を
既定値で上書きする等、会話型UIに対して安全性が劣る経路だったため。)

### 6.2 LINEメニューボタンからの登録(会話型UI、2026-08追加)

LINEトーク画面下部のリッチメニューから「📈 買った」「📉 売った」
「⭐ お気に入り登録」を選び、画面の案内(入力→確認→登録する/やり直す/
キャンセル)に従って記録する。**LINEからの登録経路は本方式のみ**(6.1の
CSVコマンドは廃止済み)。利用者向けの操作方法は機能仕様書10.4節を参照。
運用担当者が把握しておくべき点は以下のとおり。

- **内部設計**: 対話の一時状態(入力待ち・確認待ち)は`conversation_states`
  DynamoDBテーブルで管理し、TTL(20分)経過後はDynamoDB Native TTLによる
  物理削除を待たず、アプリケーション側の判定(`infrastructure/aws/
  conversation_state_store.py`)で即座に「対話なし」として扱う。
- **「登録する」実行時の書き込み**は、売買記録・保有銘柄データ更新(または
  ウォッチリスト登録)・対話状態の消費を、DynamoDB `TransactWriteItems`に
  よる単一の原子的操作として実行する(`infrastructure/aws/
  conversation_commit.py`)。既存Holdings/PurchaseLotsの更新・削除には
  楽観ロック(計画構築時点のデータと一致することを条件とする)を必須で
  付与しており、確認画面表示後に保有状況が(CLI等の別経路で)変化していた
  場合は登録自体が失敗し、利用者へ「もう一度操作してください」と案内する。
- **クラッシュ発生時の確認手順**: 「登録が完了しました」というLINE返信が
  届かず、実際に登録されたか不明な場合は、`jstock transactions list` または
  DynamoDBの`transactions`テーブルを、対話開始時に発行された`operation_id`
  (= `transaction_id`として使われる)で検索することで、実際に登録済みか
  どうかを確認できる。登録済みであれば同じ内容の再送信・再操作は不要。
- **既知の制約**: 本方式自体の二重登録防止は上記の原子的操作で保証される
  が、本方式とCLIが「ほぼ同時に同一銘柄」を操作した場合の完全な相互排他は
  対象外(9節参照)。
- **買付余力(available cash)の棚卸し(2026-09-26追加、Issue #592)**:
  同じ会話状態機械・原子コミット基盤を再利用し、`action=
  start_available_cash_reconcile`のpostbackから、所有者選択(既存所有者は
  ボタン、未登録の新規所有者は自由テキスト入力も可)→現在値表示→新しい
  金額入力→確認→登録、という流れで操作する(利用者向けの操作方法は機能
  仕様書10.6節を参照。CLI版は3.5節)。**リッチメニューへのボタン追加自体
  (A5b/#593)・AvailableCashTable/IAM配線(A6a/#595)はいずれも本Issueの
  scope外**であり、本Issueの時点ではpostback自体は実装済みでも、
  (1)リッチメニュー上に到達手段が無い(postbackを直接送信しない限り、
  利用者は本機能へ到達できない)、(2)AvailableCashTable自体がまだ
  Productionに存在しない(#595のChangeSet CREATE/EXECUTEが別途必要)、
  の2点により実際には動作しない。#593(ボタン)・#595(table/IAM)の両方が
  Productionへ反映されるまで、実質的にShadow実装の状態にある。

#### リッチメニューの登録手順(初回セットアップ・変更時のみ、人間が実行)

リッチメニューの作成・画像アップロードは`infra/line_rich_menu/
register_rich_menu.py`で行う(このスクリプトはLambda/CIからは呼ばれない、
人間が手元で実行する運用スクリプト)。定義は`infra/line_rich_menu/
rich_menu.json`にリポジトリ管理されている。**Issue #593(2026-09-26)で
2500×1686px・2行×4列の均等grid(各セル625×843)へ更新済み**(USER確定
レイアウト、issuecomment-5842895208)のため、画像ファイルもこの構成
(上段: 買った/売った/お気に入り登録/余力管理、下段: 保有銘柄/
ウォッチリスト/対象確認/銘柄分析)に合わせて別途用意すること。
「余力管理」に対応するLINE会話ロジック自体はIssue #592で別途実装する
(本Issueのscope外)。

```bash
# 1. 環境変数にチャネルアクセストークンを設定する(Secrets Managerの値をコピー)
export LINE_CHANNEL_ACCESS_TOKEN=<チャネルアクセストークン>

# 2. リッチメニューを作成し、画像をアップロードする(デフォルト設定はまだ行わない)
python infra/line_rich_menu/register_rich_menu.py --image /path/to/menu.png

# 3. 出力された「現在のデフォルトリッチメニュー」を確認し、
#    既存の別用途リッチメニューを上書きしても問題ないことを確認してから、
#    出力されたrichMenuIdを指定してデフォルトに設定する
python infra/line_rich_menu/register_rich_menu.py --rich-menu-id <richMenuId> --set-default
```

デフォルト設定(手順3)は既存のリッチメニュー設定を上書きする可能性があるため、
必ず手順2の出力を確認してから実行すること。**アプリケーションコード
(Lambda)のデプロイと、このリッチメニュー登録手順は完全に独立している**。
コードをデプロイしただけではLINEトーク画面のメニュー表示は変わらず、
本手順を別途手動で実行して初めて利用者の画面に反映される。

**「💰 余力管理」ボタンを含む本レイアウトをset-default(手順3)する前提**:
`action=start_available_cash_reconcile`(#592)を実際に処理できる
LineWebhookFunctionと、AvailableCashTable/IAM配線(#595)の両方が
Productionへ反映済みであること。いずれかが未反映のままset-defaultすると、
このボタンをタップした利用者に対して「認識できない操作です」という
案内、またはAccessDeniedException経由のエラーが返る(既存7ボタンの
動作には影響しない)。

### 6.3 保有銘柄・ウォッチリスト・対象確認(参照専用、Phase 2-A・2026-08追加)

利用者向けの操作方法は機能仕様書10.5節を参照。運用担当者が把握しておくべき
点は以下のとおり。

- **読み取り専用**: `HoldingsViewService`/`WatchlistViewService`/
  `BuyCandidateTargetViewService`(いずれも`src/jstock_advisor/services/`
  配下)は、Holding・PurchaseLot・WatchlistItem・BuyCandidateEvaluationRecord
  等の正データへの書き込みメソッドを一切持たない設計。
- **最新完了batchポインタ**: 「対象確認」「ウォッチリスト」の直近購入判定
  参照は、`BuyCandidateBatchCompletionTable`(単一行)が指す直近の
  「通常運用(NORMAL)で完了し、かつ全対象銘柄の内部記録(12.14節相当、
  `BuyCandidateEvaluationRecordsTable`)保存が確認できたbatch」のみを見る。
  検証モード(VALIDATION/DRY_RUN)実行時や、一部銘柄の内部記録保存に失敗した
  場合はこのポインタを更新しない(直前の正常完了分がそのまま参照され
  続ける)。更新されなかった場合は`buy-candidates` LambdaのCloudWatch Logs
  へERRORログ(`evaluation record save incomplete, latest batch pointer NOT
  updated`)が出力されるため、これが頻発する場合は`buy_candidate_evaluation_
  records`テーブルへの書き込み失敗の原因を調査すること。
- **GSI(`batch_id-index`)反映待ちの表示**: `BuyCandidateEvaluationRecordsTable`
  のGSIは結果整合性のみのため、直近batch完了直後の数秒間、対象確認・
  ウォッチリストの参照が「直近の分析結果を反映中です。少し時間をおいて
  再度お試しください。」と表示することがある(1回の短い自動再試行後も
  件数が一致しない場合のみ)。異常ではなく、少し待って再操作すれば解消する。
- **IAM**: `LineWebhookFunction`に`BuyCandidateEvaluationRecordsTable`・
  `BuyCandidateBatchCompletionTable`への読み取り専用アクセスを追加した
  (11節のGSI追加時の注意もあわせて参照)。

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
| LINEチャットのCSVコマンド登録(6.1節) | **廃止済み(2026-08-29、Issue #24)**。送信しても登録されずメニュー操作の案内のみ返る |
| LINEメニューボタン登録(会話型UI、6.2節) | 推奨ID・手数料・税額・メモ等は指定不可(最小限の項目のみ。詳細な記録はCLIを使用)。本方式自体の二重登録は防止しているが、本方式とCLIとの間の「ほぼ同時操作」に対する完全な相互排他は未対応(必要であれば別途PortfolioService全体の楽観ロック化を検討) |
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

**本番検証で発覚・修正した不具合(2026-08-21)**: 上記の`init-runtime-config`
(初回作成)後に行う`set-mode`/`kill-switch`(10.2〜10.4節、更新)は、`--target
aws`実行時、`RuntimeConfig`テーブルの実際の保存形式(1項目全体を単一の`data`
属性(JSON文字列)へ保存する、`infrastructure/aws/dynamodb_store.py`の
`DynamoDbCollectionStore`方式)と、更新処理側が前提としていたスキーマ
(`config_version`等を項目のトップレベル属性として直接更新)が一致しておらず、
DynamoDB上では`ConditionExpression`が常に`ConditionalCheckFailedException`
となり、**`--target aws`での`set-mode`/`kill-switch`が一度も成功していな
かった**(ローカル運用時はJSONファイルを使うため影響なし。`baseline_pointer.py`
の`update_pointer`にも同一の不整合があり、あわせて修正した)。`data`属性全体
の一致を条件とする条件付き更新へ修正し、既存の本番state(移行不要)のまま
そのまま更新できるようにした(watchlist_rotation_state.pyのrotation commit
で本番検証時に発覚・修正済みの不具合と同じ原因・同じ修正方針)。

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

**既存テーブルへGSIを追加する場合の注意(2026-08、Phase 2-A・
`BuyCandidateEvaluationRecordsTable`へ`batch_id-index`追加時に確認)**:
DynamoDBのGSIはスパースインデックスであり、GSI追加(`UpdateTable`)は既存の
全アイテムに対して自動バックフィルを行うものの、GSIのキー属性(この例では
`batch_id`)をそもそも持たないアイテムはGSIに載らない。本システムの
`DynamoDbCollectionStore`は各アイテムを`{id, "data": JSON文字列}`という
形式で保存しており、通常の`upsert()`ではGSI用の属性はトップレベルに
書き込まれない(`upsert_with_index_attributes()`を使うリポジトリのみが
書き込む)。したがって、**GSI追加前にコード変更(該当リポジトリの
`upsert_with_index_attributes()`切替)をデプロイしていない場合、GSI追加後も
既存データはGSIから検索できないまま**になる。コードとインフラ(GSI追加)を
同一デプロイで反映すれば、デプロイ後に新規書き込まれるデータから正しく
GSIに載るため、通常はこれで実害はない(過去データをGSI経由で検索する必要が
無い設計であることを事前に確認すること)。

## 12. 保有銘柄オーナー機能移行の運用(TradingPauseConfig、2026-08追加)

保有銘柄を所有者(本人/子供等)ごとに区別できるようにする機能追加(開発中、
複数フェーズに分けて実施)の準備として、LINE会話型UIの「📈 買った」
「📉 売った」操作をCLIから一時停止できる`TradingPauseConfig`を導入しました
(10節のRuntimeConfigと同じ「再デプロイ不要でCLIから切り替える」設計)。
「⭐ お気に入り登録」はHoldings/PurchaseLotsを一切更新しないため対象外で、
一時停止中も通常どおり利用できます。

**`--target`は`local`/`aws`のいずれかを必ず明示指定してください(既定値は
ありません)**。`--target awss`のような入力ミスは値検証の時点でエラー終了し、
どちらのバックエンドにも一切触れません(コードレビュー対応: 本番を一時停止
したつもりでタイプミスによりローカルだけを操作してしまい、本番が停止して
いないままデータ移行へ進んでしまう事故を防ぐため)。

**pause確認と実際のBUY/SELL登録(TransactWriteItems)は原子的です**。
LINE会話の確認画面表示時点でpause=falseだったとしても、「登録する」を
押した瞬間の書き込みそのものにTradingPauseConfigの状態確認が含まれるため、
その間に運用者が`--buy-sell`へ切り替えた場合は、書き込みの直前で
確実に失敗し(「最新の保有状況が変更されたため登録できませんでした」と
案内されます)、Holdings/PurchaseLots/Transactionsのいずれも変更されません。

### 12.1 初回作成

```bash
jstock trading-pause init --changed-by <あなたの名前> --reason "M0導入" --target local
# 本番(AWS)環境に対して初期化する場合
jstock trading-pause init --changed-by <あなたの名前> --reason "M0導入" --target aws
```

既定は`--paused`省略時`pause_buy_sell=False`(通常運用、BUY/SELLとも利用可)
です。現在の設定は次のコマンドで確認できます。

```bash
jstock trading-pause status --target aws
```

### 12.2 データ移行作業の前後での使い方

データ移行(V2テーブルへの切替等)を開始する前に、必ずBUY/SELLを停止して
ください。

```bash
jstock trading-pause set --buy-sell --changed-by <あなたの名前> \
  --reason "所有者機能移行のためBUY/SELLを一時停止" --target aws
```

移行・コード切替・VALIDATIONモードでの整合性確認がすべて完了し、問題が
無いことを確認できてから、初めて解除してください(コードのデプロイと
本フラグの解除は必ず別々の操作です。デプロイに解除を同梱しないこと)。

```bash
jstock trading-pause set --no-buy-sell --changed-by <あなたの名前> \
  --reason "移行完了・検証合格のため通常運用を再開" --target aws
```

解除を忘れたままにすると、移行作業が完了してもLINEから買付・売却が
できない状態が続くため、`status`コマンドで意図した状態になっているか
必ず確認してください。

### 12.3 データ移行本体(preflight・run、2026-08追加)

owner/holding_idへの実際のデータ移行は`jstock migrate holdings-owner`
コマンド群で行います。**preflight(検証)とrun(移行本体)は必ず別々に
実行してください**(1コマンドで連続実行する設計にはしていません)。

```bash
# 1. まず検証のみを行う(書き込みは一切発生しない)
jstock migrate holdings-owner preflight --target aws
```

`PASS`と表示されれば移行を進められます。`FAIL`の場合は表示された各
チェックの詳細(該当するrecommendation_id・notification_id等)を確認し、
原因を解消してから再実行してください。

```bash
# 2. dry-run(既定、書き込みなし)で移行結果の見込みを確認する
jstock migrate holdings-owner run --target aws

# 3. 内容に問題が無ければ、--no-dry-runを明示して実際に書き込む
jstock migrate holdings-owner run --target aws --no-dry-run
```

**移行本体(`run --no-dry-run`)は、`TradingPauseConfig.pause_buy_sell`が
`true`(12.2節で設定済み)であることをコード自身が確認してから実行します。**
`false`のまま・未初期化・取得エラーのいずれの場合も、移行は開始されず
安全側で中止されます(CLIの操作手順だけに頼らない設計です)。

移行は何度実行しても結果が変わらない(重複しない)設計のため、途中で
失敗した場合は原因を確認のうえ、そのまま再実行して構いません(再実行時、
既に正しく移行済みのholding_idを再度書き換えて二重prefix化する、といった
不整合は発生しません。万一データが破損している場合はfail-closedで移行を
中止します)。

**`--target aws`指定時は、preflight・run本体の開始から終了まで一貫して
AWS(DynamoDB)のみを参照し、途中でローカルJSONへフォールバックすることは
ありません**(逆に`--target local`指定時はAWSへ一切アクセスしません)。
1回の実行中にlocal/AWSのデータが混在することはない設計です。

HoldingsSnapshot(通常)だけでなくValidationHoldingsSnapshot(検証モード用)
についても、`active_holding=true`なのに対応するHoldingが存在しないといった
不整合をpreflightが独立に検知します。

## 13. 通知検証モード(VALIDATION)利用時の注意事項・用途別手順(2026-08追加)

`BuyCandidatesFunction`・`HoldingsWatchlistFunction`をAWSコンソール/CLIから
`{"execution_mode": "VALIDATION"}`で手動起動する「通知検証モード」
(機能仕様書12.13節)は、**`notification_mode`を指定しない限り実際にLINEへ
通知が送信されます**。これは既存仕様どおりの正常動作であり、バグでは
ありません。

owner再分類・データ移行検証(12節)・保有判断ロジックの整合性確認など、
**LINE文面の確認自体が目的ではない**検証作業でVALIDATIONを使う場合は、
実LINE送信を伴わない`notification_mode: "DRY_RUN"`を必ず使ってください
(2026-08-23、owner再分類検証作業中に意図せずLINE通知が実送信された事例を
受けて追加)。

### 13.1 LINE文面そのものを確認したい場合(実送信あり)

```json
{
  "execution_mode": "VALIDATION",
  "notification_mode": "SEND"
}
```

`notification_mode`を省略した場合も`SEND`と完全に同じ動作です(既存仕様との
後方互換性のため、`{"execution_mode": "VALIDATION"}`単体の呼び出しは今後も
挙動が変わりません)。**この形式では実際にLINEへ通知が届きます**(本文冒頭に
「🧪検証｜」が付きます)。

### 13.2 判定結果・処理の整合性だけを確認したい場合(実送信なし)

```json
{
  "execution_mode": "VALIDATION",
  "notification_mode": "DRY_RUN"
}
```

判定・通知対象選定・通知文生成・検証banner付与までは13.1と全く同じ処理を
行いますが、**外部LINE APIへの送信のみ行いません**。「実際に送るとしたら
何が送られたか」(最終文面・銘柄コード・判定区分等)はCloudWatch Logsで
確認できます。今回のM4.2のようなowner再分類・データ整合性・移行検証等、
LINE文面確認自体が目的ではない作業では、原則こちらを使ってください。

### 13.3 組み合わせの制約

`notification_mode`は`execution_mode: "VALIDATION"`と組み合わせた場合のみ
有効です。`execution_mode`が`NORMAL`(省略時含む)の状態で`notification_mode`
を指定すると、黙って無視されたりSENDへフォールバックしたりせず、明確な
エラーとしてLambda呼び出し自体が失敗します。通常の自動実行(毎日決まった
時刻)は`notification_mode`を一切指定しないため、この制約による影響は
ありません。

### 13.4 各Lambdaの`execution_mode`対応可否(Issue #286、2026-09-08追加)

**どのLambdaが検証モードを受け付けるのかは、Lambdaごとに異なります。**
受け付けないLambdaへ指定した場合の挙動も、以前は一様ではありませんでした
(黙って通常運用として実行されるものがありました)。実測した現況を以下に
まとめます。

| Lambda(handler module) | `execution_mode`指定時 | 備考 |
|---|---|---|
| `buy_candidates_handler` | **対応**(VALIDATION/NORMAL) | 13.1〜13.3の手順が使える。検証用テーブルへ隔離される |
| `holdings_watchlist_handler` | **対応**(VALIDATION/NORMAL) | 同上 |
| `disclosure_check_handler` | **対応**(VALIDATION/NORMAL) | Issue #109で対応済み |
| `watchlist_dispatcher_handler` | ★ **拒否**(Lambda呼び出しが失敗する) | Issue #286。理由は下記 |
| `watchlist_worker_handler` | ★ **拒否**(同上) | 同上 |
| `watchlist_batch_reconciler_handler` | ★ **拒否**(同上) | 同上 |
| `watchlist_terminal_failure_handler` | ★ **拒否**(同上) | 同上 |
| `evaluation_handler` | ⚠ **黙殺**(通常運用として実行される) | **Issue #287で未解消**。指定しないこと |
| `weekly_review_handler` | ⚠ **黙殺**(同上) | 同上 |
| `monthly_review_handler` | ⚠ **黙殺**(同上) | 同上 |
| `quarterly_review_handler` | ⚠ **黙殺**(同上) | 同上 |
| `line_webhook_handler` | 対象外 | LINEからのwebhook受信であり、バッチ起動の概念を持たない |

```
★ watchlist系4本を「対応」ではなく「拒否」にした理由

  検証モードが成立するには、書き込み先を検証用へ隔離できる必要がある。
  watchlistにはその隔離が存在しない(実測)。

    for_execution_context()を持つrepository  Recommendation / WatchState /
                                             HoldingsSnapshot /
                                             DailyNotificationPriority
    WatchlistRepository と rotation state   **持たない**
    infra/template.yamlのValidation*テーブル **watchlist / rotationは無い**

  したがって受け付けると、LINE送信は止められても
  **実際のwatchlistが書き換わり、巡回カーソルが前進する**。
  「検証のつもりで本番の状態を変えた」という最悪の結果になるため、
  対応せず**明示的に失敗させる**方針とした(機能仕様書12.13節の
  「ウォッチリスト自動追加の通知は対象外」という仕様は変えていない)。

★ 拒否されたときの見え方
  Lambda呼び出しが`WatchlistExecutionModeNotSupportedError`で失敗し、
  CloudWatch Logsへ「どのhandlerがどのキーを拒否したか」がERRORで残る。
  黙って握りつぶすことはない。

★ **自動実行(EventBridge Scheduler)は影響を受けない。**
  どのScheduleもInputを持たず、`execution_mode`を渡さないためである。
```

```
★ ⚠ の4本(evaluation / weekly / monthly / quarterly review)について

  **指定しても黙って通常運用として実行されます。** Issue #287で是正予定。
  それまでの間、これら4本へ`execution_mode`を渡した「検証実行」は
  **行わないでください**(通常運用の実行になります)。
  weekly_reviewはLINE送信とGitHub Issue起票を伴います。
```

### 13.5 監査記録の`execution_mode`(起動経路)の読み方(Issue #286、2026-09-08追加)

ウォッチリストのバッチ監査記録(`watchlist_auto_addition_batch`)に入る
`execution_mode`は、13.1〜13.4の**検証モードとは別の軸**です。
「どの経路で起動されたか」を表します。

```
scheduled  EventBridge Schedulerからの自動実行(eventにキーが無い)
manual     人がpayloadを与えて手動起動した(job_type / batch_idを指定)
triggered  当日の新規候補スクリーニングの完了後に、後続のメンテナンスが
           自動で連鎖起動した(trigger_type = POST_NEW_CANDIDATE_SCREENING)
```

```
★ 既知の限界(Issue #286では解消していない)

  dispatcherが判定した経路そのものはBatchRunsTableへ保存していないため、
  **手動でdispatchしたバッチの「集計時」の監査記録は`scheduled`になります**。
  経路を正しく見分けられるのは、dispatcher自身が書く監査記録
  (中止・skip時)だけです。同一batch_idの記録を突き合わせて読んでください。
```

### 13.6 データのvintage(いつ時点のデータか)の読み方(Issue #69、2026-09-09追加)

1回のスクリーニングバッチは、**取得時点の異なるデータが同居した状態**で回ります。
どの銘柄がどの時点のデータで評価されたかを事後に説明できるよう、監査へvintageを
記録しています。★ **記録するだけで、判定は止めません**(理由は本節末尾)。

#### 候補ユニバース(2ファイル)のvintage — BatchRunsTableとfinalize時点の監査ログ

候補の母集団は「東証上場銘柄一覧」と「JPX400構成銘柄」の**2ファイル**から作ります。
この2つは独立した閾値(`listed_issues_max_stale_hours` /
`jpx400_max_stale_hours`)でそれぞれ鮮度判定されるだけで、**相互の整合は
検査していません**。そのため両者の公開日がどれだけ離れているかを記録します。

```
universe_source              DOWNLOADED / CACHE(上場銘柄一覧側)
universe_promoted            今回の取得が昇格したか
universe_source_date         上場銘柄一覧の公開日
universe_cache_age_days      ★ **現在時刻から**公開日までの経過日数(暦日、切り捨て)
universe_jpx400_promoted     JPX400側の同じ値
universe_jpx400_source_date  JPX400の公開日
universe_jpx400_cache_age_days
universe_vintage_gap_days    ★ **2つの公開日どうし**の差(暦日、絶対値)
```

```
★ `*_cache_age_days`と`universe_vintage_gap_days`は**基準が違います**。
  前者は「今から見て何日前のデータか」、後者は「2ファイルがどれだけ離れているか」。
  取り違えると「片方だけが古い」と「両方とも古い」を混同します。

★ どちらかの公開日が不明なときgapは**0ではなくNone**です。
  0は「一致している」を意味し、Noneは「比較できなかった」を意味します。

★ 公開日が離れているとき、CloudWatch Logsへ
  `candidate universe vintage mismatch ... gap_days=N (処理は継続する)`
  というWARNINGが出ます。比較できなかったときは
  `candidate universe vintage gap unavailable ...`です。
  平常運転では後者は出ません(昇格済みキャッシュは必ず公開日を持つため、
  出るのは初回とキャッシュ読み取り自体が失敗した場合だけです)。
```

#### 財務・配当キャッシュのvintage — 銘柄単位の進捗行

財務・配当のキャッシュは7日(168時間)のTTLのみで鮮度を制御しており、
**キャッシュキーにJST暦日を含みません**。そのため同一バッチ内で、ある銘柄は
当日取得の財務、別の銘柄は数日前の財務で評価されます。銘柄ごとに次を記録します
(`WatchlistCandidateProgressTable`)。

```
financial_cache_reused_count     キャッシュを再利用した件数
financial_cache_refetched_count  取り直した件数(期限切れ + 初回)
financial_cache_age_hours_max    ★ **実際に使った**古さの最大(単位はhours)
financial_cache_age_hours_min    同 最小
```

```
★ 単位は**hours(小数)**です。候補ユニバース側の`*_days`(暦日)とは違います。

★ 再利用が0件のときmax/minは**0ではなくNone**(属性そのものが書かれません)。
  0は「0時間前の新しいデータを使った」と読めてしまいます。
  「取り直した件数」は残るため、測っていないわけではないと分かります。

★ 「捨てた古さ」は含みません。期限切れで取り直した場合、評価に使ったのは
  新しく取得した値であるため、古さは記録しません。

★ **価格系キャッシュは含みません**。価格はキャッシュキーにJST暦日を含むため、
  日をまたいだ再利用が構造的に起きないからです。

★ 本変更の反映前に書かれた行には属性がありません。読み出し側はNoneとして
  扱います(0で埋めません)。

同一バッチ内のvintageの幅は、銘柄単位の`financial_cache_age_hours_max`と
`financial_cache_age_hours_min`を集めれば算出できます
(バッチ単位の集計値は保存していません)。
```

#### ★ 財務キャッシュにJST暦日キーを導入しない理由(Issue #69の結論)

価格キャッシュと同様に財務キャッシュへもJST暦日キーを入れれば、日をまたいだ
再利用は構造的に無くなります。**しかし導入しません。**

```
現在   財務の再取得は1日あたり概ね400件程度
       (新規候補300 + watchlist 716の1/7が期限切れ ≒ 100)
日次キー導入後  watchlist 716 + 新規候補300 = 毎日1,000件超が必ず再取得

=> 財務系provider呼び出しが**約2.5倍**。財務は1銘柄あたり複数系列
   (財務・配当・履歴評価・CF・サプライズ)を取得するため、実HTTP数の増加は
   これより大きくなります。provider側の不安定さは別Issueで実際に問題化して
   おり、安易に増やせません。キャッシュ行数も日次で増えるため、保持期間の
   設計とも衝突します。
```

まずvintageを**記録**し、実測が貯まってから日次キー化・バッチ単位のvintage固定・
決算更新検知による無効化のいずれが必要かを判断します。

#### ★ vintageの乖離で処理を止めていない理由

vintageが離れていても、記録とWARNINGだけで**処理は継続します**。

```
候補ユニバースの取得が一時的に失敗しただけで候補の自動追加そのものが停止すると、
「取得失敗が外から分からないまま古いデータで走り続ける」問題(Issue #223)を
直したはずが、今度は「止まっていることが分かりにくい」別の問題になります。
どこまでの差を許容するかの基準は、記録が貯まってから決めます。
```

## 14. Lambda Layer依存パッケージの更新手順(Issue #35、2026-08-28追加)

本番Lambdaの依存Layer(DependenciesLayer)は、再現可能ビルドのため
lock方式で管理しています(それまでは範囲指定のみだったため、`sam build`の
実行日時によって推移的依存の解決結果が変わり、Issue #33デプロイ時に無関係な
`platformdirs 4.11.4→4.11.5`でLayerVersionローテーションが発生しました)。

- `infra/layer/requirements.in`: 人間が編集するdirect依存の正本(範囲指定)
- `infra/layer/requirements.txt`: **自動生成物(直接編集禁止)**。全依存の
  完全pin。SAMが実際に使うmanifest
- `infra/layer/build-requirements.txt`: compileツール(uv)のバージョン固定

再生成の標準コマンド・compile条件(Python 3.12/Linux x86_64)は
[infra/README.md](../infra/README.md)の「Layer dependency lock」節を参照
してください。

### 14.1 依存更新の標準フロー

1. **意図的な更新**(範囲変更・パッケージ追加/削除): `requirements.in`を
   編集 → 標準コマンドで再生成 → `requirements.txt`の差分をレビュー → PR
2. **定期更新・セキュリティ更新**(範囲内のパッチ取り込み):
   `requirements.in`は変更せず、標準コマンドへ`--upgrade`(または
   `--upgrade-package <名前>`)を付けて再生成 → 差分 = 更新一覧としてレビュー → PR
3. いずれの場合も「次回デプロイでLayerVersionローテーション(Add/Removeペア)が
   発生する意図的な変更」であることをPR上で明示してください。逆に、依存を
   変更していないデプロイのChangeSetにDependenciesLayerの差分が現れた場合は
   想定外なので、原因を確認してから実行してください。

### 14.2 運用上の注意

- CIの`layer-lock-drift`ジョブが「`.in`から再生成した結果と`requirements.txt`の
  一致」を検証します(`.in`だけ変更して再生成を忘れるとCI FAIL)。
  `dependency-audit`ジョブは本番Layerに実際に入る`requirements.txt`を
  pip-audit監査します(脆弱性検出時は14.1の2の手順で更新)
- lockを更新しない限り、`sam build`は何度・いつ実行しても同一の依存集合を
  生成します。rollback時は過去コミットをcheckoutして`sam build`すれば当時の
  Layerが再現されます(PyPI側でyank/削除されていない限り)
- `tzdata`は`requirements.in`へ明示的に記載しています(pandas等がWindows限定
  markerで宣言しているため、Linux向け解決では自動には入らない。従来の
  Windows機ビルドの本番Layerとの同一性維持と、Lambda実行環境でのzoneinfo用
  IANAタイムゾーンデータ保証のため)。除外する場合は専用Issueで判断して
  ください

## 15. NotificationLogのGSI/TTL移行(Issue #32、2026-08-28追加)

NotificationLogテーブル(`jstock-notification_log`)の再送判定・dedup読み取りは
従来「全件Scan+Pythonフィルタ」であり、通知履歴の増加に伴い読み取りコストが
単調増加する構造でした。Issue #32で、主要オンライン読み取り(銘柄scope/
保有scopeの最新1件取得)をGSI Queryへ移行し、保持期間(TTL)を導入します。

- 保持期間: **730日**(再送判定期間・評価ホライズン最大250営業日・
  backtest/replay・監査参照をすべて包含)。TTLはcleanup専用であり、業務ロジックは
  削除時刻に依存しません(TTL失効後の残留は再送抑止が効き続ける安全側)
- **PROFIT_PROTECTION_ATTENTION(利益保全注意)のみTTL対象外**(同一局面の
  再送抑止が局面変化まで無期限に必要なため。件数は極小)。将来このtypeの件数が
  増大した場合は別Issueで再評価する
- 追加されるDynamoDBトップレベル属性(既存の`data` JSON本体は一切不変):
  `nl_stock_type_key` / `nl_holding_type_key` / `nl_sent_sort`(時刻+
  notification_idによる完全順序ソートキー)/ `nl_expires_at`(TTL、epoch秒)

### 15.1 段階リリース手順(各deploy・backfillは個別に人間承認が必要)

| Phase | 内容 | 単位 |
|---|---|---|
| A | save時のindex/TTL属性dual-write(読み取りは従来Scanのまま) | PR 32-A → main merge → deploy |
| backfill | 既存itemへの属性付与(下記15.2) | 人間が手元で実行 |
| B | GSI-1(`nl_stock_type_key-index`)追加 | PR 32-B → main merge → deploy |
| C | GSI-2(`nl_holding_type_key-index`)追加 + TTL有効化 | PR 32-C → main merge → deploy |
| 検証 | 移行完了acceptance(15.3) | `--verify`(read-only) |
| D | 読み取りのGSI Query切替 | PR 32-D → main merge → deploy |

CloudFormationは1回のupdateで1つのGSIしか作成できないためB/Cを分割しています。
TTL有効化は「一度削除されたitemはtemplateをrevertしても復元できない」不可逆な
データライフサイクル変更のため、GSI移行が進んだPhase Cで有効化します。
各deployは`sam deploy --no-execute-changeset`でChangeSetのReplacement列が
すべてFalseであることを確認してから実行してください(11節と同じ運用)。

### 15.2 backfill手順(Phase Aデプロイ後・Phase D前に必須)

`scripts/backfill_notification_log_index_attributes.py`を使います。既定は
dry-run(書き込みなし)。キー生成は通常save経路と同じ関数を共有しており、
backfillと通常writeでロジックが乖離しません。冪等のため何度でも再実行できます。

```bash
# 1. dry-run(対象件数・更新予定属性の集計を表示するだけ)
python scripts/backfill_notification_log_index_attributes.py --table jstock-notification_log

# 2. 本実行(誤爆防止のため--confirm-tableでテーブル名の再入力が必要)
python scripts/backfill_notification_log_index_attributes.py --table jstock-notification_log --execute --confirm-table jstock-notification_log

# 3. 検証(read-only)
python scripts/backfill_notification_log_index_attributes.py --table jstock-notification_log --verify
```

parse不能なitemが1件でも検出された場合、スクリプトはexit 1となり
「migration完了」とは見なせません。該当itemを個別に確認・解消してから
再実行してください。

### 15.3 移行完了acceptance criteria(Phase D deploy前の必須ゲート)

`--verify`(read-only)が以下すべてを満たしPASSすること:

1. 属性coverage 100%(全itemが期待どおりのindex属性を保持。ATTENTIONは
   `nl_expires_at`を持たないことも検査)
2. parse不能item 0件
3. GSI作成済みの場合: 全distinct scope keyについて「GSI Query(降順Limit=1)の
   latest == Scan由来のlatest」が完全一致(**この一致確認が不十分なまま
   Phase Dへ進むと、GSIから見えないlegacy itemにより previous=None →
   誤再送、という最重要回帰が起きます**)

`--verify`はphase-awareであり、GSIが未作成の段階では等価性検査をskipと表示
します(その段階ではfailureではありません)。ただしPhase D前は必ずGSI作成後の
`--verify`で3.を含む全項目のPASSを確認してください。

### 15.4 rollback方針(phase別)

- **Phase A**: コードrevert+再deployで戻せる。書き込み済みのindex/TTL属性は
  残存するが、Scan読み取りは`data` JSONのみを参照するため無害
- **Phase B**: template revert(GSI-1削除)で戻せる。ベーステーブルのデータは
  不変
- **Phase C**: GSI-2削除・TTL無効化はtemplate revertで可能。**ただしTTLに
  よって既に削除されたitemは自動復元できない**(730日より古いitemのみが対象の
  ため、通常の再送判定には影響しないが、この不可逆性を理解したうえで
  有効化すること)
- **Phase D**: コードrevert+再deployでScan読み取りへ戻せる(データ非依存)

「1つ前のmain SHAへ戻せば完全rollback」ではない点に注意してください
(特にPhase C以降のTTL削除分)。

## 16. BUY calibration用datasetのexport(Issue #28 Phase B、2026-08-28追加)

買い候補判定の較正(calibration)分析に使う正規化datasetを、既存の保存データ
(Recommendation・EvaluationResult・DecisionSnapshot、いずれも読み取りのみ)
から生成するCLIです。**判定・閾値・スコアには一切影響しません**(dataset生成・
exportのみ。統計分析・成功率評価・閾値提案はPhase C以降で別途扱います)。
日次バッチ・Lambdaへは組み込まず、人間のオンデマンド実行専用です。

```bash
# canonical export(JSONL。1行目がmetadata、以降が1 Recommendation×1 horizonの行)
jstock calibration export-dataset --output dataset.jsonl

# 閲覧用CSV(dataset.csv.meta.jsonというmetadataファイルも同時に生成される)
jstock calibration export-dataset --output dataset.csv --format csv

# sample定義を非重複window方式にし、選択行のみ・horizon未到来行を除いてexport
jstock calibration export-dataset --output dataset.jsonl \
  --sample-definition non-overlapping-window --selected-only --no-include-pending
```

- **return_basis=PRICE_ONLY**: リターンは株価のみ(配当・株主優待・手数料・
  税金を含まない)。metadataに常に明記されます
- benchmark: EvaluationResult保存済みの`benchmark_symbol`(TOPIX)を事実として
  出力し、実際のinstrument(現行コードではTOPIX連動ETF 1306.T)は
  「export時点の現在コードによる解釈」としてmetadata側にのみ記録します
- 行のraw粒度は「1 Recommendation × 1 horizon」で、重複Recommendationは
  dedupしません(sample定義は行を削除せずsample_selected等の列で注釈)
- horizon未到来はNOT_YET_EVALUABLE、到来済みで評価が無い行は
  EVALUATION_MISSINGとして残ります(黙って行を落としません)

### 16.1 記述統計レポート(Phase C1、2026-08-28追加)

exportしたdatasetから記述統計のanalysis artifact(JSONL)を生成できます。
これも読み取り専用で、判定・閾値・スコアには一切影響しません。
良いBUYの定義・優劣判定・閾値提案は含みません(将来のPhase C3で別途扱う)。

```bash
# sample定義は3種類: raw / non-overlapping-window / action-change
jstock calibration export-dataset --output dataset.jsonl --sample-definition non-overlapping-window
jstock calibration analyze --input dataset.jsonl --output report.jsonl
```

- **RAW datasetの注意**: 同一銘柄の日次Recommendation重複により行は独立標本では
  ありません(SAMPLE_DEPENDENCY_WARNING)。記述統計・カバレッジ・到達率の
  観察には使えますが、RAW行を独立と仮定した信頼区間・有意差・優劣判定には
  使わないでください(analyzeもRAWにはWilson区間・bootstrapを付けません)
- non-overlapping-window等も「独立性の保証」ではなく「同一銘柄内の重複window
  によるpseudo-replicationの軽減」です(市場共通要因・セクター相関・
  銘柄内の非重複window間依存は残ります)
- 全horizonを独立表示します(60営業日は将来のprimary候補としてmetadataに
  記載されるのみ。短期・中期・長期を1つの成功率へ混ぜません)
- 小標本セルは数値を隠さずSMALL_SAMPLE_WARNINGを付与します(閾値は
  分析パラメータとしてartifactのmetadataに記録)
- benchmark将来リターンによるregime別集計はex-post層別です(LOOK-AHEAD。
  予測特徴として使わないでください)
- リターンはPRICE_ONLY(配当・優待・税・手数料を含まない)のままです

## 17. valuation集約仮説のshadow分析export(Issue #20 Phase C、2026-08-28追加)

適正価格(Fair Value)の集約・grouping仮説を、保存済みRecommendationの
判定時点値だけを入力にoffline/shadowで並行計算し、raw shadow observation
(canonical JSONL)と記述統計summary(CSV)としてexportするCLIです。
**判定・閾値・適正価格・買付/利確価格・usability・通知には一切影響しません**
(観測・比較のみ。最良仮説の決定・ランキング・閾値提案は出力しません。
将来リターンとの結合分析は16節のcalibration datasetとrecommendation_idで
joinして別途行います)。日次バッチ・Lambdaへは組み込まず、人間のオンデマンド
実行専用です。

```bash
# canonical export(JSONL。1行目がmetadata、以降が
# 1 Recommendation×1 context(BUY_RAW/BUY_DECISION/SELL_RAW)×1 仮説の行)
jstock valuation-shadow export --output shadow.jsonl

# 記述統計summary(CSV)も同時に出力
jstock valuation-shadow export --output shadow.jsonl --summary shadow_summary.csv
```

- 復元不能・保存値と照合できない記録はOBSERVATION_UNAVAILABLEとして行ごと
  残します(黙って落としません)。現在の設定・現在の株価による再計算はしません
- H_A(現行方式)×BUY_DECISIONでは保存済みvaluation_anchorの再構成self-checkを
  行い、どの現行式とも一致しない記録はRECONSTRUCTION_MISMATCHとして可視化し、
  summaryのanchor差分統計から除外します
- 探索由来の仮説(実測相関から導出)はhypothesis_origin=
  EXPLORATORY_DATA_DERIVEDとして事前定義仮説と区別されます(性能比較時は
  探索に使ったsampleとvalidation sampleを分離すること)
- SELLのusability閾値(乖離2.0倍・最少2手法)は判定記録に保存されていない
  ため、shadow計算parameterとしてmetadataに明記されます(#21で保存済みの
  使用可否・理由コードがhistorical factです)
- shadow価格(shadow_entry_price等)は仮説anchor×判定時点の保存済み
  安全余裕率による参考値であり、約定・到達の判定には使いません

---

## 18. read-only観測・health checkにおける副作用確認(Issue #120、2026-09-02追加)

### 18.1 なぜ必要か

2026-09-02 08:00 JSTに、買い候補判定・保有銘柄判定の日次バッチが**両方とも
1銘柄も処理せずに停止**する障害が発生した。当日の判定・LINE通知はいずれも
0件だった(データ破壊・誤判定は無し。「何も出さなかった」障害)。

原因は、バッチ開始時に呼ぶ株主優待レジストリの健全性チェックが、
名前上は読み取りAPIである `list_all()` を呼び、その内部で権利確定日の
再計算結果を `repository.save()`(DynamoDB PutItem)へ書き戻していたこと。
両Lambdaは当該テーブルへ**読み取り専用IAM**しか持たないため
`AccessDeniedException` となり、dispatch前にバッチ全体が落ちた。

書き戻しは「再計算値が保存値と異なるとき」だけ発生するため、権利確定日が
繰り上がる日付境界を越えた日に初めて顕在化した。**同じコード・同じIAMのまま、
日付だけで発火する**タイプの障害である。

### 18.2 恒久ルール

**実行するコマンドやメソッドの名称がread系だからという理由で、
安全(read-only)と判断してはならない。**

Production read-only verification、health check、validation、
IAM least-privilege設計を行う際は、**呼び出し先まで含めて**次の副作用が
無いことを確認する。

| 確認対象 | 具体例 |
|---|---|
| repository層の状態変更 | `save` / `upsert` / `update` / `delete` |
| DynamoDB | `PutItem` / `UpdateItem` / `DeleteItem` / `BatchWriteItem` / `TransactWriteItems` |
| S3 | `PutObject` / `DeleteObject` |
| キュー・非同期 | SQS `SendMessage` / SNS `Publish` / Lambda `Invoke` |
| 外部送信 | LINE Messaging APIへの送信 |

確認は名前の1段スキャンでは不十分である。**Issue #120の実バグは、
read-like名の関数から1段だけ辿っても検出できなかった**
(`list_all()` → `_refresh_and_persist()` → `save()` と、write動詞を持たない
privateヘルパを1段挟んでいたため)。推移的に辿ること。

### 18.3 設計時の要求

- read-onlyと定義した処理にhidden writeを持たせない。
- 書き込みを伴う場合は、**API契約・名称・IAM・テスト**からその事実が
  判別できるようにする(例: `get_or_create_*` のように名称へ表す)。
- 観測・健全性チェックの類は、失敗しても本体処理を止めない(fail-soft)。
  ただし**沈黙させない**。件数等が取得できなかった事実を構造化ログへ残す。
- fail-softの対象は観測処理自身の失敗に限る。**判定に必要なデータの取得失敗まで
  握り潰さない**(business dataの取得失敗は従来どおり銘柄単位で失敗として扱う)。

### 18.4 IAM設計との関係

新しいテーブルへの権限を設計する際は、11節(テーブル追加時の注意)に加えて、
そのLambdaが到達しうる**すべてのコードパス**の副作用を確認したうえで
最小権限を決める。read-onlyで足りるはずの経路にwriteが混ざっている場合、
権限を足すのではなく**その経路のwriteを外せないか**を先に検討する
(Issue #120では、書き戻していた値が純粋な派生値であり永続化する価値が
無かったため、IAMを広げずに読み取り側の書き込みを除去した)。

## 19. 本番シークレットのローテーション手順(Issue #117 Phase R1、2026-09-05追加)

### 19.0 なぜ手順が必要か(この節の前提)

Secrets Managerの値を更新しただけでは、**Lambdaは新しい値を使わない。**

```
credential再発行
  -> Secrets Managerを更新
  -> コード無変更でデプロイ
  -> CloudFormationが「変更なし」と判定
  -> dynamic referenceが再解決されず、Lambdaは旧credentialのまま
```

CloudFormationのdynamic referenceは、**それを含むリソースが更新されるときにしか
再解決されない**ためである。表面上はデプロイが成功するため、この状態は
気づかれないまま継続しうる。

Phase R1では、秘密と同じEnvironmentブロックへ**非秘密のマーカー**を置き、
ローテーション時に運用者が明示的にその値を変えることでリソース更新を強制する。

```
LineCredentialRotationVersion     -> LINE_CREDENTIAL_ROTATION_VERSION
EdinetCredentialRotationVersion   -> EDINET_CREDENTIAL_ROTATION_VERSION
```

```
通常のデプロイ      マーカーを変えない -> 再解決を意図しない
ローテーション時    マーカーを明示的に変える -> 再解決を強制する
```

マーカーは非秘密である。**秘密値・トークン・キーを絶対に入れない**
(この値は環境変数として平文で残り、`describe-stacks`等からも見える)。

### 19.1 この仕組みの既知の性質(手順の前に必ず理解すること)

#### 全Lambda関数が更新対象になる

LINE・EDINETの秘密は`Globals`のEnvironmentに置かれているため、
**どちらのマーカーを変えても全Lambda関数が更新対象になる。**
これは現アーキテクチャ上の制約であり、恒久対策(実行時取得方式)で解消する。
マーカーの目的は更新対象を絞ることではなく、**再解決が確実に起きること**と
**着地を非秘密の値で確認できること**である。

#### 他の秘密も同時に再解決される

全関数が更新されるため、Webhook署名検証用の秘密(ローテーション対象外)も
同時に再解決される。値を変更していなければ結果は同じ値であり実害は無いが、
**Secrets Manager側に意図しない未反映の変更が残っていると、それも一緒に
本番へ入る。** ローテーション前に、対象外の秘密について未反映の変更が無いことを
確認する。

#### R1自体のデプロイでも一度再解決が起きる

マーカー環境変数の追加はEnvironmentの変更であるため、**R1を本番へ入れる
デプロイ自体が一度の再解決を伴う。** これは仕組みが動くことの証明にもなるが、
「Secrets Managerの現在値が本番へ入る」ことを意味する。R1のデプロイ前に、
各シークレットの現在値が意図した稼働中の値であることを確認する。

### 19.2 共通のHuman Gate(R2で必須)

以下は**それぞれ別の承認**として扱う。まとめて1回の承認にしない。

```
1  credential再発行の承認        ★ 巻き戻せない操作の直前に必ず置く
2  Secrets Manager更新の承認
3  ChangeSet CREATEの承認
4  そのexact ChangeSetのEXECUTE承認   CREATE != EXECUTE
```

```
LINEとEDINETを同一波でローテーションしない。
失敗時にどちらが原因か切り分けられなくなるため。
```

#### 例外: marker-only検証deploy(実際のcredential rotationには適用されない)

「marker-only検証deploy」とは、**secretの値そのものは一切変更せず**、
ChangeSetのparameter override等でマーカー値のみ(例: 0→1)を変更し、
dynamic referenceの再解決が実際に起きることを確認する操作を指す。
具体例: Issue #227(2026-09-10実施、Release W4)。LINE・EDINET両方の
マーカーを同時に0→1へ変更したが、Secrets Manager側の値は1つも
変更していない。

```
marker-only検証deployは、上記「LINEとEDINETを同一波でローテーションしない」
の対象外である。

理由: 同一波を禁じる目的は、失敗時にどちらのcredential変更が原因か
切り分けられなくなることを防ぐためである。marker-only検証deployは
credentialを1つも変更しないため、その失敗自体が原理的に起こらない。
```

```
★ この例外は、実際のcredential rotation(secretの値そのものを更新する操作)
  には一切適用されない。credentialを1つでも変更する波は、検証目的を
  兼ねていても本節冒頭の制約(同一波で行わない)を通常どおり適用する。
  「marker-only」と呼べるのは、その波でSecrets Managerへの書き込みが
  ゼロ件である場合に限る。
```

### 19.3 LINEチャネルアクセストークンのローテーション

本システムのトークンは**長期チャネルアクセストークン(long-lived)**である
(2026-09-04に確認)。公式仕様上の性質は次のとおり。

```
同時に有効なトークンは1つだけ
再発行すると現行トークンは無効化される
ただし再発行時に、現行トークンの有効期間を**最大24時間延長**できる
```

```
★ 延長を選ばずに再発行すると、新しい値が本番へ着地するまでLINE通知が
  全面停止する。延長の選択は独立したチェック項目として扱う。
```

手順。

```
1   Human Gate: 再発行の承認を得る
2   コンソールで再発行する。このとき **現行トークンの有効期間を延長する**
    (延長を選んだことを、次へ進む前に確認する)
3   Human Gate: Secrets Manager更新の承認を得る
4   Secrets Managerの該当シークレットを新しい値へ更新する
    -> 入力方法は19.6を必ず参照(コマンド引数へ値を書かない)
5   LineCredentialRotationVersionを**明示的に別の値へ変更**する
    (例: 単調増加する整数。日付や連番でよい。秘密は入れない)
6   Human Gate: ChangeSet CREATEの承認を得る
7   ChangeSetをCREATEし、差分を確認する
    -> Lambda関数がModifyになっていること(NO_CHANGESなら19.5-Cへ)
    -> Replacementが発生していないこと
    -> 意図しないリソースが含まれていないこと
8   Human Gate: **そのexact ChangeSet**のEXECUTE承認を得る
9   EXECUTEする
10  着地確認: LINE_CREDENTIAL_ROTATION_VERSIONが新しい値になっていること
    -> 確認方法は19.6(環境変数を全件出力しない)
11  疎通確認: LINE通知が実際に送れること
    -> 延長した24時間が切れる前に完了させる
```

### 19.4 EDINET APIキーのローテーション

公式仕様(EDINET API仕様書 Version 2)上の性質。

```
再発行すると再発行前のAPIキーは無効化される
新旧の併存はできない
旧キーへ戻す手段が無い(復旧は前進のみ)
```

```
★ LINEと違い、有効期間の延長に相当する猶予が無い。
  再発行した瞬間から、新しい値が着地するまでEDINET取得は失敗する。
  したがって「戻せる状態を先に作ってから再発行する」順序にする。
```

手順。

```
1   ローテーション時間帯を決める(日次バッチと重ならない時間にする)
2   Secrets Manager更新とマーカー変更以外の準備を先に済ませる
3   Human Gate: 再発行の承認を得る ★ここから先は巻き戻せない
4   コンソールで再発行する(確認ダイアログでOKを押した時点で旧キーは無効)
5   Human Gate: Secrets Manager更新の承認を得る
6   Secrets Managerの該当シークレットを新しい値へ更新する(19.6参照)
7   EdinetCredentialRotationVersionを明示的に別の値へ変更する
8   Human Gate: ChangeSet CREATEの承認を得る
9   ChangeSetをCREATEし、19.3-7と同じ観点で差分を確認する
10  Human Gate: そのexact ChangeSetのEXECUTE承認を得る
11  EXECUTEする
12  着地確認: EDINET_CREDENTIAL_ROTATION_VERSIONが新しい値になっていること
13  疎通確認: EDINETからの取得が成功すること
```

### 19.5 失敗パターンと復旧

```
ROLLBACK = FORWARD_FIX を基本とする。
```

credentialは「元に戻す」ことができない場合がある(EDINETは常に不可、LINEは
延長期間を過ぎると不可)。**CloudFormationのアーティファクトのロールバックと、
credentialのロールバックを混同しない。** スタックを前のバージョンへ戻しても、
無効化された旧credentialは復活しない。

```
A  再発行は成功したが、Secrets Managerの更新に失敗した
   -> 新しい値は手元にある。更新をやり直す。
      再発行はやり直さない(やり直すと今の値も無効になる)

B  Secrets Manager更新は成功したが、ChangeSet CREATEに失敗した
   -> 前進して再試行する。Secrets Managerを元に戻さない
      (旧credentialは既に無効であり、戻しても復旧しない)

C  ChangeSetがNO_CHANGESになった
   -> ★ R1の設計上これは異常。マーカーの変更が効いていない可能性がある。
      次を確認する。
        マーカーの値を実際に前回と違う値にしたか
        マーカーを渡すパラメータ名が正しいか
        既定値のまま何も渡していないのではないか
      EXECUTEへ進まない。原因を特定するまで停止する

D  デプロイに失敗した
   -> スタックの状態を確認し、前進して修正する。
      credentialは既に切り替わっているため、アーティファクトだけを
      戻しても復旧しない点に注意する

E  疎通確認に失敗した
   -> まず着地確認(マーカーの値)を見る。
      マーカーが新しい値なら再解決は起きている
        -> Secrets Managerへ入れた値そのものを疑う
      マーカーが古い値のままなら再解決が起きていない
        -> Cと同じ調査へ進む
      LINEの場合、延長した24時間が残っているうちに切り分ける
```

### 19.6 セキュリティのガードレール

現アーキテクチャでは秘密がLambdaの環境変数へ平文で入る。したがって
**確認作業そのものが漏洩経路になりうる。**

```
禁止  Lambdaの構成を全件出力すること
      (環境変数がそのまま出力され、秘密が端末・履歴・ログへ残る)
必須  必要なフィールドだけを --query 等で絞って取得する
      着地確認で必要なのはマーカーの値だけであり、他の環境変数は不要
```

```
禁止  通常の確認作業でSecrets Managerの値そのものを取得すること
      (SecretString / SecretBinary を読み出さない)
      値が正しいかは「疎通するか」で確認する
```

```
禁止  秘密値をコマンドの引数に書くこと(シェル履歴へ残る)
推奨  次のいずれか
        コンソールのSecrets Manager画面で値を入力する(履歴が残らない)
        やむを得ずCLIを使う場合は、権限を絞った一時ファイルから読み込み、
        作業後にそのファイルを確実に削除する
      いずれの場合も、値を画面へ表示させない
```

```
禁止  秘密値を次へ書くこと
        Issue / PR / コミットメッセージ / CIログ / スクリーンショット
        設定ファイル / テストのfixture / この手順書
```

```
マーカーの値には秘密を入れない。
マーカーは平文で残ることを前提とした非秘密の版数である。
```

---

## 20. DynamoDBの復旧手順(Issue #137、2026-09-05追加)

### 20.1 何が設定されているか

Phase Aのデータ分類にもとづき、**失うと再生成できないデータを持つ37テーブル**へ
次の4つを`infra/template.yaml`で設定している(手動設定は行わない)。

```
PointInTimeRecoverySpecification   直近35日への時点復元
DeletionProtectionEnabled          DeleteTable自体の禁止
DeletionPolicy: Retain             stack削除・template除去で実体を残す
UpdateReplacePolicy: Retain        置換時に古い実体を残す
```

cache(外部から再取得可能)と一時状態(ロック・claim・進捗・VALIDATION専用)の
17テーブルには**意図的に付けていない**。

```
★ 一時状態は「復元してはいけない対象」である。
  ロック・claimを過去時点へ戻すと、取得済みロックの復活や
  送信済み通知の再送といった二次障害を起こす。
```

分類はテストで固定している(`tests/unit/test_infra_issue_137_dynamodb_data_protection.py`)。
新しいテーブルを追加すると、保護を付けるか対象外リストへ入れるまでテストが落ちる。

### 20.2 4つの機能の違い(混同しない)

| 機能 | 効く契機 | 守る対象 |
|---|---|---|
| PITR | 復元操作 | 直近35日の**内容** |
| DeletionProtectionEnabled | DeleteTable | **テーブル自体** |
| `DeletionPolicy: Retain` | stack削除 / templateから定義を外す | 実体を残す |
| `UpdateReplacePolicy: Retain` | 置換が発生したとき | **古い方**を残す |

互いの代替にはならない。

### 20.3 復旧目標

```
RPO_TARGET                  約5分
RESTORE_POINT_GRANULARITY   1秒
RTO_TARGET                  3時間以内
BUSINESS_TARGET             可能なら次の08:00 JSTの営業バッチまでに復旧する
```

RPOが「秒単位」ではなく約5分なのは、PITRの**最新復元可能時刻が現在時刻より
数分前**になるためである。復元時刻は1秒刻みで選べるが、直前数分ぶんは戻らない。

```
3時間はJstock側の運用目標であり、AWSが保証する値ではない。
```

### 20.4 復元の前提(必ず読む)

```
RESTORE_MODE = NEW_TABLE_ONLY
```

PITRの復元は**常に新しいテーブルを作る**。既存テーブルへのin-placeロールバックは
できない。したがって「PITRをONにすればワンクリックで戻せる」は誤りであり、
復元後のcutoverまでが手順である。

復元先テーブルへ**引き継がれない**もの。

```
PITR設定 / DeletionProtectionEnabled / TTL / タグ / stream /
auto scaling / resource policy / CloudWatchアラーム
既存ARNを参照するIAM・アプリ設定も自動追従しない
```

本システムはテーブル名を`<接頭辞>-<論理名>`として単一の環境変数から解決している。
接頭辞は54テーブル共通のため、**接頭辞の切替による復旧は使えない**
(1テーブルだけ戻したい場合でも全テーブルが切り替わる)。

### 20.5 テーブル横断の整合性

```
SHARED_RESTORE_POINT_REQUIRED = YES
```

購入・売却の確定処理は、取引・購入ロット・保有の**3テーブルを1回の
TransactWriteItemsで同時に書いている**。したがって関連テーブルを別々の復元時刻へ
戻すと、書き込み時には存在し得なかった不整合が生まれる。

```
関連テーブルを別々のrestore timestampへ戻すことは標準手順として禁止する。
```

復元後は最低限、次をread-onlyで検証する。

```
取引の累積と購入ロットの整合
購入ロットと保有の整合(数量・金額の不変条件)
orphanレコードの有無(片側にしか存在しない関連)
3テーブルが同一の復元時刻であること
```

### 20.6 復元drillの手順(実施は別Human Gate)

Production を直接巻き戻さない。隔離したテーブルへ復元して検証する。

```
PRECONDITION
  対象テーブルでPITRが有効
  Human の drill 承認を取得済み
  Production の変更が無い時間帯であること

RESTORE_TIMESTAMP
  latest restorable time を確認し、その値以前の時刻を選ぶ
  障害復旧の場合は「事象発生の直前」を選ぶ

RESTORE_TARGETS
  トランザクション整合グループ(取引・購入ロット・保有)は必ずまとめて扱う

SHARED_RESTORE_POINT
  対象テーブルすべてへ同一の復元時刻を指定する

RESTORE_DESTINATION
  本番と衝突しない名前の新規テーブルへ復元する
  例: <接頭辞>-restoredrill-<論理名>-<日時>

NETWORK/ACCESS_ISOLATION
  復元先はアプリから参照しない。CLIのread-only検証のみ
  Lambda の環境変数・接頭辞は変更しない

NO_PRODUCTION_TRAFFIC
  本番テーブルへは一切書き込まない

SCHEMA_VALIDATION      キー構成・GSI・属性の型
ITEM_COUNT_VALIDATION  件数が復元時刻の期待と矛盾しないこと
BUSINESS_KEY_VALIDATION 主キーの重複・欠落・必須項目の欠落
CROSS_TABLE_CONSISTENCY 20.5の検証を実施する

TTL_RECONFIGURATION                復元先はTTLが無効。必要なら設定し直す
TAG_RECONFIGURATION                タグは引き継がれない
STREAM_RECONFIGURATION             streamは引き継がれない
PITR_RECONFIGURATION               復元先のPITRは無効
DELETION_PROTECTION_RECONFIGURATION 復元先の削除保護は無効

CUTOVER_OPTIONS                    20.7を参照
ROLLBACK
  本番へ書き戻す場合は、実施直前に現状のon-demand backupを取得してから始める
CLEANUP
  drill用テーブルを削除する(保管コストを残さない)
EVIDENCE
  実施日時・復元時刻・検証結果・所要時間をIssueへ記録する
```

```
drillで確認したいのは「復元できること」ではなく、
「復元してから通常運用へ戻すまでの手順が実際に成立すること」である。
```


```
★ 実施して分かったこと(2026-09-08 の drill。Issue #226)

★ 1  件数の確認に ItemCount を使わない
     復元直後の describe-table は ItemCount = **0** を返す。DynamoDB の ItemCount は
     約 6 時間ごとの更新であり、新規テーブルでは 0 のままである。
     ★ **必ず Select=COUNT の scan で数え直すこと。**
     ★ これを知らないと「復元失敗」と誤判定する(本 drill で実際に 3 テーブルとも
       0 が返り、実件数は 8 / 27 / 27 で一致していた)。

★ 2  復元先を本番へ昇格する場合、**最初に** PITR と削除保護を有効化する
     復元先は PITR = DISABLED / 削除保護 = False で作られる(引き継がれない)。
     ★ 昇格した瞬間から **バックアップが無い状態**になる。
     TTL / stream / タグも引き継がれない。

★ 3  transactions は holdings_v2 への参照整合性を **持たない**
     FULL_SELL は保有を消すため、取引記録が指す holding_id が holdings_v2 に
     残らないことがある。★ 孤児が出るのは **正常**であり復元の欠陥ではない。
     ★ 判定は「本番と復元先で孤児件数が一致するか」で行う
       (本 drill では両方 5 件で一致し、復元が忠実であることを確認した)。

★ 併せて  タグは本番側も 0 件だった。★ 復元先を残した場合の課金を
     **タグでは追跡できない**。実額は請求で確認する。
```
### 20.7 実際の復旧時のcutover方針

```
第一候補  復元専用テーブルへ復元 → 検証 → 必要なレコードだけ本番へ書き戻す
```

利用者所有データは規模が小さいため、この方式が現実的である。CloudFormationの
管理下から外れるテーブルが生じない点でも安全である。

```
本番テーブルへの書き戻しは PRODUCTION_DATA_MUTATION であり、別のHuman Gateが要る。
runbookに書いてあることは実行してよいことを意味しない。
```

「本番テーブルを丸ごと復元テーブルへ切り替える」方式は標準にしない
(接頭辞が全テーブル共通であり、CloudFormationの管理とも整合しないため)。

### 20.8 直前数分ぶんの取りこぼし

```
RECENT_WRITE_RECONCILIATION_REQUIRED = YES
```

PITRの最新復元可能時刻には遅れがあるため、障害直前の数分間の更新は復元されない
可能性がある。復旧時は次を確認する。

```
1  事象の開始時刻を特定する
2  latest restorable time を確認する
3  その差分(gap window)を明示する
4  gap window中に利用者操作があったかを確認する
   LINEの操作履歴・CloudWatch Logs・取引履歴などから追跡できる範囲で確認する
5  復元されなかった操作があれば、利用者へ再登録を依頼する
6  再登録の内容と実施をIssueへ記録する
```

実データ(銘柄・数量・単価・氏名)は記録・引用しない。

### 20.9 テーブル置換が必要な変更を行うとき

```
「とりあえずDeletion Protectionを無効化する」を標準手順にしてはならない。
保護を自動で外す運用にすると、保護が実質的に無効になる。
```

置換が必要かどうかは、変更するpropertyがreplacementを要求するかで決まる。
すべての更新が置換になるわけではない。次の順で確認する。

```
1  その変更に本当にreplacementが必要か(別の手段で目的を達成できないか)
2  対象がauthoritative dataか(失うと再生成できないか)
3  PITR・backupの状態を確認する
4  依存するconsumer(Lambda・IAM・CLI)を洗い出す
5  TableNameを明示指定していることによる制約を確認する
   同名テーブルを同時に存在させられないため、置換の可否に影響する
6  ChangeSetを作成し、実際にreplacementが起きるかを確認する
7  Human Gate(ここまでは調査。ここから先は承認が要る)
8  replacementが避けられない場合のみ、そのケース固有の移行手順を設計する
9  移行後の検証(20.5と同じ整合性チェック)
10 古い実体のcleanupは別のHuman Gate(Retainにより自動削除されない)
```

```
TEMPORARY_DISABLE_REQUIRED = CASE_SPECIFIC
```

Deletion Protectionの一時解除が必要なケースが**実在すると確認できた場合にのみ**、
そのケース限定の手順として設計する。一般則にはしない。

### 20.10 今回入れていないもの

```
SCHEDULED_ON_DEMAND_BACKUP = NO
AWS_BACKUP_PLAN            = NO
CROSS_ACCOUNT_COPY         = NO
CROSS_REGION_COPY          = NO
```

PITRの35日で開始し、必要性は実績で判断する。35日を超える保管や、
AWSアカウント侵害への耐性(別アカウントへの退避)が必要になった場合の拡張点として
記録しておく。

```
★ 現在の保護は「誤操作・不具合」には有効だが、
  「credential侵害」には十分でない。
  同一アカウント内の強い権限を持つprincipalは、primaryもbackupも消せる。
  この点は Issue #133 / #164 の解消と合わせて評価する。
```

PITRの課金は保存量に比例するため、保持期間の設計(Issue #138)と足並みを揃える。

---

## 21. 公開面へ個人情報が混入した場合の是正手順(Issue #131、2026-09-06追加)

本リポジトリはPUBLICである。**Git管理ファイルだけでなく、commit message、
Issue / PR の本文とタイトル、コメント、label、branch名も、そのまま
インターネットへ公開される。**

公開面へ書く前の遵守事項は
[user_manager_collaboration_protocol.md](user_manager_collaboration_protocol.md)
11節が正本である。本節はそこを通り抜けて**露出してしまった後**の手順を扱う。

### 21.1 検出経路

| 経路 | 対象 | 実行契機 | 失敗したとき |
| --- | --- | --- | --- |
| `pii-scan` ジョブ | Git管理ファイルの内容 | 全push / PR | PRが止まる |
| `pii-scan-commit-messages` ジョブ | そのPRが持ち込むcommit message | PR | PRが止まる |
| `pii-metadata-audit` workflow | Issue / PR の本文・タイトル、コメント、label、branch名 | 日次(06:10 JST)+ 手動 | 通知のみ。PRは止まらない |

手動実行は Actions タブの `PII metadata audit` から `Run workflow`
(`workflow_dispatch`)。ローカルからは以下(read-onlyであり書き込みは行わない)。

```bash
python scripts/audit_public_metadata_pii.py kouitic/jstock_advisor
python scripts/scan_commit_messages_pii.py "<base>..<head>"
```

いずれも `scripts/scan_for_pii.py` の denylist を共有し、**一致した文字列は
出力しない**(面 / 所在 / 検出理由 / ハッシュ接頭辞のみ)。是正の際もこの
表現のまま扱い、値そのものを報告・Issueコメント・chatへ再掲しないこと。

```
★ denylist方式であり、全てのPIIを検出できる保証はない。
  検出は事後の網であって、事前防止の代わりにはならない。
  日次監査は「露出から検知まで最大で24時間かかる」ことを意味する。
```

commit trailer の `noreply@anthropic.com` とGitHubの `*.noreply.github.com` は
特定個人へ到達しない機械アドレスであり、メール様式の検出から除外している
(付与が義務づけられており、検出しても是正できないため)。除外はこの2系統に
限定してあり、ドメイン全体は除外していない。

### 21.2 検出したらまず行うこと

1. **影響範囲を確定する。** どの面 / どの所在(Issue番号・comment id・
   commit SHA・branch名) / いつ公開されたか。
2. **露出時間を見積もる。** 投稿時刻から現在まで。日次監査での検出なら
   最大で1日ぶん遡る。
3. **21.3 の是正と 21.4 の不可逆性を「両方」評価する。**
   本文を直しただけでは終わらない。

### 21.3 面ごとの是正手順

| 面 | 手順 | 残るもの |
| --- | --- | --- |
| Issue / PR の本文・コメント | 該当箇所を架空値(「所有者A」等)へ編集 | **編集履歴** |
| Issue / PR のタイトル | 同上 | **編集履歴** |
| label | rename ではなく削除して作り直す(renameは名前の履歴を残す) | 付与されていたIssueのタイムライン |
| branch名 | 新しい名前でbranchを作成してpushし、旧branchを削除 | PRのタイムラインに旧head branch名、dangling commit |
| commit message | history rewrite が必要。**mainに対しては原則行わない** | rewrite前のcommitがforkやcloneに残る |

commit message の是正は force push を伴い、他の作業者の作業branchを壊す。
**作業AIは単独で実行しない**(21.5)。未mergeかつ自分だけが使っているbranchで
あっても、実行前に人間の判断を得ること。

### 21.4 不可逆性(必ず理解しておくこと)

「編集すれば消える」は**誤り**である。編集後も次が残る。

- **編集履歴。** Issue / PR / コメントの edit history は、書き込み権限の無い
  閲覧者にも表示される。編集前の本文がそこに残る。
- **通知メール。** 投稿時点で watcher へ配信済みであり、取り消せない。
- **外部の複製。** 検索エンジンのcache、GHArchive等の公開アーカイブ、
  各種ミラー・スクレイパ。GitHubの管轄外であり、GitHub側を消しても消えない。
- **fork / clone。** commit は他者の手元に残る。

したがって是正の目的は「無かったことにする」ではなく、
**追加の露出を止め、残存経路を人間が把握したうえで判断できる状態にする**
ことである。

### 21.5 Human escalation の境界

作業AIが単独で行ってよいこと。

- 検出の報告(面 / 所在 / 検出理由 / ハッシュ接頭辞のみ)
- **自分が**作成した未mergeのPR本文・**自分の**コメントの編集
- **自分が**作成し、まだ他者が使っていないbranchの作り直し

必ず人間の判断を仰ぎ、AIが単独で実行しないこと。

- 他者が作成したIssue / PR / コメントの編集・削除
- Issue / PR そのものの削除
- history rewrite(force push)、mainへの介入
- GitHub Support への削除依頼(21.6)
- リポジトリのPRIVATE化
- 露出の事実をどこまで公表するかの判断

判断を仰ぐ際も、値そのものを書かない。所在とハッシュ接頭辞で示す。

### 21.6 GitHub Support への削除依頼の要否

依頼が要るのは「**GitHub側にしか残っておらず、こちらの操作では消せない複製**」
を消す場合である。

依頼で消せる可能性があるもの。

- 編集履歴(edit history)
- 削除済みbranch / fork に残る dangling commit
  (SHAを直接指定するURLで到達できる)

依頼でも消せないもの。

- 検索エンジンのcache、GHArchive等の外部アーカイブ、他者のclone

```
依頼する    実在人物の氏名・個人メールアドレス・住所・電話番号など、
            本人へ到達しうる情報が公開面へ出た場合(人間が実施する)
依頼しない  架空値・銘柄コード・ハッシュ接頭辞・内部の状態値のみの場合
```

依頼文へ露出した値そのものを書かない。**URLと所在で示す**
(依頼文自体がGitHubのサポート系統へ残るため)。

### 21.7 事後

- 再発防止をIssueとして起票する。3つの検出経路(21.1)のどれが漏らしたか、
  事前防止(11節)のどこを通り抜けたかを記録する。
- denylistへ追加する場合は `scripts/scan_for_pii.py` の `_KNOWN_PII_HASHES` へ
  **SHA-256ハッシュのみ**を追加する。平文をリポジトリへ書かない
  (ハッシュ値の計算はGit管理外のローカルで行う)。denylistは
  `pii-scan` / `pii-scan-commit-messages` / `pii-metadata-audit` の
  3経路が共有するため、追加は1箇所で足りる。
## 22. 投資仮説 baseline の pointer 不整合の復旧(Issue #272、2026-09-09追加)

保有判断が「その保有だけ永久に止まる」状態になる原因の 1 つが、
投資仮説 baseline の **pointer 不整合**である。

```
(A) pointer が無く、baseline の履歴だけがある
(B) pointer はあるが、指す baseline が見つからない / version が食い違う
```

いずれも `get_active_baseline()` が integrity_error を返し、保有判断はその時点で
打ち切られる。**通常経路では復旧しない**(pointer を作る `activate_baseline()` は
integrity_error の early return より後にあるため、何度実行しても到達しない)。

### 22.1 どう気づくか

**新しい alert は無い。既に見えている。**
該当保有は毎日の BATCH_SUMMARY 通知に **failed として対象つきで**現れる
(`evaluation_status = ANALYSIS_FAILED` / `error_code = DATA_INTEGRITY_ERROR`)。
監査ログ(`audit_log`)と保有評価レコードにも記録される。

つまり **利用者は気づいているが直せない**、という状態だった。本節の CLI がその手段である。

### 22.2 手順

```
1  dry-run で対象を確認する(★ 読み取りのみ。書き込みは行わない)
     jstock baseline-repair scan

   出力は holding_ref(sha256 の先頭 8 文字)・理由区分・baseline 履歴(version / status /
   origin)の一覧。★ 所有者名・銘柄コードは出力しない(Issue #135)。
   ★ baseline_id も出力しない。生成規則が `<holding_id>:v<version>` であり
     **holding_id(= 所有者#銘柄コード)がそのまま埋め込まれている**ため。
     holding 内では version が一意なので、version だけで特定できる。

2  ★ 対象が 0 件なら、そこで終わる。
   ★ 0 件は「壊れている」ではなく「対象なし」を意味する。

3  対象があれば、採用する baseline を**人が決める**。
   (A) は履歴の最新 version が既定候補として表示される。
   ★ (B) は候補を自動で選ばない。--baseline-version の明示指定が必須である。

4  ★ Production に対する修復の実行は **利用者の承認**を得てから行う。
     jstock baseline-repair apply --holding-ref <ref> [--baseline-version <n>]

5  次の 08:00 の自然実行で、その保有の判定が再開することを確認する
   (BATCH_SUMMARY の failed が減る)。
```

### 22.3 なぜ自動で直さないのか

pointer は **意図的に古い baseline を指している場合がある**
(baseline は `supersedes_baseline_id` で連鎖し、active は必ずしも最新ではない)。
baseline は保有判断スコアの比較基準であり、**active が変われば score が変わる**。

自動修復を入れると「気づかないうちに判定基準が入れ替わる」ことになり、
「毎日 failed が通知され続ける」よりも危険である。したがって
**バッチ内での自己修復は実装しない**。人が確認して実行する。

### 22.4 やってはいけないこと

```
★ 動作確認のために pointer を意図的に壊さない(証拠を人工的に作らない)。
★ apply を承認なしに Production へ実行しない。
★ scan の出力を、holding_ref 以外の形(所有者名・銘柄コード)で記録・共有しない。
```


## 23. LINE認証情報の欠落を可視化する変更(Issue #117 stage (b))のProduction反映の運用条件(2026-09-19追加)

この節は、Issue #117 stage (b)(LINE通知clientの構築を、認証情報が無い場合に黙って
ConsoleLineClientへ落とさず可視化する変更)をProductionへ反映するときの運用条件を定める。
決定の出典は #117 issuecomment-5740707381(USERの決定、MANAGER経由の伝達)。
`PRODUCTION_DEPLOY_APPROVED = NO` であり、**ChangeSetのCREATEとEXECUTEは従来どおりそれぞれ別のHuman Gate**である
(正本は `docs/user_manager_collaboration_protocol.md` 1.5節と本書19.2節)。

### 23.0 反映で何が変わるか(前提)

認証情報が正常な運用の挙動は変わらない。**欠落した場合だけ**、従来は黙って通知が失われていたものが、
次のとおり可視化される(失敗する)。

```
line_webhook                 Lambda失敗(Errors)
dispatcher(NEW_CANDIDATE)    lease取得・BatchRuns作成の前に失敗(バッチが作られない。状態変更なし)
buy_candidates / holdings /  NORMAL・VALIDATION+SENDは失敗。DRY_RUNは従来どおり(認証情報不要)
  disclosure_check
worker / terminal_failure    NEW_CANDIDATE_SCREENINGを含む呼び出しは、状態変更の前に失敗。MAINTENANCEのみは影響なし
reconciler                   ウォッチリスト登録は継続し、通知だけNOTIFICATION_FAILED。全処理の後にErrors
weekly_review・CLI 3本        未変更(従来どおり)
```

LINE認証情報は `infra/template.yaml` の `Globals.Function.Environment.Variables` で、`{{resolve:secretsmanager:...}}` により
**デプロイ時に全Lambda共通の値として固定**される(19.0節)。実行時にSecrets Managerを読みに行くのではない。

### 23.1 反映の窓と、実行直前の必須確認(D1。#430の運用条件を含む)

```
推奨する窓        土曜 12:00 以降 〜 日曜(JST)
                  ★ 時刻だけを安全条件にしない。次の必須確認を全て満たさなければ、土曜12:00以降であっても実行しない。
```

窓の根拠: dispatchは月〜金の06:00のみで、バッチは遅くとも24時間(`batch_processing_timeout_hours`)で毎時のreconcilerが確定する。
金曜開始のバッチは土曜の朝までに終端する。土曜10:00・11:00の月次・四半期レビューの後なら、LINEを使うジョブとも重ならず、
月曜06:00のdispatchまで丸1日の是正時間が取れる。

**実行直前の必須確認(全て満たすこと。read-onlyで確認する)**

```
C1 NEW_CANDIDATE_SCREENINGの進行中バッチが0であること。
   batch_runsの最新バッチが終端状態(COMPLETED / COMPLETED_WITH_NOTIFICATION_FAILURE / ABORTED / TIMED_OUT / DISPATCH_FAILED)であり、
   DISPATCHING / RUNNING / FINALIZE_* / NOTIFICATION_* / TIMEOUT_* が無いこと。
   ★ 運用条件: NEW_CANDIDATEのバッチ処理中は、LINE credentialのrotation・再デプロイを行わない(#430)。
     dispatch時点では認証情報があり、バッチ処理中に認証情報が欠落した状態で再デプロイされると、workerが評価を行えず、
     候補が登録されないままバッチが24時間後にTIMED_OUTになる見込みがある(#430。DLQ Alarmが無いため気づかれない)。
C2 WatchlistScreeningQueue / WatchlistTerminalFailureQueue / WatchlistTerminalFailureDLQ の滞留が0であること。
   (デプロイ後に増減を判定する基準にもなる)
C3 LINE credential関連のデプロイ前確認が正常であること。
   Secrets Managerの該当シークレットが**空でない**ことを、値を読まずメタデータ(存在・最新バージョン・更新日時)だけで確認する。
   空のシークレットで再デプロイすると、全Lambdaの LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID が空になる。
   値・環境変数を全件出力しない(19.6節のガードレール)。
C4 ChangeSet差分が想定内であること。Lambda関数がModifyのみで、Replacementが無く、意図しない資源が含まれないこと(19.3節の7と同じ観点)。
```

read-onlyの確認は `AWS_PROFILE=jstock-observer` で行う。観測用の権限で許可されない項目が
ある場合は、権限を回避せず、その旨を報告して判断を仰ぐ。

### 23.2 デプロイ後の手動確認(D2。暫定監視。期間限定)

恒久のAlarmが未整備であるため、**このデプロイ固有の暫定監視**として次を行う。

```
V3 06:00のdispatcher: Errors = 0、BatchRunsが作成される(認証情報が有効であることの証拠)
V4 worker: Errors = 0、WatchlistTerminalFailureDLQ(C2の基準)が増えていない
V5 バッチが終端し、NOTIFICATION_FAILED / TIMED_OUT が無い(通知が実際に送信できた)
V6 08:00のbuy / holdings、10:00・12:30・15:30のdisclosure、毎時のreconciler: Errors = 0
V7 ログ検索(CloudWatch Logs Insights、read-only): LineCredentialsMissingError が0件
```

```
期間     デプロイ直後 / 翌営業日 / その次の営業日
         ・デプロイ直後は確認できる範囲(毎時のreconcilerのErrors・キュー/DLQの滞留・ログ検索。V4・V6・V7の一部)
         ・営業日(月〜金)はV3〜V7を確認する
終了     ★ 2営業日連続で異常が無ければ、今回のデプロイ固有の手動監視は終了してよい。
         恒久監視が入るまで無期限の日次手作業にはしない。
```

**留意点**: 「Errorsが止まった = 復旧」とは限らない。worker・terminal_failureの連鎖(#430)が進んだ後は、
認証情報を直してErrorsが止まっても、DLQに残ったメッセージ・TIMED_OUTになったバッチは自動では復旧しない。
Errorsの有無だけでなく、DLQの滞留とバッチの終端状態(NOTIFICATION_FAILED / TIMED_OUT)も見ること。

**恒久監視の未整備(残る問題。2026-09-24更新)**: CloudWatch AlarmはLambda 12本すべてに
Errors alarmが接続された(Issue #504。#503のEvaluationFunctionを含む)が、**DLQの滞留Alarm
は依然として存在しない**。また、per-item/per-batchの例外を握り潰す設計の6関数
(BuyCandidates/HoldingsWatchlist/WatchlistDispatcher/WatchlistWorker/
WatchlistBatchReconciler/LineWebhook)は、Errors alarmだけでは内部異常を検知できない
(#506/#507が終端状態の監視で補完中)。Duration alarmはEvaluationFunctionのみ(#505で
WeeklyReviewFunctionへの追加を検討中)。
次の3点を #132(本番ジョブ異常の自動検知)の要件として記録した(#132 issuecomment-5740709204):
(1) WatchlistTerminalFailureDLQのメッセージ滞留監視 (2) LINE関連LambdaのErrors監視 (3) 「Errorsが止まった=復旧」とは限らない点。
**最新の状態は29節・#132の最新の記録を読むこと**(この節へ焼き込まない)。

### 23.3 DLQ redrive(障害時の候補案。★未検証。正式な復旧手順ではない)

```
REDRIVE_VERIFIED = NO
```

★ **この節は、正式な復旧手順ではない。** 検証(下記の6項目)を経るまでは「障害時の候補案」としてのみ扱う。
なお、通知だけが欠落した場合(NOTIFICATION_FAILED)は、credential復旧前に既存のretry上限へ達すると
自動通知されず、手動の retry-notification が必要になる(USER承認済みの契約。#117 issuecomment-5740407445)。

worker・terminal_failureの連鎖(#430)でDLQに溜まったメッセージについて、認証情報を直した後にWatchlistScreeningQueueへ
戻す(SQSのDLQ redrive。移動先を指定する)ことで、dispatchから24時間以内でバッチがRUNNINGのままなら、workerが再評価して
バッチが完了する見込みがある。**これは検証を経ていない候補案であり、この節を根拠にProductionへ実行してはならない**
(実行はいずれにせよ、別のHuman Gateが必要なProductionの書き込みである)。

正式な手順として本書へ反映する前に、次を検証で確認する(検証の実施はUSERの別途承認が必要):

```
1 RUNNINGのバッチへのredriveで、バッチが正常に復旧すること
2 batch_id / job_type がredriveの前後で維持されること
3 candidate lease / progress(進捗行)との整合(PENDING / PROCESSINGの行、リース取得)
4 二重評価・二重登録が起きないこと
5 TIMED_OUT後のredriveは安全に無効化されること(終端済みのためリース取得が失敗し、何も起きない)
6 24時間の境界付近(タイムアウト確定の直前・直後)の挙動
```

`REDRIVE_VERIFIED = YES` になるまでは、上記は「障害時の候補案」としてのみ扱う。

## 24. DLQ の滞留の確認手順(Issue #349 ⑤、2026-09-20追加)

この節は、SQS の DLQ(処理に失敗したメッセージが最終的に入るキュー)に**メッセージが溜まっていないかを、観測用 role で確認する手順**を定める。
**確認するための手順であって、気づく仕組みではない**(24.1)。

### 24.1 この手順の限界(★ 最初に読むこと)

```
・4本のDLQいずれにも、CloudWatch Alarm(ApproximateNumberOfMessagesVisible>=1で即時)
  →既存のIncidentNotificationTopic(#503)→LINE、という気づく仕組みが入った
  (Issue #349。infra/template.yamlへのmerge時点の記載。Production deployは別
  Human Gateのため、実際にLINEへ届くようになるのはdeploy後)。
・ただしAlarmは「1件以上見えるか(ALARM/OK)」の1bitしか伝えない。件数・最古
  メッセージの経過時間・メッセージ内容は本節の手順で別途確認する必要がある。
・DLQ のメッセージは 14 日で自動的に消える(MessageRetentionPeriod = 14 日)。
  Alarmが後からOKへ戻っても、原因が直ったとは限らない(24.5参照)。
・「誰が・いつ(どの頻度で)確認するか」は、本節では**決めていない**(未決定。Issue #349 ⑤ の残り)。
```

### 24.2 対象のキュー

キューの名前は `jstock-advisor-<種別>`(スタック名が前置される)。

```
Production に現在ある DLQ(4本とも Issue #349 で CloudWatch Alarm を接続済み)
  jstock-advisor-watchlist-terminal-failure-dlq     ウォッチリスト評価の終端失敗の DLQ
  jstock-advisor-async-invoke-failure-dlq           BuyCandidates / HoldingsWatchlist の非同期 invoke の失敗(OnFailure の宛先。
                                                     1本のDLQを両関数が共有するため、メッセージ単体からはどちらの関数由来か
                                                     区別できない。LINE通知の対象名は「非同期実行の失敗」〔#349〕)

#396(#319 Phase 1)を含む反映の後に存在するキュー(現時点でコードから参照されない。dormant。
Phase 2でdispatch側が切り替わるまで構造的にメッセージが入らないが、Alarmは監視漏れ防止のため
USER決定により先行接続済み)
  jstock-advisor-buy-candidate-terminal-failure-dlq
  jstock-advisor-holdings-watchlist-terminal-failure-dlq

参考(DLQ ではない。作業用のキュー。滞留は通常の処理中の値であり、DLQ の滞留と混同しない)
  jstock-advisor-watchlist-screening / jstock-advisor-watchlist-terminal-failure
```

### 24.3 確認の方法(read-only)

観測用 role(`AWS_PROFILE=jstock-observer`。18節と同じ)で、CloudWatch の SQS メトリクス `ApproximateNumberOfMessagesVisible`(見えているメッセージの数)を、日次の最大値で読む。

```bash
# 直近 14 日の日次の最大値。QUEUE を対象のキューの名前へ置き換える。
# (Git Bash では MSYS_NO_PATHCONV=1 を付ける)
export AWS_PROFILE=jstock-observer AWS_DEFAULT_REGION=ap-northeast-1
QUEUE=jstock-advisor-async-invoke-failure-dlq
aws cloudwatch get-metric-statistics --namespace AWS/SQS --metric-name ApproximateNumberOfMessagesVisible \
  --dimensions Name=QueueName,Value=$QUEUE \
  --start-time "$(date -u -d '14 days ago' +%Y-%m-%dT00:00:00Z)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 86400 --statistics Maximum \
  --query "sort_by(Datapoints,&Timestamp)[].[Timestamp,Maximum]" --output text
```

- **SQS の API(`ListQueues` / `GetQueueAttributes` 等)は、観測用 role に許可されていない**(AccessDenied を実測。権限を広げて回避しない)。したがって、キューの属性(実際の保持期間・redrive の設定)は、この手順では確認できない(`infra/template.yaml` の記載のみ)。
- 古い滞留を知りたいときは、同じ形で `ApproximateAgeOfOldestMessage`(最も古いメッセージの経過秒数。Statistic = Maximum)を読む。

### 24.4 結果の読み方

```
すべての日次の最大値が 0                          → その期間、見えているメッセージは無かった
0 より大きい日がある                              → その日に DLQ にメッセージがあった(24.5 へ)
データ点が無い日がある                            → ★ 「0 だった」ではなく「メトリクスが出なかった」。SQS のメトリクスは、キューに動きがあるときだけ出る。
                                                    DLQ は通常 動かないため、データ点が飛ぶのは正常でありうる。ただし、「データ点が無い = 安全」と読んではならない
                                                    (最新のデータ点の日付と、キューの作成日を確認する。作成が新しいキューは、データ点が少ない)
コマンドがエラーになる                            → 「異常なし」ではなく「確認できなかった」。エラーを記録し、確認できたとは書かない
```

### 24.5 見つかったとき

```
・この手順は read-only である。**DLQ のメッセージの再処理(redrive)・削除・キューの purge は、Production の書き込みであり、Human Gate の対象**(docs/user_manager_collaboration_protocol.md)。
  本節の範囲外であり、確認した者が実行してはならない。
・記録して報告する(どのキューか / どの日か / 最大値 / 最古のメッセージの経過時間 / 確認した日時)。メッセージの内容は、この手順では読まない(読む方法も、許可されていない)。
・MANAGER / USER へ報告し、対応(原因の調査・redrive の要否)の判断を仰ぐ。
・14 日で消えるため、最古のメッセージの経過時間が 14 日に近い場合は、消える前に判断が要ることを、報告に含める。
```

★ **DLQ Alarm が OK へ戻っても、対象 job・batch が復旧したことを意味しない**(Issue #349。
27.4 と同じ考え方)。DLQ には自動消費者が無いため、OK へ戻る経路は次の 3 通りしかなく、
いずれも「原因が直った」ことを直接には意味しない。

```
(a) 人手で redrive/purge した           → 対応者の作業内容を別途確認する(本節の範囲外)
(b) 14 日retentionで自然に消えた         → メッセージの内容は既に失われている
                                          (read-only観測でも読めない。24.5のとおり、
                                          消える前の redrive 要否判断は Human Gate)
(c) 対応job側で根本原因が直り、再実行が成功した → これだけが実際の復旧だが、Alarmの状態
                                          だけからは(a)(b)と区別できない
```

OKへ戻った場合も、対応するjobの直近の完了判定(4節・25節・27.3.4)を別途確認すること。

### 24.6 実測の例(2026-09-20。参考値であり、保証ではない)

```
watchlist-terminal-failure-dlq     直近 14 日の日次の最大値 = 0(データ点 11 日分)
async-invoke-failure-dlq           同 = 0(データ点 5 日分。キューの作成が比較的新しい)
```
出典: Issue #349 issuecomment-5746509139。

## 25. BUY / holdings バッチの完了判定の手順(Issue #344、2026-09-20追加)

この節は、**買い候補(BUY)と保有銘柄(holdings)のバッチが完了したかどうかを、`batch_runs` から判定する手順**を定める。
release 後の手動実行の完了確認など、`jstock-batch_runs` を読んで完了を判断するときに使う。

### 25.1 なぜ必要か(★ 最初に読むこと)

```
BUY / holdings のバッチは、完了しても batch_runs の status が RUNNING のまま残る。
これは欠陥ではなく設計である。status を見て「まだ実行中」「ハングした」と判断してはならない。
```

- `status` は、**ウォッチリスト自動追加(永続データを更新するバッチ)の finalize 排他制御のための field** であり、BUY / holdings にとっては適用対象外である(`batch_tracker.py` の `BatchFinalizeStatus` の docstring)。
  BUY / holdings の項目は、作成時の既定値として `RUNNING` が入るだけで、以後 `status` は更新されない。
- 2026-09-12 の release 検証(手動実行)で、`completed` が `total` に達し `failed` が 0 なのに `status` が `RUNNING` のままであるため、完了かハングかを判断できず、運用者の手が止まった。
  このとき運用文書に記載が無く、実装を読んで初めて判断できた。
- ★ 危険な側の失敗: `RUNNING` を見て「ハングした」と判断し、**再 invoke(本番の二重実行)を提案する**。実行されなくても(Human Gate がある)、提案の前提が誤っていること自体が危険である。

### 25.2 完了判定の手順

```
1  status は見ない。BUY / holdings にとって対象外の field である。
2  完了は completion_finalize_completed_at で判定する(この属性があれば、finalize は完了している)。
3  種別は batch_family で識別する(BUY_CANDIDATES / HOLDINGS_WATCHLIST)。
4  batch item は TTL 6 時間で消える。作成から 6 時間より後には、そもそも読めない(項目が無い = 完了していない、ではない)。
5  watchlist 系(NEW_CANDIDATE_SCREENING / WATCHLIST_MAINTENANCE)は別である。
   そちらは status の state machine が正本である(4.1 節)。本節の手順を当てはめない。
```

### 25.3 読み方

読む属性(いずれも batch item の属性。項目は `batch_id` で 1 件だけ指定する):

| 属性 | 意味 |
|---|---|
| `batch_family` | 種別。`BUY_CANDIDATES` または `HOLDINGS_WATCHLIST`。無い・未知の値なら、本節の対象ではない(判断しない) |
| `total` / `completed` | 対象件数と、処理が終わった件数 |
| `completion_finalize_completed_at` | **完了の判定に使う属性**。値があれば finalize 完了 |
| `completion_finalize_started_at` | finalize の取得時刻 |
| `completion_finalize_failed_at` | finalize の失敗時刻(捕捉できた失敗のみ) |
| `completion_finalize_attempt_count` | finalize の試行回数 |
| `ttl` | 項目が消える時刻(UNIX 秒) |
| `status` | ★ 判断に使わない(既定値の `RUNNING` が残るだけ) |

```
completion_finalize_completed_at がある                 → 完了している(status が RUNNING でも)
completion_finalize_completed_at が無く、started_at がある → finalize の処理中、または途中で止まった(失敗の記録があれば failed_at も見る)
                                                          started_at から 20 分(1200 秒)以上経っていれば、毎時の reconciler が再駆動の候補として扱う(バッチ側の仕組み)
completed < total                                        → 個別の処理がまだ終わっていない(finalize の前)
項目が無い                                                → 作成から 6 時間より後(TTL で削除された)、または batch_id の誤り。★「完了していない」とは読まない
```

★ 1 つの属性だけで結論を出さない。`completed = total` でも `completion_finalize_completed_at` が無ければ、finalize が終わったとは言えない。
逆に、`status` が `RUNNING` であることは、完了の否定にならない。

### 25.4 確認の方法(read-only)

観測用 role(`AWS_PROFILE=jstock-observer`。18節・24節と同じ)で、`batch_id` を指定して 1 件だけ読む(`GetItem`)。**スキャンはしない**。

```bash
# BATCH_ID を対象のバッチの batch_id へ置き換える。
# (Git Bash では MSYS_NO_PATHCONV=1 を付ける。status / total / ttl は DynamoDB の予約語のため別名を使う)
export AWS_PROFILE=jstock-observer AWS_DEFAULT_REGION=ap-northeast-1
aws dynamodb get-item --table-name jstock-batch_runs \
  --key "{\"batch_id\":{\"S\":\"$BATCH_ID\"}}" \
  --projection-expression "batch_id,batch_family,#st,#tot,completed,completion_finalize_started_at,completion_finalize_completed_at,completion_finalize_failed_at,completion_finalize_attempt_count,#tt" \
  --expression-attribute-names '{"#st":"status","#tot":"total","#tt":"ttl"}' --output json
```

- ★ **読む属性は上のとおりに限定する(ProjectionExpression。許可リスト方式)**。同じ項目には、銘柄コード・holding_id(所有者を含む)・評価額を含みうる集合の属性が保存されている。**これらを読まない・出力しない・記録に書かない**(個人情報の露出を避ける。CLAUDE.md の個人情報の規則)。**項目に保存されている実際の属性名**は次のとおりである(`batch_tracker.py` の集計の読み出し。一部は Python 側の `BatchProgress` の field 名〔例: `failed_stock_codes`〕と異なる)。
```
項目に保存されている属性名(読まない)                  中身
failed_codes                                          失敗した対象の識別子(buy = 銘柄コード / holdings = holding_id)
data_insufficient_codes                               データ不足の対象の識別子(同上)
completed_codes                                       完了報告された識別子(同上)
attention_detected_stock_codes / attention_sent_stock_codes / evaluation_record_saved_stock_codes
                                                      銘柄コード(holdings では holding_id)の集合
notification_categories / detected_categories         「種別|識別子」形式の文字列の集合
ranking_entries / near_buy_ranking_entries / watch_end_ranking_entries
                                                      「スコア|銘柄コード|…」形式の文字列の集合
sector_entries                                        「業種|評価額|銘柄コード」形式の文字列の集合(全保有銘柄)
validation_recommendation_ids                         検証用の recommendation_id の集合
```

- ★ 一覧は、`batch_tracker.py` の読み出しで確認できた集合の属性である。**一覧に無い属性も、許可リストに無ければ読まない**(許可リストが安全の根拠であり、この一覧は「なぜ限定するか」を示す例)。
- holdings では、識別子の引数に `holding_id`(= 所有者 + `#` + 銘柄コード)が渡される(`batch_tracker.py` の `BatchProgress` の docstring)。
- `batch_id` の入手方法は、本節では定めない(Lambda のログに `batch_id=` として出る箇所があるが、正常系のすべての経路で出るとは確認していない)。**full scan で探さない**。分からなければ MANAGER へ確認する。
- 出力が空(項目が無い)のときは、25.3 の「項目が無い」のとおりに読む(完了とも未完了とも判断しない)。
- 検証: このコマンドの構文と観測用 role での `GetItem` の許可は、存在しない `batch_id` を指定して、エラーにならず空の応答になることを確認した(2026-09-20。実際の項目を読んだ確認ではない)。

### 25.5 「終端 status を入れる」案を採らない理由(★ この節を消さないこと)

```
finalize 時に、BUY / holdings の batch item へ終端の status を入れる案は採らない。
これは Issue #31 の承認済み設計であり、本手順で覆さない。
```

理由(`batch_tracker.py` の記述):

- watchlist パイプラインの `try_acquire_finalize()` / status の state machine とは、**意図的に統合しない**。
  毎時の reconciler が `status` の値で scan・分岐しており、BUY / holdings の項目は `status = RUNNING` のままで、`started_at` 等を持たないことで無害に skip されている。この既存の前提を壊さないため。
  そのため BUY / holdings には、専用の `completion_finalize_*` 属性だけを追加している。
- あわせて、`status` を BUY / holdings から取り除く案も採らない。reconciler が completion recovery の候補を見つける手段が、この `status` による scan であり、取り除くと発見できなくなる。
- 書かなければ、後から「終端 status を付ければ直る」と考えた担当が、承認済みの設計を壊す。

### 25.6 したがって、やってはいけないこと

```
・batch_runs の status = RUNNING だけを根拠に、「ハングした」「未完了」と判断する
・上の判断に基づいて、再 invoke(本番の二重実行)を提案する
・BUY / holdings のバッチ項目へ、終端の status を入れるコード変更を提案・実施する(25.5)
・完了の確認のために batch_runs を full scan する、または失敗銘柄・不足銘柄の属性(所有者を含みうる)を読み出す(25.4)
・watchlist 系のバッチへ、本節の判定を当てはめる(4.1 節の status の state machine が正本)
```

## 26. shadow 監査記録の集計 CLI の使い方(Issue #458、2026-09-21追加)

判断の安全条件(G1〜G4)の shadow 監査記録(`decision_type=judgment_safety_shadow`)を集計し、`AuditLogTable` の増加量・scan の実測値を出す、**読み取り専用**の CLI である。
Phase 2(誤検出・過剰抑制のレビュー)と、専用 Table への移行の要否を判断する材料(U13 = OPTION_C)を出すためにある。

### 26.1 これは何を「しない」か(★ 最初に読むこと)

```
・【--source dynamodb(Production)】書き込み・保存・削除・invoke・通知のいずれも行わない(scan / describe_table だけを通す allowlist の proxy。それ以外の呼び出しは例外)
・【--source local(既定)】ローカルの保管ディレクトリを作る(既存の共有 store の挙動。`AuditLogRepository()` の構築時に mkdir する。冪等で、ファイルもデータも作らない)。
  それ以外(保存・削除・invoke・通知)は行わない。Production(DynamoDB)には触れない
・閾値の判定・「専用 Table へ移行すべき」等の提案をしない(判断は USER / MANAGER)
・Production を既定で読まない(既定は --source local)
・shadow の有効化(mode の変更)はしない(別の Human Gate)
```

### 26.2 使い方

```
jstock judgment-safety-shadow report                           # ローカルの JSON(既定)
jstock judgment-safety-shadow report --source dynamodb         # Production(read-only)
    [--table jstock-audit_log] [--from YYYY-MM-DD] [--to YYYY-MM-DD]   # 期間は JST 暦日
    [--describe-only]   # 表のメトリクスだけ(scan しない)。dynamodb のみ
    [--metrics-only]    # 表・scan のメトリクスだけ(shadow の集計は出さない)
    [--json]            # 機械可読
    [--baseline-records 78700] [--baseline-size-bytes 153000000] [--baseline-date 2026-09-20]
```

- `--source dynamodb` は、呼び出し元の資格情報(`AWS_PROFILE` 等)で読む。観測用の `jstock-observer` で `jstock-audit_log` を Scan できる(write 権限は不要)。実行時、標準エラーへ「read-only・対象テーブル」を表示する。
- 全件 Scan になる(2026-09-20 時点で約 78,700 件・約 153MB、概算で約 2 万読み取りユニット)。**定期実行には組み込まれていない。実行は運用者の判断で行う。**
- 終了コード: 0 = 正常(記録が 0 件でも 0)/ 2 = 引数不正 / 3 = 読み取り失敗(認証・ネットワーク。例外の型だけを表示する)。

### 26.3 読み方(誤読の防止)

- **0 件は「問題なし」ではない。** SHADOW の有効化の前、または期間外である。CLI も明示する。
- 条件別の率の分母は、`not_evaluated`(入力が無く評価できなかった条件)を**除いた**評価済みの記録数である。`not_evaluated` は「該当なし」ではなく、別掲する。
- G3 は測定可能な 2 項目の**下限値**(測定不能の 3 項目は含まない)。G4 は保有の `FULL_PROFIT_TAKE` のみが対象(買い経路は対象外)。
- `ItemCount` / `TableSizeBytes`(DescribeTable)は**概算**で、およそ 6 時間ごとに更新される。実測値は Scan の結果である。両者を区別して表示する。
- 推定コストは公開単価(コード内の定数)に基づく**概算**。単価は AWS Price List API(ap-northeast-1・Standard table class・オンデマンド読み取り = 100 万読み取りユニットあたり 0.1425 USD。公開日 2026-09-11)で 2026-09-21 に確認した値。単価は変わりうるため、出力の `estimated_read_cost_basis` の確認日を見る。table class が Standard-IA の場合は別の単価(0.178)になる。
- 月次の外挿は、shadow 記録がある日が 3 日未満のときは参考値である。
- `unparsed` は読めなかった項目の件数(沈黙させない)。未知の `schema_version` は `unparsed` ではなく別掲する。

## 27. 異常を知ったときの手順(障害対応の runbook。Issue #500〔#132 X-1〕、2026-09-21追加)

この節は、**本番のジョブの異常に気づいた人(通知を受けた人、または自分で気づいた人)が、最初に何をするか**を定める。20節(DynamoDB 復旧)・21節(PII 是正)と同じ形の runbook である。
**確認して報告するための手順であって、復旧の手順ではない**。復旧の操作(再実行・redrive・purge 等)は Production の書き込みであり、Human Gate の対象である(27.5)。

### 27.1 この節の限界(★ 最初に読むこと)

```
・(2026-09-21 時点の記述。原文のまま残す)現在の infra/template.yaml、異常を自動で知らせる経路は無い。CloudWatch Alarm は evaluation Lambda の Errors / Duration の 2 本だけで、
  どちらも AlarmActions が空(鳴っても誰にも届かない)。DLQ の滞留にも Alarm は無い(24.1)。
  通知経路・alarm の拡張は Issue #132 の段階的な実装で入る。**最新の状態は #132 の最新の記録を読むこと**(この節へ焼き込まない)。
  ★ **2026-09-24更新**: #503(段階1)でEvaluationFunction、#504(段階2)で残る11関数へも
  AlarmActions(IncidentNotificationTopic経由のLINE通知)が接続され、上記「どちらもAlarmActions
  が空」は解消済み。DLQの滞留Alarmは依然として無い(未解消のまま)。per-item例外を握る6関数の
  内部異常はErrors alarmだけでは検知できない点も未解消(#506/#507が担当)。
・したがって「通知が来ない = 正常」ではない。24.1 と同じく、**見に行かなければ気づかない**異常がある。
・通知が入った後も、対象外がある。秘密の取得失敗(SECRET_UNAVAILABLE)は LINE では知らせない方針(Issue #117)。
  監視の対象は段階的に広がる(Lambda は 12 本あり、最初から全てではない)。
・この節は「通知の文面」に依存しない。文面は Issue #501(#132 X-2)が決める。通知本文は、job 名・件数・日数・真偽値・時刻だけを出す想定
  (識別子・銘柄・所有者を出さない。#132 の承認済み計画)。本文に無い情報は、27.3 の read-only 観測で自分で取る。
・「誰が・いつ(どの頻度で)確認するか」は、この節では決めていない(24.1 と同じ。Issue #349 ⑤ の残り)。
```

### 27.2 最初にやること(3 つ。この順で)

```
1 記録する
    いつ(通知の時刻、または自分が気づいた時刻)/ どの job か / 何が異常と言われたか / どの手順で知ったか。
    記録を公開面(GitHub の Issue・PR・コメント)へ書くときは、銘柄コード・銘柄名・所有者・保有数量・AWS アカウント識別子・ARN を書かない(21節)。
2 状態を変えない
    急いで直そうとして、27.5 の操作(再実行・手動 invoke・redrive 等)をしない。
3 read-only で観測し(27.3)、27.6 の形で MANAGER / USER へ報告する
```

### 27.3 観測の手順(read-only)

観測用 role(`AWS_PROFILE=jstock-observer`。18節・24節と同じ)で行う。**名前が read 系でも副作用が無いとは限らない**(18節)。ここに書いたコマンドは、参照系の API だけを使う。

| 疑うこと | 見るもの | 手順 |
|---|---|---|
| Lambda の失敗(例外・timeout。timeout は Errors に計上される) | `AWS/Lambda` の `Errors` | 27.3.1 |
| 実行時間が Timeout に近づいている | `AWS/Lambda` の `Duration`(Maximum) | 27.3.1 の `--metric-name` を置き換える |
| 同時実行の枠に当たっている | `AWS/Lambda` の `Throttles` と `Invocations` | 27.3.1 の `--metric-name` を置き換える。watchlist-worker の Throttles は**平常時も日次で数百〜数千**あり、単日の値ではなく傾向で読む(Issue #4・#224) |
| メッセージが DLQ に溜まっている | SQS の `ApproximateNumberOfMessagesVisible` | 24節 |
| BUY / holdings のバッチが完了したか | 完了判定 | 25節 |
| ウォッチリストのバッチが終端したか / 通知が送れたか | BatchRuns の `status` と `execution_result`(終端か。`NOTIFICATION_FAILED` / `TIMED_OUT` / `FINALIZE_FAILED` 等でないか) | 27.3.4(読み方の基準は 4.1 の状態遷移と 23.2 の V5)。BatchRuns には TTL があり、古い記録は消える |
| ジョブが起動しなかった(missed schedule) | 該当の Lambda の `Invocations` / `Errors` のデータ点が、動くはずの日にあるか | 27.3.1(データ点の読み方を含む)。**当日が実行対象の日か**を先に確認する(weekly / monthly / quarterly は曜日条件で実行されない日がある。休場日の扱いは Issue #440) |
| 認証情報の欠落 | ログ検索で `LineCredentialsMissingError` の**件数** | 27.3.3(参照系の `filter-log-events`。23.2 の V7 は Logs Insights を名指しするが、観測用 role では使えない) |
| Alarm の状態 | (観測用 role では読めない) | 27.3.2 |

#### 27.3.1 Lambda のメトリクスを読む

```bash
# 直近 3 日の 1 時間ごとの合計。FN を対象の関数の名前へ置き換える。
# (Git Bash では MSYS_NO_PATHCONV=1 を付ける)
export AWS_PROFILE=jstock-observer AWS_DEFAULT_REGION=ap-northeast-1
FN=jstock-advisor-watchlist-worker
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Errors \
  --dimensions Name=FunctionName,Value=$FN \
  --start-time "$(date -u -d '3 days ago' +%Y-%m-%dT00:00:00Z)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 --statistics Sum --query "sort_by(Datapoints,&Timestamp)[].[Timestamp,Sum]" --output text
```

```
関数の名前(`jstock-advisor-` に続く部分。スタック名が前置される)
  buy-candidates / holdings-watchlist / disclosure-check / evaluation / watchlist-dispatcher / watchlist-worker /
  watchlist-terminal-failure-handler / watchlist-batch-reconciler / weekly-review / monthly-review / quarterly-review / line-webhook
```

- **データ点が無い日**は「0 だった」ではなく「メトリクスが出なかった」である(24.4 と同じ読み方)。**Lambda のメトリクスは、その関数が呼び出された期間にだけ出る**(2026-09-21 の実測: 呼び出しのあった日は `Errors` が `0.0` のデータ点として出て、呼び出しの無い日〔週末など〕は出ない)。したがって、
  - `Errors = 0.0` は「動いて、失敗が無かった」。
  - **動くはずの日に `Invocations` も `Errors` もデータ点が無い**のは、「異常なし」ではなく「起動していない」の疑い(missed schedule)。
  - 「データ点が無い = 安全」と読まない。
- コマンドがエラーになったときは「異常なし」ではなく「確認できなかった」。エラーを記録し、確認できたとは書かない。**AccessDenied は、権限を広げて回避しない**(24.3)。

#### 27.3.2 Alarm の状態は、観測用 role では読めない

- 観測用 role では、CloudWatch Alarm の状態(`DescribeAlarms`)を読めない(AccessDenied を 2026-09-21 に実測。**権限を広げて回避しない**)。
- Alarm の定義(どの関数のどの値を見ているか)は `infra/template.yaml` で確認する。現在の Alarm は evaluation Lambda の 2 本だけである(27.1)。Alarm が鳴っていないことは「他の関数も正常」を意味しない。
- Alarm の状態を確認する必要があるときは、確認できる人(AWS の画面を見られる権限を持つ人)へ依頼する。

#### 27.3.3 ログを読む(認証情報の欠落の件数)

- ログの検索は参照系の `aws logs filter-log-events` を使う。Logs Insights の `start-query` は、観測用 role に許可されていない(2026-09-20 の観測で AccessDenied を実測)。回避しない。
- **ログの本文には、銘柄コード等が含まれうる。** 本文は出力せず、**件数だけ**を読む(`--query 'length(events)'`)。公開面へは、件数・時刻・種別だけを書く(27.2 の 1)。

```bash
# LineCredentialsMissingError が、直近 3 日に何件出たか(件数のみ。本文は読まない)。FN を対象の関数の名前へ置き換える。
# LINE の認証情報は全 Lambda 共通の値で(23.0)、LINE を送る関数(line-webhook / watchlist-dispatcher / buy-candidates /
# holdings-watchlist / disclosure-check / watchlist-worker / watchlist-terminal-failure-handler / watchlist-batch-reconciler)で欠落が可視化される。
export AWS_PROFILE=jstock-observer AWS_DEFAULT_REGION=ap-northeast-1
FN=jstock-advisor-watchlist-dispatcher
aws logs filter-log-events --log-group-name /aws/lambda/$FN \
  --start-time $(( $(date -u -d '3 days ago' +%s) * 1000 )) \
  --filter-pattern LineCredentialsMissingError --query 'length(events)' --output text
```

- 結果はページごとに 1 行ずつ出ることがある(合計する)。すべて 0 なら、その期間、その関数でこのエラーは記録されていない。
- `0` は「その関数が動いて、このエラーが出なかった」とは限らない。動いたか(27.3.1 の `Invocations`)と合わせて読む。

#### 27.3.4 ウォッチリストのバッチの状態を読む

ウォッチリストのバッチ(NEW_CANDIDATE_SCREENING / WATCHLIST_MAINTENANCE)は、BUY / holdings と同じ `jstock-batch_runs` に入っている。**読み方は 25 節と違う**: 25.2 のとおり、ウォッチリスト系は `status` の状態遷移が正本である(4.1 の「バッチの状態遷移」。`completion_finalize_completed_at` で判定しない)。

```bash
# BATCH_ID を対象のバッチの batch_id へ置き換える。(Git Bash では MSYS_NO_PATHCONV=1 を付ける。status は予約語のため別名を使う)
export AWS_PROFILE=jstock-observer AWS_DEFAULT_REGION=ap-northeast-1
aws dynamodb get-item --table-name jstock-batch_runs \
  --key "{\"batch_id\":{\"S\":\"$BATCH_ID\"}}" \
  --projection-expression "batch_id,#st,execution_result,finalize_failed_at,updated_at" \
  --expression-attribute-names '{"#st":"status"}' --output json
```

- **読む属性は上のとおりに限定する(許可リスト方式。25.4 と同じ考え方)。** 項目には、銘柄コードや集計の内容を含みうる属性がある。**全属性を読まない**。`finalize_error_message` も読まない(エラーの本文を含みうる)。
- 25.4 と同じ制約: `batch_id` の入手方法は定めていない(dispatcher のログに `batch_id=` として出る箇所がある)。**full scan で探さない**。項目が無いときは、完了とも未完了とも判断しない(TTL で消えた場合がある)。
- 読み方の基準は 4.1 の状態遷移: `COMPLETED`(`execution_result = NORMAL`)が正常。`ABORTED` / `DISPATCH_FAILED` / `FINALIZE_FAILED` / `TIMED_OUT` / `COMPLETED_WITH_NOTIFICATION_FAILURE` は終端だが異常(4.1 の各説明を読む)。`DISPATCHING` / `RUNNING` / `FINALIZING` / `TIMEOUT_FINALIZING` は処理中、または止まっている可能性がある(長時間 `updated_at` が動かなければ、毎時の reconciler が処理する。バッチの仕組み)。
- 検証: このコマンドの構文と観測用 role での `GetItem` の許可は、存在しない `batch_id` を指定して、エラーにならず空の応答になることを確認した(2026-09-21)。実際の項目の中身は読んでいない。

### 27.4 「Errors が止まった = 復旧」ではない

Errors が 0 に戻っても、次を**別々に**確認するまでは、復旧したと書かない。

```
a バッチが終端したか
    BUY / holdings は 25節。ウォッチリストは BatchRuns の終端 status と、NOTIFICATION_FAILED / TIMED_OUT の有無(27.3.4。基準は 23.2 の V5)。
b DLQ に残っていないか(24節)
    worker・terminal_failure の連鎖(Issue #430)の後は、認証情報を直して Errors が止まっても、DLQ に残ったメッセージ・TIMED_OUT になったバッチは自動では復旧しない(23.2)。
c 通知が実際に送られたか
    通知だけが欠落した場合、retry の上限に達すると自動では再送されない(23.3)。手動の再送は Production の書き込みで、Human Gate の対象(27.5)。
d 「何も出さなかった」障害でないか
    18.1 の 2026-09-02 の障害は、判定・LINE 通知が 0 件の「何も出さなかった」障害だった。Errors だけでなく、処理した件数・通知の件数が期待どおりかも見る。
```

### 27.5 してはいけないこと(Human Gate の対象。確認した者が実行してはならない)

```
・Lambda の手動 invoke、バッチの再実行、スケジュールの手動起動
・DLQ のメッセージの redrive・削除、キューの purge(23.3 の redrive は未検証の候補案。REDRIVE_VERIFIED = NO)
・通知の再送(retry-notification 等)、LINE への手動送信
・4.1 に「手動で実行してください」と書かれている操作(`jstock watchlist-screening run` 等)。これも Production への書き込みなので、確認した者が独断で実行せず、報告して判断を仰ぐ
・DynamoDB・S3 への書き込み・削除(手動での編集を含む)
・設定(config)・kill switch・IAM・infra の変更(ChangeSet の CREATE と EXECUTE は別々の Human Gate で、承認は exact な対象に対してのみ有効)
・Secret のローテーション・変更
・権限を広げて AccessDenied を回避すること
```

実行の可否は docs/user_manager_collaboration_protocol.md(Human Gate)が正本である。**この節を根拠に、これらを実行してはならない。**

### 27.6 報告の形

MANAGER / USER へ、次を分けて報告する。

```
1 いつ・どの job・何が異常と言われたか(通知の時刻と、自分が確認した時刻)
2 観測した値(件数・時刻・最大値。銘柄・所有者・保有数量は書かない)
3 実行した手順(27.3 のどれか)と、確認できなかったこと(AccessDenied・データ点が無い・コマンドのエラー)
4 状態を変えていないこと(27.5 の操作を行っていないこと)
5 判断してほしいこと(原因調査の要否。再実行・redrive・再送の要否は Human Gate)
```

- 観測の結果と、原因の推測を混ぜない。**原因が分かったと書くのは、原因を実測で確かめた後**に限る。
- GitHub の Issue・コメントへ書くときは、PUBLIC_SANITIZED を守る(21節。混入した場合の是正手順も 21節)。

### 27.7 この節が決めていないこと

```
・通知の文面(Issue #501)/ 通知経路の新設(Issue #503)/ Alarm の拡張(Issue #504・#505)/ 検知の相乗り(Issue #506)。状態は #132 の最新の記録を読む
・誰が・いつ確認するか(24.1。Issue #349 ⑤ の残り)
・復旧の手順そのもの(DLQ の redrive の検証は 23.3。検証には USER の別途の承認が要る)
```

## 28. 週次評価集計(WeeklyEvaluationAggregate)の切替・照合・rebuild の手順(Issue #537、2026-09-22追加)

週次改善レビューが、毎週 raw の EvaluationResult を全件 Scan する方式から、週ごとの集計済みデータ(Aggregate)を読む方式へ移るための手順である。
**実装は既定で無効**(従来の挙動のまま)。有効化・過去分の作成・切替は、それぞれ**別の Human Gate**で、本節はその順序と、各段の確認・戻し方を定める。

### 28.1 何が変わるか(有効にした場合)

```
評価の保存(日次の定点評価。書き込み側)
    暦日 7 日(evaluation_horizon_days)の評価だけ、EvaluationResult の条件付き Put と、Aggregate の加算・週の状態・再計算対象の一覧を
    1 回の TransactWriteItems で行う。Aggregate の更新に失敗したら EvaluationResult も保存しない(翌日の日次実行で再試行)。
週次レビュー(月曜 19:00。読み取り側)
    前週の Aggregate を Query で読む。EvaluationResultsTable も Aggregate Table も全件 Scan しない。
    遅延評価が届いた過去週は、marker(REVIEW_RECOMPUTE_PENDING)から特定し、その週の Metrics だけを Aggregate から再生成する。
環境変数(SAM Parameter。既定 = false)
    WEEKLY_AGGREGATE_WRITE_ENABLED   評価の保存側
    WEEKLY_AGGREGATE_READ_ENABLED    週次レビューの読み取り側(backfill が COMPLETE でなければ、有効にしても読まない = 従来の経路へ戻る)
```

### 28.2 切替の順序(各段が別の Human Gate。順序を入れ替えない)

```
段 1  deploy(Aggregate Table・IAM・環境変数を追加。書き込み・読み取りとも false のまま)
        確認: 週次レビュー・日次評価の挙動が変わっていない(従来どおり完走。監査の aggregate_read = false)
段 2  backfill の dry-run(read-only。対象週数・行数・想定 write 数を報告) -> USER が対象と実行の可否を判断
段 3  backfill の実行(全履歴の Aggregate を、raw から週ごとに SET で作る。再実行しても二重加算にならない。backfill 状態 = COMPLETE)
        ★ 書き込み側を有効にする**前**に行う(実行中に届く評価を取りこぼさないため)。
段 4  書き込み側を有効にする(WEEKLY_AGGREGATE_WRITE_ENABLED = true)+ 直後に照合(verify)
        段 3 と段 4 の間に確定した評価を、対象週だけの照合で確認する。不一致の週は AGGREGATE_REBUILD_REQUIRED にして、指定週だけ rebuild する。
段 5  旧方式との一致確認(照合が一致 + 同じ週の Metrics を旧方式・新方式で比較)
段 6  読み取り側を有効にする(WEEKLY_AGGREGATE_READ_ENABLED = true)
        確認: 次の月曜の週次レビューが完走し、監査の aggregate_read = true、EvaluationResultsTable の Scan が無いこと(ログの scan の行が出ない)
```

### 28.3 backfill・照合・rebuild の使い方(CLI。**ローカル専用**)

```
jstock weekly-aggregate backfill                 # dry-run(既定)。対象週数・行数・想定 write 数を出す(何も書かない)
jstock weekly-aggregate backfill --execute       # ローカルの Aggregate ストアへ書く
jstock weekly-aggregate verify [--week 2026-W38] [--mark-rebuild-required]   # raw と Aggregate の突合。不一致があれば終了コード 1
jstock weekly-aggregate rebuild --week 2026-W38 [--week ...] [--execute]      # 指定週だけ raw から作り直す(dry-run 既定)
```

- 本 CLI は**ローカルの保管ディレクトリだけ**を読み書きする(Lambda 以外では、本番のテーブルにアクセスしない)。
- ★ **Production の Aggregate に対する backfill / verify / rebuild の実行手段**(どの主体・どの経路で実行するか)は、本 PR では決めていない。段 2 の前に、USER の判断で決める(実行手段の新設は別の作業・別の承認)。
- rebuild は、その週の raw を読んだ後に新しい評価が届いた場合、上書きせずに失敗する(届いた評価を消さないため)。もう一度実行する。

### 28.4 戻し方(rollback)

```
読み取り側を戻す      WEEKLY_AGGREGATE_READ_ENABLED = false -> 従来の経路(raw の走査。#377 の有界メモリ版)へ即座に戻る。Aggregate・Metrics は変わらない。
書き込み側を戻す      WEEKLY_AGGREGATE_WRITE_ENABLED = false -> 以後の評価は従来どおり保存され、Aggregate は古くなる。
                     ★ 再び有効にする前に、対象の週を verify し、不一致の週を rebuild する(古くなった期間の評価を取りこぼしたまま読まない)。
Aggregate Table       削除しない(DeletionPolicy Retain)。raw の EvaluationResult は常に正本として残る。
```

### 28.5 異常のとき

```
日次評価の監査 aggregate_commit_failed_count > 0    Aggregate の更新に失敗し、その評価は保存されていない(翌日に再試行される)。Aggregate Table の権限・存在・競合を調べる。
週次レビューが「rebuild が必要」で失敗            current week が AGGREGATE_REBUILD_REQUIRED。verify で不一致を確認し、その週を rebuild してから再実行する。
過去週が「aggregate_rebuild_required」でスキップ    その週の Metrics は作られていない(marker は残る)。rebuild の後の次回のレビューで再生成される。
```

### 28.6 この節が決めていないこと

```
・Production の Aggregate への backfill / verify / rebuild の実行手段(28.3)
・切替の各段の実施の可否・時期(別の Human Gate)
・EvaluationResults の retention(本 Issue は変更しない。データ保持期間は Issue #138)
```

## 29. 本番ジョブ異常の検知(通知経路)の Production Verification Plan(Issue #503、2026-09-24追加)

CloudWatch Alarm → SNS Topic(`IncidentNotificationTopic`)→ `IncidentNotifierFunction` → LINE、
という通知経路(段階1。#132 X-4)を Production へ反映する際の確認手順である。
**Production の SNS Topic 新設・deploy 自体は別 Human Gate**(本節は反映後の確認のみを扱う)。

### 29.1 何が変わるか(実装の要約。PR #545 merge後・infra/template.yaml実物に基づき具体化)

```
新規(infra/template.yamlに明示的に定義した5資源)
      IncidentStateTable(fingerprint単位のclaim/dedup/stale takeover。他の履歴Tableと同じ保護)
      IncidentNotificationTopic
      IncidentNotificationTopicPolicy(cloudwatch.amazonaws.comへのPublish許可)
      IncidentNotifierFunction(IAMは最小権限。GetItem/PutItem/UpdateItem/DeleteItemのみ)
      IncidentNotifierFunctionErrorsAlarm(AlarmActionsは意図的に空。自己再帰を避ける)

新規(SAMのtransformがIncidentNotifierFunctionから自動生成する付随リソース。
      明示的にtemplate.yamlへは書いていないため、ChangeSetには上記5資源に加えて
      これらのADDも現れる。正確なLogicalIdはChangeSet CREATE実物で確認する)
      IncidentNotifierFunctionの実行role(IAM Role。Policiesで宣言したReadWriteIncidentState
      Statementを含む)
      SNSがLambdaを起動するためのLambda::Permission(Events.AlarmTopicから生成)
      IncidentNotificationTopicへのAWS::SNS::Subscription(同じくEvents.AlarmTopicから生成)

変更  EvaluationFunctionErrorsAlarm / EvaluationFunctionDurationAlarm へ AlarmActions を追加
      (閾値・メトリクス・Dimensions は変更しない)

既存2 alarm以外の既存123資源(Table/Queue/他のFunction等。ListStackResourcesで実測。
2026-09-23時点デプロイ済みは計125資源)への差分は無い見込み(git diff bceaca29の^1との差分が
config/infra/lambda_handlers/domain配下の#503関連ファイルのみであることをmain merge後に
実測済み。「incident」を含むLogicalResourceIdは現行スタックに0件であることも確認済み
= 新資源の名前衝突なし)。
```

### 29.2 確認手順(USER baseline #503 issuecomment-5796588796 の10項目を具体化)

```
1  ChangeSet差分確認   29.1の新規資源(明示5資源+SAM自動生成の付随リソース)のADDと、
                      既存2 alarmのAlarmActions追加(MODIFY)のみであること。
                      それ以外の既存リソースにMODIFY/REMOVEが無いこと(他プロパティの差分ゼロ)
2  存在確認            DescribeTable(IncidentStateTable)/ GetTopicAttributes /
                      GetFunction(IncidentNotifierFunction)/ 既存2 alarmのAlarmActionsに
                      Topic ARNが入っていること(DescribeAlarms)
3  人工障害を起こさない  実際のjob失敗でのみ発火する。人工的にjobを失敗させない
4  test SNS publish    write操作のため、テスト目的のPublishは別Human Gate(本節では実行しない)
5  正常ジョブへの影響なし deploy直後の次回 evaluation Lambda 実行(平日18:00)が、従来どおり
                      完走し、Durationに変化がないこと
6  IncidentNotifier    deploy後、実際にAlarmが鳴るまでInvocations=0のまま(0件は「正常」の
   Errors/Throttles確認  確認にはならない。実着信確認[8]まで「配線済みだが未検証」として扱う)
7  IncidentState        実際にAlarmが鳴った後、GetItemでfingerprint行のstatus=SENT・
   claim/SENT確認        occurrence_countを確認(値そのものはPUBLIC repoへ書かない。件数・statusのみ)
8  LINE実着信確認        実際にAlarmが鳴った際に、LINEへ#501の文面が届くこと(利用者の実機確認)
9  duplicate suppression 7の直後にretry等で同一fingerprintが再度届いた場合、2件目以降が
   確認                  LINE送信されないことをCloudWatch Logsで確認(本文は出さない。件数のみ)
10 rollback方法確認      AlarmActionsを空へ戻すChangeSet(新資源自体はRetainのため残してよい。
                      既存の判定・通知経路には一切関与しないため実害はない)
11 運用者への明示        「Errors Alarmが鳴らないこと」は「12関数すべてが正常」を意味しない旨を
                      明記する(下記29.3参照)
```

### 29.3 既知の限界(必ず理解しておくこと)

```
Errorsメトリクスへ計上される(このAlarmで検知できる)
  disclosure-check / evaluation / weekly-review / monthly-review / quarterly-review /
  watchlist-terminal-failure-handler(未捕捉例外がハンドラを抜けて伝播する)

Errorsメトリクスへ計上されない(このAlarmでは検知できない)
  buy-candidates / holdings-watchlist / watchlist-dispatcher / watchlist-worker /
  watchlist-batch-reconciler / line-webhook
  (per-item/per-batchの`except Exception`が広く捕捉し、re-raiseしない設計。既存の設計意図どおり)
```

**段階1の完了は自動検知の完成を意味しない。監視対象はLambda12本中1本で、W6で新設した
待避先は含まない。** 加えて、Errorsベースの検知は上記6関数には効かない(担当は#506。
reconcilerが持つ終端状態〔完了判定・DLQの滞留等〕を見る仕組みが要る)。

### 29.4 self-monitoring の残存リスク

`IncidentNotifierFunction`自身のErrorsは、自己再帰(Alarm→自身のTopic→自身のFunction→…)を
避けるため、同一Topicへは接続していない。**このLambda自身が失敗した場合、本段階では誰にも
通知されない。** 第二の通知経路は本Issueのscope外(#504以降)。

### 29.5 この節が決めていないこと

```
・Production の SNS Topic 新設・deploy 自体(別 Human Gate)
・#504(Errors alarmを全12関数へ拡大)/ #505(Duration alarmの拡大)/
  #506・#507(reconciler相乗り検知)/ #508(GitHub Issue接続)の実装
・6関数(per-item捕捉型)の内部異常検知の方式そのもの(#506/#507の担当)
```

### 29.6 Issue #504のChangeSet想定差分(2026-09-24追加)

段階2(#504)は、EvaluationFunction以外の残る11関数へErrors alarmを追加する
(既存2 alarmは#503で反映済みのため変更しない)。

```
ADD     11(BuyCandidatesFunctionErrorsAlarm / HoldingsWatchlistFunctionErrorsAlarm /
           DisclosureCheckFunctionErrorsAlarm / WatchlistDispatcherFunctionErrorsAlarm /
           WatchlistWorkerFunctionErrorsAlarm /
           WatchlistTerminalFailureHandlerFunctionErrorsAlarm /
           WatchlistBatchReconcilerFunctionErrorsAlarm / WeeklyReviewFunctionErrorsAlarm /
           MonthlyReviewFunctionErrorsAlarm / QuarterlyReviewFunctionErrorsAlarm /
           LineWebhookFunctionErrorsAlarm)
MODIFY  0(既存のEvaluationFunctionErrorsAlarm/DurationAlarmは変更しない。再利用のみ)
REMOVE  0
```

C4相当の判定基準: 上記ADD 11・MODIFY 0・REMOVE 0と一致し、既存resourceへの想定外のMODIFY・
IAM権限の拡大・Alarmのthreshold等の変更が無いこと。実物のChangeSetとの照合は、他のPRが
先にdeployされていた場合(config/srcの通常のCode差分)を含めて、ChangeSet CREATE後に
再確認する(#503のRelease W8で確立した手順と同じ)。

### 29.7 Issue #505のChangeSet想定差分(2026-09-24追加)

段階2(#505)は、WeeklyReviewFunctionのみへDuration alarmを追加する(既存の
EvaluationFunctionDurationAlarmは変更しない)。他10関数(WatchlistWorker/
WatchlistDispatcher/LineWebhook/BuyCandidates/HoldingsWatchlist/DisclosureCheck/
WatchlistBatchReconciler/MonthlyReview/QuarterlyReview/WatchlistTerminalFailureHandler)
への追加は今回見送り(MANAGER判断)。

```
ADD     1(WeeklyReviewFunctionDurationAlarm。Threshold=240,000ms=Timeout 300秒×0.8)
MODIFY  0(既存のEvaluationFunctionDurationAlarm/12関数のErrors alarmは変更しない)
REMOVE  0
```

C4相当の判定基準: 上記ADD 1・MODIFY 0・REMOVE 0と一致し、既存resourceへの想定外のMODIFY・
IAM権限の拡大・Alarmのthreshold等の変更が無いこと。240秒という値の実測による裏付けは
次回自然実行(2026-09-28 19:00 JST)後に行う(29.2項目3〔人工障害を起こさない〕と同じ理由で、
それまでの人工的な実行はしない)。

## 30. Release W9 の Production Verification Plan(2026-09-24追加。骨子)

段階2〜3の複数Issue(#504・#505・#506・#507・#368・#349)をまとめてProductionへ反映する
grouped release(社内呼称 W9)の確認手順である。29節(#503。段階1)の実行経路
(Alarm → SNS → IncidentNotifier → LINE)を前提に、その先の監視対象・通知内容の
拡張分を扱う。

★ **W9の正式対象はこの6件(#504・#505・#506・#507・#368・#349)である**(USER決定)。
このうち#349は2026-09-24時点で**実装完了・PR #554でレビュー中(未merge)**であり、
**W9 ChangeSet CREATEは#349のmerge完了後まで保留**する(既存USER決定〔HANAKO-20260924-075〕
のとおり)。#349のVerification観点は30.8として下記に追加した(実装・PR段階の内容に
基づく。merge・deployを前提にした記載ではない)。**最新の対象範囲・進捗はIssue #503の
最新コメント(Release W9 inventory)を読むこと**(この節へ焼き込まない)。

### 30.1 この節が扱わないこと

```
・#503自体のend-to-end(Alarm→SNS→IncidentNotifier→IncidentState claim/SENT→LINE→
  duplicate suppression)の自然発生確認は29.2項目7〜9が正本であり、本節では重複させない
  (W9反映後もこの経路自体の設計は変わらないため)
・#508(GitHub Issue自動起票)は本節の対象外(別release候補。#132 Phase 4)
・Production の ChangeSet 新設・deploy 自体(別 Human Gate)
```

### 30.2 P0: deploy直後(read-only)

```
・stack status = UPDATE_COMPLETE / FAILED event 0件 / rollbackなし
・#537(Issue #537)のWRITE/READ flagが両方ともfalseのまま(環境変数を直接確認。
  W9はこの2値を変更しない)
・#504由来の新設Errors alarm 11本・#505由来の新設Duration alarm 1本、計12資源の存在確認
・WatchlistBatchReconcilerFunctionRoleのIAM差分が想定どおり(#506由来のsns:Publish
  〔Resource=IncidentNotificationTopicのみ〕・#507由来のcloudwatch:GetMetricData
  〔Resource="*"。CloudWatchメトリクスがARNを持たないため〕の2 Statement追加のみで、
  想定外の権限拡大が無いこと)
```

### 30.3 P1: 次回reconciler自然実行(毎時)

```
・invocation正常終了(Errors=0を最優先で確認)。
  ★ #507が追加したCloudWatch GetMetricData呼び出しには例外処理が無く、この呼び出しが
  失敗(throttling・AccessDenied等)すると、reconciler Lambdaの実行全体が失敗する
  (この回に本来行うはずだったtimeout finalize retry・maintenance trigger等の処理も
  含めて失敗として記録される。実装が意図した「握り潰さない」設計であり、バグ修正の
  対象ではないが、Errorsの原因切り分けにおいて最優先で確認すべき項目)
・GetMetricData成功(AccessDeniedなし)
・#506/#507が追加した検知(missed schedule / 候補ユニバース連続失敗 / queue backlog /
  watchlist削除ゼロ継続)のfalse positiveが無いこと(誤検知でLINEが飛んでいないこと)
・既存flag(config/watchlist_screening_rules.yamlのenabled / scheduled_run_enabled。
  #506/#507は専用のkill switchを新設せずこれらを再利用している)のsemanticsが
  変わっていないこと
```

### 30.4 P2: 次回対象通知(#368)

```
・通知本文の株価表示が「MM/DD終値」の形(as-of日付付き)になっていること
・#368のdeploy前に保存された既存Recommendationレコード(price_as_of_dateを持たない)を
  参照する通知では、日付を省略した「終値」表示にfallbackし、エラーにならないこと
```

### 30.5 P3: Evaluation horizon経過後(#368)

```
・reached_partial_profit_start_price / reached_recommended_limit_price /
  reached_full_profit_consideration_price / business_days_to_reach_sell_priceが
  EvaluationResultへ実際に保存されること(利確目安への到達確認。SELL側)
```

### 30.6 P5: 2026-09-28 19:00 JST(週次レビュー自然実行。#377/#539)

```
・Errors=0・正常完了(W9反映後、実際にこの時刻の自然実行で確認する初回)
・Max Memory Used・実際のDuration値を実測し、#377(PR #539)のOOM修正がProduction
  反映後も有効であることを確認する
・#505のWeeklyReviewFunctionDurationAlarmがOKのままであること(240秒を大きく
  下回ることを期待。既存の9/21実行の実測値はpre-#539のデータのため参考にしない)
```

### 30.7 ロールバック時の留意点(#368)

`Recommendation`はImmutableSnapshot(`extra="forbid"`のfrozenモデル)である。W9反映後に
新規保存された`price_as_of_date`入りのRecommendationレコードを、rollback後の旧コードが
読もうとするとデシリアライズに失敗する(既存の別フィールド〔`company_quality_score_
model_version`〕と同型の既知パターン)。**実害が出るのは「W9反映後に新規保存された
レコードを、その後rollbackした旧コードが読む」場合のみ**で、反映直後(新規レコードが
まだ無い間)のrollbackは安全である。

### 30.8 #349(DLQ滞留の監視・発報)のVerification観点

★ 2026-09-24時点、#349はPR #554として実装・レビュー・mainへのmergeまで完了。以下は
mainへ反映されたPR #554の実装内容に基づくVerification観点である。Productionへの
deploy・verification完了を意味するものではない。実装内容が変わった場合はこの節も更新する。

```
対象4本(真正のDLQ。命名規約ではなく、(a)redrive chainの終端として配線されている
  〔RedrivePolicy.deadLetterTargetArn / EventInvokeConfig.DestinationConfig.OnFailure.
  Destinationの宛先として参照〕、または(b)MessageRetentionPeriod=1209600〔14日。
  運用調査用の長期保持〕を持つ、の**和集合**〔かつ自身はRedrivePolicyを持たない〕で特定。
  (b)はiteration 3で追加された基準で、まだどこからも配線されていない孤立DLQを
  (a)だけでは拾えない退行への対応)
  WatchlistTerminalFailureDLQ / AsyncInvokeFailureDLQ /
  BuyCandidateTerminalFailureDLQ / HoldingsWatchlistTerminalFailureDLQ
  (後2本は#319 Phase 1で未wiringのdormant DLQだが、Phase 2でdispatch側が
  切り替わった際の監視漏れを防ぐため先行して対象に含める。USER決定)

Alarm設計(4本共通)
  Namespace=AWS/SQS, MetricName=ApproximateNumberOfMessagesVisible,
  Statistic=Maximum, Period=300, EvaluationPeriods=1, Threshold=1,
  ComparisonOperator=GreaterThanOrEqualToThreshold, TreatMissingData=notBreaching,
  AlarmActions=[IncidentNotificationTopic](#503。新規のTopic・Topic Policyは追加しない)
```

P0(deploy直後・read-only。#349分)
```
・AsyncInvokeFailureDLQ・WatchlistTerminalFailureDLQ・BuyCandidateTerminalFailureDLQ・
  HoldingsWatchlistTerminalFailureDLQの4本すべてにAlarmが接続されていること
  (Namespace/MetricName/Statistic/Period/EvaluationPeriods/Threshold/
  ComparisonOperator/TreatMissingData/AlarmActionsが上記設計どおりであること)
・新規のSNS Topic・Topic Policyが追加されていないこと(既存IncidentNotificationTopicを
  そのまま再利用する設計のため)
```

P1(次回reconciler自然実行等。平常時のノイズ確認)
```
・4本のDLQがいずれも空(平常時)のあいだ、通知・ログのノイズが増えないこと
  (TreatMissingData=notBreachingにより、データ点が飛ぶ時間帯もALARMにならないことを含む)
```

P4相当(自然発生時。人工的なDLQ投入・人工Alarm発火は行わない)
```
・DLQへメッセージが実際に滞留した場合、既存の#132/#503通知経路(Alarm→SNS→
  IncidentNotifier→LINE)へ正しく接続され、実際にLINEへ届くこと
  (`_extract_alarm_target()`のQueueName dimensionへのfallback経路を含む。
  FunctionName dimensionを持つ既存alarm〔#503〜#505〕の経路は変更していない)
・Alarmが「OK」へ戻っても、対象job・batchが復旧したことを意味しない
  (redrive・purge・retention経過のいずれでもQueue depthは0に戻るため。
  「OK = 復旧」と読まない。29節の「Errorsが止まった = 復旧ではない」と同種の注意)
```

## 31. release前validationの責務境界とChangeSet CREATE/EXECUTEの限界(Issue #559、2026-09-25追加)

Release W9のChangeSet EXECUTEが、`IncidentNotificationTopicPolicy`(AWS::SNS::
TopicPolicy)の2 StatementにSidが無いことをSNS APIが拒否して失敗した事象(直接原因は
Issue #557で修正済み。Production は自動rollbackによりW8時点のまま安全に保たれた)を
受け、release前のvalidation層それぞれが**何を検証でき、何を検証できないか**を
Issue #559で調査し、本節へ反映する。

### 31.1 CHANGESET_CREATE_COMPLETE ≠ EXECUTION_WILL_SUCCEED

```
CloudFormationは、ExecuteChangeSetを呼ぶまでリソースへの実際の変更を一切行わない
(AWS公式ドキュメント: "CloudFormation doesn't make changes until you execute the
change set." — CreateChangeSet APIリファレンス)。
```

ChangeSet **CREATE**が`CREATE_COMPLETE`になることは、テンプレートの構文・型・
IAM capability・既存stackとの差分計算が成功したことを意味するのみであり、
**実際のリソースプロバイダAPI(例: SNSの`SetTopicAttributes`)がその内容を受理する
ことを保証しない。** downstream AWS APIが呼び出し時にのみ行うbusiness rule検証
(今回のSNS TopicPolicyの複数Statement Sid一意性制約はその一例)は、CREATE_COMPLETE
では検出できず、**EXECUTEで初めて顕在化する。**

`CREATE_COMPLETE = このChangeSetをEXECUTEしてよい`という運用上の意味は変わらない
(差分確認・Human Gateの前提として引き続き必須)。ただし`CREATE_COMPLETE = EXECUTEが
必ず成功する`ではないことを、release実施者・承認者の双方が前提として持つこと。

### 31.2 validation層ごとの責務境界(現状の実測。2026-09-25時点)

```
sam validate / --lint        テンプレートの構文・SAM構文・基本的な型検証。
                              CI未導入(手動実行のみ)。AWS API固有のsemantic制約は
                              検証しない(設計上の対象外)

cfn-lint(v1.57.0で実測)     テンプレートのresource schema検証(例: 存在しない
                              propertyの検出。E3002)。Issue #559のスパイクで実測
                              (レビュー対応: PR #562。当初の記載を訂正): 今回
                              #557で実際に起きた欠陥(Sidの**欠落**。複数Statementの
                              いずれかにSid自体が無い)は**検出できない**(exit=0)。
                              一方、Sidの**重複**(複数StatementのSidが同じ値)は
                              **検出できる**(E3512 "array items are not unique
                              for keys ['Sid']")。つまりcfn-lintは一意性
                              (uniqueness)チェックは持つが、必須性
                              (presence/required)チェックを持たない、という
                              非対称な検出力である。今回のIncidentNotificationTopic
                              Policyの実際の欠陥は「欠落」型だったため、cfn-lintを
                              CIへ導入していても本件は防げなかった。CI未導入

repo独自のinfra unit test    `tests/unit/test_infra_*.py`。repo内の情報(template.yaml
(tests/unit/test_infra_*.py)  の静的構造)から導出できる契約を検証する。AWS API固有の
                              semantic制約は、**過去に実際にAWS APIから拒否された
                              制約について、resource typeベースで汎用化した契約を
                              都度追加する**ことでのみカバーされる(例:
                              `test_infra_issue_557_topic_policy_sid.py`の
                              AWS::SNS::TopicPolicy横断テスト、
                              `test_infra_issue_559_sqs_policy_sid_contract.py`の
                              AWS::SQS::QueuePolicy予防的テスト)。**悉皆的な
                              AWS API仕様の再実装ではない**(全AWS semantic
                              constraintを事前に網羅することは現実的でない)

CloudFormation ChangeSet     テンプレート差分の計算のみ。AWS API側のbusiness
CREATE                       rule検証は行わない(31.1)

Production ChangeSet EXECUTE 実際のリソースプロバイダAPI呼び出しが発生する、
                              唯一AWS API側のsemantic制約を検証できる段階。
                              ここで失敗した場合はCloudFormationの自動rollbackが
                              安全網として働く(実害ゼロで収束する設計)が、
                              「事前検知ができた」ことは意味しない
```

**実AWS環境でのpreflight/dry-run(ChangeSet CREATE〜EXECUTEを別accountの
staging環境で先行実行する等)は、本節時点では採用していない。** CI/release
pipelineへAWS credentialを新規・広範に持たせることになり、Issue #164(長期
broad credentialの恒久利用)・Issue #359(deploy principalの権限設計)が指摘する
懸念と衝突するため、着手にはUSER判断が必要(Issue #559 USER_DECISIONS_REQUIRED。
2026-09-25時点でDEFER)。

### 31.3 新しいAWS API semantic制約が判明した場合の拡張方針

将来、今回と同種の欠陥(CloudFormation上はvalidだが実AWS APIで拒否される設定)が
別のresource typeで判明した場合の追加手順:

```
1  実際のAWS APIエラー(HandlerErrorCode・エラーメッセージ)を実測で記録する
   (推測で一般化しない。31.2の「悉皆的な再実装ではない」原則どおり、実際に
   遭遇した制約のみを個別に追加していく)
2  制約の対象がresource type固有か、Issue固有のLogicalIdに限定されるかを
   AWS公式ドキュメントで一次確認する(可能な範囲で。今回のSNS/SQSのように、
   AWS公式が「一部のサービスではSidを要求する場合がある」と例示している
   ケースもあれば、明示の一次情報が見つからない場合もある〔#559参照〕)
3  `tests/unit/test_infra_issue_<N>_*.py`へ、そのresource type全体を走査する
   汎用テストを追加する(LogicalId固有の回帰テストと、resource typeベースの
   汎用テストの2段構成。#557/#559のパターンを踏襲する)
4  一次情報が無い・未確認のまま予防的に対象を広げる場合は、テストの
   docstringで「確認済みの制約」と「予防的な備え」を明確に区別する
   (#559のAWS::SQS::QueuePolicyテストの例に倣う)
5  cfn-lintは一意性(uniqueness)違反は検出できるが必須性(presence)違反は
   検出できないという非対称な検出力を持つ(31.2実測)。新しい制約が
   「欠落」型か「重複」型かを見極めたうえで、欠落型はcfn-lintに頼らず
   3の repo独自contract testを主たる防御とする。重複型はcfn-lintが既に
   検出できる可能性があるため、CI導入時はその点を活かせる(ただし本節
   時点でcfn-lint自体はCI未導入)。いずれの型であっても、Production
   ChangeSet EXECUTE時のHuman Gateでの差分確認を最終防御として維持する
```

本節は Issue #559(design-defect・priority:P2)の実装(PR-1〜PR-3)の一部として
追加した。判定ロジック・通知内容・保存データ形式・Production挙動は変更していない。

## 32. 本番ジョブ異常のGitHub Issue自動起票(Issue #508。#132 X-9)の Verification Plan(2026-09-25追加)

`IncidentNotifierFunction`(29節の通知経路)へ、本番ジョブ異常のGitHub Issue自動起票・
コメント追記(`services/incident_github_issue_service.py`)を追加する機能である。
**本節は`issue_creation_enabled`をfalse→trueへ切り替える際の確認手順のみを扱う。
切替操作自体・コード変更・config値の変更は本節の対象外**(32.6参照)。

### 32.1 前提

```
config/incident_notification.yaml の issue_creation_enabled(既定 false)
  false の間: GitHub API・Secrets Manager呼び出しを一切行わない
             (正常なスキップ。LINE通知経路〔29節〕には一切影響しない)
  true化    : deployとは別のHuman Gate(#508 USER決定。追加条件として明記)。
             Lambda LayerでYAMLを配布する静的設定のため、YAML編集だけでは
             反映されない(config編集 + 再deployが必要。5.1節のreview_improvement.yaml
             と同じ制約)
```

Issue #508 の USER 決定(issuecomment。Phase A の USER_DECISIONS_REQUIRED への回答)。

```
U-3  ALLOW(PUBLIC repositoryへの自動Issue作成を許可)。ただし
     Production環境での人工テストIssue作成は禁止
U-4  EXISTING_GITHUB_APP_REUSE(既存のGitHub App資産〔infrastructure/github/
     client.py。5.1節でWeeklyReviewFunctionが使うものと同一〕を再利用。
     新規認証方式は導入しない)
U-5  OPEN→COMMENT / CLOSED→NEW ISSUE(同一fingerprintの再発時、OPEN Issueには
     コメント追記、CLOSED後は旧Issue番号参照付きの新規Issueを作成する。
     reopenはしない)
```

### 32.2 切替前の確認事項(1): GitHub App資産の疎通確認

`IncidentNotifierFunction`は新規のGitHub Appを作らず、5.1節でセットアップ済みの
GitHub App資産(`GithubAppSecretArn`/`GithubRepository`のCloudFormation parameter・
Secrets Managerの秘密鍵)を`WeeklyReviewFunction`と共有する(U-4)。したがって
本節の確認は**新規セットアップではなく、既存資産が引き続き有効であることの確認**である。

```
1  Lambda環境変数確認   IncidentNotifierFunctionの環境変数に GITHUB_APP_SECRET_ARN /
                       GITHUB_REPOSITORY が設定されていること(値そのものではなく
                       変数名の存在のみをPUBLIC repositoryへ書く。ARN実値は書かない)
2  Secret疎通確認       secretsmanager:GetSecretValueで復号できること・JSON形式が
                       {app_id, installation_id, private_key} の3項目を持つこと
                       (5.1節の既存確認手順と同一。値そのものは記録しない)
3  GitHub App権限確認   対象リポジトリへのインストールが有効であり、
                       Issues: Read and write / Metadata: Read-only の権限を
                       保持していること(GitHub UI。5.1節セットアップ時に付与した
                       ものが失効・変更されていないかの再確認)
4  既存機能への相乗り確認 5.1節のWeeklyReviewFunction側(review_improvement.yaml の
                       issue_creation_enabled)が現在どちらの値でも、本節の確認には
                       影響しない(secretは共有だが、GithubIssueClientの呼び出しは
                       関数ごとに独立しており、片方の状態がもう片方の疎通確認結果を
                       左右しない)
```

### 32.3 切替前の確認事項(2): labelの非混在確認

```
config/review_improvement.yaml   issue_labels: [rule-improvement, auto-generated]
config/incident_notification.yaml issue_labels: [production-incident, auto-generated]
```

両者は`auto-generated`を共有するが、重複判定・close運用の実体は`auto-generated`
ではない。`services/incident_github_issue_service.py`の`_SEARCH_LABEL`は
`"production-incident"`固定であり、stale claim復旧時の実在確認
(`search_open_issue_by_marker()`)は`production-incident`ラベル**かつ**
fingerprintマーカー(HTMLコメント)の両方が一致する場合のみ既存Issueとして扱う。
`rule-improvement`ラベルのIssue(週次改善レビュー由来)を誤って本機能のdedup対象に
取り込むことはない(実装を実読して確認済み)。

```
1  既存label確認   切替前に `production-incident` ラベルを持つ既存OPEN Issueが
                  0件であること(gh issue list --label production-incident)。
                  0件でなければ、それが本機能によるものか他の経路による手動付与かを
                  切替前に確認する(手動付与された同名labelがあると、fingerprint
                  マーカーが一致しない限り実害は無いが、誤認の元になるため)
2  search label確認  `_SEARCH_LABEL`(コード実読)と
                  `config/incident_notification.yaml`の`issue_labels`先頭要素が
                  一致していること(現状は両方"production-incident"。切替前に
                  再読して確認する)
```

### 32.4 切替前の確認事項(3): IAM(exact resource scope)の確認

Issue #508 の USER 決定 U-2(PARTIAL/resource-scoped限定でPROCEED。#133 全体の
解消は待たない条件として「exact ARN指定・wildcard禁止」を付した)に対応する。

```
1  template確認     infra/template.yaml の IncidentNotifierFunction の Policies で、
                   secretsmanager:GetSecretValue の Resource が
                   !Ref GithubAppSecretArn(exact ARN 1件)のみであり、
                   `*` や `arn:aws:secretsmanager:...:secret:jstock/*` 等の
                   wildcard/prefixマッチが無いこと(コード実読で確認済み。
                   デプロイ前にも再確認する)
2  IncidentStateTableのIAMも同様に確認  dynamodb:GetItem/PutItem/UpdateItem/DeleteItemの
                   Resourceが!GetAtt IncidentStateTable.Arn(exact ARN)のみで
                   あり、Scan/Queryの権限が付与されていないこと(GSIが無く
                   fingerprint単位のみで足りるため。既存設計)
3  ChangeSet差分確認  デプロイ時のChangeSet CREATEで、上記2つのStatement以外に
                   IncidentNotifierFunctionへ新たなIAM Permissionが追加されて
                   いないこと(30〜31節の一般手順どおり、想定外のADD/MODIFYが
                   無いことを確認してからEXECUTEする)
```

### 32.5 切替後に確認すること(自然発生のincidentのみ。32.6参照)

```
1  allowlist確認     実際に作成されたGitHub Issueの本文・コメントが、
                    IncidentIssueNotice が持つフィールドのみで構成されていること
                    (job・occurred_at・fingerprint・occurrence_count・
                    failure_stage・failure_count・consecutive_days・is_ongoing)。
                    stack trace・生exception message・AWS account ID・ARN・
                    request ID・secret・tokenのいずれも含まれないこと
                    (`domain/notification/incident_github_issue_message.py`の
                    build_incident_issue_body()/build_incident_comment_body()が
                    出力する固定の文型どおりであることを実物で確認する)
2  再発契約(U-5)確認  同一fingerprintが再発した場合:
                      - 前回のIssueがOPENのまま      -> 新規Issueを作らず、
                        既存Issueへ「### 再発(N回目)」形式のコメントが
                        追記されること(occurrence_countが前回と異なる)
                      - 前回のIssueがCLOSED済み       -> reopenされず、
                        本文冒頭に "Previous issue: #<旧番号>" を含む
                        新規Issueが作成されること
                    確認は自然発生時のみ。人工的に再発条件を作らない(32.6)

   ★ 上記は「occurrence_countが実際に増えた場合」の期待結果であり、
     「同一fingerprintを再受信するたびに必ず新しいコメントが付く」という
     意味ではない(`services/incident_github_issue_service.py`の
     `_post_comment()`/`_reconcile_stale_comment()`実読で確認)。

     - 既存OPEN Issueへのコメント追記は**occurrence単位の重複抑止**に従う。
       `last_commented_occurrence_count == notice.occurrence_count`の場合
       (今回のoccurrenceについて既に投稿済み)は追加投稿しない
     - 同一fingerprintの再受信(例: dedup window内でのSNS/Lambda retry)は、
       LINE側がSUPPRESSEDのままoccurrence_countを進めないことが多く、
       **同一fingerprintの再受信とoccurrence_countの増加を同一視しない**
       (occurrence_countは`IncidentStateTracker`の状態から読むのみで、
       GitHub側の呼び出し自体はoccurrence_countを進めない)
     - 処理中claim(`comment_claim_occurrence_count`が今回のoccurrenceと
       一致し、`comment_claim_expires_at`が未失効)の間は、他実行が
       処理中のため何もしない(無条件の即時投稿を要求しない)。
       stale claim(claim期限切れ)の場合は、GitHub側の実在確認
       (`find_comment_by_marker()`)を先に行ってから再claimして投稿する
       (既存実装のstale takeover契約どおり)
3  LINE経路からの独立性確認  `lambda_handlers/incident_notifier_handler.py`の
                    `_process_signal()`/`_send_line()`と
                    `services/incident_github_issue_service.py`を実読して
                    確認した設計は次のとおりであり、GitHub処理の成否と
                    LINE経路の挙動は独立に確認する(いずれのケースも、対象
                    実行のCloudWatch Logs INFOログ`incident_notifier claim
                    source=... fingerprint=... outcome=...`でclaim outcomeを
                    確認したうえで、期待結果と実測を照合する。Lambdaの
                    ErrorsメトリクスやDynamoDBのstatusだけで因果関係を
                    断定しない)。

   a  LINE送信対象・LINE成功
      (outcome ∈ {CLAIMED_NEW, CLAIMED_AFTER_DEDUP_WINDOW,
       CLAIMED_STALE_TAKEOVER}、`_send_line()`のpush_messageが成功)
      期待結果: LINEは`mark_sent`済みで正常に完了していること。GitHub処理
      (`_attempt_github_issue()`)は成功・失敗いずれの場合も、その例外が
      `_process_signal()`側の外側try/exceptで完全に捕捉され、Lambda呼び出し
      自体を失敗させないこと(=この場合IncidentNotifierFunctionのErrorsが
      増えないこと)。GitHub側の失敗がLINEの成功結果を覆さないことを確認する
   b  LINE送信が重複・処理中により抑止
      (outcome ∈ {SUPPRESSED_DUPLICATE, SUPPRESSED_ACTIVE_CLAIM})
      期待結果: 既存の抑止動作(LINE送信を行わない)がそのまま維持されて
      いること。GitHub処理はLINEのoutcomeに関わらず試行される設計だが、
      これは既存の抑止契約を変更するものではない。**「抑止されたので
      GitHub処理側がLINEを追加送信する」ことを合格条件にしない**
      (そのような経路はコード上存在しない)
   c  LINE自体が失敗(outcome ∈ 上記CLAIMED_*、push_messageが例外)
      期待結果: 既存の`release_claim()`(outcomeがCLAIMED_NEWならfingerprint
      行を削除。それ以外は行を残したまま`claimed_at`を古い値へ書き換えて
      即座にstale takeover可能にし、`occurrence_count`を-1して打ち消す)と、
      例外の再送出(SNS/Lambda retryへ委ねる既存契約)が変わっていないこと。
      **この場合、IncidentNotifierFunctionのErrorsが増えることは想定内**
      (LINE自身の既存retry契約による。#508より前から存在する挙動)であり、
      これを「GitHub経路がLINE経路を壊した」証拠として使わない。
      outcomeがCLAIMED_NEWでLINEが失敗した場合は、fingerprint行削除後の
      部分再生成による重大な回帰(既存コードのコメントに実測記録あり)を
      避けるため、GitHub処理自体が今回スキップされる
      (`github_safe_to_attempt = False`)。CLAIMED_AFTER_DEDUP_WINDOW /
      CLAIMED_STALE_TAKEOVERでLINEが失敗した場合はGitHub処理は試行される
      (LINE失敗との因果関係を混同せず、GitHub側の成否とLINE側のErrors
      増加を別々に確認する)

   ★ 上記3ケースのうち、対象期間中に自然発生しなかったものは
     「未観測/検証待ち」とし、確認できなかったことをもってPASS扱いにしない
     (検証のための人工障害〔LINE認証情報の意図的な無効化等〕は発生させない。
     32.6参照)
4  DynamoDB確認       IncidentStateTableをfingerprint単位でGetItemし、
                    github_issue_number・github_issue_create_status・
                    previous_github_issue_number(再作成時のみ)を確認する
                    (値そのものはPUBLIC repositoryへ書かない。件数・statusのみ)
```

### 32.6 禁止事項・この節が決めていないこと

```
・Production環境での人工的なテストIssue作成は禁止(U-3)。CloudWatch Alarmの
  人工発火・SNS Publish・Lambdaの手動invokeによる本機能の動作確認は行わない。
  自然発生のincidentでのみ32.5を確認する
・issue_creation_enabledをfalseからtrueへ実際に切り替える操作(別Human Gate。
  本節はその後の確認手順のみ)
・コード変更・config値の変更(本節はいずれも行わない。#508はコード面では
  PR #563〔D5〕・PR #565〔D9〕で完了済み)
・GitHub App本体の新規作成・インストール(5.1節で既に完了済み。本機能は
  U-4により既存資産を再利用するのみ)
```

### 32.7 ロールバック手順

```
1  config/incident_notification.yaml の issue_creation_enabled を true から
   false へ戻す
2  sam build && sam deploy で再デプロイする(静的設定のため、config編集単独では
   反映されない。32.1参照)
3  ロールバック後もIncidentStateTableの既存レコード(github_issue_number等)と、
   既に作成済みのGitHub Issueはそのまま残る(削除・close操作は本節の対象外。
   必要な場合は別のHuman Gateで判断する)
4  ロールバック後はfalse運用時と同じ挙動に戻る(GitHub API・Secrets Manager
   呼び出しは発生しない。29節のLINE通知経路には影響しない)
```

### 32.8 参考

```
・障害対応runbook(27節。Issue #500)
・#508 Acceptance Criteria(Issue本文)・USER決定 U-1〜U-5(issuecomment)
・GitHub App資産のセットアップ手順(5.1節。本節は再実行しない。既存資産の
  疎通確認のみ)
・本番ジョブ異常の検知(通知経路)のVerification Plan(29節。Issue #503)
```

本節はIssue #508(#132 X-9)のPhase B(PR #563・PR #565。コードはmainへmerge済み)
の残作業として、docsのみ追加した。判定ロジック・通知内容・保存データ形式・
Production挙動は変更していない。`issue_creation_enabled`の実際の切替はいずれの
Human Gateも経ておらず、本節の追加によってもProduction上の挙動(既定falseのまま)は
変わらない。
