# EMERGENCY_STOP インシデント対応ランブック

このドキュメントは、grid_botがEMERGENCY_STOPで停止した際の自動復旧判断基準を
定めたものである。Claude Codeはこのランブックと`scripts/incident_check.py`の
出力(JSON)をもとに、人手を介さず復旧作業を行う。

## 前提として理解しておくこと

- grid_botはXRP/JPYのグリッドトレーディングボット。EMERGENCY_STOPは
  HardStopLossManagerの安全装置であり、主に「base_priceからの価格乖離が
  max_price_deviation_jpy(8.0円)を超えた」場合に発動する。含み損の有無とは
  無関係に発動することがある(含み益の状態で発動するケースも多い)。
- EMERGENCY_STOP発動時、botは可能な範囲で保有ポジションを成行で強制決済し、
  残存する未約定注文を全てキャンセルしてから、exit code 0で正常終了する。
  そのため`systemctl status`では"inactive (dead)"、`Restart=on-failure`は
  作動しない(意図的な設計)。
- 過去の運用では、EMERGENCY_STOP自体は概ね安全に機能しており、多くの場合
  実残高と帳簿(PortfolioState)は強制決済後に整合する。ただし過去に一度、
  帳簿と実残高が大きく乖離するバグ(DynamoDBのscan()が1MB制限で一部の
  OPEN注文を取りこぼす不具合)が発生したことがあり、それ以来「実行前に
  必ず実残高を確認し、帳簿の数値を鵜呑みにしない」ことを徹底している。

## 対応フロー

### Step 0: 状況収集

    venv/bin/python3 scripts/incident_check.py

このJSON出力(balances, active_orders, bot_state, consistency, errors)を
判断材料とする。

### Step 1: エスカレーション条件のチェック(最優先)

以下のいずれかに該当する場合、自動復旧を行わず人間にエスカレーションする。
このステップを飛ばして次に進んではならない。

1. errorsが空でない
   incident_check.py自体が何らかの情報を取得できていない状態。
   (例: bitbank API認証エラー、DynamoDBアクセスエラー等)
   原因不明のまま自動判断を進めるのは危険なため、必ずエスカレーションする。

2. bot_state.net_inventoryが異常な値になっている

   重要: net_inventoryは「reset_state.py実行後からの累積売買差分
   (買い数量-売り数量)」であり、口座の実際のXRP保有量とは別の指標である。
   したがってconsistency.xrp_free_vs_net_inventory_diff(実際の自由XRP残高
   -net_inventory)が大きい正の値になること自体は、リセット後にグリッド外
   のXRPを保有している状態として正常であり、単独ではエスカレーション
   理由にならない。(実際、73.5XRP保有・net_inventory=0という状態は
   通常運用そのものであり、この差分33XRPだけを見てエスカレーションするのは
   誤判定である。)

   エスカレーションすべきなのは、以下のような「帳簿バグ再発」を示す
   異常パターンに該当する場合のみ:
   - net_inventoryが負の値になっている、かつその絶対値が
     amount_per_level_xrp(config.py参照)の2倍を超える
     (現物取引ではnet_inventoryが負になること自体、通常はbot起動後の
     累積売り越しでしか起こらず、想定を超える負の大きさは異常。
     過去に-48XRP・-94.5XRPのような値が発生したことがあり、これは
     reconcileの取りこぼしや二重計上が原因だった)
   - active_orders.countとbot側が把握している未約定注文数が一致しない
     ことが別途判明している場合
   - 上記いずれにも該当しないが、net_inventoryの値が直前の既知の状態
     (前回のreset_state.py実行時=0)から、実際の取引ログでは説明が
     つかないほど大きく動いている場合

3. 直近24時間以内のEMERGENCY_STOP発生回数が3回以上
   頻発している場合、パラメータやbase_price乖離ロジック自体に問題が
   ある可能性が高く、機械的な再起動を繰り返すべきではない。
   (発生回数はSlack通知履歴やCloudWatch Logs等、参照可能な記録から
   確認する。)

4. reset_state.py実行時にキャンセル失敗が1件でも残る
   Step 2の実行結果で判断する。未キャンセルの注文が残ったまま
   帳簿だけリセットすると、実態と乖離した状態で再開してしまう。

5. 想定外のエラー・予期しないレスポンス形式
   上記のいずれにも当てはまらないが、明らかに想定外の状況
   (例: bitbank側のメンテナンス、注文が異常な件数残っている等)。

いずれかに該当する場合は、Step 2以降を実行せず、状況(incident_check.pyの
出力全文、判断理由)をSlackに通知して処理を終了する。

### Step 2: 状態のリセット

エスカレーション条件に該当しない場合、以下を実行する。

    venv/bin/python3 -m src.reset_state --pair xrp_jpy --use-dynamodb --yes

実行結果(特に「キャンセル結果: 成功=N件 失敗=M件」)を確認し、
M > 0 の場合はStep 1の条件4に該当するため、ここで処理を止めて
エスカレーションする。

### Step 3: グリッドサイズの再計算

    venv/bin/python3 -m src.resize_grid --pair xrp_jpy --apply

常に--applyまで実行する(閾値による分岐は設けない。resize_grid.py自体が
実残高のJPY予算70%・XRP保有量に基づいて計算するため、算出ロジックの
安全性はスクリプト側に委譲している)。

実行後、amount_per_level_xrpの変更内容(旧値→新値)を記録しておく。

### Step 4: git反映

Step 3でconfig.pyが変更されるため、コミット・push・PR作成・マージまで行う。

    git checkout -b chore/auto-resize-YYYYMMDD-HHMM
    git add src/config.py
    git commit -m "resize_grid.py自動適用(インシデント対応): amount_per_level_xrpをX->Yに調整"
    git push -u origin chore/auto-resize-YYYYMMDD-HHMM
    gh pr create --title "..." --body "..."
    gh pr merge --squash
    git checkout main
    git pull origin main

ブランチ名の日時は実行時刻を使う。

### Step 5: bot再起動

    sudo systemctl start grid_bot
    sudo systemctl status grid_bot

Active: active (running)になっていることを確認する。

### Step 6: 起動後の健全性確認

起動直後、以下のいずれかが起きていないか60秒程度ログを監視する。

    sudo journalctl -u grid_bot --since "1 minute ago"

- 新規発注が60001(残高不足)以外の理由で失敗し続けていないか
- 想定外のCRITICALログが出ていないか
- 起動直後に再度EMERGENCY_STOPが発生していないか(base_priceリセットが
  不十分だった可能性を示す)

再度EMERGENCY_STOPが発生した場合は、Step 1の条件3(頻発)に抵触する
可能性が高いため、それ以上の自動リトライはせずエスカレーションする。

## 報告フォーマット(Slack)

対応完了後、以下の内容をSlackに通知する。

    [インシデント自動対応] YYYY-MM-DD HH:MM
    トリガー: EMERGENCY_STOP (現在価格=X円, 含み損益=Y円)
    実施内容:
      - reset_state.py実行 (キャンセル成功=N件)
      - resize_grid.py適用: amount_per_level_xrp X -> Y
      - bot再起動: 成功
    実残高: JPY free=X円 / XRP free=Y枚
    判断根拠: (エスカレーション条件に該当しなかった理由を簡潔に)

エスカレーションした場合は以下の形式。

    [要人手対応] YYYY-MM-DD HH:MM
    トリガー: EMERGENCY_STOP (現在価格=X円, 含み損益=Y円)
    エスカレーション理由: (該当した条件を明記)
    incident_check.py出力:
    (JSON全文または要約)
    自動対応は行っていません。botは停止したままです。

## 許可されたコマンド(ホワイトリスト)

Claude Codeがこのインシデント対応で実行してよいコマンドは以下に限定する。
これ以外のコマンド(特にbitbank APIへの直接発注・キャンセル呼び出し)は
実行してはならない。

- venv/bin/python3 scripts/incident_check.py
- venv/bin/python3 -m src.reset_state --pair xrp_jpy --use-dynamodb --yes
- venv/bin/python3 -m src.resize_grid --pair xrp_jpy (dry-run確認用)
- venv/bin/python3 -m src.resize_grid --pair xrp_jpy --apply
- git コマンド全般(checkout, add, commit, push, pull)
- gh pr create, gh pr merge --squash
- sudo systemctl start grid_bot
- sudo systemctl status grid_bot
- sudo journalctl -u grid_bot --since "..."
- Slack通知の送信(既存のnotifierモジュール経由、Webhook URL直叩き不可)

## 変更履歴

- 2026-09-08: 初版作成。これまでの運用で確立した手動対応フロー
  (EMERGENCY_STOP検知 -> 実残高確認 -> reset_state.py -> resize_grid.py
  -> 再起動)を自動化するために文書化。
