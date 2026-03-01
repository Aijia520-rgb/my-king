"""
Chainlink延迟套利策略

核心原理:
- Chainlink BTC/USD价格每分钟更新一次
- Binance价格是实时的
- 在Chainlink更新前，我们已经知道BTC在过去1分钟的涨跌
- 抢在市场反应前买入正确方向

适合小资金，风险低
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional, Dict, List
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

@dataclass
class PriceTick:
    price: float
    timestamp: float

@dataclass
class MarketSession:
    market_slug: str
    up_token_id: str
    down_token_id: str
    start_time: float
    end_time: float
    start_price: Optional[float] = None
    current_up_price: float = 0.5
    current_down_price: float = 0.5
    trade_direction: Optional[str] = None
    trade_amount: float = 0.0
    trade_price: float = 0.0

class ChainlinkDelayStrategy:
    def __init__(self, config, clob_client, trade_executor, dry_run: bool = False):
        self.config = config
        self.clob_client = clob_client
        self.trade_executor = trade_executor
        self._dry_run = dry_run
        
        self.binance_prices: List[PriceTick] = []
        self.chainlink_prices: List[PriceTick] = []
        
        self.current_market: Optional[MarketSession] = None
        
        self.chainlink_update_interval = 60
        self.last_chainlink_update = 0
        
        self.trade_amount = 15.0
        self.min_price_change = 0.0005
        self.max_price_change = 0.01
        
        self._running = False
        
        self.stats = {
            'signals_generated': 0,
            'trades_executed': 0,
            'wins': 0,
            'losses': 0,
            'total_profit': 0.0,
            'chainlink_updates': 0,
        }
        
        logger.info("[STRATEGY] Chainlink延迟套利策略初始化")
        logger.info(f"[STRATEGY] 每次下注金额: ${self.trade_amount}")
        logger.info(f"[STRATEGY] 最小价格变动: {self.min_price_change * 100:.2f}%")

    async def start(self):
        self._running = True
        logger.info("[STRATEGY] Chainlink延迟套利策略启动")
        
        asyncio.create_task(self._monitor_chainlink_updates())

    async def stop(self):
        self._running = False
        logger.info("[STRATEGY] Chainlink延迟套利策略停止")

    def update_binance_price(self, price: float, volume: float = 0.0):
        tick = PriceTick(price=price, timestamp=time.time())
        self.binance_prices.append(tick)
        
        while len(self.binance_prices) > 1000:
            self.binance_prices.pop(0)

    def update_chainlink_price(self, price: float):
        tick = PriceTick(price=price, timestamp=time.time())
        self.chainlink_prices.append(tick)
        
        while len(self.chainlink_prices) > 100:
            self.chainlink_prices.pop(0)
        
        self.last_chainlink_update = time.time()
        self.stats['chainlink_updates'] += 1
        
        logger.info(f"[CHAINLINK] 价格更新: ${price:,.2f}")

    def set_current_market(self, market: MarketSession):
        self.current_market = market
        logger.info(f"[STRATEGY] 设置当前市场: {market.market_slug}")
        logger.info(f"[STRATEGY] 开始时间: {datetime.fromtimestamp(market.start_time, tz=timezone.utc)}")
        logger.info(f"[STRATEGY] 结束时间: {datetime.fromtimestamp(market.end_time, tz=timezone.utc)}")

    async def _monitor_chainlink_updates(self):
        while self._running:
            try:
                await self._check_trading_opportunity()
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[STRATEGY] 监控错误: {e}")
                await asyncio.sleep(1)

    async def _check_trading_opportunity(self):
        if not self.current_market:
            return
        
        now = time.time()
        market = self.current_market
        
        if now < market.start_time or now > market.end_time - 30:
            return
        
        if market.trade_direction:
            return
        
        time_since_last_chainlink = now - self.last_chainlink_update
        
        if time_since_last_chainlink < 50:
            return
        
        recent_prices = [p for p in self.binance_prices if p.timestamp >= now - 60]
        
        if len(recent_prices) < 10:
            return
        
        first_price = recent_prices[0].price
        last_price = recent_prices[-1].price
        price_change = (last_price - first_price) / first_price
        
        logger.debug(f"[STRATEGY] 距Chainlink更新: {time_since_last_chainlink:.0f}s, 价格变动: {price_change*100:.3f}%")
        
        if abs(price_change) < self.min_price_change:
            logger.debug(f"[STRATEGY] 价格变动太小，跳过")
            return
        
        if abs(price_change) > self.max_price_change:
            logger.debug(f"[STRATEGY] 价格变动太大（可能已反应），跳过")
            return
        
        direction = "UP" if price_change > 0 else "DOWN"
        
        logger.info(f"\n{'='*60}")
        logger.info(f"[SIGNAL] Chainlink延迟套利机会!")
        logger.info(f"[SIGNAL] 方向: {direction}")
        logger.info(f"[SIGNAL] 价格变动: {price_change * 100:.3f}%")
        logger.info(f"[SIGNAL] 当前价格: ${last_price:,.2f}")
        logger.info(f"[SIGNAL] 距Chainlink更新: {time_since_last_chainlink:.0f}秒")
        logger.info(f"[SIGNAL] 市场剩余: {market.end_time - now:.0f}秒")
        logger.info(f"{'='*60}\n")
        
        self.stats['signals_generated'] += 1
        
        await self._execute_trade(direction, price_change)

    async def _execute_trade(self, direction: str, price_change: float):
        if not self.current_market:
            return
        
        market = self.current_market
        
        if direction == "UP":
            token_id = market.up_token_id
            expected_price = market.current_up_price
        else:
            token_id = market.down_token_id
            expected_price = market.current_down_price
        
        confidence = min(abs(price_change) / self.min_price_change, 2.0)
        trade_amount = min(self.trade_amount * confidence, self.trade_amount * 1.5)
        
        logger.info(f"[TRADE] 执行Chainlink延迟套利:")
        logger.info(f"[TRADE] 方向: {direction}")
        logger.info(f"[TRADE] 金额: ${trade_amount:.2f}")
        logger.info(f"[TRADE] 预期价格: {expected_price:.3f}")
        
        if self._dry_run:
            logger.info(f"[TRADE] 模拟模式 - 不执行实际下单")
            market.trade_direction = direction
            market.trade_amount = trade_amount
            market.trade_price = expected_price
            self.stats['trades_executed'] += 1
            return
        
        try:
            result = await self.trade_executor.place_market_order(
                token_id=token_id,
                side="BUY",
                amount=trade_amount,
                price=expected_price * 1.05,
            )
            
            if result and result.success:
                market.trade_direction = direction
                market.trade_amount = trade_amount
                market.trade_price = result.avg_price
                
                self.stats['trades_executed'] += 1
                
                logger.info(f"[TRADE] 下单成功! 订单ID: {result.order_id}")
                logger.info(f"[TRADE] 成交价格: {result.avg_price:.4f}")
                logger.info(f"[TRADE] 成交金额: ${result.filled_size:.2f}")
            else:
                error = result.error_message if result else "Unknown error"
                logger.warning(f"[TRADE] 下单失败: {error}")
                
        except Exception as e:
            logger.error(f"[TRADE] 下单异常: {e}")

    def get_statistics(self) -> Dict:
        return {
            **self.stats,
            'current_market': self.current_market.market_slug if self.current_market else None,
            'last_chainlink_update': self.last_chainlink_update,
        }

    def print_summary(self):
        logger.info(f"\n{'='*60}")
        logger.info("[STRATEGY] Chainlink延迟套利统计")
        logger.info(f"{'='*60}")
        logger.info(f"生成信号数: {self.stats['signals_generated']}")
        logger.info(f"执行交易数: {self.stats['trades_executed']}")
        logger.info(f"Chainlink更新次数: {self.stats['chainlink_updates']}")
        logger.info(f"胜/负: {self.stats['wins']}/{self.stats['losses']}")
        if self.stats['trades_executed'] > 0:
            win_rate = self.stats['wins'] / self.stats['trades_executed'] * 100
            logger.info(f"胜率: {win_rate:.1f}%")
        logger.info(f"总利润: ${self.stats['total_profit']:.2f}")
        logger.info(f"{'='*60}\n")
