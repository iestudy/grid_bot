"""
bitbank REST API クライアント。

認証方式は公式ドキュメント (https://github.com/bitbankinc/bitbank-api-docs) に基づく:
- ヘッダー: ACCESS-KEY, ACCESS-NONCE, ACCESS-SIGNATURE
- 署名対象文字列:
    GET:  nonce + path + query_string
    POST: nonce + json_body
- HMAC-SHA256でAPIシークレットを鍵として署名

重要な注意:
- ここに書いたエンドポイント名・レスポンス形式は実装時点の一般的な利用例に基づく。
  本番投入前に必ず公式APIドキュメントで最新仕様を確認すること。
- 冪等性はbitbank API自体には（クライアント側from-order-id指定のような機能は）
  保証されていないため、こちらのSTATE STORE側でPENDING状態を管理し、
  二重発注を自前で防止する設計にしている（state_store.pyを参照）。
"""

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

import requests


class BitbankAPIError(Exception):
    def __init__(self, message: str, code: int = None):
        super().__init__(message)
        self.code = code


class BitbankClient:
    PUBLIC_BASE = "https://public.bitbank.cc"
    PRIVATE_BASE = "https://api.bitbank.cc/v1"

    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None, timeout: float = 10.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self._session = requests.Session()

    # ---------- Public API（認証不要） ----------

    def get_ticker(self, pair: str) -> Dict[str, Any]:
        url = f"{self.PUBLIC_BASE}/{pair}/ticker"
        r = self._session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_depth(self, pair: str) -> Dict[str, Any]:
        url = f"{self.PUBLIC_BASE}/{pair}/depth"
        r = self._session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_transactions(self, pair: str, yyyymmdd: Optional[str] = None) -> Dict[str, Any]:
        """
        指定日の全約定履歴を取得。YYYYMMDD省略時は直近60件。
        バックテスト用のヒストリカルデータ取得に使用する。
        """
        path = f"/{pair}/transactions"
        if yyyymmdd:
            path += f"/{yyyymmdd}"
        url = f"{self.PUBLIC_BASE}{path}"
        r = self._session.get(url, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("data", {})

    def get_candlestick(self, pair: str, candle_type: str, yyyymmdd: str) -> Dict[str, Any]:
        url = f"{self.PUBLIC_BASE}/{pair}/candlestick/{candle_type}/{yyyymmdd}"
        r = self._session.get(url, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("data", {})

    # ---------- Private API（署名必須） ----------

    def _require_credentials(self):
        if not self.api_key or not self.api_secret:
            raise BitbankAPIError("api_key / api_secret が設定されていません（.envを確認）")

    def _sign_get(self, path: str, query: str = "") -> Dict[str, str]:
        nonce = str(int(time.time() * 1000))
        message = nonce + "/v1" + path + query
        signature = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-NONCE": nonce,
            "ACCESS-SIGNATURE": signature,
        }

    def _sign_post(self, body_json: str) -> Dict[str, str]:
        nonce = str(int(time.time() * 1000))
        message = nonce + body_json
        signature = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-NONCE": nonce,
            "ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
        }

    def get_assets(self) -> Dict[str, Any]:
        self._require_credentials()
        path = "/user/assets"
        headers = self._sign_get(path)
        r = self._session.get(self.PRIVATE_BASE + path, headers=headers, timeout=self.timeout)
        return self._handle_response(r)

    def get_active_orders(self, pair: str) -> Dict[str, Any]:
        """未約定注文一覧。WebSocket再接続時のリコンシリエーションに使用。"""
        self._require_credentials()
        path = "/user/spot/active_orders"
        query = f"?pair={pair}"
        headers = self._sign_get(path, query)
        r = self._session.get(self.PRIVATE_BASE + path + query, headers=headers, timeout=self.timeout)
        return self._handle_response(r)

    def get_order_status(self, pair: str, order_id: int) -> Dict[str, Any]:
        """
        個別注文の現在状態を取得する。
        get_active_ordersから注文が消えた際、それが「約定した」のか
        「キャンセルされた」のかを判別するために使用する。
        """
        self._require_credentials()
        path = "/user/spot/order"
        query = f"?pair={pair}&order_id={order_id}"
        headers = self._sign_get(path, query)
        r = self._session.get(self.PRIVATE_BASE + path + query, headers=headers, timeout=self.timeout)
        return self._handle_response(r)

    def get_subscribe_info(self) -> Dict[str, Any]:
        """
        Privateストリーム(PubNub経由の注文・資産更新通知)用のチャンネル名と
        トークンを取得する。トークンは12時間で失効するため、ws_private_client側で
        定期的に再取得する必要がある。
        """
        self._require_credentials()
        path = "/user/subscribe"
        headers = self._sign_get(path)
        r = self._session.get(self.PRIVATE_BASE + path, headers=headers, timeout=self.timeout)
        return self._handle_response(r)

    def create_order(
        self,
        pair: str,
        price: float,
        amount: float,
        side: str,
        order_type: str = "limit",
        post_only: bool = True,
    ) -> Dict[str, Any]:
        """
        post_only=True の場合、メイカー確定できない価格では約定拒否される
        （成行化を避けるため、呼び出し側でBest Bid/Askから1tick離した価格を渡すこと）。
        """
        self._require_credentials()
        path = "/user/spot/order"
        body = {
            "pair": pair,
            "price": str(price),
            "amount": str(amount),
            "side": side,
            "type": order_type,
        }
        if post_only:
            body["post_only"] = True
        body_json = json.dumps(body)
        headers = self._sign_post(body_json)
        r = self._session.post(self.PRIVATE_BASE + path, headers=headers, data=body_json, timeout=self.timeout)
        return self._handle_response(r)

    def cancel_order(self, pair: str, order_id: int) -> Dict[str, Any]:
        self._require_credentials()
        path = "/user/spot/cancel_order"
        body = {"pair": pair, "order_id": order_id}
        body_json = json.dumps(body)
        headers = self._sign_post(body_json)
        r = self._session.post(self.PRIVATE_BASE + path, headers=headers, data=body_json, timeout=self.timeout)
        return self._handle_response(r)

    @staticmethod
    def _handle_response(r: requests.Response) -> Dict[str, Any]:
        r.raise_for_status()
        data = r.json()
        if data.get("success") != 1:
            code = data.get("data", {}).get("code") if isinstance(data.get("data"), dict) else None
            raise BitbankAPIError(f"bitbank API error: {data}", code=code)
        return data.get("data", {})
