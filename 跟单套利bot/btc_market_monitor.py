"""
Polymarket BTC 5分钟市场自动发现和监控
直接查询当前活跃的event获取市场数据
"""
import asyncio
import logging
import time
import aiohttp
import json
from typing import Optional, Dict, List
from dataclasses import dataclass
from datetime import datetime, timezone
import math

logger = logging.getLogger(__name__)

@dataclass
class BTCMarket:
    slug: str
    up_token_id: str
    down_token_id: str
    start_time: float
    end_time: float
    up_price: float
    down_price: float
    accepting_orders: bool

class BTCMarketMonitor:
    def __init__(self, config, clob_client=None):
        self.config = config
        self.clob_client = clob_client
        
        self.gamma_api_url = "https://gamma-api.polymarket.com"
        self.session: Optional[aiohttp.ClientSession] = None
        
        self.current_market: Optional[BTCMarket] = None
        self.next_market: Optional[BTCMarket] = None
        self.market_history: List[BTCMarket] = []
        
        self._running = False
        self._poll_interval = 3
        
        self.stats = {
            'markets_discovered': 0,
            'markets_traded': 0,
        }
        
        self._on_new_market = None
        
        logger.info("[BTC-MARKET] BTC市场监控器初始化")

    def set_clob_client(self, clob_client):
        self.clob_client = clob_client

    def on_new_market(self, callback):
        self._on_new_market = callback

    async def start(self):
        self._running = True
        self.session = aiohttp.ClientSession()
        
        logger.info("[BTC-MARKET] 启动市场监控...")
        
        while self._running:
            try:
                await self._discover_current_market()
                await asyncio.sleep(self._poll_interval)
            except Exception as e:
                logger.error(f"[BTC-MARKET] 监控错误: {e}")
                await asyncio.sleep(1)

    async def stop(self):
        self._running = False
        
        if self.session:
            await self.session.close()
        
        logger.info("[BTC-MARKET] 市场监控器停止")

    async def _discover_current_market(self):
        try:
            now = time.time()
            
            current_ts = math.floor(now / 300) * 300
            next_ts = current_ts + 300
            
            current_slug = f"btc-updown-5m-{current_ts}"
            next_slug = f"btc-updown-5m-{next_ts}"
            
            current_market = await self._fetch_market(current_slug)
            
            if current_market:
                if not self.current_market or current_market.slug != self.current_market.slug:
                    self.current_market = current_market
                    self.stats['markets_discovered'] += 1
                    
                    remaining = current_market.end_time - now
                    
                    logger.info(f"\n{'='*60}")
                    logger.info(f"[BTC-MARKET] 发现活跃市场!")
                    logger.info(f"[BTC-MARKET] Slug: {current_market.slug}")
                    logger.info(f"[BTC-MARKET] 开始: {datetime.fromtimestamp(current_market.start_time, tz=timezone.utc)}")
                    logger.info(f"[BTC-MARKET] 结束: {datetime.fromtimestamp(current_market.end_time, tz=timezone.utc)}")
                    logger.info(f"[BTC-MARKET] Up价格: {current_market.up_price:.3f}")
                    logger.info(f"[BTC-MARKET] Down价格: {current_market.down_price:.3f}")
                    logger.info(f"[BTC-MARKET] 剩余时间: {remaining:.0f}秒")
                    logger.info(f"{'='*60}\n")
                    
                    if self._on_new_market:
                        await self._on_new_market(current_market)
            else:
                next_market = await self._fetch_market(next_slug)
                if next_market:
                    self.next_market = next_market
                    wait_time = next_market.start_time - now
                    logger.info(f"[BTC-MARKET] 等待下一个市场: {next_slug} ({wait_time:.0f}秒后开始)")
                    
        except Exception as e:
            logger.error(f"[BTC-MARKET] 发现市场失败: {e}")
            import traceback
            traceback.print_exc()

    async def _fetch_market(self, slug: str) -> Optional[BTCMarket]:
        try:
            url = f"{self.gamma_api_url}/events"
            params = {'slug': slug}
            
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                
                data = await resp.json()
            
            if not data:
                return None
            
            event = data[0] if isinstance(data, list) else data
            markets = event.get('markets', [])
            
            if not markets:
                return None
            
            for m in markets:
                market = self._parse_market(m)
                if market:
                    return market
            
            return None
            
        except Exception as e:
            logger.debug(f"[BTC-MARKET] 获取市场 {slug} 失败: {e}")
            return None

    def _parse_market(self, data: Dict) -> Optional[BTCMarket]:
        try:
            slug = data.get('slug', '')
            if not slug.startswith('btc-updown-5m'):
                return None
            
            accepting = data.get('acceptingOrders', False)
            closed = data.get('closed', True)
            
            if closed and not accepting:
                return None
            
            end_date = data.get('endDate', '')
            
            if end_date:
                end_time = datetime.fromisoformat(end_date.replace('Z', '+00:00')).timestamp()
            else:
                return None
            
            try:
                ts_str = slug.split('-')[-1]
                start_time = int(ts_str)
            except:
                start_time = end_time - 300
            
            clob_token_ids_str = data.get('clobTokenIds', '')
            if not clob_token_ids_str:
                return None
            
            try:
                token_ids = json.loads(clob_token_ids_str)
            except:
                return None
            
            if len(token_ids) < 2:
                return None
            
            up_token_id = token_ids[0]
            down_token_id = token_ids[1]
            
            outcome_prices_str = data.get('outcomePrices', '[0.5, 0.5]')
            try:
                prices = json.loads(outcome_prices_str)
                up_price = float(prices[0]) if len(prices) > 0 else 0.5
                down_price = float(prices[1]) if len(prices) > 1 else 0.5
            except:
                up_price = 0.5
                down_price = 0.5
            
            return BTCMarket(
                slug=slug,
                up_token_id=up_token_id,
                down_token_id=down_token_id,
                start_time=start_time,
                end_time=end_time,
                up_price=up_price,
                down_price=down_price,
                accepting_orders=accepting,
            )
            
        except Exception as e:
            logger.warning(f"[BTC-MARKET] 解析市场失败: {e}")
            return None

    async def get_market_prices(self, market: BTCMarket) -> Dict:
        if not self.clob_client:
            return {'up': market.up_price, 'down': market.down_price}
        
        try:
            up_book = self.clob_client.get_order_book(market.up_token_id)
            down_book = self.clob_client.get_order_book(market.down_token_id)
            
            up_price = market.up_price
            down_price = market.down_price
            
            if up_book and up_book.get('asks'):
                up_price = float(up_book['asks'][0]['price'])
            
            if down_book and down_book.get('asks'):
                down_price = float(down_book['asks'][0]['price'])
            
            return {'up': up_price, 'down': down_price}
            
        except Exception as e:
            logger.warning(f"[BTC-MARKET] 获取价格失败: {e}")
            return {'up': market.up_price, 'down': market.down_price}

    def get_statistics(self) -> Dict:
        return {
            **self.stats,
            'current_market': self.current_market.slug if self.current_market else None,
            'markets_history': len(self.market_history),
        }

    def print_summary(self):
        logger.info(f"\n{'='*60}")
        logger.info("[BTC-MARKET] 市场监控统计")
        logger.info(f"{'='*60}")
        logger.info(f"发现市场数: {self.stats['markets_discovered']}")
        logger.info(f"交易市场数: {self.stats['markets_traded']}")
        if self.current_market:
            remaining = max(0, self.current_market.end_time - time.time())
            logger.info(f"当前市场: {self.current_market.slug}")
            logger.info(f"剩余时间: {remaining:.0f}秒")
        logger.info(f"{'='*60}\n")
