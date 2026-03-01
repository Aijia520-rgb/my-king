"""
Polymarket套利机器人 - 主程序入口

套利策略说明:
1. YES/NO套利: 当YES价格 + NO价格 < 1时，同时买入两边
   - 到期时无论结果如何，都能获得1的赔付
   - 利润 = 1 - (YES价格 + NO价格)
   
2. 使用方法:
   python main.py                    # 实盘模式（扫描真实市场，执行交易）
   python main.py --dry-run          # 模拟模式（扫描真实市场，不下单）
   python main.py --scan-only        # 仅扫描一次，检查套利机会
   python main.py --markets slug1,slug2  # 指定市场
"""
import asyncio
import signal
import sys
import argparse
import logging
import time
from typing import List

from config import Config, get_config, logger

class ArbitrageBot:
    def __init__(self, config: Config):
        self.config = config
        self.clob_client = None
        self.trade_executor = None
        self.arbitrage_engine = None
        self.market_monitor = None
        
        self._running = False
        self._dry_run = False
        
        self.stats = {
            'opportunities_found': 0,
            'trades_executed': 0,
            'total_profit': 0.0,
            'total_volume': 0.0,
        }
        
    async def initialize(self, dry_run: bool = False):
        self._dry_run = dry_run
        
        if not self.config.validate():
            raise ValueError("配置验证失败")
        
        logger.info("[INIT] 初始化CLOB客户端...")
        self.clob_client = self.config.create_clob_client()
        
        from trade_executor import TradeExecutor
        from arbitrage_engine import ArbitrageEngine
        from market_monitor import MarketMonitor
        
        logger.info("[INIT] 初始化交易执行器...")
        self.trade_executor = TradeExecutor(self.config, self.clob_client)
        
        logger.info("[INIT] 初始化套利引擎...")
        self.arbitrage_engine = ArbitrageEngine(
            self.config,
            self.clob_client,
            self.trade_executor,
        )
        
        logger.info("[INIT] 初始化市场监控器...")
        self.market_monitor = MarketMonitor(self.config, self.clob_client)
        
        await self.market_monitor.start()
        await self.arbitrage_engine.start()
        
        logger.info("[INIT] 初始化完成")
    
    async def run_auto_scan(self, max_markets: int = 50):
        logger.info(f"\n{'='*60}")
        logger.info(f"[BOT] {'模拟模式' if self._dry_run else '实盘模式'}启动")
        logger.info(f"[BOT] 最大市场数: {max_markets}")
        logger.info(f"[BOT] 最小利润阈值: {self.config.arbitrage.min_profit_threshold*100:.2f}%")
        logger.info(f"[BOT] 最小交易金额: ${self.config.arbitrage.min_trade_size:.2f}")
        logger.info(f"[BOT] 最大交易金额: ${self.config.arbitrage.max_trade_size:.2f}")
        logger.info(f"[BOT] 是否下单: {'否' if self._dry_run else '是'}")
        logger.info(f"{'='*60}\n")
        
        registered = await self.market_monitor.scan_and_register_markets(
            self.arbitrage_engine,
            max_markets=max_markets,
        )
        
        if registered == 0:
            logger.warning("[BOT] 未找到有效市场，退出")
            return
        
        await self._run_arbitrage_loop()
    
    async def run_specific_markets(self, market_slugs: List[str]):
        logger.info(f"\n{'='*60}")
        logger.info(f"[BOT] {'模拟模式' if self._dry_run else '实盘模式'} - 指定市场")
        logger.info(f"[BOT] 目标市场: {market_slugs}")
        logger.info(f"[BOT] 最小利润阈值: {self.config.arbitrage.min_profit_threshold*100:.2f}%")
        logger.info(f"[BOT] 是否下单: {'否' if self._dry_run else '是'}")
        logger.info(f"{'='*60}\n")
        
        await self.market_monitor.register_specific_markets(
            self.arbitrage_engine,
            market_slugs,
        )
        
        await self._run_arbitrage_loop()
    
    async def run_scan_only(self, max_markets: int = 50):
        logger.info(f"\n{'='*60}")
        logger.info("[BOT] 仅扫描模式（扫描一次后退出）")
        logger.info(f"{'='*60}\n")
        
        await self.market_monitor.scan_and_register_markets(
            self.arbitrage_engine,
            max_markets=max_markets,
        )
        
        logger.info("\n[BOT] 扫描完成，检查套利机会...")
        
        opportunities = await self.arbitrage_engine.scan_all_markets()
        
        if opportunities:
            logger.info(f"\n[BOT] 发现 {len(opportunities)} 个套利机会:")
            for opp in opportunities:
                logger.info(f"\n  {'='*50}")
                logger.info(f"  市场: {opp.market_slug}")
                logger.info(f"  YES价格: {opp.yes_price:.4f}")
                logger.info(f"  NO价格: {opp.no_price:.4f}")
                logger.info(f"  价格总和: {opp.price_sum:.4f}")
                logger.info(f"  利润率: {opp.profit_percent:.2f}%")
                logger.info(f"  建议金额: ${opp.recommended_size:.2f}")
        else:
            logger.info("[BOT] 未发现套利机会")
        
        self.market_monitor.print_summary()
        self.arbitrage_engine.print_summary()
        
        await self.shutdown()
    
    async def _run_arbitrage_loop(self):
        self._running = True
        start_time = time.time()
        iteration = 0
        
        def handle_exit(sig, frame):
            logger.info("\n[SHUTDOWN] 接收到退出信号...")
            self._running = False
        
        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)
        
        logger.info("[BOT] 开始套利循环... (Ctrl+C 退出)\n")
        
        interval = self.config.arbitrage.price_check_interval
        
        while self._running:
            try:
                iteration += 1
                elapsed = time.time() - start_time
                
                logger.info(f"[SCAN] 第 {iteration} 轮扫描 (已运行 {elapsed/60:.1f} 分钟)")
                
                opportunities = await self.arbitrage_engine.scan_all_markets()
                
                if opportunities:
                    self.stats['opportunities_found'] += len(opportunities)
                    logger.info(f"[SCAN] 发现 {len(opportunities)} 个套利机会")
                    
                    for opp in opportunities:
                        if opp.profit_percent >= self.config.arbitrage.min_profit_threshold * 100:
                            if self._dry_run:
                                logger.info(f"\n[DRY-RUN] 发现套利机会（模拟模式不下单）:")
                                logger.info(f"  市场: {opp.market_slug}")
                                logger.info(f"  YES价格: {opp.yes_price:.4f}")
                                logger.info(f"  NO价格: {opp.no_price:.4f}")
                                logger.info(f"  价格总和: {opp.price_sum:.4f}")
                                logger.info(f"  利润率: {opp.profit_percent:.2f}%")
                                logger.info(f"  建议金额: ${opp.recommended_size:.2f}")
                                
                                self.stats['trades_executed'] += 1
                                profit = opp.recommended_size * (1 - opp.price_sum)
                                self.stats['total_profit'] += profit
                                self.stats['total_volume'] += opp.recommended_size * 2
                            else:
                                success = await self.arbitrage_engine.execute_yes_no_arbitrage(opp)
                                if success:
                                    self.stats['trades_executed'] += 1
                                    profit = opp.recommended_size * (1 - opp.price_sum)
                                    self.stats['total_profit'] += profit
                                    self.stats['total_volume'] += opp.recommended_size * 2
                else:
                    logger.info("[SCAN] 本轮未发现套利机会")
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                logger.error(f"[BOT] 套利循环错误: {e}")
                await asyncio.sleep(5)
        
        await self.shutdown()
    
    async def shutdown(self):
        logger.info("\n[SHUTDOWN] 正在关闭...")
        
        if self.trade_executor:
            await self.trade_executor.cancel_all_orders()
        
        self.print_summary()
        
        if self.market_monitor:
            await self.market_monitor.stop()
        
        logger.info("[SHUTDOWN] 关闭完成")
    
    def print_summary(self):
        logger.info(f"\n{'='*60}")
        logger.info("[STATS] 运行统计")
        logger.info(f"{'='*60}")
        logger.info(f"发现机会数: {self.stats['opportunities_found']}")
        logger.info(f"执行交易数: {self.stats['trades_executed']}")
        logger.info(f"预计总利润: ${self.stats['total_profit']:.2f}")
        logger.info(f"总交易量: ${self.stats['total_volume']:.2f}")
        if self.stats['trades_executed'] > 0:
            avg_profit = self.stats['total_profit'] / self.stats['trades_executed']
            logger.info(f"平均单笔利润: ${avg_profit:.2f}")
        logger.info(f"{'='*60}\n")


async def main():
    parser = argparse.ArgumentParser(description='Polymarket套利机器人')
    parser.add_argument('--dry-run', action='store_true', help='模拟模式（扫描真实市场，不下单）')
    parser.add_argument('--scan-only', action='store_true', help='仅扫描一次，检查套利机会')
    parser.add_argument('--markets', type=str, help='指定市场slug，逗号分隔')
    parser.add_argument('--max-markets', type=int, default=50, help='最大扫描市场数')
    
    args = parser.parse_args()
    
    logger.info("\n" + "="*60)
    logger.info("Polymarket套利机器人 v1.0")
    logger.info("="*60)
    
    config = get_config()
    bot = ArbitrageBot(config)
    
    try:
        if args.scan_only:
            await bot.initialize(dry_run=True)
            await bot.run_scan_only(max_markets=args.max_markets)
        elif args.markets:
            market_slugs = [m.strip() for m in args.markets.split(',')]
            await bot.initialize(dry_run=args.dry_run)
            await bot.run_specific_markets(market_slugs)
        else:
            await bot.initialize(dry_run=args.dry_run)
            await bot.run_auto_scan(max_markets=args.max_markets)
            
    except ValueError as e:
        logger.error(f"[ERROR] 配置错误: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"[ERROR] 运行错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
