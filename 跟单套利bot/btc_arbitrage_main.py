"""
BTC延迟套利机器人主程序
整合Binance价格监控、Chainlink价格监控、Polymarket市场发现和交易执行
"""
import asyncio
import logging
import signal
import time
import argparse
from datetime import datetime, timezone
from typing import Optional

from config import get_config
from trade_executor import TradeExecutor
from latency_arbitrage import LatencyArbitrageStrategy, MarketSession
from binance_monitor import BinancePriceMonitor
from chainlink_monitor import ChainlinkPriceMonitor
from btc_market_monitor import BTCMarketMonitor

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

class BTCLatencyArbitrageBot:
    def __init__(self, config):
        self.config = config
        
        self.clob_client = None
        self.trade_executor: Optional[TradeExecutor] = None
        self.strategy: Optional[LatencyArbitrageStrategy] = None
        self.binance_monitor: Optional[BinancePriceMonitor] = None
        self.chainlink_monitor: Optional[ChainlinkPriceMonitor] = None
        self.market_monitor: Optional[BTCMarketMonitor] = None
        
        self._dry_run = False
        self._running = False
        
        self.stats = {
            'start_time': 0,
            'trades': 0,
            'profit': 0.0,
        }

    async def initialize(self, dry_run: bool = False):
        self._dry_run = dry_run
        
        logger.info("[INIT] 初始化BTC延迟套利机器人...")
        
        self.clob_client = self.config.create_clob_client()
        logger.info("[INIT] CLOB客户端初始化完成")
        
        self.trade_executor = TradeExecutor(self.config, self.clob_client)
        logger.info("[INIT] 交易执行器初始化完成")
        
        self.strategy = LatencyArbitrageStrategy(self.config, self.clob_client, self.trade_executor, dry_run=dry_run)
        await self.strategy.start()
        logger.info("[INIT] 套利策略初始化完成")
        
        self.binance_monitor = BinancePriceMonitor(
            on_price_update=self._on_binance_price
        )
        logger.info("[INIT] Binance监控器初始化完成")
        
        self.chainlink_monitor = ChainlinkPriceMonitor(
            on_price_update=self._on_chainlink_price
        )
        logger.info("[INIT] Chainlink监控器初始化完成")
        
        self.market_monitor = BTCMarketMonitor(self.config, self.clob_client)
        self.market_monitor.on_new_market(self._on_new_market)
        logger.info("[INIT] BTC市场监控器初始化完成")
        
        logger.info(f"[INIT] 初始化完成，模式: {'模拟' if dry_run else '实盘'}")

    async def _on_binance_price(self, price: float, volume: float):
        if self.strategy:
            self.strategy.update_binance_price(price, volume)

    async def _on_chainlink_price(self, price: float):
        if self.strategy:
            self.strategy.update_chainlink_price(price)

    async def _on_new_market(self, market):
        logger.info(f"\n[MARKET] 新市场开始: {market.slug}")
        logger.info(f"[MARKET] Up Token: {market.up_token_id[:20]}...")
        logger.info(f"[MARKET] Down Token: {market.down_token_id[:20]}...")
        
        if self.trade_executor:
            logger.info("[MARKET] 预加载市场参数...")
            await self.trade_executor.prefetch_market(market.up_token_id, market.down_token_id)
        
        if self.strategy:
            session = MarketSession(
                market_slug=market.slug,
                up_token_id=market.up_token_id,
                down_token_id=market.down_token_id,
                start_time=market.start_time,
                end_time=market.end_time,
                current_up_price=market.up_price,
                current_down_price=market.down_price,
            )
            self.strategy.set_current_market(session)

    async def run(self):
        self._running = True
        self.stats['start_time'] = time.time()
        
        logger.info("\n" + "="*60)
        logger.info("[BOT] BTC延迟套利机器人启动")
        logger.info(f"[BOT] 模式: {'模拟（不下单）' if self._dry_run else '实盘'}")
        logger.info("="*60 + "\n")
        
        tasks = [
            asyncio.create_task(self.binance_monitor.start()),
            asyncio.create_task(self.chainlink_monitor.start()),
            asyncio.create_task(self.market_monitor.start()),
            asyncio.create_task(self._status_loop()),
        ]
        
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[ERROR] 运行错误: {e}")
            import traceback
            traceback.print_exc()
        finally:
            for task in tasks:
                task.cancel()
            
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.shutdown()

    async def _status_loop(self):
        while self._running:
            await asyncio.sleep(60)
            
            if self.binance_monitor:
                binance_stats = self.binance_monitor.get_statistics()
                logger.info(f"[STATUS] Binance最新价格: ${binance_stats['last_price']:,.2f}")
            
            if self.strategy:
                strategy_stats = self.strategy.get_statistics()
                logger.info(f"[STATUS] 信号数: {strategy_stats['signals_generated']}, 交易数: {strategy_stats['trades_executed']}")

    async def shutdown(self):
        logger.info("\n[SHUTDOWN] 正在关闭...")
        
        if self.binance_monitor:
            await self.binance_monitor.stop()
        
        if self.chainlink_monitor:
            await self.chainlink_monitor.stop()
        
        if self.market_monitor:
            await self.market_monitor.stop()
        
        if self.strategy:
            await self.strategy.stop()
            self.strategy.print_summary()
        
        if self.trade_executor:
            await self.trade_executor.cancel_all_orders()
            await self.trade_executor.close_session()
        
        self.print_summary()
        logger.info("[SHUTDOWN] 关闭完成")

    def print_summary(self):
        elapsed = time.time() - self.stats['start_time'] if self.stats['start_time'] else 0
        
        logger.info(f"\n{'='*60}")
        logger.info("[STATS] 运行统计")
        logger.info(f"{'='*60}")
        logger.info(f"运行时间: {elapsed/60:.1f}分钟")
        
        if self.strategy:
            stats = self.strategy.get_statistics()
            logger.info(f"生成信号: {stats['signals_generated']}")
            logger.info(f"执行交易: {stats['trades_executed']}")
            logger.info(f"胜/负: {stats['wins']}/{stats['losses']}")
            if stats['trades_executed'] > 0:
                win_rate = stats['wins'] / stats['trades_executed'] * 100
                logger.info(f"胜率: {win_rate:.1f}%")
            logger.info(f"总利润: ${stats['total_profit']:.2f}")
        
        logger.info(f"{'='*60}\n")


async def main():
    parser = argparse.ArgumentParser(description='BTC延迟套利机器人')
    parser.add_argument('--dry-run', action='store_true', help='模拟模式（监控价格，不下单）')
    
    args = parser.parse_args()
    
    logger.info("\n" + "="*60)
    logger.info("BTC延迟套利机器人 v1.0")
    logger.info("="*60)
    
    config = get_config()
    bot = BTCLatencyArbitrageBot(config)
    
    try:
        await bot.initialize(dry_run=args.dry_run)
        await bot.run()
    except Exception as e:
        logger.error(f"[ERROR] 运行错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        logger.info("[BOT] 程序结束")

if __name__ == "__main__":
    asyncio.run(main())
