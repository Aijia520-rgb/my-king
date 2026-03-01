"""
Polymarket套利机器人 - 配置模块
复用跟单项目的API配置，支持代理钱包模式
"""
import os
import sys
import logging
from dotenv import load_dotenv
from dataclasses import dataclass
from typing import List, Optional

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

@dataclass
class ArbitrageConfig:
    min_profit_threshold: float = 0.005
    min_trade_size: float = 10.0
    max_trade_size: float = 500.0
    max_position_size: float = 1000.0
    price_check_interval: float = 2.0
    order_timeout: int = 30
    max_slippage: float = 0.01
    price_change_threshold: float = 0.001
    lookback_seconds: int = 5
    min_confidence: float = 0.6

@dataclass
class RiskConfig:
    max_total_exposure: float = 5000.0
    max_single_market_exposure: float = 1000.0
    stop_loss_percent: float = 5.0
    daily_loss_limit: float = 200.0
    max_concurrent_positions: int = 10

class Config:
    CLOB_BASE_URL: str = "https://clob.polymarket.com"
    GAMMA_API_URL: str = "https://gamma-api.polymarket.com"
    DATA_API_URL: str = "https://data-api.polymarket.com"
    RPC_URL: str = "https://polygon-mainnet.infura.io/v3/f86eb30a36a147fb9eb117314407dfb6"
    
    CHAIN_ID: int = 137
    
    WALLET_PRIVATE_KEY: str = ""
    LOCAL_WALLET_ADDRESS: str = ""
    PROXY_WALLET_ADDRESS: str = ""
    WALLET_SWITCH: int = 2
    
    _clob_client_instance = None
    
    def __init__(self):
        self.WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
        self.LOCAL_WALLET_ADDRESS = os.getenv("LOCAL_WALLET_ADDRESS", "")
        self.PROXY_WALLET_ADDRESS = os.getenv("PROXY_WALLET_ADDRESS", "")
        self.WALLET_SWITCH = int(os.getenv("WALLET_SWITCH", "2"))
        self.CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
        
        self.CLOB_BASE_URL = os.getenv("CLOB_BASE_URL", "https://clob.polymarket.com")
        self.GAMMA_API_URL = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com")
        self.DATA_API_URL = os.getenv("DATA_API_URL", "https://data-api.polymarket.com")
        self.RPC_URL = os.getenv("POLYGON_RPC_URL", os.getenv("RPC_URL", self.RPC_URL))
        
        self.arbitrage = ArbitrageConfig(
            min_profit_threshold=float(os.getenv("MIN_PROFIT_THRESHOLD", "0.005")),
            min_trade_size=float(os.getenv("MIN_TRADE_SIZE", "10")),
            max_trade_size=float(os.getenv("MAX_TRADE_SIZE", "500")),
            max_position_size=float(os.getenv("MAX_POSITION_SIZE", "1000")),
            price_check_interval=float(os.getenv("PRICE_CHECK_INTERVAL", "2")),
            order_timeout=int(os.getenv("ORDER_TIMEOUT", "30")),
            max_slippage=float(os.getenv("MAX_SLIPPAGE", "0.01")),
        )
        
        self.risk = RiskConfig(
            max_total_exposure=float(os.getenv("MAX_TOTAL_EXPOSURE", "5000")),
            max_single_market_exposure=float(os.getenv("MAX_SINGLE_MARKET_EXPOSURE", "1000")),
            stop_loss_percent=float(os.getenv("STOP_LOSS_PERCENT", "5")),
            daily_loss_limit=float(os.getenv("DAILY_LOSS_LIMIT", "200")),
            max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "10")),
        )
        
        self.watch_markets: List[str] = []
        watch_markets_str = os.getenv("WATCH_MARKETS", "")
        if watch_markets_str:
            self.watch_markets = [m.strip() for m in watch_markets_str.split(",")]
    
    @property
    def wallet_address(self) -> str:
        if self.WALLET_SWITCH == 2:
            return self.PROXY_WALLET_ADDRESS
        return self.LOCAL_WALLET_ADDRESS or self.PROXY_WALLET_ADDRESS
    
    def validate(self, require_api: bool = True) -> bool:
        errors = []
        
        if require_api:
            if not self.WALLET_PRIVATE_KEY:
                errors.append("WALLET_PRIVATE_KEY (私钥) 未配置")
            
            if self.WALLET_SWITCH == 2:
                if not self.PROXY_WALLET_ADDRESS:
                    errors.append("PROXY_WALLET_ADDRESS (代理钱包地址) 未配置")
            else:
                if not self.LOCAL_WALLET_ADDRESS and not self.PROXY_WALLET_ADDRESS:
                    errors.append("LOCAL_WALLET_ADDRESS 或 PROXY_WALLET_ADDRESS 未配置")
        
        if errors:
            for error in errors:
                logger.error(f"[CONFIG] 配置错误: {error}")
            return False
        
        logger.info("[CONFIG] 配置验证通过")
        logger.info(f"[CONFIG] 钱包模式: {'代理钱包' if self.WALLET_SWITCH == 2 else '本地钱包'}")
        logger.info(f"[CONFIG] 钱包地址: {self.wallet_address}")
        return True
    
    def create_clob_client(self):
        if self._clob_client_instance:
            return self._clob_client_instance
        
        from py_clob_client.client import ClobClient
        
        if not self.WALLET_PRIVATE_KEY:
            raise ValueError("WALLET_PRIVATE_KEY 未配置")
        
        if self.WALLET_SWITCH == 2:
            funder = self.PROXY_WALLET_ADDRESS
            signature_type = 2
            if not funder:
                raise ValueError("PROXY_WALLET_ADDRESS 未配置 (代理钱包模式需要)")
        else:
            funder = self.LOCAL_WALLET_ADDRESS or self.PROXY_WALLET_ADDRESS
            signature_type = 0
            if not funder:
                raise ValueError("LOCAL_WALLET_ADDRESS 或 PROXY_WALLET_ADDRESS 未配置")
        
        client = ClobClient(
            host=self.CLOB_BASE_URL,
            key=self.WALLET_PRIVATE_KEY,
            chain_id=self.CHAIN_ID,
            signature_type=signature_type,
            funder=funder,
        )
        
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            logger.info(f"[CONFIG] API凭证已自动生成: {creds.api_key[:8]}...")
        except Exception as e:
            logger.warning(f"[CONFIG] 自动生成API凭证失败: {e}")
        
        self._clob_client_instance = client
        return client

def get_config() -> Config:
    return Config()
