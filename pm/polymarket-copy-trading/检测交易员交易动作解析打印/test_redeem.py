#!/usr/bin/env python3
"""
赎回测试脚本
用于测试和调试自动赎回功能

使用方法:
    python test_redeem.py              # 执行一次赎回
    python test_redeem.py --check      # 仅检查可赎回持仓，不执行赎回
    python test_redeem.py --verbose    # 显示详细日志
"""

import sys
import argparse
from pathlib import Path

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from config import Config, logger
from auto_redeem import AutoRedeemService
import logging


def setup_verbose_logging():
    """设置详细日志输出"""
    # 设置根日志级别为 DEBUG
    logging.getLogger().setLevel(logging.DEBUG)
    
    # 添加文件处理器
    file_handler = logging.FileHandler('test_redeem.log', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    print("[INFO] 详细日志已开启，同时输出到 test_redeem.log")


def check_only(service: AutoRedeemService, wallet_address: str):
    """仅检查可赎回持仓，不执行"""
    print("\n" + "="*80)
    print("检查可赎回持仓")
    print("="*80)
    
    positions = service.get_redeemable_positions(wallet_address)
    
    if not positions:
        print(f"\n未找到可赎回持仓")
        return
    
    print(f"\n找到 {len(positions)} 个可赎回持仓:\n")
    
    for i, pos in enumerate(positions, 1):
        print(f"[{i}] {pos.get('slug', 'Unknown')}")
        print(f"    Condition ID: {pos.get('conditionId', 'N/A')}")
        print(f"    Size: {pos.get('size', 0)} 股")
        print(f"    Current Value: {pos.get('currentValue', 0)} USDC")
        print(f"    Outcome: {pos.get('outcome', 'N/A')}")
        print(f"    Outcome Index: {pos.get('outcomeIndex', 'N/A')}")
        print(f"    Redeemable: {pos.get('redeemable', False)}")
        print()


def execute_redeem(service: AutoRedeemService, wallet_address: str):
    """执行赎回"""
    print("\n" + "="*80)
    print("开始执行赎回")
    print("="*80)
    print(f"钱包地址: {wallet_address}")
    print()
    
    # 先检查
    positions = service.get_redeemable_positions(wallet_address)
    
    if not positions:
        print("没有可赎回持仓，退出")
        return
    
    print(f"发现 {len(positions)} 个可赎回持仓")
    print()
    
    # 自动执行，无需确认
    print("\n开始赎回...\n")
    
    # 执行赎回
    service.execute()
    
    print("\n" + "="*80)
    print("赎回执行完成")
    print("="*80)


def main():
    parser = argparse.ArgumentParser(description='Polymarket 赎回测试脚本')
    parser.add_argument('--check', action='store_true', 
                        help='仅检查可赎回持仓，不执行赎回')
    parser.add_argument('--verbose', action='store_true',
                        help='显示详细日志')
    
    args = parser.parse_args()
    
    # 设置详细日志
    if args.verbose:
        setup_verbose_logging()
    
    print("\n" + "="*80)
    print("Polymarket 自动赎回测试")
    print("="*80)
    
    # 初始化配置
    try:
        config = Config()
        print(f"\n配置加载成功")
        print(f"代理钱包: {config.proxy_wallet_address}")
        print(f"RPC URL: {config.rpc_url}")
    except Exception as e:
        print(f"配置加载失败: {e}")
        sys.exit(1)
    
    # 初始化服务
    try:
        service = AutoRedeemService(config)
        print("赎回服务初始化成功")
    except Exception as e:
        print(f"赎回服务初始化失败: {e}")
        sys.exit(1)
    
    # 获取钱包地址
    wallet_address = config.proxy_wallet_address
    if not wallet_address:
        print("错误: 未配置代理钱包地址")
        sys.exit(1)
    
    # 执行操作
    if args.check:
        check_only(service, wallet_address)
    else:
        execute_redeem(service, wallet_address)
    
    print("\n完成!")


if __name__ == "__main__":
    main()
