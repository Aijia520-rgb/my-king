#!/usr/bin/env python3
"""
自动赎回守护进程
用于持续自动赎回，每小时执行一次，每10分钟心跳一次

使用方法:
    python redeem_daemon.py              # 启动守护进程
    python redeem_daemon.py --verbose    # 显示详细日志
    python redeem_daemon.py --silent     # 完全静默（只有心跳）
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from config import Config, logger
from auto_redeem import AutoRedeemService
import logging


def setup_logging(verbose=False, silent=False):
    """设置日志级别"""
    if silent:
        # 静默模式：只显示心跳，其他都忽略
        logging.getLogger().setLevel(logging.ERROR)
    elif verbose:
        # 详细模式
        logging.getLogger().setLevel(logging.DEBUG)
        file_handler = logging.FileHandler('redeem_daemon.log', encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    else:
        # 默认模式：只显示错误
        logging.getLogger().setLevel(logging.WARNING)


def print_heartbeat():
    """打印心跳信息"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] 💓 赎回守护进程运行中... (下次赎回: 每小时整点)")


def main():
    parser = argparse.ArgumentParser(description='Polymarket 自动赎回守护进程')
    parser.add_argument('--verbose', action='store_true', 
                        help='显示详细日志')
    parser.add_argument('--silent', action='store_true',
                        help='完全静默模式（只有心跳）')
    
    args = parser.parse_args()
    
    # 设置日志
    setup_logging(verbose=args.verbose, silent=args.silent)
    
    print("\n" + "="*80)
    print("Polymarket 自动赎回守护进程")
    print("="*80)
    print("\n配置:")
    print("  - 赎回频率: 每小时 1 次")
    print("  - 心跳频率: 每 10 分钟 1 次")
    print("  - 按 Ctrl+C 停止\n")
    
    # 初始化配置
    try:
        config = Config()
        print(f"✅ 配置加载成功")
        print(f"   代理钱包: {config.proxy_wallet_address}")
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
        sys.exit(1)
    
    # 初始化服务
    try:
        service = AutoRedeemService(config)
        print("✅ 赎回服务初始化成功\n")
    except Exception as e:
        print(f"❌ 赎回服务初始化失败: {e}")
        sys.exit(1)
    
    # 获取钱包地址
    wallet_address = config.proxy_wallet_address
    if not wallet_address:
        print("❌ 错误: 未配置代理钱包地址")
        sys.exit(1)
    
    # 记录上次赎回时间
    last_redeem_time = 0
    heartbeat_counter = 0
    
    print("🚀 守护进程已启动，开始运行...")
    print("="*80 + "\n")
    
    try:
        while True:
            current_time = time.time()
            
            # 每小时执行一次赎回（3600秒）
            if current_time - last_redeem_time >= 3600:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f"\n[{now}] 🔄 开始执行自动赎回...")
                
                try:
                    # 静默执行赎回
                    service.execute(silent=not args.verbose)
                    last_redeem_time = current_time
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ 赎回执行完成\n")
                except Exception as e:
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ❌ 赎回失败: {e}\n")
                    # 即使失败也更新时间，避免频繁重试
                    last_redeem_time = current_time
            
            # 每10分钟输出一次心跳（600秒）
            if heartbeat_counter >= 60:  # 60 * 10秒 = 600秒 = 10分钟
                print_heartbeat()
                heartbeat_counter = 0
            
            # 休眠10秒
            time.sleep(10)
            heartbeat_counter += 1
            
    except KeyboardInterrupt:
        print("\n\n👋 守护进程已停止")
        print("="*80)


if __name__ == "__main__":
    main()
