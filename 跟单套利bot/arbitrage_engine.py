"""
Polymarket套利引擎 - 核心套利逻辑

套利策略:
1. YES/NO套利: 当YES+NO价格之和 < 1时，同时买入两边锁定无风险利润
2. 订单簿套利: 利用买卖价差异常进行套利
3. 价差套利: 监控价格波动，在价差足够大时执行套利
"""
import asyncio
import time
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from decimal import Decimal

logger = logging.getLogger(__name__)

@dataclass
class MarketPair:
    yes_token_id: str
    no_token_id: str
    market_slug: str
    condition_id: str

@dataclass
class PriceQuote:
    token_id: str
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    timestamp: float

@dataclass
class ArbitrageOpportunity:
    opportunity_type: str
    market_slug: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    price_sum: float
    profit_percent: float
    recommended_size: float
    timestamp: float

class ArbitrageEngine:
    def __init__(self, config, clob_client, trade_executor):
        self.config = config
        self.clob_client = clob_client
        self.trade_executor = trade_executor
        
        self.market_pairs: Dict[str, MarketPair] = {}
        self.price_cache: Dict[str, PriceQuote] = {}
        self.opportunity_history: List[ArbitrageOpportunity] = []
        
        self.stats = {
            'opportunities_found': 0,
            'trades_executed': 0,
            'total_profit': 0.0,
            'total_volume': 0.0,
            'failed_trades': 0,
        }
        
        self._running = False
        logger.info("[ARBITRAGE] 套利引擎初始化完成")

    async def start(self):
        self._running = True
        logger.info("[ARBITRAGE] 套利引擎启动")

    async def stop(self):
        self._running = False
        logger.info("[ARBITRAGE] 套利引擎停止")

    def register_market_pair(self, yes_token_id: str, no_token_id: str, 
                             market_slug: str, condition_id: str = ""):
        self.market_pairs[market_slug] = MarketPair(
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_slug=market_slug,
            condition_id=condition_id,
        )
        logger.info(f"[ARBITRAGE] 注册市场对: {market_slug}")

    async def get_orderbook_prices(self, token_id: str) -> Optional[PriceQuote]:
        try:
            orderbook = self.clob_client.get_order_book(token_id)
            
            if not orderbook or not orderbook.get('bids') or not orderbook.get('asks'):
                return None
            
            best_bid = orderbook['bids'][0] if orderbook['bids'] else None
            best_ask = orderbook['asks'][0] if orderbook['asks'] else None
            
            if not best_bid or not best_ask:
                return None
            
            return PriceQuote(
                token_id=token_id,
                bid_price=float(best_bid['price']),
                ask_price=float(best_ask['price']),
                bid_size=float(best_bid['size']),
                ask_size=float(best_ask['size']),
                timestamp=time.time(),
            )
        except Exception as e:
            logger.warning(f"[ARBITRAGE] 获取订单簿失败 {token_id[:16]}...: {e}")
            return None

    async def get_midpoint_prices(self, yes_token_id: str, no_token_id: str) -> Tuple[Optional[float], Optional[float]]:
        try:
            yes_mid = self.clob_client.get_midpoint(yes_token_id)
            no_mid = self.clob_client.get_midpoint(no_token_id)
            
            yes_price = float(yes_mid) if yes_mid else None
            no_price = float(no_mid) if no_mid else None
            
            return yes_price, no_price
        except Exception as e:
            logger.warning(f"[ARBITRAGE] 获取中间价失败: {e}")
            return None, None

    async def scan_yes_no_arbitrage(self, market_slug: str) -> Optional[ArbitrageOpportunity]:
        if market_slug not in self.market_pairs:
            return None
        
        pair = self.market_pairs[market_slug]
        
        yes_quote = await self.get_orderbook_prices(pair.yes_token_id)
        no_quote = await self.get_orderbook_prices(pair.no_token_id)
        
        if not yes_quote or not no_quote:
            return None
        
        self.price_cache[pair.yes_token_id] = yes_quote
        self.price_cache[pair.no_token_id] = no_quote
        
        yes_ask = yes_quote.ask_price
        no_ask = no_quote.ask_price
        price_sum = yes_ask + no_ask
        
        if price_sum >= 1.0:
            return None
        
        profit_percent = (1.0 - price_sum) / price_sum * 100
        min_profit = self.config.arbitrage.min_profit_threshold * 100
        
        if profit_percent < min_profit:
            return None
        
        min_size = min(yes_quote.ask_size, no_quote.ask_size)
        max_trade = self.config.arbitrage.max_trade_size
        recommended_size = min(min_size * yes_ask, max_trade)
        recommended_size = max(recommended_size, self.config.arbitrage.min_trade_size)
        
        opportunity = ArbitrageOpportunity(
            opportunity_type="YES_NO_ARBITRAGE",
            market_slug=market_slug,
            yes_token_id=pair.yes_token_id,
            no_token_id=pair.no_token_id,
            yes_price=yes_ask,
            no_price=no_ask,
            price_sum=price_sum,
            profit_percent=profit_percent,
            recommended_size=recommended_size,
            timestamp=time.time(),
        )
        
        self.stats['opportunities_found'] += 1
        self.opportunity_history.append(opportunity)
        
        return opportunity

    async def scan_orderbook_arbitrage(self, token_id: str) -> Optional[Dict]:
        try:
            quote = await self.get_orderbook_prices(token_id)
            if not quote:
                return None
            
            spread = quote.ask_price - quote.bid_price
            spread_percent = spread / quote.bid_price * 100 if quote.bid_price > 0 else 0
            
            if spread_percent > 5.0:
                return {
                    'type': 'ORDERBOOK_SPREAD',
                    'token_id': token_id,
                    'bid': quote.bid_price,
                    'ask': quote.ask_price,
                    'spread_percent': spread_percent,
                    'timestamp': time.time(),
                }
            
            return None
        except Exception as e:
            logger.warning(f"[ARBITRAGE] 订单簿扫描失败: {e}")
            return None

    async def execute_yes_no_arbitrage(self, opportunity: ArbitrageOpportunity) -> bool:
        logger.info(f"\n{'='*60}")
        logger.info(f"[ARBITRAGE] 发现YES/NO套利机会!")
        logger.info(f"[ARBITRAGE] 市场: {opportunity.market_slug}")
        logger.info(f"[ARBITRAGE] YES价格: {opportunity.yes_price:.4f}")
        logger.info(f"[ARBITRAGE] NO价格: {opportunity.no_price:.4f}")
        logger.info(f"[ARBITRAGE] 价格总和: {opportunity.price_sum:.4f}")
        logger.info(f"[ARBITRAGE] 利润率: {opportunity.profit_percent:.2f}%")
        logger.info(f"[ARBITRAGE] 建议金额: ${opportunity.recommended_size:.2f}")
        logger.info(f"{'='*60}\n")
        
        try:
            trade_size = opportunity.recommended_size
            
            yes_shares = trade_size / opportunity.yes_price
            no_shares = trade_size / opportunity.no_price
            
            logger.info(f"[ARBITRAGE] 执行买入YES: {yes_shares:.2f}股 @ {opportunity.yes_price:.4f}")
            yes_result = await self.trade_executor.place_market_order(
                token_id=opportunity.yes_token_id,
                side="BUY",
                amount=trade_size,
                price=opportunity.yes_price * (1 + self.config.arbitrage.max_slippage),
            )
            
            if not yes_result:
                logger.error("[ARBITRAGE] 买入YES失败，放弃套利")
                self.stats['failed_trades'] += 1
                return False
            
            logger.info(f"[ARBITRAGE] 执行买入NO: {no_shares:.2f}股 @ {opportunity.no_price:.4f}")
            no_result = await self.trade_executor.place_market_order(
                token_id=opportunity.no_token_id,
                side="BUY",
                amount=trade_size,
                price=opportunity.no_price * (1 + self.config.arbitrage.max_slippage),
            )
            
            if not no_result:
                logger.error("[ARBITRAGE] 买入NO失败! 已买入YES，需要手动对冲")
                self.stats['failed_trades'] += 1
                return False
            
            profit = trade_size * (1 - opportunity.price_sum)
            self.stats['trades_executed'] += 1
            self.stats['total_profit'] += profit
            self.stats['total_volume'] += trade_size * 2
            
            logger.info(f"[ARBITRAGE] 套利执行成功! 预计利润: ${profit:.2f}")
            logger.info(f"[ARBITRAGE] 累计利润: ${self.stats['total_profit']:.2f}")
            
            return True
            
        except Exception as e:
            logger.error(f"[ARBITRAGE] 套利执行失败: {e}")
            self.stats['failed_trades'] += 1
            return False

    async def scan_all_markets(self) -> List[ArbitrageOpportunity]:
        opportunities = []
        
        for market_slug in self.market_pairs:
            try:
                opp = await self.scan_yes_no_arbitrage(market_slug)
                if opp:
                    opportunities.append(opp)
            except Exception as e:
                logger.warning(f"[ARBITRAGE] 扫描市场 {market_slug} 失败: {e}")
        
        return opportunities

    async def run_continuous_scan(self, interval: float = 2.0):
        logger.info(f"[ARBITRAGE] 开始持续扫描，间隔 {interval}秒")
        
        while self._running:
            try:
                opportunities = await self.scan_all_markets()
                
                for opp in opportunities:
                    if opp.profit_percent >= self.config.arbitrage.min_profit_threshold * 100:
                        await self.execute_yes_no_arbitrage(opp)
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                logger.error(f"[ARBITRAGE] 扫描循环错误: {e}")
                await asyncio.sleep(5)

    def get_statistics(self) -> Dict:
        return {
            **self.stats,
            'markets_watched': len(self.market_pairs),
            'opportunities_in_history': len(self.opportunity_history),
        }

    def print_summary(self):
        logger.info("\n" + "="*60)
        logger.info("[ARBITRAGE] 套利引擎统计摘要")
        logger.info("="*60)
        logger.info(f"监控市场数: {len(self.market_pairs)}")
        logger.info(f"发现机会数: {self.stats['opportunities_found']}")
        logger.info(f"执行交易数: {self.stats['trades_executed']}")
        logger.info(f"失败交易数: {self.stats['failed_trades']}")
        logger.info(f"总利润: ${self.stats['total_profit']:.2f}")
        logger.info(f"总交易量: ${self.stats['total_volume']:.2f}")
        logger.info("="*60 + "\n")
