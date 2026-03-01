"""
Polymarket套利机器人 - 交易执行模块 (优化版)
缓存市场参数以减少下单延迟
"""
import asyncio
import time
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass
import aiohttp

logger = logging.getLogger(__name__)

@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    filled_size: float = 0.0
    avg_price: float = 0.0
    error_message: str = ""

@dataclass
class TokenInfo:
    tick_size: float
    neg_risk: bool
    fee_rate: float
    last_updated: float

class FastTradeExecutor:
    def __init__(self, config, clob_client):
        self.config = config
        self.clob_client = clob_client
        
        self.pending_orders: Dict[str, Dict] = {}
        self.order_history: list = []
        
        self.token_cache: Dict[str, TokenInfo] = {}
        self.cache_ttl = 3600
        
        self.session: Optional[aiohttp.ClientSession] = None
        
        self.stats = {
            'orders_placed': 0,
            'orders_filled': 0,
            'orders_failed': 0,
            'total_volume': 0.0,
            'cache_hits': 0,
            'cache_misses': 0,
        }
        
        logger.info("[TRADE] 快速交易执行器初始化完成")

    async def init_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def prefetch_token_info(self, token_id: str) -> TokenInfo:
        if token_id in self.token_cache:
            cached = self.token_cache[token_id]
            if time.time() - cached.last_updated < self.cache_ttl:
                self.stats['cache_hits'] += 1
                return cached
        
        self.stats['cache_misses'] += 1
        
        await self.init_session()
        
        base_url = "https://clob.polymarket.com"
        
        async def fetch(endpoint):
            url = f"{base_url}{endpoint}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        
        tick_size = 0.01
        neg_risk = False
        fee_rate = 0.0
        
        tick_data, neg_data, fee_data = await asyncio.gather(
            fetch(f"/tick-size?token_id={token_id}"),
            fetch(f"/neg-risk?token_id={token_id}"),
            fetch(f"/fee-rate?token_id={token_id}"),
            return_exceptions=True
        )
        
        if tick_data and not isinstance(tick_data, Exception):
            tick_size = float(tick_data.get('minimum_tick_size', 0.01))
        
        if neg_data and not isinstance(neg_data, Exception):
            neg_risk = neg_data.get('neg_risk', False)
        
        if fee_data and not isinstance(fee_data, Exception):
            fee_rate = float(fee_data.get('fee_rate', 0.0))
        
        info = TokenInfo(
            tick_size=tick_size,
            neg_risk=neg_risk,
            fee_rate=fee_rate,
            last_updated=time.time()
        )
        
        self.token_cache[token_id] = info
        logger.info(f"[TRADE] 预获取token参数: {token_id[:20]}... tick={tick_size}, neg_risk={neg_risk}")
        
        return info

    async def prefetch_market(self, up_token_id: str, down_token_id: str):
        start = time.time()
        await asyncio.gather(
            self.prefetch_token_info(up_token_id),
            self.prefetch_token_info(down_token_id),
        )
        elapsed = time.time() - start
        logger.info(f"[TRADE] 市场参数预加载完成: {elapsed*1000:.0f}ms")

    async def place_market_order_fast(
        self,
        token_id: str,
        side: str,
        amount: float,
        price: float,
    ) -> Optional[OrderResult]:
        start = time.time()
        
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL
            
            token_info = await self.prefetch_token_info(token_id)
            
            side_const = BUY if side.upper() == "BUY" else SELL
            
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=side_const,
                price=price,
            )
            
            logger.info(f"[TRADE] 创建市价单: {side} ${amount:.2f} @ {price:.4f}")
            
            signed_order = self.clob_client.create_market_order(order_args)
            
            if not signed_order:
                logger.error("[TRADE] 创建订单签名失败")
                self.stats['orders_failed'] += 1
                return OrderResult(success=False, error_message="签名失败")
            
            response = self.clob_client.post_order(signed_order, orderType=OrderType.FOK)
            
            elapsed = time.time() - start
            
            if response and response.get('orderID'):
                order_id = response.get('orderID', '')
                self.stats['orders_placed'] += 1
                self.stats['orders_filled'] += 1
                self.stats['total_volume'] += amount
                
                logger.info(f"[TRADE] 订单成交: {order_id} (耗时: {elapsed*1000:.0f}ms)")
                
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    filled_size=amount,
                    avg_price=price,
                )
            else:
                error_msg = response.get('error', 'Unknown error') if response else 'No response'
                logger.warning(f"[TRADE] 订单未成交: {error_msg} (耗时: {elapsed*1000:.0f}ms)")
                self.stats['orders_failed'] += 1
                return OrderResult(success=False, error_message=error_msg)
                
        except Exception as e:
            elapsed = time.time() - start
            logger.error(f"[TRADE] 下单异常: {e} (耗时: {elapsed*1000:.0f}ms)")
            self.stats['orders_failed'] += 1
            return OrderResult(success=False, error_message=str(e))

    async def place_market_order(
        self,
        token_id: str,
        side: str,
        amount: float,
        price: float,
    ) -> Optional[OrderResult]:
        return await self.place_market_order_fast(token_id, side, amount, price)

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        order_type: str = "GTC",
    ) -> Optional[OrderResult]:
        start = time.time()
        
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL
            
            token_info = await self.prefetch_token_info(token_id)
            
            side_const = BUY if side.upper() == "BUY" else SELL
            
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side_const,
            )
            
            logger.info(f"[TRADE] 创建限价单: {side} {size:.2f}股 @ {price:.4f}")
            
            signed_order = self.clob_client.create_order(order_args)
            
            if not signed_order:
                logger.error("[TRADE] 创建订单签名失败")
                self.stats['orders_failed'] += 1
                return OrderResult(success=False, error_message="签名失败")
            
            order_type_enum = getattr(OrderType, order_type, OrderType.GTC)
            response = self.clob_client.post_order(signed_order, orderType=order_type_enum)
            
            elapsed = time.time() - start
            
            if response and response.get('orderID'):
                order_id = response.get('orderID', '')
                self.stats['orders_placed'] += 1
                
                logger.info(f"[TRADE] 限价单已挂出: {order_id} (耗时: {elapsed*1000:.0f}ms)")
                
                self.pending_orders[order_id] = {
                    'token_id': token_id,
                    'side': side,
                    'size': size,
                    'price': price,
                    'placed_at': time.time(),
                }
                
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    filled_size=0,
                    avg_price=price,
                )
            else:
                error_msg = response.get('error', 'Unknown error') if response else 'No response'
                logger.warning(f"[TRADE] 限价单失败: {error_msg}")
                self.stats['orders_failed'] += 1
                return OrderResult(success=False, error_message=error_msg)
                
        except Exception as e:
            elapsed = time.time() - start
            logger.error(f"[TRADE] 限价单异常: {e} (耗时: {elapsed*1000:.0f}ms)")
            self.stats['orders_failed'] += 1
            return OrderResult(success=False, error_message=str(e))

    async def cancel_order(self, order_id: str) -> bool:
        try:
            result = self.clob_client.cancel(order_id)
            
            if order_id in self.pending_orders:
                del self.pending_orders[order_id]
            
            logger.info(f"[TRADE] 订单已取消: {order_id}")
            return True
            
        except Exception as e:
            logger.error(f"[TRADE] 取消订单失败: {e}")
            return False

    async def cancel_all_orders(self) -> int:
        try:
            result = self.clob_client.cancel_all()
            cancelled_count = len(self.pending_orders)
            self.pending_orders.clear()
            
            logger.info(f"[TRADE] 已取消所有订单: {cancelled_count}个")
            return cancelled_count
            
        except Exception as e:
            logger.error(f"[TRADE] 批量取消失败: {e}")
            return 0

    async def get_order_status(self, order_id: str) -> Optional[Dict]:
        try:
            order = self.clob_client.get_order(order_id)
            return order
        except Exception as e:
            logger.warning(f"[TRADE] 获取订单状态失败: {e}")
            return None

    async def check_pending_orders(self):
        for order_id, order_info in list(self.pending_orders.items()):
            status = await self.get_order_status(order_id)
            
            if status:
                order_status = status.get('status', '')
                
                if order_status in ['FILLED', 'MATCHED']:
                    self.stats['orders_filled'] += 1
                    filled_size = float(status.get('sizeMatched', 0))
                    self.stats['total_volume'] += filled_size * order_info['price']
                    del self.pending_orders[order_id]
                    logger.info(f"[TRADE] 订单已成交: {order_id}")
                    
                elif order_status in ['CANCELLED', 'EXPIRED']:
                    del self.pending_orders[order_id]
                    logger.info(f"[TRADE] 订单已结束: {order_id} ({order_status})")

    def get_statistics(self) -> Dict:
        return {
            **self.stats,
            'pending_orders': len(self.pending_orders),
            'cached_tokens': len(self.token_cache),
        }

    def print_summary(self):
        logger.info("\n" + "="*60)
        logger.info("[TRADE] 交易执行器统计摘要")
        logger.info("="*60)
        logger.info(f"下单数: {self.stats['orders_placed']}")
        logger.info(f"成交数: {self.stats['orders_filled']}")
        logger.info(f"失败数: {self.stats['orders_failed']}")
        logger.info(f"待处理: {len(self.pending_orders)}")
        logger.info(f"总交易量: ${self.stats['total_volume']:.2f}")
        logger.info(f"缓存命中: {self.stats['cache_hits']}")
        logger.info(f"缓存未命中: {self.stats['cache_misses']}")
        logger.info("="*60 + "\n")

TradeExecutor = FastTradeExecutor
