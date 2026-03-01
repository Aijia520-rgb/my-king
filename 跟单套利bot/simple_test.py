"""简单测试 - 验证价格监控和信号检测"""
import asyncio
import time
import logging
import aiohttp
import json
import math
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)

class SimpleBTCArbitrage:
    def __init__(self):
        self.binance_prices = []
        self.current_market = None
        self.price_change_threshold = 0.001
        self.lookback_seconds = 5
        self._running = False
        
    async def monitor_binance(self):
        ws_url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                logger.info("[BINANCE] WebSocket连接成功")
                
                async for msg in ws:
                    if not self._running:
                        break
                    
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        price = float(data.get('p', 0))
                        
                        if price > 0:
                            self.binance_prices.append({
                                'price': price,
                                'timestamp': time.time()
                            })
                            
                            while len(self.binance_prices) > 100:
                                self.binance_prices.pop(0)
                            
                            await self.analyze_price()
    
    async def monitor_market(self):
        gamma_api = "https://gamma-api.polymarket.com"
        
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    now = time.time()
                    current_ts = math.floor(now / 300) * 300
                    slug = f"btc-updown-5m-{current_ts}"
                    
                    url = f"{gamma_api}/events"
                    params = {'slug': slug}
                    
                    async with session.get(url, params=params) as resp:
                        data = await resp.json()
                    
                    if data:
                        event = data[0] if isinstance(data, list) else data
                        markets = event.get('markets', [])
                        
                        for m in markets:
                            if m.get('slug', '').startswith('btc-updown-5m'):
                                accepting = m.get('acceptingOrders', False)
                                closed = m.get('closed', True)
                                
                                if not closed and accepting:
                                    end_date = m.get('endDate', '')
                                    if end_date:
                                        end_time = datetime.fromisoformat(end_date.replace('Z', '+00:00')).timestamp()
                                        remaining = end_time - now
                                        
                                        if remaining > 0:
                                            if not self.current_market or self.current_market['slug'] != m.get('slug'):
                                                self.current_market = {
                                                    'slug': m.get('slug'),
                                                    'end_time': end_time,
                                                    'up_price': 0.5,
                                                    'down_price': 0.5
                                                }
                                                logger.info(f"\n{'='*60}")
                                                logger.info(f"[MARKET] 发现活跃市场: {m.get('slug')}")
                                                logger.info(f"[MARKET] 剩余时间: {remaining:.0f}秒")
                                                logger.info(f"{'='*60}\n")
                    
                    await asyncio.sleep(5)
                    
                except Exception as e:
                    logger.error(f"[MARKET] 监控错误: {e}")
                    await asyncio.sleep(1)
    
    async def analyze_price(self):
        if not self.current_market:
            return
        
        now = time.time()
        
        if now > self.current_market['end_time'] - 10:
            return
        
        recent = [p for p in self.binance_prices if p['timestamp'] >= now - self.lookback_seconds]
        
        if len(recent) < 3:
            return
        
        first = recent[0]['price']
        last = recent[-1]['price']
        change = (last - first) / first
        
        if abs(change) >= self.price_change_threshold:
            direction = "UP" if change > 0 else "DOWN"
            
            logger.info(f"\n{'='*60}")
            logger.info(f"[SIGNAL] 检测到价格变动!")
            logger.info(f"[SIGNAL] 方向: {direction}")
            logger.info(f"[SIGNAL] 变动幅度: {change * 100:.3f}%")
            logger.info(f"[SIGNAL] 当前价格: ${last:,.2f}")
            logger.info(f"[SIGNAL] 市场: {self.current_market['slug']}")
            logger.info(f"{'='*60}\n")
    
    async def run(self):
        self._running = True
        
        logger.info("="*60)
        logger.info("[BOT] BTC延迟套利测试启动")
        logger.info("="*60)
        
        tasks = [
            asyncio.create_task(self.monitor_binance()),
            asyncio.create_task(self.monitor_market()),
        ]
        
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    bot = SimpleBTCArbitrage()
    asyncio.run(bot.run())
