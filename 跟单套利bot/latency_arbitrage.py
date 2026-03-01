"""
延迟套利策略 - 利用Chainlink价格延迟抢先交易

策略逻辑:
1. 监控Binance BTC/USDT实时价格（毫秒级）
2. 当检测到价格快速变动时
3. 在Chainlink价格反映到Polymarket前，抢先买入对应方向
4. 等待市场结算获利

关键:
- Binance价格比Chainlink快几秒
- 5分钟市场每5分钟结算一次
- 需要在市场开始后的前几秒内做出判断
"""
import asyncio
import logging
import time
import json
from dataclasses import dataclass
from typing import Optional, Dict, List, Callable
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

@dataclass
class PriceTick:
    source: str
    price: float
    timestamp: float
    volume: float = 0.0

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

class LatencyArbitrageStrategy:
    def __init__(self, config, clob_client, trade_executor, dry_run: bool = False):
        self.config = config
        self.clob_client = clob_client
        self.trade_executor = trade_executor
        self._dry_run = dry_run
        
        self.binance_prices: List[PriceTick] = []
        self.chainlink_prices: List[PriceTick] = []
        
        self.current_market: Optional[MarketSession] = None
        self.market_history: List[MarketSession] = []
        
        self.stats = {
            'signals_generated': 0,
            'trades_executed': 0,
            'wins': 0,
            'losses': 0,
            'total_profit': 0.0,
        }
        
        self._running = False
        self._callbacks: List[Callable] = []
        
        self.price_change_threshold = getattr(config.arbitrage, 'price_change_threshold', 0.001)
        self.lookback_seconds = getattr(config.arbitrage, 'lookback_seconds', 5)
        self.min_confidence = getattr(config.arbitrage, 'min_confidence', 0.6)
        
        logger.info(f"[STRATEGY] 延迟套利策略初始化")
        logger.info(f"[STRATEGY] 价格变动阈值: {self.price_change_threshold * 100:.2f}%")
        logger.info(f"[STRATEGY] 回看时间: {self.lookback_seconds}秒")

    def add_callback(self, callback: Callable):
        self._callbacks.append(callback)

    async def start(self):
        self._running = True
        logger.info("[STRATEGY] 延迟套利策略启动")

    async def stop(self):
        self._running = False
        logger.info("[STRATEGY] 延迟套利策略停止")

    def update_binance_price(self, price: float, volume: float = 0.0):
        tick = PriceTick(
            source='binance',
            price=price,
            timestamp=time.time(),
            volume=volume,
        )
        self.binance_prices.append(tick)
        
        while len(self.binance_prices) > 1000:
            self.binance_prices.pop(0)
        
        if self._running and self.current_market:
            try:
                asyncio.create_task(self._analyze_price_movement())
            except Exception as e:
                logger.warning(f"[STRATEGY] 价格分析任务创建失败: {e}")

    def update_chainlink_price(self, price: float):
        tick = PriceTick(
            source='chainlink',
            price=price,
            timestamp=time.time(),
        )
        self.chainlink_prices.append(tick)
        
        while len(self.chainlink_prices) > 100:
            self.chainlink_prices.pop(0)

    def set_current_market(self, market: MarketSession):
        if self.current_market:
            self.market_history.append(self.current_market)
        
        self.current_market = market
        logger.info(f"[STRATEGY] 设置当前市场: {market.market_slug}")
        logger.info(f"[STRATEGY] 开始时间: {datetime.fromtimestamp(market.start_time, tz=timezone.utc)}")
        logger.info(f"[STRATEGY] 结束时间: {datetime.fromtimestamp(market.end_time, tz=timezone.utc)}")

    async def _analyze_price_movement(self):
        if not self.current_market:
            return
        
        now = time.time()
        market = self.current_market
        
        if now < market.start_time or now > market.end_time:
            return
        
        time_in_market = now - market.start_time
        remaining_time = market.end_time - now
        
        if remaining_time < 10:
            return
        
        recent_prices = [
            p for p in self.binance_prices 
            if p.timestamp >= now - self.lookback_seconds
        ]
        
        if len(recent_prices) < 3:
            return
        
        first_price = recent_prices[0].price
        last_price = recent_prices[-1].price
        price_change = (last_price - first_price) / first_price
        
        if market.start_price is None:
            market.start_price = first_price
            logger.info(f"[STRATEGY] 市场起始价格: ${first_price:,.2f}")
        
        if abs(price_change) < self.price_change_threshold:
            return
        
        direction = "UP" if price_change > 0 else "DOWN"
        confidence = min(abs(price_change) / self.price_change_threshold, 1.0)
        
        if confidence < self.min_confidence:
            return
        
        logger.info(f"\n{'='*60}")
        logger.info(f"[SIGNAL] 检测到价格变动!")
        logger.info(f"[SIGNAL] 方向: {direction}")
        logger.info(f"[SIGNAL] 变动幅度: {price_change * 100:.3f}%")
        logger.info(f"[SIGNAL] 信心度: {confidence:.2f}")
        logger.info(f"[SIGNAL] 当前价格: ${last_price:,.2f}")
        logger.info(f"[SIGNAL] 市场已运行: {time_in_market:.1f}秒")
        logger.info(f"[SIGNAL] 剩余时间: {remaining_time:.1f}秒")
        logger.info(f"{'='*60}\n")
        
        self.stats['signals_generated'] += 1
        
        await self._execute_trade(direction, confidence, last_price)

    async def _execute_trade(self, direction: str, confidence: float, current_price: float):
        if not self.current_market:
            return
        
        market = self.current_market
        
        if direction == "UP":
            token_id = market.up_token_id
            expected_price = market.current_up_price
        else:
            token_id = market.down_token_id
            expected_price = market.current_down_price
        
        trade_size = self._calculate_trade_size(confidence)
        
        logger.info(f"[TRADE] 准备下单:")
        logger.info(f"[TRADE] 方向: {direction}")
        logger.info(f"[TRADE] Token: {token_id[:20]}...")
        logger.info(f"[TRADE] 金额: ${trade_size:.2f}")
        logger.info(f"[TRADE] 预期价格: {expected_price:.3f}")
        
        if self._dry_run:
            logger.info(f"[TRADE] 模拟模式 - 不执行实际下单")
            self.stats['trades_executed'] += 1
            return
        
        try:
            result = await self.trade_executor.place_market_order(
                token_id=token_id,
                side="BUY",
                amount=trade_size,
                price=expected_price * 1.02,
            )
            
            if result:
                self.stats['trades_executed'] += 1
                logger.info(f"[TRADE] 下单成功!")
                
                for callback in self._callbacks:
                    await callback({
                        'type': 'trade_executed',
                        'direction': direction,
                        'size': trade_size,
                        'price': current_price,
                        'market': market.market_slug,
                    })
            else:
                logger.warning(f"[TRADE] 下单失败")
                
        except Exception as e:
            logger.error(f"[TRADE] 下单异常: {e}")

    def _calculate_trade_size(self, confidence: float) -> float:
        base_size = self.config.arbitrage.min_trade_size
        max_size = self.config.arbitrage.max_trade_size
        
        size = base_size + (max_size - base_size) * confidence
        return min(size, max_size)

    def record_result(self, won: bool, profit: float):
        if won:
            self.stats['wins'] += 1
        else:
            self.stats['losses'] += 1
        
        self.stats['total_profit'] += profit
        
        win_rate = self.stats['wins'] / max(self.stats['trades_executed'], 1) * 100
        logger.info(f"[STATS] 胜率: {win_rate:.1f}% ({self.stats['wins']}/{self.stats['trades_executed']})")
        logger.info(f"[STATS] 总利润: ${self.stats['total_profit']:.2f}")

    def get_statistics(self) -> Dict:
        return {
            **self.stats,
            'win_rate': self.stats['wins'] / max(self.stats['trades_executed'], 1),
            'current_market': self.current_market.market_slug if self.current_market else None,
        }

    def print_summary(self):
        logger.info(f"\n{'='*60}")
        logger.info("[STRATEGY] 延迟套利策略统计")
        logger.info(f"{'='*60}")
        logger.info(f"生成信号数: {self.stats['signals_generated']}")
        logger.info(f"执行交易数: {self.stats['trades_executed']}")
        logger.info(f"胜/负: {self.stats['wins']}/{self.stats['losses']}")
        if self.stats['trades_executed'] > 0:
            win_rate = self.stats['wins'] / self.stats['trades_executed'] * 100
            logger.info(f"胜率: {win_rate:.1f}%")
        logger.info(f"总利润: ${self.stats['total_profit']:.2f}")
        logger.info(f"{'='*60}\n")
