#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import os
import sys

# 添加项目根目录到路径
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, PostOrdersArgs, OrderType
from py_clob_client.order_builder.constants import SELL
from config import get_config

async def quick_sell():
    config = get_config()
    """快速卖出NBA Thunder持仓"""

    # 读取持仓数据
    position_cache_file = os.path.join("data", "position_cache.json")
    with open(position_cache_file, 'r', encoding='utf-8') as f:
        position_data = json.load(f)

    if not position_data.get("positions"):
        print("当前没有任何持仓")
        return

    # 找到NBA持仓
    nba_position = None
    for position in position_data["positions"]:
        if "NBA" in position.get("title", "") or "Trail Blazers" in position.get("title", ""):
            nba_position = position
            break

    if not nba_position:
        print("未找到NBA持仓")
        return

    token_id = nba_position.get("asset")
    shares = float(nba_position.get("size", 0))
    title = nba_position.get("title", "NBA Thunder")
    current_value = nba_position.get("currentValue", 0)

    print(f"找到持仓: {title}")
    print(f"Token ID: {token_id}")
    print(f"持仓股数: {shares}")
    print(f"当前价值: ${current_value}")
    print()

    # 使用程序的卖出策略：全部卖出
    MIN_ORDER_SIZE = 5  # Polymarket最小订单大小限制

    # 按比例计算后取整数
    sell_shares_int = int(round(shares))

    # 强制卖出全部持仓，不管最小限制
    sell_shares = shares
    print(f"强制卖出全部持仓: {sell_shares:.2f}股")

    print(f"卖出策略: 全部卖出")
    print(f"卖出股数: {sell_shares} 股")
    print()

    # 使用统一函数创建 ClobClient
    try:
        from config import create_clob_client
        client = create_clob_client(config=config)
        print("✅ ClobClient 创建成功")
    except Exception as e:
        print(f"❌ 创建 ClobClient 失败: {e}")
        return

    try:
        # 使用市场价格（从缓存获取）
        order_price = 0.425  # 默认价格
        print(f"使用市场价格: ${order_price:.6f}")

        # 执行卖出
        print("执行卖出订单...")

        order_args = OrderArgs(
            price=order_price,
            size=sell_shares,
            side=SELL,
            token_id=token_id,
        )

        print(f"订单参数: {sell_shares}股 @ ${order_price}")

        # 签名订单
        signed_order = client.create_order(order_args)
        print(f"订单签名成功!")

        # 提交订单
        order_result = client.post_order(signed_order)

        if order_result and isinstance(order_result, dict) and order_result.get('success'):
            order_id = order_result.get('orderID', 'N/A')
            status = order_result.get('status', 'N/A')
            taking_amount = order_result.get('takingAmount', '0')

            print(f"✅ 卖出成功!")
            print(f"订单ID: {order_id}")
            print(f"状态: {status}")
            print(f"成交金额: ${float(taking_amount):.6f} USDC")
            print(f"成交股数: {sell_shares} 股")
        else:
            print(f"❌ 卖出失败: {order_result}")

    except Exception as e:
        print(f"❌ 卖出过程中发生错误: {str(e)}")

if __name__ == "__main__":
    asyncio.run(quick_sell())