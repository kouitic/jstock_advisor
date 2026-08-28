# AWSデプロイ手順(SAM)

## 事前準備

1. AWSアカウント・IAMユーザー(デプロイ権限を持つもの)を用意し、`aws configure`で認証情報とリージョン(ap-northeast-1推奨)を設定する。
2. [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)をインストールする。
3. Dockerを起動しておく(`sam build --use-container`で使用)。
4. 以下4つのSecrets Managerシークレットを作成する(値は各自のLINE Developersコンソール・EDINET登録情報から取得したものを設定する。**このリポジトリ・チャット上には値を書かないこと**)。

```bash
aws secretsmanager create-secret --name jstock/edinet-api-key
aws secretsmanager create-secret --name jstock/line-channel-access-token
aws secretsmanager create-secret --name jstock/line-user-id
aws secretsmanager create-secret --name jstock/line-channel-secret
```

作成後、それぞれに実際の値を投入する(対話的に入力するか、ローカルの安全な方法で値を用意してから実行する):

```bash
aws secretsmanager put-secret-value --secret-id jstock/edinet-api-key --secret-string "<値>"
aws secretsmanager put-secret-value --secret-id jstock/line-channel-access-token --secret-string "<値>"
aws secretsmanager put-secret-value --secret-id jstock/line-user-id --secret-string "<値>"
aws secretsmanager put-secret-value --secret-id jstock/line-channel-secret --secret-string "<値>"
```

作成したシークレットのARNを控えておく(`aws secretsmanager describe-secret --secret-id jstock/edinet-api-key`等で確認可能)。

## 依存Layerのlock管理(Layer dependency lock、Issue #35)

DependenciesLayerの依存は2ファイル方式で管理する(再現可能ビルドのため。
範囲指定のみだったころは、`sam build`実行日時によって推移的依存の解決結果が
変わり、無関係なデプロイでLayerVersionローテーションが発生していた)。

| ファイル | 役割 |
|---|---|
| `layer/requirements.in` | **人間が編集する**direct依存の正本(範囲指定) |
| `layer/requirements.txt` | **自動生成物**。全direct/推移的依存の完全pin。SAM PythonPipBuilderが実際にインストールするmanifest |
| `layer/build-requirements.txt` | compileに使うuvのバージョン固定(ローカル・CIで共通) |

**`layer/requirements.txt`を直接編集しないこと。**依存を変更する場合は必ず
`layer/requirements.in`を編集し、以下の標準コマンドで再生成する。

```bash
pip install -r infra/layer/build-requirements.txt
```

```bash
python -m uv pip compile infra/layer/requirements.in -o infra/layer/requirements.txt --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --no-annotate --custom-compile-command "python -m uv pip compile infra/layer/requirements.in -o infra/layer/requirements.txt --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --no-annotate"
```

- 標準compile条件はPython 3.12・Lambda実行環境(Linux/x86_64)。リポジトリルートから
  実行すること(生成ヘッダを全環境で一致させるため、コマンドは上記をそのまま使う)
- uvは既存の`requirements.txt`にあるpinを範囲内で優先するため、`.in`を変更していない
  再実行では勝手に最新版へ上がらない。**意図的に依存を更新する場合のみ**
  `--upgrade`(全体)または`--upgrade-package <名前>`(個別)を付けて再生成し、
  差分をレビュー可能なcommit/PRとして提出する
- CIの`layer-lock-drift`ジョブが「`.in`から再生成した結果と`requirements.txt`の
  一致」を検証する(再生成忘れはCI FAIL)。`dependency-audit`ジョブは
  `requirements.txt`をpip-audit監査する
- rollback時は過去コミットをcheckoutして`sam build`すれば、pin済み集合により
  当時と同一のLayerが再現される(PyPI上でyank/削除されていない限り)
- 運用フロー全体は`docs/operations_manual.md`14節を参照

## ビルド・デプロイ

```bash
cd infra
sam build
sam deploy --guided
```

`--guided`実行時に、上記4つのARNをパラメータとして入力する(2回目以降は`samconfig.toml`に保存されるため`sam deploy`のみでよい)。

## デプロイ後

1. 出力される`WebhookUrl`を確認する。
2. LINE Developersコンソールの対象チャネル → Messaging API設定 → Webhook URLに`<WebhookUrl>/webhook`を設定し、Webhookの利用を「オン」にする。
3. 「検証」ボタンで疎通確認する(4つのシークレットに値が入っていないと署名検証・送信に失敗する)。
4. CloudWatch Logsで各Lambda関数のログを確認する(ロググループ名は`/aws/lambda/<StackName>-<関数名>`)。

## シークレット値を後から変更する場合

`put-secret-value`で更新するだけでは、既にデプロイ済みのLambda環境変数には反映されない
(CloudFormationのdynamic referenceはデプロイ時点で値を解決するため)。再反映するには
`sam deploy`を再実行するか、対象Lambda関数に対して以下のように空更新をかける:

```bash
aws lambda update-function-configuration --function-name <関数名> --environment "Variables={}" 
# その後 sam deploy を再実行して正しい環境変数に戻す
```

## ローカルでの動作確認(デプロイ前)

```bash
sam build
sam local invoke BuyCandidatesFunction --event events/buy_candidates_event.json
```

EventBridge Scheduler起動のLambdaは`event`の中身を参照しないため、空の`{}`を渡せばよい。
