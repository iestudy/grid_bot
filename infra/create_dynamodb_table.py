"""
DynamoDBテーブル作成スクリプト。

実行前提: AWS認証情報が環境変数またはAWS CLIの設定済みprofileで利用可能なこと
    aws configure  # または環境変数 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY

重要: 必ず PAY_PER_REQUEST（オンデマンド）で作成すること。
      PROVISIONED（プロビジョニングモード）にすると、放置しているだけで
      月額コストが発生し続ける（レビューでも指摘済みの注意点）。
"""

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = "grid_bot_orders"
REGION = "ap-northeast-1"


def create_table():
    client = boto3.client("dynamodb", region_name=REGION)
    try:
        client.describe_table(TableName=TABLE_NAME)
        print(f"テーブル '{TABLE_NAME}' は既に存在します。作成をスキップします。")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    print(f"テーブル '{TABLE_NAME}' を作成します（オンデマンドモード）...")
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "request_id", "KeyType": "HASH"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "request_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",  # ← ここが重要。放置コスト回避
    )

    waiter = client.get_waiter("table_exists")
    waiter.wait(TableName=TABLE_NAME)
    print(f"テーブル '{TABLE_NAME}' の作成が完了しました。")


if __name__ == "__main__":
    create_table()
