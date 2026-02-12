import aiohttp
import asyncio
from config import get_config, logger

class EnrichmentService:
    def __init__(self):
        self.config = get_config()
        self.market_map = {} # 内存缓存: token_id -> market_info
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession()

    async def stop(self):
        if self.session:
            await self.session.close()

    async def get_market_info(self, token_id):
        """
        将 Token ID 解析为可读的市场信息
        由于API数据不一致，暂时返回通用信息，让monitor_service.py使用正确的title
        """
        token_id_str = str(token_id)

        # 1. 查缓存
        if token_id_str in self.market_map:
            return self.market_map[token_id_str]

        # 2. 由于API数据不一致，直接返回通用市场信息
        # 让monitor_service.py使用从data-api获取的正确title
        market_info = {
            'market_slug': 'Unknown Market (Using data-api title)',
            'question': 'Market information will be taken from transaction data',
            'outcome': 'UNKNOWN'
        }

        # 写入缓存，避免重复查询
        self.market_map[token_id_str] = market_info
        return market_info

    def enrich_trade_data(self, trade_data):
        """
        增强交易数据，计算金额和方向
        """
        # 这是一个同步包装器，实际数据获取应该是异步的
        # 在主流程中，我们会在获取到基础数据后调用异步的 get_market_info
        pass

    def format_amount(self, amount_wei):
        """
        将 Wei (USDC 6 decimals) 转换为 float
        """
        return float(amount_wei) / 1e6