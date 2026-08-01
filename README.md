# jstock_advisor

ClaudeCodeで作った日本株売買の補助アプリ。

> **投資助言ではありません。** 本システムが出力する判定・スコア・価格目安・
> ランキング等は、設定したルールに基づく機械的な計算結果であり、特定の金融商品の
> 売買を推奨するものではありません。投資判断は必ずご自身の責任で行ってください。
>
> **データは遅延・誤りを含む場合があります。** 株価・財務情報はyfinance(非公式)、
> 上場銘柄一覧・JPX400構成銘柄はJPX/日経の公開ファイル、適時開示情報はEDINET由来
> です。いずれも取得タイミングのずれ・提供元側の障害・パース誤りにより、最新でない
> 値や誤った値が含まれる可能性があります。重要な判断の前には一次情報源で確認して
> ください。

## セットアップ(ローカル)

```bash
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
```

## LINE通知の設定(任意、ローカルCLI用)

`--notify` オプションで実際にLINEへ通知するには、`.env.example` をコピーして
`.env` を作成し、LINE Messaging APIのチャネルアクセストークンとuserIdを設定してください。

```powershell
Copy-Item .env.example .env
# .env を編集してLINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID を設定
```

`.env` が無い、または値が未設定の場合は標準出力へのドライラン表示のみになります
(実際の送信は行われません)。`.env` は `.gitignore` で追跡対象外です。
AWSデプロイ時はSecrets Manager経由で同等の値を設定する(下記「デプロイ手順」参照)。

## CLIコマンド例

```bash
jstock holdings add 8136 --shares 100 --price 3775 --account-type NISA
jstock holdings import-csv holdings.csv
jstock watchlist add 7203 --priority HIGH
jstock analyze buy-candidates --notify
jstock analyze holdings --notify
jstock analyze watchlist --notify
jstock watchlist-screening run --dry-run
```

## アーキテクチャ概要

- **ローカルCLI** (`jstock ...`): ローカルJSONストアを使い、保有銘柄登録・売買記録・
  分析コマンドを単一プロセスで実行する(開発・小規模検証・手動運用向け)。
- **AWSデプロイ** (`infra/template.yaml`、AWS SAM): 同じドメインロジック・サービス層を
  Lambda上で実行し、DynamoDBを永続化層として使う。日次のBUY候補・保有銘柄分析・
  適時開示チェックは単一Lambdaの自己完結処理。**週次のウォッチリスト自動追加**
  (東証プライム+スタンダード全銘柄、約3,122件を対象)のみ、SQSベースの分散処理
  (Dispatcher → Worker(並列) → Terminal Failure Handler → Reconciler(毎時))で
  構成されている(詳細は[docs/functional_spec.md](docs/functional_spec.md)、
  運用手順は[docs/operations_manual.md](docs/operations_manual.md)参照)。
- ローカルJSONストア/DynamoDBの切り替えは`AWS_LAMBDA_FUNCTION_NAME`環境変数の
  有無で自動判定される(`infrastructure/collection_store.py`)。

## AWSリソース一覧

完全な一覧・IAMポリシー・環境変数は[infra/template.yaml](infra/template.yaml)が
唯一の正(このREADMEは概要のみ)。主要なもの:

| 種別 | 主なリソース | 用途 |
| --- | --- | --- |
| Lambda | `BuyCandidatesFunction` / `HoldingsWatchlistFunction` / `DisclosureCheckFunction` / `EvaluationFunction` / `WeeklyReviewFunction` 等 | 日次・週次・月次の各分析バッチ(自己完結) |
| Lambda | `WatchlistDispatcherFunction` / `WatchlistWorkerFunction` / `WatchlistTerminalFailureHandlerFunction` / `WatchlistBatchReconcilerFunction` | 週次ウォッチリスト自動追加(SQS分散処理) |
| Lambda | `LineWebhookFunction` + `WebhookApi`(API Gateway) | LINEチャットからの売買記録登録 |
| SQS | `WatchlistScreeningQueue` / `WatchlistTerminalFailureQueue` (+DLQ) | ウォッチリスト候補の銘柄単位ディスパッチ |
| DynamoDB | `HoldingsTable` / `WatchlistTable` / `RecommendationsTable` 等、業務データ用の各テーブル | 保有・ウォッチリスト・推奨結果等の永続化 |
| DynamoDB | `BatchRunsTable` / `WatchlistCandidateProgressTable` | ウォッチリスト自動追加のバッチ状態・銘柄単位進捗 |
| DynamoDB | `WatchlistPriceCacheTable` / `WatchlistFinancialCacheTable` | ウォッチリスト専用の株価・財務データキャッシュ(運用ハードニング4節) |
| DynamoDB | `EdinetFilingCacheTable` / `EdinetDisclosureCacheTable` | EDINET適時開示データのキャッシュ |
| S3 | `CandidateUniverseCacheBucket` | JPX上場銘柄一覧・JPX400構成銘柄の検証済みキャッシュ(current/archive) |
| Secrets Manager | LINE関連3種・EDINET APIキー | `infra/template.yaml`のParameterに ARN を指定して連携 |

## データフロー

1. **日次(BUY候補・保有銘柄分析)**: yfinance等から`ProviderBundle`経由でスナップショット取得
   → `domain/`配下の判定ロジックで評価 → `RecommendationsTable`等へ記録 → 条件を満たせばLINE通知。
2. **週次(ウォッチリスト自動追加)**: Dispatcherが候補ユニバース(JPX全銘柄)を確定し
   `WatchlistCandidateProgressTable`へ銘柄単位の行を作成、SQSへ投入 → Workerが並列に
   1銘柄ずつ評価・完了記録 → 全件完了で自動finalize(ウォッチリストへの追加・LINE通知)
   → Reconcilerが毎時、滞留・失敗バッチを検知して復旧を試みる。
3. **適時開示チェック・振り返り(週次/月次/四半期)**: 別経路で`AuditLogTable`
   等の記録を集計・報告する。

詳細なシーケンスは[docs/functional_spec.md](docs/functional_spec.md)を参照。

## スケジュール

主要な定期実行(JST、`infra/template.yaml`の`ScheduleV2`が正):

| 時刻 | 対象 |
| --- | --- |
| 平日08:00 | BUY候補分析・保有銘柄分析 |
| 平日10:00/12:30/15:30 | 適時開示チェック |
| 平日18:00 | 評価記録(evaluation) |
| 毎週土曜07:00 | ウォッチリスト自動追加(Dispatcher起動) |
| 毎時 | ウォッチリスト自動追加のReconciler(滞留・失敗の自動復旧) |
| 毎週土曜09:00/10:00/11:00 | 週次/月次/四半期レビュー |

完全な一覧は[docs/operations_manual.md](docs/operations_manual.md)の該当表を参照。

## デプロイ手順

```bash
cd infra
sam build
sam deploy --guided   # 初回のみ。以降は sam deploy
```

事前にSecrets Manager側でLINE/EDINET関連のシークレットを作成し、そのARNを
`sam deploy`のパラメータへ渡す必要がある(`infra/samconfig.toml.example`参照、
`infra/samconfig.toml`自体はコミットしない)。全件処理(`candidate_limit: null`)を
有効化する場合のみ`AllowFullMarketScreening=true`を追加で指定する(下記「段階導入手順」参照)。

## テスト実行方法

```bash
pip install -e .[dev]
pytest tests -q
ruff check src tests
mypy src
```

CI(`.github/workflows/ci.yml`)はこの3つに加え、`requirements-lock.txt`
(検証済み固定バージョン一式)からのインストール、gitleaksによる秘密情報スキャン、
pip-auditによる依存脆弱性スキャンを実行する。

## 外部データソース一覧

いずれも**無保証・非公式または公開データ**であり、仕様変更・障害・遅延がありうる。

| ソース | 用途 | 備考 |
| --- | --- | --- |
| yfinance(非公式) | 株価・財務指標・配当情報 | Yahoo Financeの非公式ラッパー。429/403/5xx等はウォッチリスト自動追加側で再試行・障害分類する(運用ハードニング3節) |
| JPX(日本取引所グループ)公開`data_j.xls` | 東証上場銘柄一覧(プライム+スタンダード) | 週次でダウンロード・検証してS3へキャッシュ |
| 日経(Nikkei)公開CSV | JPX400構成銘柄(スコアリングには使わずフラグのみ) | 同上 |
| EDINET | 適時開示・臨時報告書 | 金融庁の公開API |

## 段階導入手順(ウォッチリスト自動追加)

現在の既定値は`config/watchlist_screening_rules.yaml`の`staged_rollout`で
`candidate_limit: 100`・`market_segment_filter: ["プライム（内国株式）"]`
(安全側デフォルト)。段階的に対象を拡大する場合:

1. `candidate_limit`を100→300→500…と引き上げ、各回で本README末尾の
   「100/300/500/全件へロールアウトを拡大する判定基準」を確認する。
2. 全件処理(`candidate_limit: null`)へ移行する場合は、YAMLの変更に加えて
   `ALLOW_FULL_MARKET_SCREENING=true`をデプロイ時に明示的に指定する(二重の安全策、
   運用ハードニング2節)。片方だけでは起動しない。
3. `jstock watchlist-screening batch-status <batch_id>`で直近バッチの
   `staged_rollout_candidate_limit`等を確認し、意図どおりの設定が適用されたことを
   確認する。

## 障害対応・バッチ再実行方法

ウォッチリスト自動追加のバッチが異常終了・停滞した場合は、まずReconciler
(毎時実行)による自動復旧を待つ。1時間以上復旧しない場合は以下のCLIで手動対応する
(いずれも本番DynamoDBへ接続するため`AWS_LAMBDA_FUNCTION_NAME`環境変数トリックが
必要。詳細手順は[docs/operations_manual.md](docs/operations_manual.md)参照)。

```bash
jstock watchlist-screening batch-status <batch_id>       # 状態確認(読み取り専用)
jstock watchlist-screening list-incomplete <batch_id>    # 未完了銘柄の一覧(読み取り専用)
jstock watchlist-screening retry-finalize <batch_id> --execute   # finalize再試行
jstock watchlist-screening retry-stock <batch_id> <stock_code> --execute  # 銘柄単体の再評価
jstock watchlist-screening abort <batch_id> --reason "..." --execute     # 手動中断
```

`--execute`を付けない場合はすべてdry-run(現在の状態表示のみ)。CloudWatch Logsで
`watchlist dispatcher` / `watchlist worker` / `watchlist reconciler`のログを
検索することで、各バッチの進行状況・障害分類(データ提供元障害の疑い/主要項目欠損率)
を確認できる。

## コスト確認方法

AWS Cost Explorerで、このスタックがデプロイされたリソースへ付与される
タグ(CloudFormationスタック名)によりフィルタして確認する。主要なコスト要因は
DynamoDB(PAY_PER_REQUEST、ウォッチリスト自動追加週次実行時にWorker Lambdaの
実行時間・DynamoDB読み書きが最も大きい)とLambda実行時間。SQS・S3・Secrets
Managerのコストは小さい。

## 既知の制約・運用リスク

[docs/operations_manual.md](docs/operations_manual.md)の「既知の制約事項」を参照。

### 100/300/500/全件へロールアウトを拡大する判定基準

直近の`candidate_limit`でのバッチ実行結果を`batch-status`で確認し、以下をすべて
満たす場合に次の段階へ進める:

- `execution_result`が`NORMAL`であること(`HIGH_THROTTLE_RATE`・
  `SCORING_DATA_QUALITY_DEGRADED`・`REQUIRED_DATA_QUALITY_DEGRADED`・
  `EXCESSIVE_DATA_ERRORS`・`EXCESSIVE_NOT_FOUND`・`EXCESSIVE_TERMINAL_FAILURES`・
  `OPERATOR_ABORTED`のいずれでもない)。
- Reconcilerによる`FINALIZE_FAILED`の自動復旧が発生していない、または発生していても
  再試行で正常完了していること。
- CloudWatch Logsでyfinance側の429/403/5xx等の障害疑いログが目立って増えていないこと。
- 実行時間がLambda Timeoutに対して十分な余裕を持って完了していること
  (Worker/Dispatcherのタイムアウト設計値は`docs/functional_spec.md`参照)。

いずれかを満たさない場合は、原因を特定し設定(キャッシュTTL・並列実行数・
段階導入のcandidate_limit)を調整したうえで同じ段階を再実行し、安定を確認してから
次の段階へ進める。
