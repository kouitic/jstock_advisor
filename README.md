# jstock_advisor
ClaudeCodeで作った日本株売買の補助アプリ

## セットアップ

```bash
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
```

## LINE通知の設定(任意)

`--notify` オプションで実際にLINEへ通知するには、`.env.example` をコピーして
`.env` を作成し、LINE Messaging APIのチャネルアクセストークンとuserIdを設定してください。

```powershell
Copy-Item .env.example .env
# .env を編集してLINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID を設定
```

`.env` が無い、または値が未設定の場合は標準出力へのドライラン表示のみになります
(実際の送信は行われません)。`.env` は `.gitignore` で追跡対象外です。

## CLIコマンド例

```bash
jstock holdings add 8136 --shares 100 --price 3775 --account-type NISA
jstock holdings import-csv holdings.csv
jstock watchlist add 7203 --priority HIGH
jstock analyze buy-candidates --notify
jstock analyze holdings --notify
jstock analyze watchlist --notify
```
