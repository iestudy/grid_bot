# grid_bot — bitbank XRP/JPY グリッド取引ボット

これまでの設計議論に基づく実装。**Phase 1（HardStopLossManager）を最優先で完成させ、
単体テストが全て通ってから次のPhaseに進むこと。** これが唯一、いかなる自動パラメータ
変更（Tier1/Tier2）の対象からも外れる「聖域」です。

## ディレクトリ構成

```
grid_bot/
├── src/
│   ├── hard_stop_loss.py   # Phase 1: 最終防衛線（API非依存、純粋ロジック）
│   ├── bitbank_client.py   # Phase 2: bitbank REST API クライアント（署名・冪等制御）
│   ├── grid_engine.py      # Phase 2: グリッド生成 + base_price自動ドリフト補正
│   ├── state_store.py      # Phase 2: 状態管理（InMemory / DynamoDB 切替可能）
│   ├── paper_trading.py    # Phase 3: Paper Trading シミュレータ（保守的な約定判定）
│   └── config.py           # envelope・閾値などの設定一元管理
├── tests/
│   └── test_hard_stop_loss.py
├── infra/
│   └── create_dynamodb_table.py   # DynamoDBテーブル作成スクリプト（オンデマンド）
├── .github/workflows/
│   └── weekly_backtest.yml        # 週次バックテストジョブの雛形（Tier1パイプラインの土台）
├── requirements.txt
└── .env.example
```

## 構築手順

### Step 0. 環境準備
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # bitbank APIキー等は絶対にgit管理下に置かない
```

### Step 1. HardStopLossManagerの単体テスト（最優先・ここが通るまで先に進まない）
```bash
pytest tests/test_hard_stop_loss.py -v
```
全テストが通ることを確認。ここが資金防御の生命線なので、追加のエッジケースを
思いついたら必ずテストを足してから実装を変更すること。

### Step 2. bitbank API疎通確認（Public APIのみ、資金は動かさない）
```bash
python -c "from src.bitbank_client import BitbankClient; c = BitbankClient(); print(c.get_ticker('xrp_jpy'))"
```

### Step 3. Paper Tradingでグリッドロジックを検証
```bash
python -m src.paper_trading --pair xrp_jpy --days 7
```
過去の値動き（別途CSV等で用意）に対して、保守的な約定判定（指値を板が
「突き抜けた」場合のみ約定）でシミュレーションする。ここで機能しないロジックは
本番でも機能しない。

### Step 4. DynamoDBテーブル作成（AWS認証情報を設定してから）
```bash
python infra/create_dynamodb_table.py
```
オンデマンドモード（PAY_PER_REQUEST）で作成される。プロビジョニングモードに
しないこと（放置コストが発生する）。

### Step 5. 少額実資金でのテスト稼働
最小ロットで、HardStopLossManagerとgrid_engineが正しく連携するか確認する。
この段階ではEC2に載せず、ローカルまたは開発用インスタンスで動かして様子を見る。

### Step 6以降（本README範囲外・今後の実装）
- EC2（オンデマンド、t4g.nano）への本番デプロイ
- WebSocket常時接続 + REST APIでのリコンシリエーション
- GitHub Actionsでの週次バックテスト自動化・Tier1パイプライン
- Claude APIによるレジーム分類の統合

## 絶対に守ること
1. HardStopLossManagerの閾値は、いかなる自動化パイプライン（Tier1/Tier2）からも変更対象外にする
2. APIキー・シークレットは`.env`のみに置き、`.gitignore`で除外する（本リポジトリ雛形には未設定なので必ず追加すること）
3. 本番bot用インスタンスにスポットインスタンスは使わない（中断リスクがあるため、可用性目的に反する）
