"""
Polymarket套利机器人 - 市场监控模块
使用CLOB API获取市场数据
"""
import asyncio
import aiohttp
import logging
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class MarketInfo:
    condition_id: str
    question_id: str
    market_slug: str
    question: str
    yes_token_id: str
    no_token_id: str
    active: bool
    end_date: str

class MarketMonitor:
    def __init__(self, config, clob_client=None):
        self.config = config
        self.clob_client = clob_client
        self.gamma_api_url = config.GAMMA_API_URL
        
        self.markets: Dict[str, MarketInfo] = {}
        self.session: Optional[aiohttp.ClientSession] = None
        
        self.stats = {
            'markets_scanned': 0,
            'valid_pairs_found': 0,
        }
        
        logger.info("[MONITOR] 市场监控器初始化完成")

    async def start(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        logger.info("[MONITOR] 市场监控器启动")

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None
        logger.info("[MONITOR] 市场监控器停止")

    def set_clob_client(self, clob_client):
        self.clob_client = clob_client

    async def fetch_markets_via_clob(self, limit: int = 100) -> List[Dict]:
        if not self.clob_client:
            logger.error("[MONITOR] CLOB客户端未设置")
            return []
        
        try:
            markets = self.clob_client.get_simplified_markets()
            if markets and 'data' in markets:
                return markets['data'][:limit]
            return []
        except Exception as e:
            logger.error(f"[MONITOR] CLOB获取市场失败: {e}")
            return []

    async def fetch_markets_via_gamma(self, limit: int = 100) -> List[Dict]:
        try:
            url = f"{self.gamma_api_url}/markets"
            params = {'limit': limit, 'active': 'true'}
            
            async with self.session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return data if isinstance(data, list) else []
                else:
                    logger.error(f"[MONITOR] Gamma API请求失败: HTTP {response.status}")
                    return []
        except Exception as e:
            logger.error(f"[MONITOR] Gamma API获取市场异常: {e}")
            return []

    async def fetch_market_by_slug(self, slug: str) -> Optional[Dict]:
        try:
            url = f"{self.gamma_api_url}/markets"
            params = {'slug': slug}
            
            async with self.session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and len(data) > 0:
                        return data[0]
                return None
        except Exception as e:
            logger.error(f"[MONITOR] 获取市场 {slug} 失败: {e}")
            return None

    def parse_market_tokens(self, market_data: Dict) -> Optional[Tuple[str, str]]:
        try:
            tokens = market_data.get('tokens', [])
            if not tokens or len(tokens) < 2:
                return None
            
            yes_token = None
            no_token = None
            
            for token in tokens:
                outcome = token.get('outcome', '').upper()
                token_id = token.get('token_id', '')
                
                if outcome == 'YES':
                    yes_token = token_id
                elif outcome == 'NO':
                    no_token = token_id
            
            if yes_token and no_token:
                return yes_token, no_token
            
            token_ids = [t.get('token_id', '') for t in tokens]
            if len(token_ids) >= 2:
                return token_ids[0], token_ids[1]
            
            return None
            
        except Exception as e:
            logger.warning(f"[MONITOR] 解析市场tokens失败: {e}")
            return None

    async def scan_and_register_markets(self, arbitrage_engine, max_markets: int = 50):
        logger.info(f"[MONITOR] 开始扫描市场，最多 {max_markets} 个...")
        
        markets_data = []
        
        if self.clob_client:
            markets_data = await self.fetch_markets_via_clob(limit=max_markets)
        
        if not markets_data:
            logger.info("[MONITOR] CLOB获取失败，尝试Gamma API...")
            markets_data = await self.fetch_markets_via_gamma(limit=max_markets)
        
        if not markets_data:
            logger.warning("[MONITOR] 无法获取市场数据")
            return 0
        
        registered = 0
        for market_data in markets_data:
            try:
                tokens = self.parse_market_tokens(market_data)
                if not tokens:
                    continue
                
                yes_token, no_token = tokens
                
                condition_id = market_data.get('condition_id', '')
                question_id = market_data.get('question_id', '')
                slug = market_data.get('market_slug', market_data.get('slug', ''))
                question = market_data.get('question', '')
                active = market_data.get('active', True)
                end_date = market_data.get('end_date', '')
                
                if not slug:
                    slug = f"market_{condition_id[:8]}"
                
                market_info = MarketInfo(
                    condition_id=condition_id,
                    question_id=question_id,
                    market_slug=slug,
                    question=question,
                    yes_token_id=yes_token,
                    no_token_id=no_token,
                    active=active,
                    end_date=end_date,
                )
                
                self.markets[slug] = market_info
                
                arbitrage_engine.register_market_pair(
                    yes_token_id=yes_token,
                    no_token_id=no_token,
                    market_slug=slug,
                    condition_id=condition_id,
                )
                
                registered += 1
                self.stats['valid_pairs_found'] += 1
                
            except Exception as e:
                logger.warning(f"[MONITOR] 处理市场数据失败: {e}")
                continue
        
        self.stats['markets_scanned'] = len(markets_data)
        logger.info(f"[MONITOR] 扫描完成: 扫描 {len(markets_data)} 个市场，注册 {registered} 个有效市场对")
        
        return registered

    async def register_specific_markets(self, arbitrage_engine, market_slugs: List[str]):
        logger.info(f"[MONITOR] 注册指定市场: {market_slugs}")
        
        registered = 0
        for slug in market_slugs:
            try:
                market_data = await self.fetch_market_by_slug(slug)
                if not market_data:
                    logger.warning(f"[MONITOR] 未找到市场: {slug}")
                    continue
                
                tokens = self.parse_market_tokens(market_data)
                if not tokens:
                    logger.warning(f"[MONITOR] 市场 {slug} 无法解析tokens")
                    continue
                
                yes_token, no_token = tokens
                condition_id = market_data.get('condition_id', '')
                question = market_data.get('question', '')
                
                market_info = MarketInfo(
                    condition_id=condition_id,
                    question_id='',
                    market_slug=slug,
                    question=question,
                    yes_token_id=yes_token,
                    no_token_id=no_token,
                    active=True,
                    end_date='',
                )
                
                self.markets[slug] = market_info
                
                arbitrage_engine.register_market_pair(
                    yes_token_id=yes_token,
                    no_token_id=no_token,
                    market_slug=slug,
                    condition_id=condition_id,
                )
                
                registered += 1
                self.stats['valid_pairs_found'] += 1
                
            except Exception as e:
                logger.error(f"[MONITOR] 注册市场 {slug} 失败: {e}")
                continue
        
        logger.info(f"[MONITOR] 指定市场注册完成: {registered}/{len(market_slugs)}")
        return registered

    def get_statistics(self) -> Dict:
        return {
            **self.stats,
            'markets_cached': len(self.markets),
        }

    def print_summary(self):
        logger.info(f"\n{'='*60}")
        logger.info("[MONITOR] 市场监控器统计摘要")
        logger.info(f"{'='*60}")
        logger.info(f"已扫描市场: {self.stats['markets_scanned']}")
        logger.info(f"有效市场对: {self.stats['valid_pairs_found']}")
        logger.info(f"缓存市场数: {len(self.markets)}")
        logger.info(f"{'='*60}\n")
