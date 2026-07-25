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
