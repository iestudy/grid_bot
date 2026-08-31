"""
状態管理レイヤー。

役割:
- 発注の冪等制御: bitbank API自体はクライアント指定の冪等キーをサポートしないため、
  「発注試行中(PENDING)」の状態をここで保持し、API応答が不明なまま再送しないようにする
- WebSocket再接続時のリコンシリエーション用に、自システムが認識している注文状態を保持

2つの実装を用意:
- InMemoryStateStore: ローカル開発・テスト・Paper Trading用
- DynamoDBStateStore: 本番用（boto3、オンデマンドモード前提のテーブル設計）
"""

import uuid
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class OrderState(Enum):
    PENDING = "PENDING"    # API送信済み、応答待ち・応答不明
    OPEN = "OPEN"          # 板に乗っている（未約定）
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"


@dataclass
class OrderRecord:
    request_id: str          # 自システム生成の冪等キー（bitbank注文IDとは別物）
    pair: str
    side: str
    price: float
    amount: float
    state: OrderState
    exchange_order_id: Optional[str] = None
    created_at: str = ""     # ISO8601形式。GSI(state, created_at)のソートキー用


@dataclass
class PortfolioState:
    """
    バックテストのcompute_pnlと数学的に同一の会計方式で運用中のポジションを追跡する。
      cash_flow: 買いで支払った額をマイナス、売りで受け取った額をプラスとして積算
      net_inventory: 買い数量合計 - 売り数量合計（プラスなら在庫超過、マイナスなら在庫不足）
      realized_profit_jpy: PositionLedgerのFIFOマッチングで確定した往復益の累積
      total_fill_count: 累積約定件数(往復ではなく個々の約定単位でのカウント)

    この2つの値(cash_flow, net_inventory)から、任意の時点の含み損益は
      unrealized_pnl = cash_flow + net_inventory * current_price
    で計算できる（グリッドのメイカーリベートは別途加算）。
    """
    cash_flow: float = 0.0
    net_inventory: float = 0.0
    realized_profit_jpy: float = 0.0
    total_fill_count: int = 0


@dataclass
class DailySnapshot:
    """日次サマリー通知用の前回スナップショット。"""
    realized_profit_jpy: float = 0.0
    total_fill_count: int = 0
    timestamp: float = 0.0


class StateStore(ABC):
    @abstractmethod
    def save_order(self, record: OrderRecord) -> None: ...

    @abstractmethod
    def get_order(self, request_id: str) -> Optional[OrderRecord]: ...

    @abstractmethod
    def list_open_orders(self) -> Dict[str, OrderRecord]: ...

    @abstractmethod
    def update_state(self, request_id: str, state: OrderState, exchange_order_id: Optional[str] = None) -> None: ...

    @abstractmethod
    def get_portfolio_state(self) -> PortfolioState: ...

    @abstractmethod
    def save_portfolio_state(self, state: PortfolioState) -> None: ...

    @abstractmethod
    def get_position_ledger_data(self) -> Optional[dict]: ...

    @abstractmethod
    def save_position_ledger_data(self, data: dict) -> None: ...

    @abstractmethod
    def get_daily_snapshot(self) -> DailySnapshot: ...

    @abstractmethod
    def save_daily_snapshot(self, snapshot: DailySnapshot) -> None: ...

    @abstractmethod
    def get_base_price(self) -> Optional[float]: ...

    @abstractmethod
    def save_base_price(self, base_price: float) -> None: ...

    def new_request_id(self) -> str:
        return str(uuid.uuid4())


class InMemoryStateStore(StateStore):
    def __init__(self):
        self._orders: Dict[str, OrderRecord] = {}
        self._portfolio_state = PortfolioState()
        self._ledger_data: Optional[dict] = None
        self._daily_snapshot = DailySnapshot()
        self._base_price: Optional[float] = None

    def save_order(self, record: OrderRecord) -> None:
        self._orders[record.request_id] = record

    def get_order(self, request_id: str) -> Optional[OrderRecord]:
        return self._orders.get(request_id)

    def list_open_orders(self) -> Dict[str, OrderRecord]:
        return {
            rid: rec for rid, rec in self._orders.items()
            if rec.state in (OrderState.PENDING, OrderState.OPEN)
        }

    def update_state(self, request_id: str, state: OrderState, exchange_order_id: Optional[str] = None) -> None:
        rec = self._orders.get(request_id)
        if rec is None:
            raise KeyError(f"unknown request_id: {request_id}")
        rec.state = state
        if exchange_order_id is not None:
            rec.exchange_order_id = exchange_order_id

    def get_portfolio_state(self) -> PortfolioState:
        return self._portfolio_state

    def save_portfolio_state(self, state: PortfolioState) -> None:
        self._portfolio_state = state

    def get_position_ledger_data(self) -> Optional[dict]:
        return self._ledger_data

    def save_position_ledger_data(self, data: dict) -> None:
        self._ledger_data = data

    def get_daily_snapshot(self) -> DailySnapshot:
        return self._daily_snapshot

    def save_daily_snapshot(self, snapshot: DailySnapshot) -> None:
        self._daily_snapshot = snapshot

    def get_base_price(self) -> Optional[float]:
        return self._base_price

    def save_base_price(self, base_price: float) -> None:
        self._base_price = base_price


class DynamoDBStateStore(StateStore):
    """
    テーブル設計（infra/create_dynamodb_table.py で作成）:
      table: grid_bot_orders
      partition key: request_id (S)
      billing_mode: PAY_PER_REQUEST（オンデマンド、放置コスト回避のため必須）
    """

    def __init__(self, table_name: str = "grid_bot_orders", region_name: str = "ap-northeast-1"):
        import boto3  # 遅延importでboto3未インストール環境でもテスト可能にする
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def save_order(self, record: OrderRecord) -> None:
        created_at = record.created_at or datetime.now(timezone.utc).isoformat()
        self._table.put_item(Item={
            "request_id": record.request_id,
            "pair": record.pair,
            "side": record.side,
            "price": str(record.price),
            "amount": str(record.amount),
            "state": record.state.value,
            "exchange_order_id": record.exchange_order_id or "",
            "created_at": created_at,
        })

    def get_order(self, request_id: str) -> Optional[OrderRecord]:
        resp = self._table.get_item(Key={"request_id": request_id})
        item = resp.get("Item")
        if item is None:
            return None
        return OrderRecord(
            request_id=item["request_id"],
            pair=item["pair"],
            side=item["side"],
            price=float(item["price"]),
            amount=float(item["amount"]),
            state=OrderState(item["state"]),
            exchange_order_id=item.get("exchange_order_id") or None,
            created_at=item.get("created_at", ""),
        )

    # 通常の注文レコード以外に、このテーブルへ同居させている特殊レコードのキー。
    # list_open_ordersのscanから必ず除外すること(stateキーを持たないため)。
    _SENTINEL_KEYS = {"__PORTFOLIO_STATE__", "__POSITION_LEDGER__", "__DAILY_SNAPSHOT__", "__BASE_PRICE__"}

    def list_open_orders(self) -> Dict[str, OrderRecord]:
        # GSI(state, created_at)をqueryし、OPEN/PENDINGの各stateごとに
        # ページネーション(LastEvaluatedKey)を追いながら全件取得する。
        # scan()は8万件超のFAILEDレコードまで毎回1MB分読みに行ってしまい、
        # レスポンスサイズ上限で打ち切られ一部のOPENレコードを取りこぼす
        # 不具合があったため、GSI経由のqueryに置き換えた。
        from boto3.dynamodb.conditions import Key  # 遅延import(トップレベルimport boto3を避ける設計に合わせる)
        result: Dict[str, OrderRecord] = {}
        for target_state in (OrderState.PENDING, OrderState.OPEN):
            last_evaluated_key = None
            while True:
                kwargs = {
                    "IndexName": "state-created_at-index",
                    "KeyConditionExpression": Key("state").eq(target_state.value),
                }
                if last_evaluated_key:
                    kwargs["ExclusiveStartKey"] = last_evaluated_key
                resp = self._table.query(**kwargs)
                for item in resp.get("Items", []):
                    if item.get("request_id") in self._SENTINEL_KEYS:
                        continue
                    result[item["request_id"]] = OrderRecord(
                        request_id=item["request_id"],
                        pair=item["pair"],
                        side=item["side"],
                        price=float(item["price"]),
                        amount=float(item["amount"]),
                        state=OrderState(item["state"]),
                        exchange_order_id=item.get("exchange_order_id") or None,
                        created_at=item.get("created_at", ""),
                    )
                last_evaluated_key = resp.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break
        return result

    def update_state(self, request_id: str, state: OrderState, exchange_order_id: Optional[str] = None) -> None:
        update_expr = "SET #s = :s"
        expr_names = {"#s": "state"}
        expr_values = {":s": state.value}
        if exchange_order_id is not None:
            update_expr += ", exchange_order_id = :eid"
            expr_values[":eid"] = exchange_order_id
        self._table.update_item(
            Key={"request_id": request_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )

    def get_portfolio_state(self) -> PortfolioState:
        resp = self._table.get_item(Key={"request_id": "__PORTFOLIO_STATE__"})
        item = resp.get("Item")
        if item is None:
            return PortfolioState()
        return PortfolioState(
            cash_flow=float(item["cash_flow"]),
            net_inventory=float(item["net_inventory"]),
            realized_profit_jpy=float(item.get("realized_profit_jpy", "0")),
            total_fill_count=int(item.get("total_fill_count", "0")),
        )

    def save_portfolio_state(self, state: PortfolioState) -> None:
        # ordersテーブルと同じテーブルに固定キー"__PORTFOLIO_STATE__"で1レコードとして保存する。
        # 頻繁に更新されるため、別テーブルに切り出す場合は将来的に見直すこと。
        self._table.put_item(Item={
            "request_id": "__PORTFOLIO_STATE__",
            "cash_flow": str(state.cash_flow),
            "net_inventory": str(state.net_inventory),
            "realized_profit_jpy": str(state.realized_profit_jpy),
            "total_fill_count": str(state.total_fill_count),
        })

    def get_position_ledger_data(self) -> Optional[dict]:
        resp = self._table.get_item(Key={"request_id": "__POSITION_LEDGER__"})
        item = resp.get("Item")
        if item is None:
            return None
        import json
        return json.loads(item["ledger_json"])

    def save_position_ledger_data(self, data: dict) -> None:
        import json
        self._table.put_item(Item={
            "request_id": "__POSITION_LEDGER__",
            "ledger_json": json.dumps(data),
        })

    def get_daily_snapshot(self) -> DailySnapshot:
        resp = self._table.get_item(Key={"request_id": "__DAILY_SNAPSHOT__"})
        item = resp.get("Item")
        if item is None:
            return DailySnapshot()
        return DailySnapshot(
            realized_profit_jpy=float(item["realized_profit_jpy"]),
            total_fill_count=int(item["total_fill_count"]),
            timestamp=float(item["timestamp"]),
        )

    def save_daily_snapshot(self, snapshot: DailySnapshot) -> None:
        self._table.put_item(Item={
            "request_id": "__DAILY_SNAPSHOT__",
            "realized_profit_jpy": str(snapshot.realized_profit_jpy),
            "total_fill_count": str(snapshot.total_fill_count),
            "timestamp": str(snapshot.timestamp),
        })

    def get_base_price(self) -> Optional[float]:
        resp = self._table.get_item(Key={"request_id": "__BASE_PRICE__"})
        item = resp.get("Item")
        if item is None:
            return None
        return float(item["base_price"])

    def save_base_price(self, base_price: float) -> None:
        self._table.put_item(Item={
            "request_id": "__BASE_PRICE__",
            "base_price": str(base_price),
        })
