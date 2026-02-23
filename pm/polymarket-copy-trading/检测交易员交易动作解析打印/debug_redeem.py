#!/usr/bin/env python3
"""
调试脚本：检查持仓详情和链上余额
用于排查赎回问题
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from auto_redeem import AutoRedeemService
from web3 import Web3
import requests


def debug_position():
    """调试持仓查询"""
    config = Config()
    service = AutoRedeemService(config)
    
    wallet_address = config.proxy_wallet_address
    print(f"\n钱包地址: {wallet_address}")
    print(f"RPC URL: {config.rpc_url}")
    print("="*80)
    
    # 1. 从 API 获取持仓
    print("\n【1】从 Polymarket API 获取持仓...")
    api_url = "https://data-api.polymarket.com/positions"
    params = {'user': wallet_address, 'limit': 100}
    
    response = requests.get(api_url, params=params)
    positions = response.json()
    
    print(f"API 返回 {len(positions)} 个持仓")
    
    # 过滤可赎回的
    redeemable = [p for p in positions if p.get('redeemable', False) and p.get('currentValue', 0) > 0]
    print(f"其中可赎回的: {len(redeemable)} 个\n")
    
    for i, pos in enumerate(redeemable, 1):
        print(f"\n持仓 [{i}]:")
        print(f"  市场: {pos.get('slug', 'Unknown')}")
        print(f"  Condition ID: {pos.get('conditionId', 'N/A')}")
        print(f"  Token ID: {pos.get('asset', 'N/A')}")
        print(f"  Size: {pos.get('size', 0)}")
        print(f"  Current Value: {pos.get('currentValue', 0)} USDC")
        print(f"  Outcome: {pos.get('outcome', 'N/A')}")
        print(f"  Outcome Index: {pos.get('outcomeIndex', 'N/A')}")
        print(f"  Redeemable: {pos.get('redeemable', False)}")
        
        # 2. 检查链上余额
        print(f"\n  【2】检查链上余额...")
        condition_id = pos.get('conditionId')
        
        if not condition_id:
            print(f"  ⚠️ 缺少 condition_id")
            continue
        
        # 计算 Index Set (1 和 2 对应 Index 0 和 1)
        for idx, index_set in enumerate([1, 2], 0):
            try:
                # 计算 collection_id
                parent = "0x0000000000000000000000000000000000000000000000000000000000000000"
                collection_id = Web3.solidity_keccak(
                    ['bytes32', 'bytes32', 'uint256'],
                    [parent, condition_id, index_set]
                )
                
                # 计算 position_id
                position_id = Web3.solidity_keccak(
                    ['address', 'bytes32'],
                    [Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"), collection_id]
                )
                position_id_int = int.from_bytes(position_id, byteorder='big')
                
                # 查询余额
                balance = service.ctf_contract.functions.balanceOf(wallet_address, position_id_int).call()
                
                print(f"    Index {idx} (Set {index_set}): {balance}")
                
                # 查询 payout
                try:
                    payout = service.ctf_contract.functions.payoutNumerators(condition_id, idx).call()
                    print(f"      └─ Payout: {payout}")
                except Exception as e:
                    print(f"      └─ Payout 查询失败: {e}")
                    
            except Exception as e:
                print(f"    Index {idx} 查询失败: {e}")
    
    print("\n" + "="*80)
    print("调试完成")
    print("="*80)


if __name__ == "__main__":
    debug_position()
