# CLAUDE.md

- 判定ロジック・通知内容・データ管理機能など、システムの仕様に変わる変更を行った場合は、
  必ず [docs/functional_spec.md](docs/functional_spec.md)(非技術者向けの機能仕様書)を
  合わせて更新し、末尾の変更履歴に日付と概要を追記すること。

- **GitHub Issueを作成・調査・更新・closeする場合は、
  [docs/issue_label_policy.md](docs/issue_label_policy.md) を必ず読み、
  そのルールに従うこと。** labelはIssue Type / Priority / Severity /
  Release Blockerの4軸を独立して判定し、相互に自動推論しない。
  Issue本文・最新コメント・labelsが矛盾する場合は、勝手に推測して実装を進めず、
  どれが最新の確定判断かを確認すること。
  (同文書はAI非依存のリポジトリ運用ポリシーであり、本ファイルはその入口に過ぎない。
  ルールを変更する場合は同文書を更新する。)

- **メソッド名だけを根拠にread-onlyと判断してはならない。**
  `get` / `list` / `find` / `read` / `check` / `health` 等の名称は副作用の有無を
  保証しない。Productionのread-only観測・health check・validation・verification・
  IAM least-privilege設計を行う場合は、**呼び出し先を含むcall graphを確認**し、
  repositoryのsave/update/delete、DynamoDB/S3 write、queue publish、Lambda invoke、
  LINE送信等の外部状態変更が無いことを確かめること。read-onlyと定義した処理に
  hidden writeを持たせない。writeを伴う場合は、API契約・名称・IAM・テストから
  その事実が判別できなければならない。
  (背景と具体的な確認手順は
  [docs/operations_manual.md](docs/operations_manual.md) 18節。
  2026-09-02に、read名のAPIが内部で書き込みを行い、読み取り専用IAMのLambdaで
  AccessDeniedとなって日次バッチ全体が停止するProduction障害が発生している。)

- **実在人物の個人情報をGit管理対象へ含めない。** 氏名・家族名・個人メール
  アドレス・住所・電話番号等を、ソースコード、テストデータ、fixture、コメント、
  ドキュメント、サンプル、コミットメッセージへ記録してはならない。所有者等を
  例示する場合は「所有者A」「owner-a」等の架空値を使用する。本番データの値
  (実在の氏名、実際の保有数量・取得単価等)をテスト・ドキュメントへ転記しない。
  一回限りの移行スクリプト等が実データを必要とする場合は、実データをGit管理
  対象外のローカルファイル(`.gitignore`で除外)から実行時に読み込む設計とし、
  ソースコードには実データを埋め込まないこと。CIのPIIスキャン
  (`scripts/scan_for_pii.py`、`.github/workflows/ci.yml`の`pii-scan`ジョブ)が
  既知の実在人物名を検知した場合はビルドを失敗させる(denylist方式であり、
  全てのPIIを検出できる保証はない。上記ルールの遵守が前提)。
