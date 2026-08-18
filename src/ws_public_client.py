"""
bitbank Publicストリーム(リアルタイムデータ配信API)クライアント。

重要: bitbankのPublicストリームはPubNubではなく、Socket.IO 4.x
(Engine.io protocol v4)で実装されている(2022-07-26以降)。
    エンドポイント: wss://stream.bitbank.cc
    購読方法: 接続後に "join-room" イベントでルーム名(例: "ticker_xrp_jpy")を送信
    受信: サーバーから "message" イベントで {"room_name": ..., "message": {...}} が届く

このモジュールはpython-socketioライブラリ(生のwebsocket-clientではない)を使う。

注意: メッセージのイベント名・ペイロード構造は公式ドキュメント
(bitbank-api-docs/public-stream_JP.md)および複数の実装例に基づく一般的な
パターンで実装している。本番投入前に、実際の接続で受信するペイロードの
生ログを一度必ず確認し、想定通りの構造か検証すること。
"""

import logging
import threading
import time
from typing import Optional

import socketio

logger = logging.getLogger(__name__)

STREAM_URL = "https://stream.bitbank.cc"


class WebSocketPriceFeed:
    """
    指定ペアのtickerルームを購読し、最新価格をスレッドセーフに保持する。
    REST APIのget_tickerポーリングの代替として使う想定。

    使い方:
        feed = WebSocketPriceFeed(pair="xrp_jpy")
        feed.connect()
        ...
        price = feed.get_latest_price()  # 未受信ならNone
        ...
        feed.disconnect()
    """

    def __init__(self, pair: str):
        self.pair = pair
        self.room_name = f"ticker_{pair}"
        self._sio = socketio.Client(reconnection=True, reconnection_attempts=0, reconnection_delay=1, reconnection_delay_max=30)
        self._lock = threading.Lock()
        self._latest_price: Optional[float] = None
        self._latest_timestamp: Optional[float] = None
        self._connected = False

        self._sio.on("connect", self._on_connect)
        self._sio.on("disconnect", self._on_disconnect)
        self._sio.on("message", self._on_message)

    def _on_connect(self):
        logger.info(f"WebSocket接続確立、ルーム購読開始: {self.room_name}")
        self._connected = True
        # 再接続時もこのハンドラが呼ばれるため、ここで毎回join-roomすることで
        # 再接続後にルーム購読が失われる問題を回避する。
        self._sio.emit("join-room", self.room_name)

    def _on_disconnect(self):
        logger.warning("WebSocket切断を検知しました。自動再接続を待ちます。")
        self._connected = False

    def _on_message(self, data):
        try:
            if not isinstance(data, dict):
                return
            if data.get("room_name") != self.room_name:
                return
            message = data.get("message", {})
            ticker_data = message.get("data", message)
            last_price = ticker_data.get("last")
            if last_price is None:
                return
            with self._lock:
                self._latest_price = float(last_price)
                self._latest_timestamp = time.time()
        except Exception as e:
            logger.error(f"tickerメッセージのパースに失敗: {e} data={data!r}")

    def connect(self, timeout: float = 10.0) -> None:
        self._sio.connect(STREAM_URL, transports=["websocket"], wait_timeout=timeout)

    def disconnect(self) -> None:
        self._sio.disconnect()

    def is_connected(self) -> bool:
        return self._connected

    def get_latest_price(self, max_age_sec: float = 30.0) -> Optional[float]:
        """
        最新価格を返す。max_age_secより古いデータしかない場合はNoneを返し、
        呼び出し側にREST APIへのフォールバックを促す(接続断・購読漏れ対策)。
        """
        with self._lock:
            if self._latest_price is None or self._latest_timestamp is None:
                return None
            if time.time() - self._latest_timestamp > max_age_sec:
                logger.warning(f"WebSocket価格が古すぎます({time.time() - self._latest_timestamp:.1f}秒前)。フォールバック推奨。")
                return None
            return self._latest_price


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="xrp_jpy")
    parser.add_argument("--duration", type=float, default=15.0, help="受信を確認する秒数")
    args = parser.parse_args()

    feed = WebSocketPriceFeed(args.pair)
    feed.connect()

    start = time.time()
    while time.time() - start < args.duration:
        price = feed.get_latest_price()
        print(f"最新価格: {price}")
        time.sleep(2)

    feed.disconnect()
