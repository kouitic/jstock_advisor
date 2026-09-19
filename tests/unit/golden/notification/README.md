# 通知文面の golden テスト(Issue #255)

`tests/unit/test_notification_message_golden.py` が、5 種類の LINE 本文をこのディレクトリのスナップショット
(`*.txt`)と突き合わせる。

| スナップショット | 内容 |
|---|---|
| `stock_analysis_buy.txt` | 銘柄分析の通知(BUY 判定の詳細) |
| `stock_analysis_holding_profit_taking_status.txt` | 銘柄分析の通知(保有銘柄。利確判定の状況。#222 N-5) |
| `stock_analysis_holding_hold_facts.txt` | 銘柄分析の通知(保有継続。投資前提悪化ルールの状況。#222 N-3) |
| `profit_taking.txt` | 利確判定の通知(実送信の本文。全部売却の検討) |
| `sell.txt` | 売却判断の通知(実送信の本文) |
| `watchlist_addition.txt` | 監視銘柄の追加通知(追加あり) |
| `universe_fetch_failure_day.txt` | 候補一覧の取得に失敗した日の通知(#234) |

## このテストの意味

**「変えてはいけない」ためのものではなく、「変えたことに気づく」ためのもの**である。判定ロジック・整形部品・
設定値の変更が、利用者に届く文面を意図せず変えていないかを、CI で検出する。

## テストが落ちたとき

失敗メッセージに、どの行が変わったかを示す unified diff が出る。

1. diff を読む。**意図した変更か、意図しない波及か**を判断する
2. 意図しない波及なら、コードを直す(スナップショットは触らない)
3. 意図した変更なら、スナップショットを更新して、**同じ PR に含める**

## スナップショットの更新手順(意図した変更のとき)

```bash
UPDATE_GOLDEN=1 python -m pytest tests/unit/test_notification_message_golden.py
git diff tests/unit/golden/notification/
```

- `UPDATE_GOLDEN=1` を付けて実行すると、現在の出力でスナップショットが書き換わる(テストは通る)
- **書き換わった差分を必ず読み**、意図した変更だけであることを確認してから commit する。中身を見ずに更新しない
- PR の本文へ、どの文面がどう変わったか(利用者への影響)を書く

## fixture の規則(変更するとき)

- **架空値のみ**。実在の銘柄コード・企業名・所有者名・保有数量・取得単価・含み益の実額を使わない
  (銘柄コードは実在しない `0000` 系、企業名は `架空銘柄A` のように書く。所有者は `所有者A`)
- **日付・時刻は固定する**(実行日時・実行 ID など、実行ごとに変わる値を含めない)。毎回落ちる golden テストは、
  いずれ中身を見ずに更新されるため
- スナップショットは LF・UTF-8。比較は、改行コード(CRLF / LF)と末尾の改行の差を無視する
  (Windows の `autocrlf` で差が出ないようにするため)
