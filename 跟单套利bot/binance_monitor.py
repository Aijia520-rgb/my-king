"""
Binance WebSocket价格监控
实时获取BTC/USDT价格
"""
import asyncio
import logging
import json
import time
from typing import Callable, Optional
import aiohttp

logger = logging.getLogger(__name__)

class BinancePriceMonitor:
    def __init__(self, on_price_update: Callable[[float, float], None]):
        self.on_price_update = on_price_update
        
        self.ws_url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        
        self._running = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 30
        
        self.stats = {
            'messages_received': 0,
            'reconnects': 0,
            'last_price': 0.0,
            'last_update': 0.0,
        }
        
        logger.info("[BINANCE] 价格监控器初始化")

    async def start(self):
        self._running = True
        logger.info("[BINANCE] 启动WebSocket连接...")
        
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.error(f"[BINANCE] 连接错误: {e}")
                self.stats['reconnects'] += 1
                
                if self._running:
                    logger.info(f"[BINANCE] {self._reconnect_delay}秒后重连...")
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def stop(self):
        self._running = False
        
        if self.ws:
            await self.ws.close()
        
        if self.session:
            await self.session.close()
        
        logger.info("[BINANCE] 价格监控器停止")

    async def _connect(self):
        self.session = aiohttp.ClientSession()
        
        try:
            async with self.session.ws_connect(self.ws_url) as ws:
                self.ws = ws
                self._reconnect_delay = 1
                
                logger.info("[BINANCE] WebSocket连接成功")
                
                async for msg in ws:
                    if not self._running:
                        break
                    
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"[BINANCE] WebSocket错误: {ws.exception()}")
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        logger.warning("[BINANCE] WebSocket连接已关闭")
                        break
        except Exception as e:
            logger.error(f"[BINANCE] WebSocket连接异常: {e}")
            raise

    async def _handle_message(self, data: str):
        try:
            trade = json.loads(data)
            
            price = float(trade.get('p', 0))
            volume = float(trade.get('q', 0))
            timestamp = trade.get('T', time.time() * 1000) / 1000
            
            if price > 0:
                self.stats['messages_received'] += 1
                self.stats['last_price'] = price
                self.stats['last_update'] = time.time()
                
                if self.on_price_update:
                    if asyncio.iscoroutinefunction(self.on_price_update):
                        await self.on_price_update(price, volume)
                    else:
                        self.on_price_update(price, volume)
                        
        except Exception as e:
            logger.warning(f"[BINANCE] 解析消息失败: {e}")

    def get_statistics(self) -> dict:
        return {
            **self.stats,
            'connected': self.ws is not None and not self.ws.closed,
        }

    def print_summary(self):
        logger.info(f"\n{'='*60}")
        logger.info("[BINANCE] 价格监控统计")
        logger.info(f"{'='*60}")
        logger.info(f"接收消息数: {self.stats['messages_received']}")
        logger.info(f"重连次数: {self.stats['reconnects']}")
        logger.info(f"最新价格: ${self.stats['last_price']:,.2f}")
        logger.info(f"{'='*60}\n")
