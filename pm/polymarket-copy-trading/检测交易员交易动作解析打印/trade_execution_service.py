"""
交易执行服务模块
基于交易功能开发模块.md实现完整交易功能

功能:
- 信号有效性校验 (去重 + 时效)
- 持仓检查 (仅针对卖出)
- 市场价格获取
- GTC限价单下单 (支持部分成交)
- 订单状态管理
"""

import asyncio
import time
import aiohttp
import json
import os
import sys
import logging
import math
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from config import Config, logger
from enrichment_service import EnrichmentService
from ding import send_ding_async, format_combined_msg, get_trader_display_info

# Polymarket CLOB client
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, MarketOrderArgs, OrderType as ClobOrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    HAS_CLOB = True
    logger.info("[TRADE] py-clob-client imported successfully")
except ImportError as e:
    logger.error(f"[TRADE] 导入 py-clob-client 失败: {e}")
    logger.error(f"[TRADE] sys.executable: {sys.executable}")
    logger.error(f"[TRADE] sys.path: {sys.path}")
    HAS_CLOB = False
    # 占位类，但不会真正使用，因为导入失败后任何调用都会抛出 ImportError
    class ClobClient:
        pass
    class ApiCreds:
        pass
    class OrderArgs:
        pass
    class MarketOrderArgs:
        pass
    ClobOrderType = type('ClobOrderType', (), {'GTC': 'GTC', 'FOK': 'FOK'})
    BUY = 'BUY'
    SELL = 'SELL'

@dataclass
class TradeSignal:
    """交易信号数据结构"""
    # 来源信息
    source_address: str        # 交易员地址 (maker/taker)
    original_tx_hash: str      # 原始交易哈希
    detection_source: str      # "PENDING" | "CONFIRMED"

    # 交易信息
    token_id: str             # Token ID
    side: str                 # "BUY" | "SELL" (从side: 0/1转换)
    amount_usdc: float        # 交易金额 (USDC，通过EnrichmentService.format_amount转换)

    # 市场信息
    market_info: dict         # EnrichmentService.get_market_info()返回

    # 元数据
    detected_at: float        # 检测时间戳
    processed: bool = False   # 是否已处理 (去重用)

    # 可选字段（有默认值，必须放在最后）
    price: float = None       # 交易员成交价格 (每股价格)
    shares: float = None      # 购买/卖出股数

@dataclass
class OrderStatus:
    """订单状态数据结构"""
    order_id: str            # 订单ID
    token_id: str           # Token ID
    side: str              # BUY/SELL
    amount: float          # 订单数量 (USDC)
    filled_amount: float   # 已成交数量
    price: float           # 订单价格
    created_at: float      # 创建时间
    status: str           # pending/partial_filled/filled/cancelled/expired
    expires_at: float     # 过期时间
    original_signal: str  # 原始信号哈希

class TradeExecutionService:
    """交易执行服务"""

    def __init__(self, config: Config, enricher: EnrichmentService, memory_monitor=None):
        self.config = config
        self.enricher = enricher
        self.memory_monitor = memory_monitor  # 内存监控实例

        # 内存缓存数据结构
        self.processed_signals = set()  # 去重集合: signal_tx_hash
        self.orders_cache = {}          # 订单状态缓存: order_id -> OrderStatus
        self.positions_cache = {}       # 持仓信息缓存: token_id -> position_info

        # 钱包切换配置
        wallet_switch = config.wallet_switch
        
        if wallet_switch == 1:
            # 使用本地钱包
            self.proxy_wallet = config.local_wallet_address
            self.wallet_type = "本地钱包"
            logger.info("[TRADE] 钱包切换: 使用本地钱包")
        elif wallet_switch == 2:
            # 使用代理钱包 (本地私钥签名 + 代理钱包扣款)
            self.proxy_wallet = config.proxy_wallet_address
            self.wallet_type = "代理钱包"
            logger.info("[TRADE] 钱包切换: 使用代理钱包 (本地私钥签名 + 代理钱包扣款)")
        else:
            # 默认使用本地钱包
            self.proxy_wallet = config.local_wallet_address
            self.wallet_type = "本地钱包(默认)"
            logger.warning(f"[TRADE] 无效的钱包切换配置 {wallet_switch}，使用默认本地钱包")

        # 策略配置
        self.copy_ratio = config.copy_ratio
        self.order_timeout = config.order_timeout
        self.signal_expiry = config.signal_expiry

        # HTTP会话 (支持HTTP/2)
        self.session: Optional[aiohttp.ClientSession] = None

        logger.info("[TRADE] 交易执行服务已初始化")
        logger.info(f"[TRADE] 使用钱包类型: {self.wallet_type}")
        logger.info(f"[TRADE] 当前钱包地址: {self.proxy_wallet}")
        logger.info(f"[TRADE] 跟单比例: {self.copy_ratio}")
        logger.info(f"[TRADE] 订单超时: {self.order_timeout}秒")
        logger.info(f"[TRADE] 信号有效期: {self.signal_expiry}秒")

    async def start(self):
        """启动交易服务"""
        if not self.session:
            # 创建支持HTTP/2的会话
            connector = aiohttp.TCPConnector(
                force_close=False,
                enable_cleanup_closed=True,
                keepalive_timeout=300
            )

            timeout = aiohttp.ClientTimeout(total=30, connect=10)

            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={
                    'User-Agent': 'Polymarket-CopyTrading/1.0'
                }
            )

        logger.info("[TRADE] 交易服务已启动")

    async def stop(self):
        """停止交易服务"""
        if self.session:
            await self.session.close()
            self.session = None

        logger.info("[TRADE] 交易服务已停止")

    async def execute_copy_trade(self, signal):
        """
        执行跟单交易（下单主链路入口）。

        运行逻辑（高层流程）：
        1) 校验信号有效性：去重 + 时效（[`TradeExecutionService._validate_signal()`](trade_execution_service.py:300)）
        2) 持仓/仓位相关检查：
           - SELL：我们必须有持仓，否则跳过；并尝试读取交易员持仓用于计算卖出比例（仅用于日志/策略提示）
           - BUY：检查交易员是否有持仓（用于日志/未来策略）；读取我们是否有持仓（用于日志/未来策略）
        3) 定价：获取 order_price（[`TradeExecutionService._get_current_price()`](trade_execution_service.py:523)）
           - 当前实现：order_price = trader_price + 0.02，然后 clamp 到 [0.001, 0.99]
           - 说明：规划里你新增的“防越界二次规则（BUY>0.99->0.99 / SELL<0.01->0.01）”目前尚未在代码中启用（仅写入规划文档）
        4) 算参：根据余额比例/最小限制等，计算下单金额(USDC)与股数(shares)（[`TradeExecutionService._calculate_order_params()`](trade_execution_service.py:702)）
           - 当前实现（策略C）：不做任何补偿性调整（不买一股兜底、不把金额抬到$1、不额外加溢价）；不满足约束则直接跳过
        5) 提交：构造 CLOB 订单并提交（[`TradeExecutionService._place_gtc_order()`](trade_execution_service.py:1000)）
           - 当前实现（策略C）：不修改 shares；仅做必要的“提前跳过”预检（例如 notional<$1 或按 step=1e-4 下取整后 notional<$1）

        输入：
        - signal: [`TradeSignal`](trade_execution_service.py:51) 实例，由监控模块构造（[`MonitorService.execute_trade()`](monitor_service.py:112)）
          - signal.price/signal.shares 可能来自 data-api 的 price 与 sizeUsd/price 推导；若缺失会在定价里尝试用 amount_usdc/shares 推导。

        输出：
        - bool：是否成功提交订单（注意：即使提交失败也会尝试触发持仓/余额更新逻辑用于刷新缓存）

        重要约束（已从日志/服务端报错确认）：
        - CLOB 价格区间：min 0.001, max 0.99（因此 price>=0.991 会被拒绝；当前已在定价+提交阶段做 clamp/校验）
        - marketable BUY 最小名义金额：$1（即使本地估算>=1，服务端也可能因量化/费用/撮合规则判定不足）
        """
        try:
            logger.info(f"\n[TRADE] 开始处理跟单信号...")
            logger.info(f"[TRADE] 交易员: {get_trader_display_info(signal.source_address)}")
            logger.info(f"[TRADE] 市场: {signal.market_info.get('market_slug', 'Unknown')}")
            logger.info(f"[TRADE] 方向: {signal.side}")
            logger.info(f"[TRADE] 金额: {signal.amount_usdc:.2f} USDC")

            # 用于通知显示的比例 (BUY: 占余额比例, SELL: 占持仓比例)
            display_ratio = 0.0

            # 1. 信号验证 (去重 + 时效)
            if not await self._validate_signal(signal):
                return False

            # 2. 持仓检查
            if signal.side == "SELL":
                # 卖出：检查我们和交易员的持仓情况
                our_has_position = await self._check_position(signal.token_id)
                if not our_has_position:
                    # logger.info(f"[TRADE] 我们无持仓，跳过卖出信号")  # 已删除日志输出
                    return False

                # 检查交易员卖出前有多少持仓（用于计算卖出比例）
                trader_position_info = await self._get_trader_position_detail(signal.token_id, signal.source_address)
                if trader_position_info['total_shares'] > 0:
                    # 优先使用股数计算比例
                    trader_sold_shares = signal.shares
                    if not trader_sold_shares and signal.price and signal.price > 0:
                        trader_sold_shares = signal.amount_usdc / signal.price
                    
                    if trader_sold_shares:
                        display_ratio = (trader_sold_shares / trader_position_info['total_shares']) * 100
                    else:
                        # 兜底：使用金额/成本计算
                        if trader_position_info['avg_price'] > 0:
                            display_ratio = (signal.amount_usdc / (trader_position_info['total_shares'] * trader_position_info['avg_price'])) * 100
                    
                    # 限制最大100%
                    if display_ratio > 100: display_ratio = 100.0
                        
                    logger.info(f"[TRADE] 交易员卖出比例: {display_ratio:.2f}% (持仓{trader_position_info['total_shares']:.2f}股)")
                else:
                    logger.info(f"[TRADE] 交易员无持仓数据，使用固定比例策略")

            elif signal.side == "BUY":
                # 买入：检查交易员和我们的持仓情况
                trader_has_position = await self._check_trader_position(signal.token_id, signal.source_address)
                logger.info(f"[TRADE] 交易员持仓检查: {'有持仓' if trader_has_position else '无持仓'}")

                # 规则：先做“交易员本次下单占其余额比例”过滤（小于 0.1% 不跟）
                # - 阈值配置：[`Config.MIN_TRADE_RATIO`](检测交易员交易动作解析打印/config.py:100)（默认 0.1，按“百分比 0.1%”解释，即 0.1/100）
                # - 与交易员是否已有持仓无关：先过滤，再进入后续算参
                if await self._should_skip_buy_due_to_trader_balance_threshold(signal):
                    return False

                # 检查我们自己的持仓（用于后续策略选择）
                our_position_shares = await self._get_position_shares(signal.token_id)
                if our_position_shares > 0:
                    logger.info(f"[POSITION] Token ID {signal.token_id[:10]}... 我们有持仓: {our_position_shares:.2f} 股")
                else:
                    # logger.info(f"[POSITION] Token ID {signal.token_id[:10]}... 我们无持仓")  # 已删除日志输出
                    pass  # 无持仓时什么都不做

            # 3. 获取当前市场价格
            order_price = await self._get_current_price(signal)
            if not order_price:
                return False

            # 4. 计算订单参数
            # 重构：统一接收 (order_params, calc_details, error_reason)
            order_params, calc_details, error_reason = await self._calculate_order_params(signal, order_price)
            
            if not order_params and not error_reason:
                # 兜底：如果都为空，设置默认错误
                error_reason = "计算失败或不满足条件"
                if not calc_details:
                    calc_details = "计算过程未知"

            # 准备交易员信息 (用于合并通知)
            try:
                trader_balance = await self._get_trader_usdc_balance(signal.source_address)
                
                # 如果是BUY，在这里计算余额比例
                if signal.side == "BUY":
                    if trader_balance > 0:
                        display_ratio = (signal.amount_usdc / trader_balance) * 100
                    else:
                        display_ratio = 0.0
                
                trader_info = {
                    'action_type': signal.side,
                    'trader_address': signal.source_address,
                    'balance': trader_balance,
                    'outcome': signal.market_info.get('outcome', 'Unknown'),
                    'price': signal.price if signal.price else 0,
                    'ratio': display_ratio,
                    'market_name': signal.market_info.get('market_slug', 'Unknown'),
                    'amount_usdc': signal.amount_usdc
                }
            except Exception as e:
                logger.error(f"[DING] 获取交易员信息失败: {e}")
                trader_info = {}

            # 检查订单参数是否有效
            if not order_params:
                logger.info(f"[TRADE] 订单参数无效，跳过此跟单信号")
                # 发送合并通知 (跳过/失败)
                try:
                    our_balance = await self._get_our_usdc_balance()
                    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    wallet_name = self.config.user_name if hasattr(self.config, 'user_name') else "未知钱包"
                    
                    # 使用具体的错误原因
                    error_msg = error_reason if error_reason else "订单参数无效或不满足过滤条件"
                    
                    our_info = {
                        'our_address': self.proxy_wallet,
                        'our_balance': our_balance,
                        'amount': 0,
                        'shares': 0,
                        'price': order_price if order_price else 0,
                        'calc_process': calc_details,
                        'status': "跟单跳过/失败",
                        'error_msg': error_msg
                    }
                    if trader_info:
                        send_ding_async(format_combined_msg(trader_info, our_info, current_time, wallet_name))
                except Exception as e:
                    logger.error(f"[DING] 发送失败通知异常: {e}")
                return False

            # 5. 执行GTC限价单
            # 5. 执行GTC限价单
            # 修改：接收 (order_id, taking_amount) 元组
            place_result = await self._place_gtc_order(order_params)
            if place_result:
                order_id, taking_amount = place_result
            else:
                order_id, taking_amount = None, None
            
            # 发送合并通知 (尝试下单后)
            try:
                our_balance = await self._get_our_usdc_balance()
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                wallet_name = self.config.user_name if hasattr(self.config, 'user_name') else "未知钱包"
                
                # 状态判断逻辑优化
                if order_id:
                    # 尝试解析 taking_amount
                    try:
                        taking_amt_val = float(taking_amount) if taking_amount else 0.0
                    except:
                        taking_amt_val = 0.0
                        
                    if taking_amt_val > 0:
                        status_text = "跟单成功"
                    else:
                        status_text = "挂单中"
                else:
                    status_text = "跟单失败"

                error_text = None if order_id else "下单请求被拒绝或网络错误"
                
                our_info = {
                    'our_address': self.proxy_wallet,
                    'our_balance': our_balance,
                    'amount': order_params['size'],
                    'shares': order_params['shares'],
                    'price': order_params['price'],
                    'calc_process': calc_details,
                    'status': status_text,
                    'error_msg': error_text
                }
                
                if trader_info:
                    send_ding_async(format_combined_msg(trader_info, our_info, current_time, wallet_name))
            except Exception as e:
                logger.error(f"[DING] 发送跟单通知失败: {e}")

            if not order_id:
                # 即使下单失败也要更新持仓数据
                await self._update_positions_after_trade(signal)
                return False

            # 5.1 缓存挂单：用于5分钟后检查未成交/部分成交并撤单
            try:
                if self.memory_monitor and hasattr(self.memory_monitor, "cache_open_order"):
                    self.memory_monitor.cache_open_order(
                        order_id=order_id,
                        token_id=signal.token_id,
                        side=signal.side,
                        created_at=time.time(),
                        delay_seconds=300,  # 按需求：5分钟后检查
                    )
            except Exception as e:
                logger.warning(f"[ORDER-CACHE] 缓存挂单失败: {e}")

            # 6. 启动订单管理任务
            asyncio.create_task(self._manage_order_lifecycle(order_id, signal))

            # 7. 记录处理状态
            self.processed_signals.add(signal.original_tx_hash)

            logger.info(f"[TRADE] 跟单信号处理成功，订单ID: {order_id}")
            return True

        except Exception as e:
            logger.error(f"[TRADE] 执行跟单失败: {e}")
            # 异常情况下也要更新持仓数据
            try:
                await self._update_positions_after_trade(signal)
            except Exception as update_error:
                logger.warning(f"[UPDATE] 异常情况下持仓更新也失败: {update_error}")
            return False

    async def _validate_signal(self, signal) -> bool:
        """
        验证信号有效性（下单前的硬性门槛）。

        运行逻辑：
        1) 去重：同一个 `signal.original_tx_hash` 只处理一次（避免重复下单）
        2) 时效：超过 `self.signal_expiry` 秒的信号直接丢弃（避免追单）
        3) 基础字段：token_id / side / amount_usdc 必须有效

        返回：
        - True：允许进入定价/算参/下单
        - False：跳过该信号
        """
        # 去重检查
        if signal.original_tx_hash in self.processed_signals:
            logger.info(f"[VALIDATE] 信号已处理，跳过: {signal.original_tx_hash}")
            return False

        # 时效性检查
        current_time = time.time()
        if current_time - signal.detected_at > self.signal_expiry:
            logger.info(f"[VALIDATE] 信号已过期，跳过: {current_time - signal.detected_at:.1f}s > {self.signal_expiry}s")
            return False

        # 基本参数验证
        if not signal.token_id or not signal.side or signal.amount_usdc <= 0:
            logger.warning(f"[VALIDATE] 信号参数无效: {signal}")
            return False

        # logger.info(f"[VALIDATE] 信号验证通过")  # 已删除日志输出
        return True

    async def _check_position(self, token_id: str) -> bool:
        """检查持仓 (仅针对卖出) - 使用数据API计算持仓"""
        try:
            logger.info(f"[POSITION] 检查持仓状态: Token ID {token_id}")

            # 获取我们的钱包地址
            our_wallet = self.proxy_wallet
            if not our_wallet:
                logger.error(f"[POSITION] 未配置钱包地址，无法检查持仓")
                return False

            # 使用数据API查询我们的交易记录来计算持仓，避免依赖CLOB API
            url = f"https://data-api.polymarket.com/activity"
            params = {
                'user': our_wallet,
                'limit': 100  # 获取最近的交易记录
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        activities = await response.json()

                        # 计算净持仓: 买入 - 卖出，只计算指定token的记录
                        net_position = 0.0
                        matching_activities = 0
                        for activity in activities:
                            # 过滤：只处理指定token的记录
                            activity_asset = str(activity.get('asset', ''))
                            if activity_asset != token_id:
                                continue

                            matching_activities += 1
                            side = activity.get('side', '').upper()
                            shares = float(activity.get('size', 0))

                            if side == 'BUY':
                                net_position += shares
                            elif side == 'SELL':
                                net_position -= shares

                        has_position = net_position > 0
                        if matching_activities > 0:
                            logger.info(f"[POSITION] Token ID {token_id[:10]}... 找到{matching_activities}条匹配记录，持仓: {net_position:.2f} 股")
                        else:
                            logger.info(f"[POSITION] Token ID {token_id[:10]}... 无交易记录，无持仓")

                        return has_position
                    else:
                        logger.error(f"[POSITION] 数据API请求失败: HTTP {response.status}")
                        return False

        except Exception as e:
            logger.error(f"[POSITION] 持仓计算失败: {e}")
            return False

    async def _check_trader_position(self, token_id: str, trader_address: str) -> bool:
        """检查交易员是否有指定token的持仓"""
        try:
            # logger.info(f"[POSITION] 检查交易员持仓: Token ID {token_id[:10]}..., 交易员 {trader_address[:10]}...")  # 已删除日志输出

            # 使用数据API查询交易员的交易记录
            url = f"https://data-api.polymarket.com/activity"
            params = {
                'user': trader_address,
                'limit': 100  # 获取最近的交易记录
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        activities = await response.json()

                        # 计算交易员的净持仓，只计算指定token的记录
                        net_position = 0.0
                        matching_activities = 0
                        for activity in activities:
                            # 过滤：只处理指定token的记录
                            activity_asset = str(activity.get('asset', ''))
                            if activity_asset != token_id:
                                continue

                            matching_activities += 1
                            side = activity.get('side', '').upper()
                            shares = float(activity.get('size', 0))

                            if side == 'BUY':
                                net_position += shares
                            elif side == 'SELL':
                                net_position -= shares

                        has_position = net_position > 0
                        if matching_activities > 0:
                            logger.info(f"[POSITION] 交易员 {get_trader_display_info(trader_address)} Token {token_id[:10]}... 持仓: {net_position:.2f} 股")
                        else:
                            logger.info(f"[POSITION] 交易员 {get_trader_display_info(trader_address)} Token {token_id[:10]}... 无交易记录")

                        return has_position
                    else:
                        logger.error(f"[POSITION] 交易员持仓查询失败: HTTP {response.status}")
                        return False

        except Exception as e:
            logger.error(f"[POSITION] 交易员持仓检查失败: {e}")
            return False

    async def _should_skip_buy_due_to_trader_balance_threshold(self, signal: TradeSignal) -> bool:
        """
        BUY 信号的“交易员余额阈值过滤”。

        需求语义：
        - 当交易员对该 token “有持仓”时，才触发本过滤；
        - 若交易员本次下单金额未达到其余额的一定比例阈值，则跳过该订单。

        阈值定义：
        - 阈值百分比读取自 [`Config.MIN_TRADE_RATIO`](检测交易员交易动作解析打印/config.py:100)
          - 默认 0.1，按“百分比 0.1%”解释，因此这里要除以 100。

        返回：
        - True：应跳过该 BUY 信号
        - False：继续执行后续定价/算参/下单
        """
        try:
            trader_balance = await self._get_trader_usdc_balance(signal.source_address)
            min_trade_ratio_percent = self.config.min_trade_ratio * 100
            min_trade_amount = float(trader_balance) * (min_trade_ratio_percent / 100.0)

            if signal.amount_usdc < min_trade_amount:
                logger.info(
                    f"[TRADE] 交易员有持仓但本次下单金额 {signal.amount_usdc:.2f} USDC "
                    f"< 交易员余额 {float(trader_balance):.2f} USDC 的 {min_trade_ratio_percent:.3f}% 阈值 "
                    f"({min_trade_amount:.2f} USDC)，跳过该订单"
                )
                return True

            return False
        except Exception as e:
            # 过滤逻辑异常时，为避免误跳过（漏跟单），默认继续执行
            logger.warning(f"[TRADE] 交易员余额阈值过滤计算失败，继续执行该订单: {e}")
            return False

    async def _get_trader_position_detail(self, token_id: str, trader_address: str) -> dict:
        """获取交易员详细的持仓信息 - 从内存变量读取"""
        try:
            # 默认返回值
            result = {
                'total_shares': 0.0,
                'avg_price': 0.0,
                'total_cost': 0.0
            }

            # 获取交易员持仓缓存
            if self.memory_monitor:
                trader_positions_cache = self.memory_monitor.get_trader_positions_cache()
            else:
                # 兜底：如果没有传入内存监控，创建新实例
                from balance_monitor import IntegratedMonitor
                monitor = IntegratedMonitor()
                trader_positions_cache = monitor.get_trader_positions_cache()

            # 检查交易员持仓缓存是否存在
            if not trader_positions_cache:
                logger.warning(f"[POSITION] 交易员持仓缓存为空")
                return result

            # 查找交易员（大小写不敏感）
            target_trader_key = None
            if trader_address in trader_positions_cache:
                target_trader_key = trader_address
            else:
                trader_address_lower = trader_address.lower()
                for cached_addr in trader_positions_cache:
                    if cached_addr.lower() == trader_address_lower:
                        target_trader_key = cached_addr
                        break
            
            if not target_trader_key:
                logger.warning(f"[POSITION] 未找到交易员 {get_trader_display_info(trader_address)} 的持仓数据")
                return result

            # 查找指定token的持仓
            for position in trader_positions_cache[target_trader_key]:
                # 兼容不同的字段名 (API返回的是 asset 不是 token_id)
                asset_id = position.get("asset") or position.get("token_id")
                if asset_id == token_id:
                    size = float(position.get("size", 0))
                    avg_price = float(position.get("avg_price", 0))
                    total_bought = float(position.get("total_bought", 0))

                    # 计算总成本（基于平均价格和总买入量）
                    total_cost = avg_price * total_bought if avg_price > 0 and total_bought > 0 else 0.0

                    result = {
                        'total_shares': size,
                        'avg_price': avg_price,
                        'total_cost': total_cost
                    }

                    logger.info(f"[POSITION] 从内存读取交易员 {get_trader_display_info(trader_address)} Token {token_id[:8]}... 持仓: {size:.2f}股, 平均价格 ${avg_price:.4f}")
                    return result

            logger.info(f"[POSITION] 交易员 {get_trader_display_info(trader_address)} 未持有 Token {token_id[:8]}...")
            return result

        except Exception as e:
            logger.warning(f"[POSITION] 从缓存读取交易员持仓失败: {e}")
            return {'total_shares': 0.0, 'avg_price': 0.0, 'total_cost': 0.0}

    async def _get_position_shares(self, token_id: str, show_warning: bool = True) -> float:
        """获取指定token的持仓股数 - 从内存变量读取"""
        try:
            # 使用传入的内存监控实例
            if self.memory_monitor:
                position_cache = self.memory_monitor.get_position_cache()
            else:
                # 兜底：如果没有传入内存监控，创建新实例（通常是空数据）
                from balance_monitor import IntegratedMonitor
                monitor = IntegratedMonitor()
                position_cache = monitor.get_position_cache()

            if not position_cache or not position_cache.get('positions'):
                if show_warning:
                    logger.warning(f"[POSITION] 内存持仓缓存为空")
                return 0.0

            # 查找指定token的持仓
            for position in position_cache.get("positions", []):
                # 兼容不同的字段名 (API返回的是 asset 不是 token_id)
                asset_id = position.get("asset") or position.get("token_id")
                if asset_id == token_id:
                    size = float(position.get("size", 0))
                    logger.info(f"[POSITION] 从内存读取持仓 Token {token_id[:8]}...: {size:.6f} 股")
                    return size

            # 买入时不显示此警告日志，只在卖出时显示相关信息
            return 0.0

        except Exception as e:
            logger.warning(f"[POSITION] 从缓存获取持仓失败: {e}")
            return 0.0

    def _calculate_price_with_premium(self, side: str, trader_price: float) -> Optional[float]:
        """
        计算溢价后的下单价格
        
        策略：
        - BUY: trader_price + 0.02, max 0.99. (若 trader_price > 0.99 则返回 None 跳过)
        - SELL: trader_price - 0.02, min 0.01.
        """
        try:
            premium = 0.02
            cap_min = 0.001
            cap_max = 0.99
            
            price_f = float(trader_price)
            
            if side == "BUY":
                if price_f > 0.99:
                    logger.info(f"[PRICE] BUY 跳过: trader_price={price_f:.6f} > 0.99")
                    return None
                
                raw_price = price_f + premium
                # clamp(raw, 0.001, 0.99)
                controlled_price = min(max(raw_price, cap_min), cap_max)
                
            else: # SELL
                raw_price = price_f - premium
                # max(raw, 0.01) -> min(..., 0.99)
                controlled_price = max(raw_price, 0.01)
                controlled_price = min(controlled_price, cap_max)

            final_price = controlled_price
                
            logger.info(
                f"[PRICE] {side} 溢价计算: base={price_f:.6f}, premium={premium}, "
                f"raw={raw_price:.6f} => final={final_price:.6f}"
            )
            return final_price
            
        except Exception as e:
            logger.warning(f"[PRICE] 溢价计算出错: {e}")
            return None

    async def _get_current_price(self, signal):
        """
        获取当前下单价格（order_price）。

        核心职责：
        - 从 CLOB orderbook 读取盘口（best ask / best bid）作为 market_price（仅用于兜底/诊断）
        - 优先使用 signal.price（交易员成交价）作为 trader_price
          - 若 signal.price 缺失，则尝试用 amount_usdc / shares 推导 trader_price（前提：shares>0）
        - 将 trader_price 按策略转换为我们的 order_price
          - 当前实现：order_price = trader_price + 0.02，然后 clamp 到 [0.001, 0.99]
          - 备注：规划里你新增的“溢价后防越界二次规则（BUY>0.99->0.99 / SELL<0.01->0.01）”目前尚未在代码中启用

        返回：
        - float: order_price（用于后续算参与下单提交）
        - None: 无法获取价格时返回 None，主流程会跳过该信号

        日志说明：
        - 当前有部分日志用 `:.2f` 打印，可能把 0.999 显示成 1.00（误导）；Phase 2 将统一提升精度。
        """
        try:
            # 1. 优先使用信号中的交易员价格 (最高优先级)
            trader_price = getattr(signal, 'price', None)
            if trader_price is None:
                # 如果信号中没有价格信息，尝试从交易金额计算
                if hasattr(signal, 'amount_usdc') and hasattr(signal, 'shares') and signal.shares is not None:
                    if signal.shares > 0:
                        trader_price = signal.amount_usdc / signal.shares
                        logger.info(f"[PRICE] 从交易金额计算交易员价格: {trader_price:.6f}")

            if trader_price is not None:
                return self._calculate_price_with_premium(signal.side, trader_price)

            # 2. 如果没有交易员价格，对于卖出订单，尝试使用持仓缓存价格
            if signal.side == "SELL":
                try:
                    # 使用内存监控实例获取持仓缓存
                    if self.memory_monitor:
                        position_cache = self.memory_monitor.get_position_cache()
                    else:
                        # 兜底：如果没有传入内存监控，创建新实例
                        from balance_monitor import IntegratedMonitor
                        monitor = IntegratedMonitor()
                        position_cache = monitor.get_position_cache()
                    if position_cache and position_cache.get('positions'):
                        for position in position_cache.get("positions", []):
                            asset_id = position.get("asset") or position.get("token_id")
                            if asset_id == signal.token_id:
                                cache_price = position.get("curPrice")
                                if cache_price and cache_price > 0:
                                    # 使用提取的函数计算溢价价格 (SELL: price - 0.02)
                                    logger.info(f"[PRICE] 卖出订单使用内存缓存价格作为基准: {float(cache_price):.6f}")
                                    calculated_price = self._calculate_price_with_premium("SELL", cache_price)
                                    if calculated_price is not None:
                                        return calculated_price
                except Exception as e:
                    logger.warning(f"[PRICE] 从内存持仓缓存获取价格失败: {e}")

            # logger.info(f"[PRICE] 获取市场价格: Token ID {signal.token_id}")  # 已删除日志输出

            # 检查依赖
            if not HAS_CLOB:
                logger.warning("[PRICE] py-clob-client 未安装，无法获取价格")
                return None
            # if not self.api_key or not self.api_secret or not self.passphrase:
            #     logger.warning("[PRICE] API 凭证未配置，无法获取价格")
            #     return None

            try:
                client = self._get_clob_client()
                # 尝试 get_orderbook，若失败则尝试 get_order_book (旧版本兼容)
                try:
                    orderbook = await asyncio.to_thread(client.get_orderbook, signal.token_id)
                except AttributeError:
                    orderbook = await asyncio.to_thread(client.get_order_book, signal.token_id)

                # 解析 orderbook 结构 - OrderBookSummary 对象使用属性访问
                try:
                    # 尝试使用属性访问
                    bids = orderbook.bids if hasattr(orderbook, 'bids') else []
                    asks = orderbook.asks if hasattr(orderbook, 'asks') else []
                except AttributeError:
                    # 如果属性访问失败，尝试字典访问（兼容旧版本）
                    if isinstance(orderbook, dict):
                        bids = orderbook.get('bids', [])
                        asks = orderbook.get('asks', [])
                    else:
                        logger.warning(f"[PRICE] 无法解析 orderbook 类型: {type(orderbook)}")
                        return None

                if not bids and not asks:
                    logger.warning("[PRICE] 获取的订单簿为空")
                    return None

                if signal.side == "BUY":
                    if not asks:
                        logger.warning("[PRICE] 无卖单盘口数据")
                        return None
                    # 处理asks数据 - 可能是对象列表或元组列表
                    try:
                        if asks and hasattr(asks[0], 'price'):
                            best_ask = float(asks[0].price)
                        elif isinstance(asks[0], (list, tuple)):
                            best_ask = float(asks[0][0])
                        elif isinstance(asks[0], dict):
                            best_ask = float(asks[0]['price'])
                        else:
                            logger.warning(f"[PRICE] 无法解析asks数据格式: {type(asks[0])}")
                            return None
                        current_price = best_ask
                    except (IndexError, KeyError, ValueError, TypeError) as e:
                        logger.warning(f"[PRICE] 解析asks价格失败: {e}")
                        return None
                else:  # SELL
                    if not bids:
                        logger.warning("[PRICE] 无买单盘口数据")
                        return None
                    # 处理bids数据
                    try:
                        if bids and hasattr(bids[0], 'price'):
                            best_bid = float(bids[0].price)
                        elif isinstance(bids[0], (list, tuple)):
                            best_bid = float(bids[0][0])
                        elif isinstance(bids[0], dict):
                            best_bid = float(bids[0]['price'])
                        else:
                            logger.warning(f"[PRICE] 无法解析bids数据格式: {type(bids[0])}")
                            return None
                        current_price = best_bid
                    except (IndexError, KeyError, ValueError, TypeError) as e:
                        logger.warning(f"[PRICE] 解析bids价格失败: {e}")
                        return None
            except Exception as e:
                logger.warning(f"[PRICE] 获取价格失败: {e}")
                return None

            # 3. 如果以上都无法获取价格，尝试从持仓缓存获取价格 (作为最后的兜底)
            if signal.side == "SELL":
                    # 卖出时，从我们的持仓缓存获取当前价格
                    try:
                        position_cache_file = os.path.join("data", "position_cache.json")
                        with open(position_cache_file, 'r', encoding='utf-8') as f:
                            position_data = json.load(f)

                        for position in position_data.get("positions", []):
                            asset_id = position.get("asset") or position.get("token_id")
                            if asset_id == signal.token_id:
                                cache_price = position.get("curPrice")
                                if cache_price and cache_price > 0:
                                    # 使用提取的函数计算溢价价格 (SELL: price - 0.02)
                                    logger.info(f"[PRICE] 从持仓缓存获取价格作为基准: {float(cache_price):.6f}")
                                    calculated_price = self._calculate_price_with_premium("SELL", cache_price)
                                    if calculated_price is not None:
                                        return calculated_price
                    except Exception as e:
                        logger.warning(f"[PRICE] 从持仓缓存获取价格失败: {e}")

            logger.warning("[PRICE] 无法获取交易员价格，使用当前市场价格")

            # 最终兜底：无论来自 trader_price 定价还是盘口 best_bid/best_ask，都强制 clamp 到 [0.001, 0.99]
            # 以避免出现 price=0.999 的情况（你最新要求“最高只能到 0.99”）。
            cap_min = 0.001
            cap_max = 0.99
            try:
                raw_price = float(current_price)
            except Exception:
                logger.warning(f"[PRICE] 最终价格无法转为 float: {current_price}")
                return None

            current_price = min(max(raw_price, cap_min), cap_max)
            if abs(current_price - raw_price) > 1e-12:
                logger.info(
                    f"[PRICE] 最终价格触发 clamp: raw={raw_price:.6f} => clamped={current_price:.6f} (range=[{cap_min:.6f},{cap_max:.6f}])"
                )

            logger.info(f"[PRICE] 最终下单价格: {current_price:.6f}")
            return current_price

        except Exception as e:
            logger.warning(f"[PRICE] 获取价格失败: {e}")
            return None

    def _apply_buy_minimums(self, target_amount: float, order_price: float) -> Optional[float]:
        """
        BUY 最小下单规则（按你最新口径）：

        执行顺序：
        1) 先按比例金额计算 shares
        2) shares 与 5 股取较大值
        3) 用最终 shares 计算名义金额 amount = shares * order_price
        4) 若 amount < 1 USDC：不跟（返回 None）

        说明：
        - `order_price` 已包含溢价 0.02 的影响（来自 [`TradeExecutionService._get_current_price()`](检测交易员交易动作解析打印/trade_execution_service.py:564)）。
        - 本函数只负责“最小股数/最小资金”的硬性约束；余额是否够用由后续余额检查处理。

        返回：
        - float：调整后的下单金额（USDC）
        - None：不满足最小资金限制（<1）或无法计算
        """
        try:
            price = float(order_price) if order_price else 0.0
            if price <= 0:
                return None

            min_shares = 5.0
            min_amount = 1.0

            amt = float(target_amount)
            raw_shares = amt / price if price > 0 else 0.0
            final_shares = max(raw_shares, min_shares)

            final_amount = final_shares * price

            if final_amount < min_amount:
                logger.info(
                    f"[ORDER] BUY 最小资金不满足：max(shares={raw_shares:.6f}, 5)={final_shares:.6f} "
                    f"=> amount={final_amount:.6f} < {min_amount:.2f} USDC，跳过该订单"
                )
                return None

            if final_shares != raw_shares:
                logger.info(
                    f"[ORDER] BUY 最小下单调整：shares {raw_shares:.6f} -> {final_shares:.6f} (min=5)，"
                    f"金额 {amt:.6f} -> {final_amount:.6f} USDC (price={price:.6f})"
                )

            return final_amount
        except Exception as e:
            logger.warning(f"[ORDER] BUY 最小下单规则计算失败，跳过该订单: {e}")
            return None

    async def _calculate_order_params(self, signal, order_price):
        """
        计算订单参数（用于下单提交前的“金额/股数/客户端ID”等字段推导）。

        目标：
        - BUY：基于“交易员本次交易金额占其余额比例”，按同一比例使用我们的余额跟单；
               策略C：不做任何补偿性调整（不买一股兜底、不抬升到$1、不二次加溢价），不满足约束则跳过下单。
        - SELL：按“清仓”处理（当前实现：交易员清仓信号 -> 我们也清仓），直接用我们持仓股数作为卖出股数。

        输入：
        - signal: [`TradeSignal`](trade_execution_service.py:51)
          - signal.amount_usdc：交易员交易金额（USDC）
          - signal.price/signal.shares：交易员成交价/股数（仅用于日志/推导 trader_price_per_share）
        - order_price: float
          - 来自 [`TradeExecutionService._get_current_price()`](trade_execution_service.py:523)，当前版本已在定价阶段完成溢价与价格区间控制

        输出：
        - tuple: (order_params, calc_details, error_reason)
          - order_params: 成功时为 dict，失败时为 None
          - calc_details: 计算过程描述字符串
          - error_reason: 失败原因字符串，成功时为 None

        关键点（与当前问题现象对应）：
        - BUY marketable 最小名义金额 $1：本函数不“抬升”金额，低于 $1 直接跳过（避免隐式改变 shares）
        - 溢价只计算一次：当前版本溢价只发生在定价阶段，本函数不再重复加溢价
        """
        try:
            # 3. 智能跟单策略：根据买卖类型使用不同逻辑
            use_market_order = False  # 默认为限价单
            calc_details = "" # 计算过程描述
            error_reason = None # 错误原因

            if signal.side == "SELL":
                # 卖出订单：基于持仓份额比例
                logger.info(f"[ORDER] 卖出订单：基于持仓份额比例计算")
                trader_balance = 1000.0
                our_balance = 1000.0
                trader_usage_ratio = 0

                # 获取我们的持仓
                our_position_shares = await self._get_position_shares(signal.token_id)
                if our_position_shares <= 0:
                    msg = "无持仓可供卖出"
                    logger.warning(f"[ORDER] {msg}，跳过")
                    return None, calc_details, msg

                # 获取交易员持仓（用于计算比例）
                trader_pos = await self._get_trader_position_detail(signal.token_id, signal.source_address)
                trader_total = trader_pos.get('total_shares', 0)

                # 计算交易员卖出股数
                trader_sold = signal.shares
                if not trader_sold or trader_sold <= 0:
                    # 尝试从金额推导
                    ref_price = signal.price if signal.price else order_price
                    if ref_price and ref_price > 0:
                        trader_sold = signal.amount_usdc / ref_price
                    else:
                        trader_sold = 0

                # 计算卖出比例
                sell_ratio = 1.0
                if trader_total > 0 and trader_sold and trader_sold > 0:
                    sell_ratio = trader_sold / trader_total
                    # 限制比例不超过 100%
                    if sell_ratio > 1.0: sell_ratio = 1.0
                
                logger.info(f"[ORDER] 交易员卖出比例: {sell_ratio*100:.2f}% ({float(trader_sold or 0):.2f}/{trader_total:.2f})")
                calc_details = f"卖出比例: {sell_ratio*100:.2f}% (交易员卖出{float(trader_sold or 0):.2f}/持仓{trader_total:.2f})"

                # 策略分支：
                # 1. 如果当前总持仓 < 5 股（零头），则直接使用市价单清仓
                # 2. 如果当前总持仓 >= 5 股，则按比例卖出，且最小卖出 5 股（使用限价单）
                
                if our_position_shares < 5.0:
                    logger.info(f"[ORDER] 当前持仓 {our_position_shares:.4f} < 5，属于零头，使用市价单 (Market Order) 清仓")
                    our_sell_shares = our_position_shares
                    use_market_order = True
                    calc_details += f"\n策略: 零头清仓 (持仓{our_position_shares:.2f}<5)"
                else:
                    # 计算我们的卖出股数
                    our_sell_shares = our_position_shares * sell_ratio
                    logger.info(f"[ORDER] 初步计算卖出: {our_sell_shares:.4f}股 (比例: {sell_ratio*100:.2f}%)")

                    # 规则1：最小卖出 5 股
                    if our_sell_shares < 5.0:
                        logger.info(f"[ORDER] 卖出股数 {our_sell_shares:.4f} < 5，调整为最小卖出单位 5 股")
                        our_sell_shares = 5.0
                        calc_details += f"\n调整: 最小卖出5股"
                    
                    # 安全检查：卖出不能超过持仓
                    if our_sell_shares > our_position_shares:
                        our_sell_shares = our_position_shares
                    
                    use_market_order = False
                    calc_details += f"\n最终卖出: {our_sell_shares:.2f}股"

                target_amount = our_sell_shares * order_price if order_price else 0
                logger.info(f"[ORDER] 最终卖出: {our_sell_shares:.4f}股 (持仓: {our_position_shares:.4f})")

            else:  # BUY 买入逻辑
                # 买入时需要检查余额
                trader_balance = await self._get_trader_usdc_balance(signal.source_address)
                our_balance = await self._get_our_usdc_balance()

                # 计算交易员余额使用比例
                if trader_balance > 0:
                    trader_usage_ratio = (signal.amount_usdc / trader_balance) * 100
                else:
                    logger.warning(f"[ORDER] 交易员余额为0，无法计算比例，使用固定跟单")
                    trader_usage_ratio = 0

                # 策略优化：大额低比例跟单优化
                # 如果交易员下单金额 > 1000 USDC 且 比例 < 1%，则强制提升比例至 1%
                # 原因：避免交易员余额过大导致大额真实交易被误判为微小试仓
                if signal.amount_usdc > 1000 and 0 < trader_usage_ratio < 1.0:
                    logger.info(f"[STRATEGY] 触发大额低比例优化: 交易员下单 {signal.amount_usdc:.2f} USDC (>1000) 且比例 {trader_usage_ratio:.3f}% (<1%)")
                    logger.info(f"[STRATEGY] 强制将跟单参考比例提升至 1.0%")
                    trader_usage_ratio = 1.0

                # 买入订单：只使用余额比例计算，不看持仓
                logger.info(f"[ORDER] 买入订单：使用余额比例计算策略")

                # 买入策略：基于交易员余额使用比例
                if trader_usage_ratio > 0:
                    # 风控规则1：限制交易员使用比例不超过配置上限 (默认10%)
                    # 如果交易员梭哈(100%)，我们只跟配置的上限比例
                    # config配置为小数(如0.1)，此处转换为百分比(10.0)以匹配 trader_usage_ratio
                    max_cap = self.config.max_trader_usage_cap * 100
                    effective_ratio = min(trader_usage_ratio, max_cap)
                    
                    # 记录风控触发
                    risk_msg = ""
                    if effective_ratio < trader_usage_ratio:
                        risk_msg = f"⚠️ 触发风控: 比例从 {trader_usage_ratio:.2f}% 限制为 {max_cap}%"
                        logger.info(f"[RISK] {risk_msg}")
                    
                    # 获取跟单比例
                    copy_ratio = self.config.get_trader_copy_ratio(signal.source_address)

                    # 按比例计算我们要投入的USDC金额 (使用限制后的比例)
                    proportional_amount = our_balance * (effective_ratio / 100)

                    # 应用跟单比例
                    target_amount = proportional_amount * copy_ratio
                    
                    # 风控规则2：跟单金额不超过交易员下单金额
                    # 取小值：min(计算金额, 交易员金额)
                    original_target = target_amount
                    target_amount = min(target_amount, signal.amount_usdc)
                    
                    if target_amount < original_target:
                        logger.info(f"[RISK] 计算金额 {original_target:.2f} 超过交易员下单金额 {signal.amount_usdc:.2f}，限制为交易员金额")
                    
                    logger.info(f"[ORDER] 买入按比例: 我们的余额({our_balance:.2f}) × {effective_ratio:.3f}% (原{trader_usage_ratio:.2f}%) = {proportional_amount:.2f} USDC")
                    logger.info(f"[ORDER] 应用跟单比例 {copy_ratio}x 后，最终下单金额: {target_amount:.2f} USDC")
                    
                    calc_details = (
                        f"交易员使用比例: {trader_usage_ratio:.2f}% (风控限制后: {effective_ratio:.2f}%)\n"
                        f"我们余额: {our_balance:.2f}\n"
                        f"按比例金额: {proportional_amount:.2f}\n"
                        f"跟单倍数: {copy_ratio}x\n"
                        f"目标金额: {target_amount:.2f} (已限制不超过交易员金额)"
                    )
                    if risk_msg:
                        calc_details += f"\n{risk_msg}"

                    # BUY 最小下单规则（按最新口径）：
                    # - 先算 shares，再与 5 股取较大值，再算金额
                    # - 若最终金额 < 1 USDC：不跟
                    adjusted_amount = self._apply_buy_minimums(target_amount, order_price)
                    if adjusted_amount is None:
                        msg = "不满足最小下单金额/股数限制"
                        return None, calc_details, msg
                    
                    if adjusted_amount != target_amount:
                        calc_details += f"\n最小限制调整: {target_amount:.2f} -> {adjusted_amount:.2f}"
                    target_amount = adjusted_amount

                    # 计算可以买入的股数（仅用于日志记录）
                    additional_shares = target_amount / order_price if order_price else 0
                    logger.info(f"[ORDER] 买入计划: {target_amount:.2f} USDC 可买入 {additional_shares:.2f} 股")

                else:
                    # 交易员余额为0时，不再使用固定跟单，而是跳过
                    msg = "交易员余额为0，无法计算比例，跳过跟单"
                    logger.warning(f"[ORDER] {msg}")
                    return None, f"跳过: {msg}", msg

            # 4. 应用订单大小限制（买入和卖出使用不同逻辑）
            max_order_size = self.config.max_order_size  # 最大订单限制
            min_order_size = self.config.min_order_size  # 最小订单限制，可能为None表示无限制

            if signal.side == "SELL":
                # 卖出订单：基于实际持仓价值
                logger.info(f"[ORDER] 卖出订单：使用持仓价值 {target_amount:.2f} USDC (卖出不设最大金额限制)")
                # 卖出不再应用最大订单限制，确保能按比例或全额卖出
            else:
                # 买入订单：应用最大订单限制
                if target_amount > max_order_size:
                    logger.warning(f"[ORDER] 跟单金额 {target_amount:.2f} 超过最大限制 {max_order_size} USDC，调整为最大值")
                    target_amount = max_order_size

                # 策略C：不做任何补偿性调整（包含最小订单金额补偿）；不满足则跳过。
                if min_order_size is not None and target_amount < min_order_size:
                    msg = f"跟单金额 {target_amount:.2f} 低于最小限制 {min_order_size} USDC"
                    logger.info(f"[ORDER] {msg}，策略C不补偿，跳过下单")
                    return None, calc_details, msg

  
            # 5. 特殊检查：对于卖出订单，确保不会超过持仓数量
            if signal.side == "SELL":
                # 重新获取持仓数量进行最终验证
                our_position_shares = await self._get_position_shares(signal.token_id)
                if our_position_shares <= 0:
                    msg = "卖出订单：无持仓"
                    logger.warning(f"[ORDER] {msg}，跳过此交易")
                    return None, calc_details, msg

                # 计算当前持仓价值（以当前市场价格计算）
                position_value_usdc = our_position_shares * order_price if order_price else 0

                # 卖出金额不能超过持仓价值
                if target_amount > position_value_usdc:
                    logger.warning(f"[ORDER] 卖出金额({target_amount:.2f})超过持仓价值({position_value_usdc:.2f})，调整为全部持仓")
                    target_amount = position_value_usdc

                logger.info(f"[ORDER] 卖出限制检查: 持仓价值 {position_value_usdc:.2f} USDC，卖出金额 {target_amount:.2f} USDC")

            # 6. 检查账户余额（仅对买入订单有效，卖出不需要USDC余额）
            if signal.side == "BUY":
                # 根据钱包类型决定是否预留gas费
                if hasattr(self, 'wallet_type') and self.wallet_type == "代理钱包":
                    max_affordable = our_balance  # 代理钱包模式不需要预留gas费
                else:
                    max_affordable = our_balance * 0.9  # 本地钱包模式保留10%作为gas费

                # 检查余额是否足够支付计算出的跟单金额
                if max_affordable < target_amount:
                    wallet_type_text = "代理钱包" if (hasattr(self, 'wallet_type') and self.wallet_type == "代理钱包") else "本地钱包"
                    msg = f"{wallet_type_text}余额不足，可用余额 {max_affordable:.2f} USDC 小于跟单金额 {target_amount:.2f} USDC"
                    logger.warning(f"[ORDER] {msg}，跳过此交易")
                    return None, calc_details, msg
            else:
                max_affordable = float('inf')  # 卖出订单不受USDC余额限制

            # 取较小值
            if target_amount > max_affordable:
                logger.warning(f"[ORDER] 余额不足，跟单金额从 {target_amount:.2f} 调整为 {max_affordable:.2f} USDC")
                order_amount = max_affordable
            else:
                order_amount = target_amount

            # 策略C：不对订单金额做最小值抬升；不满足则跳过（避免“隐式改变 shares”）。
            if min_order_size is not None and order_amount < min_order_size:
                msg = f"订单金额 {order_amount:.2f} 小于最小限制 {min_order_size} USDC"
                logger.info(f"[ORDER] {msg}，策略C不补偿，跳过下单")
                return None, calc_details, msg

            # BUY 最小下单约束由 [`TradeExecutionService._apply_buy_minimums()`](检测交易员交易动作解析打印/trade_execution_service.py:743) 负责：
            # - shares = max(按比例换算shares, 5)
            # - amount = shares * price
            # - amount < 1 USDC：跳过

            # 最终检查余额是否足够
            if order_amount > max_affordable:
                msg = f"余额不足 (需 {order_amount:.2f}, 有 {max_affordable:.2f})"
                logger.warning(f"[ORDER] {msg}，跳过此交易")
                return None, calc_details, msg

            if order_amount <= 0:
                msg = f"订单金额无效: {order_amount}"
                logger.warning(f"[ORDER] {msg}")
                return None, calc_details, msg

            limit_info = f"(最大限制: {max_order_size} USDC)" if signal.side == "BUY" else "(卖出无金额限制)"
            logger.info(f"[ORDER] 订单金额检查通过: {order_amount:.2f} USDC {limit_info}")

            # 计算股数 - 统一按金额计算 (卖出时 order_amount 已经是按比例计算好的)
            shares = order_amount / order_price if order_price and order_price > 0 else 0
            
            if signal.side == "SELL":
                logger.info(f"[ORDER] 卖出订单股数: {shares:.2f} 股 (基于金额 {order_amount:.2f} USDC)")

            # 构建订单参数
            order_params = {
                'token_id': signal.token_id,
                'side': signal.side,  # BUY/SELL
                'price': order_price,
                'size': order_amount,  # 金额（USDC）
                'shares': shares,  # 股数（卖出时直接使用持仓股数）
                'order_type': 'MARKET' if use_market_order else 'GTC',  # 根据情况选择订单类型
                'reduce_only': False,
                'time_in_force': 'FOK' if use_market_order else 'GTC', # 市价单通常使用 FOK
                'client_order_id': f"copy_{int(time.time())}_{signal.original_tx_hash[:8]}"
            }

            logger.info(f"[ORDER] 订单参数计算完成")
            logger.info(f"[ORDER] 目标金额: {target_amount:.6f} USDC")
            logger.info(f"[ORDER] 实际金额: {order_amount:.6f} USDC")
            logger.info(f"[ORDER] 下单股数: {shares:.6f} 股")
            logger.info(f"[ORDER] 下单价格: {order_price:.6f}")

            return order_params, calc_details, None

        except Exception as e:
            logger.error(f"[ORDER] 计算订单参数失败: {e}")
            return None, "", str(e)

    def _get_clob_client(self):
        """
        获取 ClobClient 实例（使用统一创建函数）。
        
        返回：
        - ClobClient：可用于下单、查询等操作
        """
        if not HAS_CLOB:
            raise ImportError("py-clob-client is not installed. Please install it with 'pip install py-clob-client'")
        
        # 使用 config.py 中的统一创建函数（支持懒加载缓存）
        from config import create_clob_client
        return create_clob_client(
            config=self.config
        )

    async def _place_gtc_order(self, order_params):
        """
        提交 GTC 限价单到 Polymarket CLOB（最终下单环节）。

        输入：
        - order_params: dict（来自 [`TradeExecutionService._calculate_order_params()`](trade_execution_service.py:702)）
          - price: 每股价格
          - size: 目标名义金额（USDC）
          - shares: 目标股数（BUY 通常为 size/price；SELL 通常为我们持仓股数）

        关键逻辑：
        - 校验交易环境（py-clob-client、私钥、funder 钱包）
        - SELL：直接使用 order_params.shares（我们持仓股数）
        - BUY（策略C）：
          - 不修改 shares、不抬升到$1、不做向上取整/加 buffer
          - 若 notional < $1 则直接跳过
          - 额外做“预检”：按 step=1e-4 对 shares 下取整后的 notional 若 < $1，也直接跳过（避免服务端量化后拒单）
        - 打印最终提交 payload（order_params/order_args/signed_order），用于排查“到底提交了什么”

        注意事项：
        - 价格上限：服务端 max=0.99，因此本函数在下单前会校验 0.001 <= price <= 0.99
        """
        try:
            logger.info(f"[PLACE] 开始下单...")
            # logger.info(f"[PLACE] Token ID: {order_params['token_id']}")  # 已删除日志输出
            logger.info(f"[PLACE] 方向: {order_params['side']}")
            # 根据买卖方向显示对应的价格描述
            side = BUY if order_params['side'] == 'BUY' else SELL
            price_text = "买入价格" if side == BUY else "卖出价格"
            logger.info(f"[PLACE] {price_text}: {order_params['price']:.6f}")

            # 显示股数而不是USDC金额
            shares = order_params.get('shares', order_params['size'] / order_params['price'] if order_params['price'] > 0 else 0)
            logger.info(f"[PLACE] 股数: {float(shares):.6f}")

            # 检查必要依赖
            if not HAS_CLOB:
                logger.error("[PLACE] py-clob-client 未安装，无法下单")
                return None

            # 实盘下单
            price = order_params['price']
            size_usdc = order_params['size']

            # CLOB 价格约束：min 0.001 - max 0.99（服务端会拒绝 >0.99）
            if price is None or not isinstance(price, (int, float)) or price < 0.001 or price > 0.99:
                logger.error(f"[PLACE] Invalid price {price}, cannot place order (price must be 0.001 <= price <= 0.99)")
                return None

            # 特殊处理：对于卖出订单，直接使用持仓股数，不进行激进定价
            side = BUY if order_params['side'] == 'BUY' else SELL
            if side == SELL:
                # 卖出时直接使用我们计算的持仓股数，不进行价格调整
                # 从order_params中获取shares，这是基于持仓计算出的正确股数
                shares = order_params.get('shares', size_usdc / price if price > 0 else 0)
                logger.info(f"[PLACE] 卖出订单使用持仓股数: {shares:.2f}股")
            else:
                # BUY：客户端预检（按已观测到的服务端硬性规则）
                # - marketable BUY 最小名义金额：$1
                # - 最小股数：5
                shares = order_params.get('shares', size_usdc / price if price > 0 else 0)

                if shares is None or not isinstance(shares, (int, float)) or shares <= 0:
                    logger.error(f"[PLACE] Invalid shares {shares}, cannot place order (shares must be > 0)")
                    return None

                notional = float(shares) * float(price)
                logger.info(
                    f"[PLACE] BUY 下单参数确认: price={float(price):.6f}, shares={float(shares):.6f}, notional={notional:.6f}"
                )

                min_buy_notional = 1.0
                min_buy_shares = 5.0

                # 添加浮点数精度容差
                if float(shares) < min_buy_shares - 0.000001:
                    logger.info(
                        f"[PLACE] BUY shares={float(shares):.6f} < min_buy_shares={min_buy_shares:.0f}，跳过下单"
                    )
                    return None

                if notional < min_buy_notional:
                    logger.info(
                        f"[PLACE] BUY notional={notional:.6f} < min_buy_notional={min_buy_notional:.6f}，跳过下单"
                    )
                    return None

            if order_params.get('order_type') == 'MARKET':
                # 市价单参数
                order_args = MarketOrderArgs(
                    token_id=order_params['token_id'],
                    amount=shares, # MarketOrderArgs 使用 amount 表示股数
                    side=side,
                )
                logger.info(f"[PLACE] 构建市价单参数: {shares} 股")
            else:
                # 限价单参数
                order_args = OrderArgs(
                    token_id=order_params['token_id'],
                    price=price,
                    size=shares,
                    side=side,
                )

            # ===== 下单入参打印（用于排查“到底提交了什么”）=====
            try:
                notional = float(shares) * float(price)
            except Exception:
                notional = None

            try:
                logger.info(
                    "[PLACE-PAYLOAD] order_params="
                    + json.dumps(
                        {
                            "token_id": str(order_params.get("token_id")),
                            "side": str(order_params.get("side")),
                            "price": float(price) if isinstance(price, (int, float)) else price,
                            "size_usdc_intended": float(order_params.get("size")) if isinstance(order_params.get("size"), (int, float)) else order_params.get("size"),
                            "shares_submitted": float(shares) if isinstance(shares, (int, float)) else shares,
                            "notional_estimated": float(notional) if isinstance(notional, (int, float)) else notional,
                            "client_order_id": str(order_params.get("client_order_id")),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            except Exception as e:
                logger.warning(f"[PLACE-PAYLOAD] 打印 order_params 失败: {e}")

            try:
                logger.info(
                    "[PLACE-PAYLOAD] order_args="
                    + json.dumps(
                        {
                            "token_id": str(order_args.token_id),
                            "side": str(order_args.side),
                            "price": float(order_args.price),
                            "size_shares": float(order_args.size),
                            "notional_estimated": float(order_args.size) * float(order_args.price),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            except Exception as e:
                logger.warning(f"[PLACE-PAYLOAD] 打印 order_args 失败: {e}")

            client = self._get_clob_client()

            # 使用quick_sell.py的成功逻辑：重新生成API凭证并直接提交
            try:
                # 重新生成API凭证
                creds = await asyncio.to_thread(client.create_or_derive_api_creds)
                client.set_api_creds(creds)
                # logger.info(f"[PLACE] API凭证重新生成成功: {creds.api_key[:8]}...")  # 已删除日志输出
            except Exception as e:
                logger.error(f"[PLACE] API凭证生成失败: {e}")
                return None

            # 同步方法在异步环境中执行，使用线程池避免阻塞事件循环
            if order_params.get('order_type') == 'MARKET':
                signed_order = await asyncio.to_thread(client.create_market_order, order_args)
            else:
                signed_order = await asyncio.to_thread(client.create_order, order_args)

            # ===== 签名订单打印（不含任何私钥；仅签名结果/结构）=====
            try:
                if isinstance(signed_order, dict):
                    logger.info(
                        "[PLACE-PAYLOAD] signed_order(dict)="
                        + json.dumps(signed_order, ensure_ascii=False, separators=(",", ":"))
                    )
                else:
                    logger.info(f"[PLACE-PAYLOAD] signed_order(type)={type(signed_order)} value={signed_order}")
            except Exception as e:
                logger.warning(f"[PLACE-PAYLOAD] 打印 signed_order 失败: {e}")

            # 直接使用post_order提交（quick_sell.py成功方式）
            try:
                if order_params.get('order_type') == 'MARKET':
                    resp = await asyncio.to_thread(client.post_order, signed_order, ClobOrderType.FOK)
                else:
                    resp = await asyncio.to_thread(client.post_order, signed_order)
                logger.info(f"[PLACE] 订单提交成功")
                try:
                    logger.info(
                        "[PLACE-PAYLOAD] post_order_resp="
                        + json.dumps(resp, ensure_ascii=False, separators=(",", ":"))
                    )
                except Exception:
                    logger.info(f"[PLACE-PAYLOAD] post_order_resp(type)={type(resp)} value={resp}")
            except Exception as e:
                logger.error(f"[PLACE] 订单提交失败: {e}")
                return None

            # 使用quick_sell.py的响应判断逻辑
            if resp and isinstance(resp, dict) and resp.get('success'):
                order_id = resp.get('orderID')
                if not order_id:
                    logger.error(f"[PLACE] 实盘下单返回缺少 orderID: {resp}")
                    return None

                status = resp.get('status', 'N/A')
                taking_amount = resp.get('takingAmount', '0')

                logger.info(f"[PLACE] 实盘下单成功，订单ID: {order_id}")
                logger.info(f"[PLACE] 订单状态: {status}")

                # 注意：Polymarket/py-clob-client 的响应字段 takingAmount 在 BUY 场景下更像“成交股数”
                # 你的现象：price≈0.934 时，takingAmount=5 但你期望成交金额≈4.67（=5股*0.934）
                # 因此这里按 side 做区分打印，并给出“成交金额(按 shares*price 计算)”以对齐你关注的金额口径。
                try:
                    taking_amount_f = float(taking_amount)
                except Exception:
                    taking_amount_f = None

                if side == BUY:
                    if taking_amount_f is not None:
                        est_notional_usdc = taking_amount_f * float(price)
                        logger.info(f"[PLACE] 成交股数(takingAmount): {taking_amount_f:.6f} 股")
                        logger.info(f"[PLACE] 成交金额(按 shares*price 计算): {est_notional_usdc:.6f} USDC")
                    else:
                        logger.info(f"[PLACE] 成交股数(takingAmount): {taking_amount} (raw)")
                else:
                    # SELL 场景下保持原语义（通常 takingAmount 更接近 USDC）
                    logger.info(f"[PLACE] 成交金额(takingAmount): {taking_amount} USDC")

                logger.info(f"[PLACE] 下单股数: {shares} 股")

                # 创建订单状态记录
                self.orders_cache[order_id] = OrderStatus(
                    order_id=order_id,
                    token_id=order_params['token_id'],
                    side=order_params['side'],
                    amount=size_usdc,
                    filled_amount=0,
                    price=price,
                    created_at=time.time(),
                    status='pending',
                    expires_at=time.time() + self.order_timeout,
                    original_signal=order_params['client_order_id']
                )

                # 关键修复：返回 order_id（供订单生命周期管理/撤单使用）
                # 同时返回 taking_amount 用于判断是否立即成交
                return order_id, taking_amount
            else:
                logger.error(f"[PLACE] 实盘下单失败: {resp}")
                return None

        except Exception as e:
            error_msg = str(e)

            # 精确匹配余额不足的错误信息
            if "not enough balance / allowance" in error_msg:
                logger.error(f"[PLACE] 下单失败: 余额不足或授权不足")
                logger.error(f"[BALANCE] ⚠️  余额检查提醒:")
                logger.error(f"[BALANCE]    - 当前尝试交易金额: {size_usdc:.2f} USDC")
                logger.error(f"[BALANCE]    - 钱包地址: {self.config.proxy_wallet_address}")
                logger.error(f"[BALANCE]    - 请检查:")
                logger.error(f"[BALANCE]      1. USDC余额是否充足 (需要至少 {size_usdc:.2f} USDC)")
                logger.error(f"[BALANCE]      2. USDC是否已授权给CLOB交易所")
                logger.error(f"[BALANCE]    - 运行 check_balance.py 检查当前余额")

            else:
                logger.error(f"[PLACE] 下单失败: {e}", exc_info=True)

            return None

    async def _manage_order_lifecycle(self, order_id, signal):
        """管理订单生命周期"""
        try:
            logger.info(f"[MANAGE] 开始管理订单: {order_id}")

            # 定期检查订单状态
            check_interval = 10  # 10秒检查一次
            start_time = time.time()

            while time.time() - start_time < self.order_timeout:
                await asyncio.sleep(check_interval)

                # 检查订单状态
                order_status = await self._get_order_status(order_id)
                if not order_status:
                    # 订单状态查询失败，可能是网络问题或API问题
                    # 但订单可能已经成交，需要更新持仓数据
                    logger.warning(f"[MANAGE] 订单状态查询失败: {order_id}，可能是网络问题，更新持仓数据")
                    await self._update_positions_after_trade(signal)
                    continue

                # 更新订单缓存
                if order_id in self.orders_cache:
                    self.orders_cache[order_id] = order_status

                # 订单完成检查
                if order_status.status in ['filled', 'cancelled']:
                    logger.info(f"[MANAGE] 订单已完成: {order_id}, 状态: {order_status.status}")
                    break

                # 部分成交处理
                if order_status.status == 'partial_filled':
                    logger.info(f"[MANAGE] 订单部分成交: {order_id}, 已成交: {order_status.filled_amount:.2f}")

                # 超时检查
                if time.time() > order_status.expires_at:
                    logger.info(f"[MANAGE] 订单超时，执行撤单: {order_id}")
                    await self._cancel_order(order_id)
                    break

        except Exception as e:
            logger.error(f"[MANAGE] 订单管理失败: {e}")

        # 无论订单管理如何结束，都要更新持仓数据
        logger.info(f"[MANAGE] 订单管理结束，更新持仓数据")
        await self._update_positions_after_trade(signal)

    async def _get_order_status(self, order_id):
        """获取订单状态"""
        try:

            client = self._get_clob_client()
            # 获取订单详情
            order_data = await asyncio.to_thread(client.get_order, order_id)

            # 映射状态
            status_map = {
                'OPEN': 'pending',
                'PARTIAL_FILLED': 'partial_filled',
                'FILLED': 'filled',
                'CANCELLED': 'cancelled',
                'EXPIRED': 'expired',
            }
            raw_status = order_data.get('status', 'UNKNOWN')
            status = status_map.get(raw_status, 'unknown')

            # 确保 side 是大写
            side = order_data.get('side', '').upper()

            # 解析数量
            # API返回的size和filledSize可能是字符串
            size = float(order_data.get('size', 0))
            filled = float(order_data.get('filledSize', 0))
            price = float(order_data.get('price', 0))

            # 时间戳处理（可能为毫秒）
            created_at = order_data.get('createdAt')
            if created_at is not None:
                try:
                    # 如果是毫秒，转换为秒
                    if created_at > 1e10:  # 大于 2e9 认为是毫秒
                        created_at = created_at / 1000.0
                except:
                    created_at = time.time()
            else:
                created_at = time.time()

            expires_at = order_data.get('expiresAt')
            if expires_at is not None:
                try:
                    if expires_at > 1e10:
                        expires_at = expires_at / 1000.0
                except:
                    expires_at = time.time() + self.order_timeout
            else:
                expires_at = time.time() + self.order_timeout

            order_status = OrderStatus(
                order_id=order_id,
                token_id=order_data.get('tokenId', ''),
                side=side,
                amount=size,
                filled_amount=filled,
                price=price,
                created_at=created_at,
                status=status,
                expires_at=expires_at,
                original_signal=''  # 无法从API获取，留空
            )
            # 更新缓存
            self.orders_cache[order_id] = order_status
            return order_status

        except Exception as e:
            logger.error(f"[STATUS] 获取订单状态失败: {e}")
            return None

    async def _cancel_order(self, order_id):
        """取消订单"""
        try:
            logger.info(f"[CANCEL] 取消订单: {order_id}")

            client = self._get_clob_client()

            # 尝试刷新凭证以确保撤单成功 (参考下单逻辑)
            try:
                creds = await asyncio.to_thread(client.create_or_derive_api_creds)
                client.set_api_creds(creds)
            except Exception as e:
                logger.warning(f"[CANCEL] 刷新凭证失败，尝试使用现有凭证撤单: {e}")

            # 执行撤单并获取响应
            resp = await asyncio.to_thread(client.cancel, order_id)
            
            logger.info(f"[CANCEL] 撤单响应: {resp}")

            # 更新缓存
            if order_id in self.orders_cache:
                self.orders_cache[order_id].status = 'cancelled'
            logger.info(f"[CANCEL] 订单已标记为取消")
        except Exception as e:
            logger.error(f"[CANCEL] 取消订单失败: {e}")


    # API调用方法 (待实现)
    async def _get_trader_usdc_balance(self, trader_address):
        """
        获取交易员 USDC 余额（用于“余额比例跟单”的分母）。

        数据来源：
        - 优先从内存监控器 `memory_monitor.get_balance_cache()` 读取（减少 API 请求）
        - 读取失败时返回一个保守的默认值（当前实现：1000.0），避免主流程中断

        注意：
        - 该余额仅用于计算比例与日志，不直接参与链上资金扣款。
        """
        try:
            # 优先从内存变量读取
            if hasattr(self, 'memory_monitor') and self.memory_monitor:
                balance_cache = self.memory_monitor.get_balance_cache()
                
                target_key = None
                if balance_cache:
                    if trader_address in balance_cache:
                        target_key = trader_address
                    else:
                        # 大小写不敏感查找
                        trader_lower = trader_address.lower()
                        for k in balance_cache:
                            if k.lower() == trader_lower:
                                target_key = k
                                break

                if target_key:
                    # 修复：BalanceRecord是对象，需要用.属性访问，而不是字典键访问
                    balance_record = balance_cache[target_key]
                    balance_usdc = float(balance_record.balance)
                    logger.info(f"[BALANCE] 交易员 {trader_address[:8]}... 余额: {balance_usdc:.2f} USDC (从内存读取)")
                    return balance_usdc
                else:
                    logger.warning(f"[BALANCE] 交易员 {trader_address[:8]}... 余额信息不存在于内存中")
                    if balance_cache:
                        logger.warning(f"[BALANCE] 内存中可用地址示例: {list(balance_cache.keys())[:3]}...")
                    return 1000.0  # 返回默认值避免程序中断
            else:
                # 如果没有内存监控器，返回默认值
                logger.warning(f"[BALANCE] 内存监控器不可用，使用默认余额")
                return 1000.0

        except Exception as e:
            logger.warning(f"[BALANCE] 从内存获取交易员余额失败: {e}")
            return 1000.0  # 返回默认值避免程序中断

    async def _get_our_usdc_balance(self):
        """
        获取我们钱包的 USDC 余额（用于决定是否能下单/以及跟单金额上限）。

        数据来源：
        - 优先从内存监控器 `memory_monitor.get_balance_cache()` 读取（减少 API 请求）
        - 读取失败时返回默认值（当前实现：10.0），避免主流程中断

        余额地址选择：
        - 优先使用 `self.proxy_wallet`
        - 否则按环境变量 `PROXY_WALLET_ADDRESS` -> `LOCAL_WALLET_ADDRESS` 兜底
        """
        try:
            # 获取我们的钱包地址（优先使用代理钱包地址）
            our_address = None
            if hasattr(self, 'proxy_wallet') and self.proxy_wallet:
                our_address = self.proxy_wallet
            else:
                # 从配置获取代理钱包地址
                our_address = self.config.proxy_wallet_address

            # 如果没有代理钱包地址，尝试从本地钱包地址获取
            if not our_address:
                our_address = self.config.local_wallet_address

            if not our_address:
                logger.error(f"[BALANCE] 未配置我们的钱包地址")
                return 10.0  # 返回默认值避免程序中断

            # 优先从内存变量读取
            if hasattr(self, 'memory_monitor') and self.memory_monitor:
                balance_cache = self.memory_monitor.get_balance_cache()

                target_key = None
                if balance_cache:
                    if our_address in balance_cache:
                        target_key = our_address
                    else:
                        # 大小写不敏感查找
                        our_lower = our_address.lower()
                        for k in balance_cache:
                            if k.lower() == our_lower:
                                target_key = k
                                break

                if target_key:
                    # 修复：BalanceRecord是对象，需要用.属性访问，而不是字典键访问
                    balance_record = balance_cache[target_key]
                    balance_usdc = float(balance_record.balance)
                    logger.info(f"[BALANCE] 我们的钱包 {our_address[:8]}... 余额: {balance_usdc:.2f} USDC (从内存读取)")
                    return balance_usdc
                else:
                    logger.warning(f"[BALANCE] 我们的钱包 {our_address[:8]}... 余额信息不存在于内存中")
                    return 10.0  # 返回默认值避免程序中断
            else:
                # 如果没有内存监控器，返回默认值
                logger.warning(f"[BALANCE] 内存监控器不可用，使用默认余额")
                return 10.0

        except Exception as e:
            logger.warning(f"[BALANCE] 从内存获取我们的余额失败: {e}")
            return 10.0  # 返回默认值避免程序中断

    async def _get_account_balance(self):
        """获取账户余额（保留兼容性）"""
        return await self._get_our_usdc_balance()

    def get_order_statistics(self):
        """获取订单统计信息"""
        total_orders = len(self.orders_cache)
        pending_orders = sum(1 for order in self.orders_cache.values() if order.status == 'pending')
        filled_orders = sum(1 for order in self.orders_cache.values() if order.status == 'filled')

        return {
            'total_orders': total_orders,
            'pending_orders': pending_orders,
            'filled_orders': filled_orders,
            'processed_signals': len(self.processed_signals)
        }

    async def _update_positions_after_trade(self, signal):
        """跟单成功后更新持仓数据（仅目标交易员和自己）"""
        try:
            logger.info(f"[UPDATE] 跟单成交，开始更新持仓数据...")

            # 1. 更新目标交易员的持仓
            logger.info(f"[UPDATE] 更新目标交易员 {signal.source_address[:8]}... 的持仓")
            await self._fetch_trader_positions(signal.source_address)

            # 2. 更新我们自己的持仓
            logger.info(f"[UPDATE] 更新我们自己的持仓")
            await self._fetch_and_cache_positions()

            # 3. 更新余额数据
            logger.info(f"[UPDATE] 更新余额数据")
            await self._update_trader_balance(signal.source_address)
            await self._fetch_and_cache_balance()

            logger.info(f"[UPDATE] 持仓数据更新完成")

        except Exception as e:
            logger.warning(f"[UPDATE] 跟单后持仓更新失败: {e}")
            # 更新失败不影响交易执行，只记录日志

    async def _fetch_trader_positions(self, trader_address: str):
        """获取并缓存指定交易员的持仓"""
        try:
            client = self._get_clob_client()
            # 获取交易员持仓
            positions_data = await asyncio.to_thread(client.get_positions, trader_address)

            # 更新交易员持仓缓存
            trader_positions_file = os.path.join("data", "trader_positions_cache.json")

            # 读取现有缓存
            if os.path.exists(trader_positions_file):
                with open(trader_positions_file, 'r', encoding='utf-8') as f:
                    trader_positions_cache = json.load(f)
            else:
                trader_positions_cache = {}

            # 更新缓存
            trader_positions_cache[trader_address] = positions_data

            # 保存缓存
            with open(trader_positions_file, 'w', encoding='utf-8') as f:
                json.dump(trader_positions_cache, f, indent=2, ensure_ascii=False)

            logger.info(f"[POSITION] 交易员 {trader_address[:8]}... 持仓缓存已更新: {len(positions_data)}个持仓")

        except Exception as e:
            logger.warning(f"[POSITION] 获取交易员持仓失败: {e}")

    async def _fetch_and_cache_positions(self):
        """获取并缓存我们自己的持仓"""
        try:
            client = self._get_clob_client()
            # 获取我们的持仓
            positions_data = await asyncio.to_thread(client.get_positions)

            # 更新持仓缓存
            position_cache_file = os.path.join("data", "position_cache.json")

            # 读取我们的钱包地址
            our_address = self.config.proxy_wallet_address
            if not our_address:
                our_address = "0xCf2EAeAaB60579cd9d5750b5FF37a29F7d92c972"  # 默认地址

            # 构建持仓缓存数据
            position_cache = {
                "wallet_address": our_address,
                "positions": positions_data,
                "timestamp": datetime.now().isoformat(),
                "total_positions": len(positions_data)
            }

            # 保存缓存
            with open(position_cache_file, 'w', encoding='utf-8') as f:
                json.dump(position_cache, f, indent=2, ensure_ascii=False)

            logger.info(f"[POSITION] 我们持仓缓存已更新: {len(positions_data)}个持仓")

        except Exception as e:
            logger.warning(f"[POSITION] 获取我们持仓失败: {e}")

    async def _update_trader_balance(self, trader_address: str):
        """更新交易员余额缓存"""
        try:
            client = self._get_clob_client()
            # 获取交易员余额
            balance_data = await asyncio.to_thread(client.get_balance, trader_address)

            # 更新余额缓存
            balance_cache_file = os.path.join("data", "balance_cache.json")

            # 读取现有缓存
            if os.path.exists(balance_cache_file):
                with open(balance_cache_file, 'r', encoding='utf-8') as f:
                    balance_cache = json.load(f)
            else:
                balance_cache = {}

            # 更新缓存
            balance_cache[trader_address] = {
                "balance": float(balance_data),
                "timestamp": datetime.now().isoformat()
            }

            # 保存缓存
            with open(balance_cache_file, 'w', encoding='utf-8') as f:
                json.dump(balance_cache, f, indent=2, ensure_ascii=False)

            logger.info(f"[BALANCE] 交易员 {trader_address[:8]}... 余额已更新: {float(balance_data):.2f} USDC")

        except Exception as e:
            logger.warning(f"[BALANCE] 更新交易员余额失败: {e}")

    async def _fetch_and_cache_balance(self):
        """获取并缓存我们自己的余额"""
        try:
            client = self._get_clob_client()
            # 获取我们的余额
            balance_data = await asyncio.to_thread(client.get_balance)

            # 更新余额缓存
            balance_cache_file = os.path.join("data", "balance_cache.json")

            # 读取我们的钱包地址
            our_address = self.config.proxy_wallet_address
            if not our_address:
                our_address = "0xCf2EAeAaB60579cd9d5750b5FF37a29F7d92c972"  # 默认地址

            # 读取现有缓存
            if os.path.exists(balance_cache_file):
                with open(balance_cache_file, 'r', encoding='utf-8') as f:
                    balance_cache = json.load(f)
            else:
                balance_cache = {}

            # 更新缓存
            balance_cache[our_address] = {
                "balance": float(balance_data),
                "timestamp": datetime.now().isoformat()
            }

            # 保存缓存
            with open(balance_cache_file, 'w', encoding='utf-8') as f:
                json.dump(balance_cache, f, indent=2, ensure_ascii=False)

            logger.info(f"[BALANCE] 我们余额已更新: {float(balance_data):.2f} USDC")

        except Exception as e:
            logger.warning(f"[BALANCE] 更新我们余额失败: {e}")