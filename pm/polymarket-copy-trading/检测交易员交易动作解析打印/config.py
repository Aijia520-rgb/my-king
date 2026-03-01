import os
import json
import logging
import sys
import gc
from web3 import Web3
from dotenv import load_dotenv
from typing import Optional, Dict, Any, List

def setup_logger(name="PolymarketMonitor"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        '%(asctime)s - [%(levelname)s] - %(message)s',
        datefmt='%H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

logger = setup_logger()

TRADER_NICKNAME_CACHE: Dict[str, str] = {}

class SecureConfig:
    """
    安全配置类 - 从.env文件加载配置，敏感数据保留在内存中方便使用
    """
    
    def __init__(self):
        """初始化安全配置"""
        self._initialized = False
        
        # 初始化所有配置属性
        # ===== Gamma API配置 (用于市场信息) =====
        self.gamma_api_url: str = "https://gamma-api.polymarket.com"

        # ===== RPC配置 =====
        self.rpc_url: str = "https://polygon-mainnet.infura.io/v3/f86eb30a36a147fb9eb117314407dfb6"
        
        # ===== 目标交易员配置 =====
        self.target_traders: List[str] = []
        self.trader_configs: Dict[str, Any] = {}

        # ===== CLOB API配置 =====
        self.clob_base_url: str = "https://clob.polymarket.com"
        
        # 网络链ID (137: Polygon mainnet, 80002: Amoy testnet)
        self.chain_id: int = 137
        
        # 下单钱包切换项: 1=本地钱包, 2=代理钱包
        self.wallet_switch: int = 2
        
        # 本地钱包地址 (与私钥对应)
        self.local_wallet_address: str = ""
        # 代理钱包地址 (持有资金，用于扣款)
        self.proxy_wallet_address: str = ""

        # ===== 交易策略配置 =====
        self.copy_ratio: float = 0.3
        self.order_expiry_seconds: int = 300
        self.signal_expiry: int = 60
        self.buy_order_type: str = "GTD"
        self.sell_order_type: str = "FOK"
        self.max_order_size: float = 10000
        self.min_order_size: Optional[float] = None

        # ===== 风险控制配置 =====
        self.max_position_size: float = 50000
        self.min_trade_ratio: float = 0.1 / 100
        self.max_trader_usage_cap: float = 0.1
        self.max_retry_attempts: int = 3
        self.retry_delay: float = 1.0
        self.market_title_blacklist: List[str] = []
        self.buy_premium: float = 0.01
        self.low_price_buy_premium: float = 0.1
        self.low_price_threshold: float = 0.3
        self.sell_premium: float = 0.02
        self.max_price_threshold: float = 0.98
        self.large_order_threshold: float = 1000
        self.min_trader_order_size: float = 500
        self.trader_min_order_sizes: Dict[str, float] = {}
        self.max_position_per_market_ratio: float = 0.1
        self.max_position_per_market_amount: Optional[float] = None
        self.max_position_per_market_shares: Optional[float] = None

        # ===== 监控配置 =====
        self.order_check_interval: int = 10

        # ===== 调试配置 =====
        self.enable_debug_logging: bool = False

        # 敏感信息（保留在内存中，方便使用）
        self.wallet_private_key: str = ""
        self._temp_decrypted_keys: Dict[str, str] = {}
        
        # 缓存实例
        self._web3_instance = None
        self._account_instance = None
        self._clob_client_instance = None
        
        # 从 .env 文件加载配置
        self._load_from_env_file()
    
    def reload(self):
        """重新加载配置"""
        logger.info("正在重新加载配置...")
        self._load_from_env_file()
    
    def _load_from_env_file(self):
        """从.env文件加载配置（回退方案）"""
        logger.info("正在从.env文件加载配置...")
        
        # 加载.env文件
        load_dotenv()
        # API配置
        self.gamma_api_url = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com")
        self.rpc_url = os.getenv("POLYGON_RPC_URL", os.getenv("RPC_URL", "https://polygon-rpc.com"))
        self.clob_base_url = os.getenv("CLOB_BASE_URL", "https://clob.polymarket.com")
        
        # 目标交易员
        try:
            self.target_traders = json.loads(os.getenv("TARGET_TRADERS", "[]"))
            self.target_traders = [addr.lower() for addr in self.target_traders]
        except json.JSONDecodeError:
            logger.warning("TARGET_TRADERS in .env is not a valid JSON list. Using empty list.")
            self.target_traders = []
        
        # 钱包配置
        self.wallet_switch = int(os.getenv("WALLET_SWITCH", "2"))
        self.local_wallet_address = os.getenv("LOCAL_WALLET_ADDRESS", "")
        self.proxy_wallet_address = os.getenv("PROXY_WALLET_ADDRESS", "")
        
        # 解密私钥（存储在内存中）
        raw_private_key = os.getenv("WALLET_PRIVATE_KEY", "")
        if raw_private_key:
            try:
                from secure_tool import CryptoManager
                self.wallet_private_key = CryptoManager.decrypt(raw_private_key)
                self._temp_decrypted_keys['private_key'] = self.wallet_private_key
                logger.info("Successfully decrypted WALLET_PRIVATE_KEY")
            except Exception as e:
                self.wallet_private_key = raw_private_key
                self._temp_decrypted_keys['private_key'] = self.wallet_private_key
                logger.debug(f"Using raw WALLET_PRIVATE_KEY (decryption skipped: {str(e)})")
        else:
            self.wallet_private_key = ""
        
        
        # 交易配置
        try:
            self.copy_ratio = float(os.getenv("COPY_RATIO", "0.1"))
        except ValueError:
            self.copy_ratio = 0.1
            logger.warning("Invalid COPY_RATIO, using default 0.1")
        
        try:
            self.order_expiry_seconds = int(os.getenv("ORDER_EXPIRY_SECONDS", "300"))
        except ValueError:
            self.order_expiry_seconds = 300
        
        try:
            self.signal_expiry = int(os.getenv("SIGNAL_EXPIRY", "60"))
        except ValueError:
            self.signal_expiry = 60
        
        # 买入订单类型
        buy_order_type = os.getenv("BUY_ORDER_TYPE", "GTD").upper()
        if buy_order_type not in ["GTD", "FOK", "FAK", "GTC"]:
            logger.warning(f"Invalid BUY_ORDER_TYPE '{buy_order_type}', using default GTD")
            buy_order_type = "GTD"
        self.buy_order_type = buy_order_type
        
        # 卖出订单类型
        sell_order_type = os.getenv("SELL_ORDER_TYPE", "FOK").upper()
        if sell_order_type not in ["GTD", "FOK", "FAK", "GTC"]:
            logger.warning(f"Invalid SELL_ORDER_TYPE '{sell_order_type}', using default FOK")
            sell_order_type = "FOK"
        self.sell_order_type = sell_order_type
        
        try:
            self.max_order_size = float(os.getenv("MAX_ORDER_SIZE", "10000"))
        except ValueError:
            self.max_order_size = 10000
        
        # 最小订单限制
        min_order_size_env = os.getenv("MIN_ORDER_SIZE", "").strip()
        if min_order_size_env == "":
            self.min_order_size = None
        else:
            try:
                self.min_order_size = float(min_order_size_env)
            except ValueError:
                self.min_order_size = None
        
        try:
            self.max_position_size = float(os.getenv("MAX_POSITION_SIZE", "50000"))
        except ValueError:
            self.max_position_size = 50000
        
        try:
            # 环境变量中配置的是百分比（如 0.1 表示 0.1%），需要除以 100 转换为小数
            self.min_trade_ratio = float(os.getenv("MIN_TRADE_RATIO", "0.1")) / 100.0
        except ValueError:
            self.min_trade_ratio = 0.1 / 100.0
        
        try:
            self.max_trader_usage_cap = float(os.getenv("MAX_TRADER_USAGE_CAP", "0.1"))
        except ValueError:
            self.max_trader_usage_cap = 0.1

        # 重试配置
        try:
            self.max_retry_attempts = int(os.getenv("MAX_RETRY_ATTEMPTS", "3"))
        except ValueError:
            self.max_retry_attempts = 3
        
        try:
            self.retry_delay = float(os.getenv("RETRY_DELAY", "1.0"))
        except ValueError:
            self.retry_delay = 1.0
        
        try:
            self.order_check_interval = int(os.getenv("ORDER_CHECK_INTERVAL", "10"))
        except ValueError:
            self.order_check_interval = 10
        
        # 市场标题黑名单
        try:
            self.market_title_blacklist = json.loads(os.getenv("MARKET_TITLE_BLACKLIST", "[]"))
        except json.JSONDecodeError:
            self.market_title_blacklist = []
        
        # 买入溢价
        try:
            self.buy_premium = float(os.getenv("BUY_PREMIUM", "0.01"))
        except ValueError:
            self.buy_premium = 0.01
        
        # 低价股买入溢价
        try:
            self.low_price_buy_premium = float(os.getenv("LOW_PRICE_BUY_PREMIUM", "0.1"))
        except ValueError:
            self.low_price_buy_premium = 0.1
        
        # 低价股阈值
        try:
            self.low_price_threshold = float(os.getenv("LOW_PRICE_THRESHOLD", "0.3"))
        except ValueError:
            self.low_price_threshold = 0.3
        
        # 卖出折价
        try:
            self.sell_premium = float(os.getenv("SELL_PREMIUM", "0.02"))
        except ValueError:
            self.sell_premium = 0.02
        
        # 最高跟单价格
        try:
            self.max_price_threshold = float(os.getenv("MAX_PRICE_THRESHOLD", "0.98"))
        except ValueError:
            self.max_price_threshold = 0.98
        
        # 大额交易阈值
        try:
            self.large_order_threshold = float(os.getenv("LARGE_ORDER_THRESHOLD", "1000"))
        except ValueError:
            self.large_order_threshold = 1000
        
        # 交易员最小下单金额
        try:
            self.min_trader_order_size = float(os.getenv("MIN_TRADER_ORDER_SIZE", "500"))
        except ValueError:
            self.min_trader_order_size = 500
        
        # 交易员单独最小下单金额
        try:
            self.trader_min_order_sizes = json.loads(os.getenv("TRADER_MIN_ORDER_SIZES", "{}"))
            self.trader_min_order_sizes = {k.lower(): v for k, v in self.trader_min_order_sizes.items()}
        except json.JSONDecodeError:
            logger.warning("TRADER_MIN_ORDER_SIZES in .env is not a valid JSON. Using empty config.")
            self.trader_min_order_sizes = {}
        
        # 每个市场持仓限制
        try:
            self.max_position_per_market_ratio = float(os.getenv("MAX_POSITION_PER_MARKET_RATIO", "0.1"))
        except ValueError:
            self.max_position_per_market_ratio = 0.1
        
        max_position_per_market_amount_env = os.getenv("MAX_POSITION_PER_MARKET_AMOUNT", "").strip()
        if max_position_per_market_amount_env == "":
            self.max_position_per_market_amount = None
        else:
            try:
                self.max_position_per_market_amount = float(max_position_per_market_amount_env)
            except ValueError:
                self.max_position_per_market_amount = None
        
        max_position_per_market_shares_env = os.getenv("MAX_POSITION_PER_MARKET_SHARES", "").strip()
        if max_position_per_market_shares_env == "":
            self.max_position_per_market_shares = None
        else:
            try:
                self.max_position_per_market_shares = float(max_position_per_market_shares_env)
            except ValueError:
                self.max_position_per_market_shares = None
        
        # 调试配置
        self.enable_debug_logging = os.getenv("ENABLE_DEBUG_LOGGING", "false").lower() == "true"
        
        # 交易员配置
        try:
            self.trader_configs = json.loads(os.getenv("TRADER_CONFIGS", "{}"))
            self.trader_configs = {k.lower(): v for k, v in self.trader_configs.items()}
        except json.JSONDecodeError:
            logger.warning("TRADER_CONFIGS in .env is not a valid JSON. Using empty config.")
            self.trader_configs = {}
        
        # 初始化 Web3 和 ClobClient
        self.get_web3_and_account(auto_clear_key=False)
        self.create_clob_client(auto_clear_key=True)
        
        self._initialized = True
    
    def clear_sensitive_data(self):
        """
        清除内存中的敏感数据（安全机制）
        
        为了防止内存dump等方式窃取私钥，可以在使用完后调用此方法清除内存中的敏感数据。
        注意：清除后将无法再次获取私钥，需要重新初始化配置。
        """
        if self.wallet_private_key:
            logger.info("正在清除内存中的私钥...")
            # 覆盖内存
            self.wallet_private_key = "0" * len(self.wallet_private_key)
            self.wallet_private_key = ""
        
        # 清除临时存储的解密数据
        for key in list(self._temp_decrypted_keys.keys()):
            value = self._temp_decrypted_keys[key]
            # 覆盖内存
            self._temp_decrypted_keys[key] = "0" * len(value)
            del self._temp_decrypted_keys[key]
        
        # 强制垃圾回收
        gc.collect()
        
        logger.info("已清除内存中的敏感数据")
    
    def get_trader_copy_ratio(self, trader_address: str) -> float:
        """获取指定交易员的跟单比例"""
        if not trader_address:
            return self.copy_ratio
        
        trader_address = trader_address.lower()
        if trader_address in self.trader_configs:
            trader_config = self.trader_configs[trader_address]
            if "copy_ratio" in trader_config:
                try:
                    return float(trader_config["copy_ratio"])
                except (ValueError, TypeError):
                    logger.warning(f"Invalid copy_ratio for trader {trader_address}, using global default")
        
        return self.copy_ratio
    
    def get_trader_min_order_size(self, trader_address: str) -> float:
        """获取指定交易员的最小下单金额"""
        if not trader_address:
            return self.min_trader_order_size
        
        trader_address = trader_address.lower()
        if trader_address in self.trader_min_order_sizes:
            return float(self.trader_min_order_sizes[trader_address])
        
        return self.min_trader_order_size
    
    def get_trade_config_summary(self):
        """获取交易配置摘要"""
        return {
            "copy_ratio": self.copy_ratio,
            "trader_configs": self.trader_configs,
            "order_expiry_seconds": self.order_expiry_seconds,
            "signal_expiry": self.signal_expiry,
            "max_order_size": self.max_order_size,
            "min_order_size": self.min_order_size,
            "max_position_size": self.max_position_size,
            "min_trade_ratio": self.min_trade_ratio,
            "max_trader_usage_cap": self.max_trader_usage_cap,
            "proxy_wallet_configured": bool(self.proxy_wallet_address),
            "wallet_private_key_configured": bool(self.wallet_private_key),
        }
    
    def validate(self):
        """验证必要的配置"""
        # 注意：这里不验证私钥，因为私钥可能已经被清除
        if not self.target_traders:
            logger.warning("[CONFIG] TARGET_TRADERS is empty. No trader will be monitored.")
        # 可以添加其他必要的验证

    def get_web3_and_account(self, auto_clear_key: bool = True):
        """
        获取 Web3 实例和账户对象（单例模式）
        
        注意：首次调用会自动加载私钥并创建实例，随后立即清除内存中的私钥字符串。
        后续调用直接返回缓存的实例。
        
        Returns:
            tuple: (w3, account)
            - w3: Web3 实例
            - account: 账户对象 (如果私钥存在)
        """
        # 1. 如果已有缓存，直接返回
        if self._web3_instance and self._account_instance:
            return self._web3_instance, self._account_instance
            
        # 2. 初始化 Web3 (如果尚未初始化)
        if not self._web3_instance:
            self._web3_instance = Web3(Web3.HTTPProvider(self.rpc_url))
        
        if not self._web3_instance.is_connected():
            logger.error(f"无法连接到 RPC: {self.rpc_url}")
            return None, None

        # 3. 初始化账户 (如果尚未初始化)
        if not self._account_instance:
            # 确保私钥存在
            if not self.wallet_private_key:
                self.reload()
            
            if self.wallet_private_key:
                try:
                    self._account_instance = self._web3_instance.eth.account.from_key(self.wallet_private_key)
                    # 创建完实例后，立即清除私钥字符串，确保安全
                    if auto_clear_key:
                        self.clear_sensitive_data()
                except Exception as e:
                    logger.error(f"初始化账户失败: {e}")
            else:
                logger.warning("未找到私钥，无法创建账户对象")
        
        return self._web3_instance, self._account_instance

    def create_clob_client(self, auto_clear_key: bool = True):
        """
        创建并返回 ClobClient 实例（单例模式）
        
        注意：首次调用会自动加载私钥并创建实例，随后立即清除内存中的私钥字符串。
        后续调用直接返回缓存的实例。
        
        Returns:
            ClobClient 实例
        """
        # 1. 如果已有缓存，直接返回
        if self._clob_client_instance:
            return self._clob_client_instance

        try:
            from py_clob_client.client import ClobClient
        except ImportError:
            logger.error("py-clob-client 未安装，无法创建 ClobClient")
            raise ImportError("py-clob-client is not installed. Please install it with 'pip install py-clob-client'")

        # 2. 检查私钥，如果已被清除则尝试重新加载
        if not self.wallet_private_key:
            logger.info("检测到私钥缺失（可能已被清除），正在尝试重新加载...")
            self.reload()
            if not self.wallet_private_key:
                raise ValueError("Wallet private key (WALLET_PRIVATE_KEY) is required and could not be reloaded")
        
        # 3. 确定 funder 地址
        if self.wallet_switch == 2:
            # 代理钱包模式
            funder = self.proxy_wallet_address
            signature_type = 2
            if not funder:
                raise ValueError("Proxy wallet address (PROXY_WALLET_ADDRESS) is required for proxy wallet mode")
        else:
            # 本地钱包模式
            funder = self.local_wallet_address or self.proxy_wallet_address
            signature_type = 0
            if not funder:
                raise ValueError("Local wallet address (LOCAL_WALLET_ADDRESS) is required for local wallet mode")
        
        # 4. 创建新的 client 实例
        client = ClobClient(
            host=self.clob_base_url,
            key=self.wallet_private_key,
            chain_id=self.chain_id,
            signature_type=signature_type,
            funder=funder,
        )
        
        # 5. 自动创建或派生 API 凭证
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            logger.info(f"API凭证已自动生成: {creds.api_key[:8]}...")
        except Exception as e:
            logger.warning(f"自动生成API凭证失败: {e}")
            # 不抛出异常，让调用者决定如何处理
        
        # 6. 缓存实例
        self._clob_client_instance = client
        
        # 7. 安全增强：创建完成后清除内存中的私钥
        if auto_clear_key:
            self.clear_sensitive_data()
            
        return client

# 全局配置实例
_config_instance = None

def get_config() -> SecureConfig:
    """获取全局配置实例"""
    global _config_instance
    if _config_instance is None:
        _config_instance = SecureConfig()
    return _config_instance

# 为了向后兼容，创建 Config 别名
Config = SecureConfig

def create_clob_client(config: SecureConfig):
    """
    兼容旧接口的包装函数：统一调用 config.create_clob_client()
    
    注意：所有参数（use_cache, cache_holder, auto_clear_key）现在都被忽略，
    因为 SecureConfig 内部强制实现了单例缓存和安全清除策略。
    """
    return config.create_clob_client()
