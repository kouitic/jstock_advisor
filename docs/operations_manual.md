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

### 3.4 ウォッチリスト自動追加の候補銘柄一覧(2026-08-01追加)

`data/universe/candidate_universe.csv`(列: `stock_code`、任意で`memo`)に、
週次スクリーニング(5.7節)の評価対象としたい証券コードを登録してください。
このファイルに載っていない銘柄は自動追加の対象になりません(東証全銘柄の
自動スキャンは行いません)。初期値には、既存の`watchlist_candidates_2026-07-30.csv`
(低PER/PBRスクリーニング結果)の証券コード列を流用しています。追加・削除は
CSVを直接編集するだけで反映されます(取込コマンドの実行は不要)。

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

## 4.1 ウォッチリスト自動追加(週次、2026-08-01追加)

| 時刻 | schedule.yamlのジョブ | 対応コマンド | 対応Lambda関数 |
|---|---|---|---|
| 毎週土曜07:00 | (未登録。`infra/template.yaml`にcron直書き) | `jstock watchlist-screening run` | `WatchlistAutoAdditionFunction` |

候補銘柄一覧(3.4節)を評価し、条件を満たした銘柄をウォッチリストへ自動追加します。
`config/watchlist_screening_rules.yaml`の`enabled`/`weekly_schedule_enabled`が
両方`true`の場合のみ実行されます(`enabled=false`でも後述の`--dry-run`は実行可能)。

実際に登録・通知を行わずに結果だけ確認したい場合は、次のコマンドを使ってください
(WatchlistRepositoryへの書き込み・LINE通知・監査ログ記録は一切行いません)。

```bash
jstock watchlist-screening run --dry-run
```

`--dry-run`を付けずに実行すると、CLIを実行した端末上で(Lambdaの並列処理と
違い単一プロセスで)候補銘柄すべてを評価し、実際にウォッチリストへ追加します。
ローカル運用でこの機能を使いたい場合は、このコマンドを`config/schedule.yaml`の
想定どおり毎週土曜朝に実行するようタスクスケジューラ等へ登録してください
(9節相当、AWS版は自動実行されます)。

---

## 5. 週次・月次・四半期レビュー

| 頻度 | schedule.yamlのジョブ | 対応コマンド | 対応Lambda関数 |
|---|---|---|---|
| 週次(土09:00) | `weekly_review` | `jstock review report --notify` | `WeeklyReviewFunction` |
| 月次(第1土10:00) | `monthly_review` | `jstock review report --notify`(全ホライズン合算)<br>必要に応じ `jstock performance summary --horizon <N>` で特定ホライズンを確認 | `MonthlyReviewFunction` |
| 四半期(1,4,7,10月第1土11:00) | `quarterly_logic_review` | 第7節「ルール改善承認フロー」を参照 | `QuarterlyReviewFunction`(★LINEでリマインドを送るのみ。提案の自動生成はしない) |

ローカルCLIには「当月第1土曜日か」を判定する処理はありません(実行タイミングは
利用者が手動判断してください)。AWS Lambda版(`MonthlyReviewFunction`/
`QuarterlyReviewFunction`)は毎週土曜に起動したうえで、`lambda_handlers/_scheduling.py`が
当月第1土曜日かどうかを内部判定し、該当しない場合は何もせず終了します。
`QuarterlyReviewFunction`はルール改善提案(リスク影響・過学習リスク評価等の自由記述を
要する)を自動生成しません(要求仕様45節の人間承認必須の原則のため)。レビュー時期が
来たことをLINEで知らせるのみで、実際の`rules backtest`/`rules propose`は利用者が
手動で実行してください。

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
jstock evaluation list --recommendation-id <推奨ID>  # 定点評価結果の確認
jstock feedback add --recommendation-id <推奨ID> --satisfaction-score 4
```

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
