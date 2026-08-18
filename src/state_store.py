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


class StateStore(ABC):
    @abstractmethod
    def save_order(self, record: OrderRecord) -> None: ...

    @abstractmethod
    def get_order(self, request_id: str) -> Optional[OrderRecord]: ...

    @abstractmethod
    def list_open_orders(self) -> Dict[str, OrderRecord]: ...

    @abstractmethod
    def update_state(self, request_id: str, state: OrderState, exchange_order_id: Optional[str] = None) -> None: ...

    def new_request_id(self) -> str:
        return str(uuid.uuid4())


class InMemoryStateStore(StateStore):
    def __init__(self):
        self._orders: Dict[str, OrderRecord] = {}

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
        self._table.put_item(Item={
            "request_id": record.request_id,
            "pair": record.pair,
            "side": record.side,
            "price": str(record.price),
            "amount": str(record.amount),
            "state": record.state.value,
            "exchange_order_id": record.exchange_order_id or "",
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
        )

    def list_open_orders(self) -> Dict[str, OrderRecord]:
        # 件数が少ない前提（3万円運用のgrid本数は数本〜十数本）でscanを許容。
        # 件数が増える場合はGSI（state, updated_at等）を追加してqueryに切り替えること。
        resp = self._table.scan()
        result = {}
        for item in resp.get("Items", []):
            state = OrderState(item["state"])
            if state in (OrderState.PENDING, OrderState.OPEN):
                result[item["request_id"]] = OrderRecord(
                    request_id=item["request_id"],
                    pair=item["pair"],
                    side=item["side"],
                    price=float(item["price"]),
                    amount=float(item["amount"]),
                    state=state,
                    exchange_order_id=item.get("exchange_order_id") or None,
                )
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
