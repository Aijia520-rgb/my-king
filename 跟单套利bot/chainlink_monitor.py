"""
Chainlink价格监控
通过Chainlink Data Feeds获取BTC/USD价格
"""
import asyncio
import logging
import time
import aiohttp
from typing import Callable, Optional

logger = logging.getLogger(__name__)

class ChainlinkPriceMonitor:
    def __init__(self, on_price_update: Callable[[float], None]):
        self.on_price_update = on_price_update
        
        self.api_url = "https://data.chain.link/streams/btc-usd"
        self.fallback_url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        
        self.session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self.poll_interval = 1
        
        self.stats = {
            'updates_received': 0,
            'errors': 0,
            'last_price': 0.0,
            'last_update': 0.0,
        }
        
        logger.info("[CHAINLINK] 价格监控器初始化")

    async def start(self):
        self._running = True
        self.session = aiohttp.ClientSession()
        
        logger.info("[CHAINLINK] 启动价格轮询...")
        
        while self._running:
            try:
                price = await self._fetch_price()
                
                if price and price > 0:
                    self.stats['updates_received'] += 1
                    self.stats['last_price'] = price
                    self.stats['last_update'] = time.time()
                    
                    if self.on_price_update:
                        if asyncio.iscoroutinefunction(self.on_price_update):
                            await self.on_price_update(price)
                        else:
                            self.on_price_update(price)
                
            except Exception as e:
                self.stats['errors'] += 1
                logger.warning(f"[CHAINLINK] 获取价格失败: {e}")
            
            await asyncio.sleep(self.poll_interval)

    async def stop(self):
        self._running = False
        
        if self.session:
            await self.session.close()
        
        logger.info("[CHAINLINK] 价格监控器停止")

    async def _fetch_price(self) -> Optional[float]:
        try:
            async with self.session.get(self.api_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    if isinstance(data, dict):
                        if 'answer' in data:
                            return float(data['answer']) / 1e8
                        elif 'price' in data:
                            return float(data['price'])
                        elif 'data' in data and isinstance(data['data'], dict):
                            if 'answer' in data['data']:
                                return float(data['data']['answer']) / 1e8
                            elif 'price' in data['data']:
                                return float(data['data']['price'])
        except Exception as e:
            logger.debug(f"[CHAINLINK] 主API失败: {e}")
        
        try:
            async with self.session.get(self.fallback_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if 'bitcoin' in data and 'usd' in data['bitcoin']:
                        return float(data['bitcoin']['usd'])
        except Exception as e:
            logger.debug(f"[CHAINLINK] 备用API失败: {e}")
        
        return None

    def get_statistics(self) -> dict:
        return {
            **self.stats,
            'running': self._running,
        }

    def print_summary(self):
        logger.info(f"\n{'='*60}")
        logger.info("[CHAINLINK] 价格监控统计")
        logger.info(f"{'='*60}")
        logger.info(f"更新次数: {self.stats['updates_received']}")
        logger.info(f"错误次数: {self.stats['errors']}")
        logger.info(f"最新价格: ${self.stats['last_price']:,.2f}")
        logger.info(f"{'='*60}\n")
