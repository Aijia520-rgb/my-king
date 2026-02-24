#!/usr/bin/env python3
"""
集成监控程序
监控交易员余额、我的持仓、交易员持仓，并保存到JSON文件
"""

import asyncio
import aiohttp
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass
import argparse
import logging
from config import get_config, logger
from rate_limiter import global_rate_limiter

@dataclass
class BalanceRecord:
    """余额记录"""
    balance: float
    timestamp: str

@dataclass
class TraderPosition:
    """交易员持仓记录"""
    trader_address: str
    token_id: str
    condition_id: str
    title: str
    outcome: str
    size: float  # 当前持仓股数
    total_bought: float  # 总买入股数
    avg_price: float  # 平均买入价格 (USDC/股)
    cur_price: float   # 当前价格 (USDC/股)
    initial_value: float  # 初始价值 (USDC)
    current_value: float  # 当前价值 (USDC)
    cash_pnl: float  # 现金盈亏 (USDC)
    percent_pnl: float  # 百分比盈亏 (%)
    end_date: str
    redeemable: bool
    negative_risk: bool
    timestamp: str

    # 添加注释字段
    def get_status_comment(self) -> str:
        """获取持仓状态注释"""
        if self.cash_pnl > 0:
            return f"盈利 {self.percent_pnl:.2f}%"
        elif self.cash_pnl < 0:
            return f"亏损 {abs(self.percent_pnl):.2f}%"
        else:
            return "持平"

    def get_risk_level(self) -> str:
        """获取风险等级注释"""
        if self.negative_risk:
            return "低风险(保本型)"
        elif self.percent_pnl < -50:
            return "高风险(大幅亏损)"
        elif self.percent_pnl > 50:
            return "高收益(大幅盈利)"
        else:
            return "中等风险"

class IntegratedMonitor:
    """集成监控器 - 监控交易员余额、我的持仓、交易员持仓"""

    def __init__(self):
        # 内存缓存变量
        self.balance_cache = {}
        self.position_cache = {}
        self.trader_positions_cache = {}
        self.shared_positions_cache = {}

        self.api_url = "https://data-api.polymarket.com/positions"
        self.config = get_config()
        
        # 并发控制信号量 (限制同时进行的RPC请求数)
        self.semaphore = asyncio.Semaphore(5)

    def _load_trader_addresses(self) -> List[str]:
        """从配置加载交易员地址"""
        try:
            # 优先使用 self.config.target_traders
            addresses = self.config.target_traders
            
            # 确保是列表
            if not isinstance(addresses, list):
                addresses = []

            # 过滤无效地址
            valid_addresses = []
            for addr in addresses:
                if isinstance(addr, str):
                    clean_addr = addr.strip().strip('"\'[]')
                    if clean_addr.startswith('0x') and len(clean_addr) == 42:
                        valid_addresses.append(clean_addr)
            
            return valid_addresses
        except Exception as e:
            print(f"加载交易员地址失败: {e}")
            return []

    async def get_trader_balance(self, trader_address: str) -> Optional[float]:
        """获取交易员余额（使用RPC，带重试和并发控制）"""
        async with self.semaphore:  # 并发控制
            try:
                # 从配置加载RPC节点
                rpc_url = self.config.rpc_url

                # 修正后的合约地址列表
                usdc_contracts = [
                    ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "USDC (Bridged)"),
                    ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "USDC (Native)"),
                ]

                total_balance = 0.0
                errors = []
                success_count = 0

                async with aiohttp.ClientSession() as session:
                    for contract_addr, token_name in usdc_contracts:
                        data = {
                            "jsonrpc": "2.0",
                            "method": "eth_call",
                            "params": [{
                                "to": contract_addr,
                                "data": f"0x70a08231000000000000000000000000{trader_address[2:]}"
                            }, "latest"],
                            "id": 1
                        }

                        # 重试逻辑
                        max_retries = 3
                        for attempt in range(max_retries):
                            try:
                                async with session.post(rpc_url, json=data, timeout=10) as response:
                                    if response.status == 200:
                                        result = await response.json()
                                        if "result" in result and result["result"] and result["result"] != "0x":
                                            # 大多数稳定币都有6位小数
                                            balance_wei = int(result["result"], 16)
                                            balance = balance_wei / 1000000
                                            total_balance += balance
                                            success_count += 1
                                            break  # 成功，跳出重试循环
                                        elif "error" in result:
                                            if attempt == max_retries - 1:
                                                errors.append(f"{token_name} RPC Error: {result['error']}")
                                    elif response.status == 429:
                                        # 限流，等待后重试
                                        wait_time = 2 * (attempt + 1)
                                        await asyncio.sleep(wait_time)
                                        continue
                                    else:
                                        if attempt == max_retries - 1:
                                            errors.append(f"{token_name} HTTP {response.status}")
                            except Exception as e:
                                if attempt == max_retries - 1:
                                    errors.append(f"{token_name} Exception: {str(e)}")
                                await asyncio.sleep(1)
                
                # 如果所有尝试都失败了，且没有获取到任何余额
                if errors and success_count == 0:
                    raise Exception("; ".join(errors))

                return total_balance
            except Exception as e:
                # 将异常向上传递，以便在外部捕获并显示
                raise e

    def load_balances(self) -> Dict[str, BalanceRecord]:
        """从内存变量加载余额"""
        return self.balance_cache

    def save_balances(self, balances: Dict[str, BalanceRecord]):
        """保存余额到内存变量"""
        self.balance_cache = balances

    def _load_my_wallet_addresses(self) -> List[str]:
        """从配置加载自己的钱包地址"""
        try:
            addresses = []

            # 根据WALLET_SWITCH加载对应的钱包地址
            wallet_switch = self.config.wallet_switch

            if wallet_switch == 1:
                # 本地钱包
                local_address = self.config.local_wallet_address
                if local_address and local_address.startswith('0x') and len(local_address) == 42:
                    addresses.append(local_address)
            else:
                # 代理钱包
                proxy_address = self.config.proxy_wallet_address
                if proxy_address and proxy_address.startswith('0x') and len(proxy_address) == 42:
                    addresses.append(proxy_address)

            return addresses
        except Exception as e:
            print(f"加载钱包地址失败: {e}")
            return []

    async def get_my_positions(self, wallet_address: str) -> List[Dict]:
        """获取自己钱包的持仓"""
        try:
            # 使用Polymarket Data API获取持仓
            api_url = "https://data-api.polymarket.com/positions"
            all_positions = []
            offset = 0
            limit = 100  # API单次查询限制

            while True:
                params = {
                    'user': wallet_address,
                    'limit': limit,
                    'offset': offset
                }

                # 获取令牌
                await global_rate_limiter.acquire()

                async with aiohttp.ClientSession() as session:
                    async with session.get(api_url, params=params) as response:
                        if response.status == 200:
                            positions = await response.json()
                            if not positions:
                                break  # 没有更多数据了

                            all_positions.extend(positions)

                            # 如果返回的持仓数量少于limit，说明已经获取完所有数据
                            if len(positions) < limit:
                                break

                            offset += limit
                        else:
                            print(f"获取持仓失败: HTTP {response.status}")
                            break

            return all_positions
        except Exception as e:
            print(f"获取持仓失败: {e}")
            return []

    def save_positions(self, wallet_address: str, positions: List[Dict]):
        """保存持仓到内存变量"""
        try:
            data = {
                "wallet_address": wallet_address,
                "positions": positions,
                "timestamp": datetime.now().isoformat(),
                "total_positions": len(positions)
            }

            # 保存到内存变量
            self.position_cache = data
        except Exception as e:
            print(f"保存持仓失败: {e}")

    def load_positions(self) -> Optional[Dict]:
        """从内存变量加载持仓"""
        return self.position_cache if self.position_cache else None

    def print_positions_summary(self, positions_data: Dict):
        """打印持仓汇总 - 简化版"""
        if not positions_data or not positions_data.get('positions'):
            print("[INFO] 当前无持仓")
            return

        wallet = positions_data.get('wallet_address', '')[:10] + '...'
        timestamp = positions_data.get('timestamp', '')
        positions = positions_data.get('positions', [])

        # 计算汇总数据
        total_initial = 0
        total_current = 0
        total_pnl = 0
        profit_count = 0
        loss_count = 0

        for pos in positions:
            initial_value = float(pos.get('initialValue', 0))
            current_value = float(pos.get('currentValue', 0))
            cash_pnl = float(pos.get('cashPnl', 0))

            total_initial += initial_value
            total_current += current_value
            total_pnl += cash_pnl

            if cash_pnl > 0:
                profit_count += 1
            elif cash_pnl < 0:
                loss_count += 1

        print(f"\n===== 我的持仓汇总 ({wallet}) =====")
        print(f"[总数] {len(positions)} 个持仓")
        print(f"[盈亏] ${total_pnl:+.2f} USDC ({total_pnl/total_initial*100:+.2f}%)")
        print(f"[价值] 初始: ${total_initial:.2f} → 当前: ${total_current:.2f}")
        print(f"[盈亏数量] 盈利: {profit_count} | 亏损: {loss_count} | 打平: {len(positions)-profit_count-loss_count}")
        print("=" * 45)

    async def update_my_positions(self, show_log: bool = True):
        """更新我的持仓"""
        wallet_addresses = self._load_my_wallet_addresses()

        if not wallet_addresses:
            print("错误: 未找到钱包地址")
            print("请在.env中配置 LOCAL_WALLET_ADDRESS 或 PROXY_WALLET_ADDRESS")
            return

        for wallet_address in wallet_addresses:
            positions = await self.get_my_positions(wallet_address)

            # 持仓更新日志已删除 - 不需要显示更新数量

            # 无论是否有持仓，都要保存最新状态
            self.save_positions(wallet_address, positions)


    async def get_trader_positions(self, trader_address: str) -> List[Dict]:
        """获取交易员持仓"""
        try:
            all_positions = []
            offset = 0
            limit = 100  # API单次查询限制

            while True:
                params = {
                    'user': trader_address,
                    'limit': limit,
                    'offset': offset
                }

                # 获取令牌
                await global_rate_limiter.acquire()

                async with aiohttp.ClientSession() as session:
                    async with session.get(self.api_url, params=params) as response:
                        if response.status == 200:
                            positions = await response.json()
                            if not positions:
                                break  # 没有更多数据了

                            all_positions.extend(positions)

                            # 如果返回的持仓数量少于limit，说明已经获取完所有数据
                            if len(positions) < limit:
                                break

                            offset += limit
                        else:
                            print(f"获取交易员 {trader_address[:8]}... 持仓失败: HTTP {response.status}")
                            break

            return all_positions
        except Exception as e:
            print(f"获取交易员 {trader_address[:8]}... 持仓异常: {e}")
            return []

    def parse_trader_position_data(self, positions: List[Dict], trader_address: str) -> List[TraderPosition]:
        """解析交易员持仓数据"""
        trader_positions = []

        for pos in positions:
            try:
                position = TraderPosition(
                    trader_address=trader_address,
                    token_id=pos.get('asset', ''),
                    condition_id=pos.get('conditionId', ''),
                    title=pos.get('title', ''),
                    outcome=pos.get('outcome', 'UNKNOWN'),
                    size=float(pos.get('size', 0)),
                    total_bought=float(pos.get('totalBought', 0)),
                    avg_price=float(pos.get('avgPrice', 0)),
                    cur_price=float(pos.get('curPrice', 0)),
                    initial_value=float(pos.get('initialValue', 0)),
                    current_value=float(pos.get('currentValue', 0)),
                    cash_pnl=float(pos.get('cashPnl', 0)),
                    percent_pnl=float(pos.get('percentPnl', 0)),
                    end_date=pos.get('endDate', ''),
                    redeemable=pos.get('redeemable', False),
                    negative_risk=pos.get('negativeRisk', False),
                    timestamp=datetime.now().isoformat()
                )

                trader_positions.append(position)

            except (ValueError, TypeError, KeyError) as e:
                print(f"解析持仓数据失败: {e}")
                continue

        return trader_positions

    def load_trader_positions_cache(self) -> Dict[str, List[Dict]]:
        """从内存变量加载缓存的交易员持仓数据"""
        return self.trader_positions_cache

    def save_trader_positions_to_cache(self, trader_positions: Dict[str, List[Dict]]):
        """保存交易员持仓数据到内存变量"""
        self.trader_positions_cache = trader_positions

    async def update_trader_positions(self):
        """更新所有交易员的持仓数据（已禁用，改为只更新共同持仓）"""
        print("[INFO] 全量交易员持仓更新已禁用，使用精确查询只更新共同持仓")
        # 不再查询所有持仓，只通过 shared_position_check 更新共同持仓
        return

    def print_trader_positions_summary(self):
        """打印所有交易员的持仓汇总"""
        cached_data = self.load_trader_positions_cache()

        if not cached_data:
            print("未找到交易员持仓数据")
            return

        print(f"\n===== 交易员持仓汇总 =====")

        total_positions = 0
        total_initial_value = 0.0
        total_current_value = 0.0
        total_pnl = 0.0

        for trader_address, positions in cached_data.items():
            if not positions:
                continue

            # 统计该交易员的持仓
            position_count = len(positions)
            trader_initial_value = sum(pos.get('initial_value', 0) for pos in positions)
            trader_current_value = sum(pos.get('current_value', 0) for pos in positions)
            trader_pnl = sum(pos.get('cash_pnl', 0) for pos in positions)

            total_positions += position_count
            total_initial_value += trader_initial_value
            total_current_value += trader_current_value
            total_pnl += trader_pnl

            print(f"交易员 {trader_address[:8]}...: {position_count} 个持仓")
            print(f"  初始价值: ${trader_initial_value:.2f}")
            print(f"  当前价值: ${trader_current_value:.2f}")
            print(f"  盈亏: ${trader_pnl:.2f}")

            # 显示该交易员最近3个持仓详情
            recent_positions = sorted(positions, key=lambda x: x.get('timestamp', ''), reverse=True)[:3]
            for i, pos in enumerate(recent_positions, 1):
                title = pos.get('title', 'N/A')[:45]
                outcome = pos.get('outcome', 'N/A')
                size = pos.get('size', 0)
                avg_price = pos.get('avg_price', 0)
                cur_price = pos.get('cur_price', 0)
                cash_pnl = pos.get('cash_pnl', 0)
                percent_pnl = pos.get('percent_pnl', 0)
                initial_value = pos.get('initial_value', 0)
                current_value = pos.get('current_value', 0)
                total_bought = pos.get('total_bought', 0)
                negative_risk = pos.get('negative_risk', False)

                # 创建TraderPosition对象来获取注释
                trader_pos = TraderPosition(
                    trader_address='', token_id='', condition_id='', title=title,
                    outcome=outcome, size=size, total_bought=total_bought,
                    avg_price=avg_price, cur_price=cur_price, initial_value=initial_value,
                    current_value=current_value, cash_pnl=cash_pnl, percent_pnl=percent_pnl,
                    end_date='', redeemable=False, negative_risk=negative_risk, timestamp=''
                )

                risk_level = trader_pos.get_risk_level()
                status_comment = trader_pos.get_status_comment()

                print(f"  {i}. {title}... ({outcome})")
                print(f"     持仓: {size:.2f}股 | 总买入: {total_bought:.2f}股 | {risk_level}")
                print(f"     均价: ${avg_price:.4f}/股 | 现价: ${cur_price:.4f}/股 | {status_comment}")
                print(f"     初始价值: ${initial_value:.2f} | 当前价值: ${current_value:.2f} | 盈亏: ${cash_pnl:.2f}")

            if position_count > 3:
                print(f"  ... 还有 {position_count - 3} 个持仓")
            print()

        print(f"总计: {total_positions} 个持仓")
        print(f"  总初始价值: ${total_initial_value:.2f}")
        print(f"  总当前价值: ${total_current_value:.2f}")
        print(f"  总盈亏: ${total_pnl:.2f}")
        print("=" * 60)

    def load_our_positions(self) -> List[Dict]:
        """加载我们自己的持仓数据"""
        try:
            positions_data = self.load_positions()
            if not positions_data or not positions_data.get('positions'):
                return []
            return positions_data['positions']
        except Exception as e:
            print(f"[ERROR] 加载我们持仓失败: {e}")
            return []

    def check_trader_has_token(self, trader_data: Dict, trader_address: str, token_id: str) -> bool:
        """检查指定交易员是否有某个token的持仓"""
        # 大小写不敏感匹配交易员地址
        trader_address_lower = trader_address.lower()
        target_trader = None

        for cached_address in trader_data.keys():
            if cached_address.lower() == trader_address_lower:
                target_trader = cached_address
                break

        if not target_trader:
            return False

        # 查找指定token_id的持仓
        for position in trader_data[target_trader]:
            # 兼容不同的字段名 (API返回的是 asset 不是 token_id)
            asset_id = position.get("asset") or position.get("token_id")
            if asset_id == token_id:
                return True

        return False

    def get_trader_position_info(self, trader_data: Dict, trader_address: str, token_id: str) -> Optional[Dict]:
        """获取交易员对某个token的持仓详细信息"""
        # 大小写不敏感匹配交易员地址
        trader_address_lower = trader_address.lower()
        target_trader = None

        for cached_address in trader_data.keys():
            if cached_address.lower() == trader_address_lower:
                target_trader = cached_address
                break

        if not target_trader:
            return None

        # 查找指定token_id的持仓
        for position in trader_data[target_trader]:
            # 兼容不同的字段名 (API返回的是 asset 不是 token_id)
            asset_id = position.get("asset") or position.get("token_id")
            if asset_id == token_id:
                return {
                    "size": float(position.get("size", 0)),
                    "avg_price": float(position.get("avg_price", 0)),
                    "cur_price": float(position.get("cur_price", 0)),
                    "current_value": float(position.get("current_value", 0)),
                    "cash_pnl": float(position.get("cash_pnl", 0)),
                    "percent_pnl": float(position.get("percent_pnl", 0)),
                    "title": position.get("title", ""),
                    "outcome": position.get("outcome", ""),
                    "token_id": asset_id
                }

        return None

    async def check_trader_has_token_api(self, trader_address: str, token_id: str, condition_id: str) -> Optional[Dict]:
        """通过API查询交易员是否有指定token的持仓 - 精确查询版本 (带重试)"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # 使用conditionId进行精确过滤，一个API调用直接返回目标持仓
                params = {
                    'user': trader_address,
                    'market': condition_id  # 使用conditionId进行精确过滤
                }

                # 获取令牌
                await global_rate_limiter.acquire()

                async with aiohttp.ClientSession() as session:
                    async with session.get(self.api_url, params=params) as response:
                        if response.status == 200:
                            positions = await response.json()

                            if not positions:
                                return None

                            # 遍历返回的持仓，寻找token_id匹配的记录
                            for position in positions:
                                asset_id = position.get("asset") or position.get("token_id")

                                if asset_id == token_id:
                                    return {
                                        "size": float(position.get("size", 0)),
                                        "avg_price": float(position.get("avgPrice", 0)),
                                        "cur_price": float(position.get("curPrice", 0)),
                                        "current_value": float(position.get("currentValue", 0)),
                                        "cash_pnl": float(position.get("cashPnl", 0)),
                                        "percent_pnl": float(position.get("percentPnl", 0)),
                                        "title": position.get("title", ""),
                                        "outcome": position.get("outcome", ""),
                                        "token_id": asset_id,
                                        "condition_id": position.get("conditionId", ""),
                                        "total_bought": float(position.get("totalBought", 0)),
                                        "initial_value": float(position.get("initialValue", 0)),
                                        "end_date": position.get("endDate", ""),
                                        "redeemable": position.get("redeemable", False),
                                        "negative_risk": position.get("negativeRisk", False)
                                    }
                            return None
                        
                        elif response.status == 429:
                            # 遇到限流，等待后重试
                            wait_time = 5 * (attempt + 1)
                            if attempt < max_retries - 1:
                                await asyncio.sleep(wait_time)
                                continue
                            else:
                                # 最后一次重试失败才打印
                                print(f"[WARNING] API请求限流 (429) 重试失败: {trader_address[:8]}...")
                                return None
                        else:
                            print(f"[ERROR] API请求失败: {response.status}")
                            return None

            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                print(f"[ERROR] 精确查询失败: {e}")
                return None
        return None

    async def update_trader_positions_cache_with_shared_positions(self, shared_positions: Dict):
        """将找到的共享持仓更新到交易员持仓缓存中（只保存共同持仓）"""
        try:
            # 创建新的交易员持仓缓存，只包含共同持仓
            trader_data = {}
            updated = False

            for token_id, token_info in shared_positions.items():
                for trader_match in token_info["matching_traders"]:
                    trader_address = trader_match["trader_address"]

                    # 检查交易员是否已在缓存中
                    if trader_address not in trader_data:
                        trader_data[trader_address] = []

                    # 检查这个token是否已在交易员的持仓中
                    token_exists = False
                    for existing_pos in trader_data[trader_address]:
                        existing_token_id = existing_pos.get("asset") or existing_pos.get("token_id")
                        if existing_token_id == token_id:
                            token_exists = True
                            break

                    if not token_exists:
                        # 修复：从 trader_match["info"] 中获取持仓数据
                        trader_info = trader_match.get("info", {})

                        # 只添加共同持仓记录到缓存
                        new_position = {
                            "trader_address": trader_address,
                            "token_id": token_id,
                            "condition_id": trader_match.get("condition_id", ""),
                            "title": token_info["title"],
                            "outcome": token_info["outcome"],
                            "size": trader_info.get("size", 0),
                            "total_bought": trader_info.get("total_bought", trader_info.get("size", 0)),
                            "avg_price": trader_info.get("avg_price", 0),
                            "cur_price": trader_info.get("cur_price", 0),
                            "initial_value": trader_info.get("initial_value", trader_info.get("current_value", 0)),
                            "current_value": trader_info.get("current_value", 0),
                            "cash_pnl": trader_info.get("cash_pnl", 0),
                            "percent_pnl": trader_info.get("percent_pnl", 0),
                            "end_date": trader_info.get("end_date", ""),
                            "redeemable": trader_info.get("redeemable", False),
                            "negative_risk": trader_info.get("negative_risk", False),
                            "timestamp": datetime.now().isoformat(),
                            "source": "shared_position_check"
                        }

                        trader_data[trader_address].append(new_position)
                        updated = True

            if updated:
                # 直接保存新的缓存（覆盖旧的，只保留共同持仓）
                self.save_trader_positions_to_cache(trader_data)
        except Exception:
            pass  # 静默处理错误，不打扰后台运行

    async def check_shared_positions_by_tokens(self):
        """通过我们持仓的token_id通过API查询目标交易员是否有相同市场的持仓"""
        # 初始化变量（放在try块外）
        shared_positions_cache = {}
        total_matches = 0

        try:
            # 加载我们的持仓
            our_positions = self.load_our_positions()
            if not our_positions:
                # 确保清空缓存
                self.shared_positions_cache = {}
                self.trader_positions_cache = {}
                return

            # 获取目标交易员地址
            trader_addresses = self._load_trader_addresses()
            if not trader_addresses:
                return

            # 批量查询所有持仓的共同交易员 - 超级并发！

            # 创建超级批量查询任务 - 所有持仓的所有交易员查询同时进行
            async def super_batch_query():
                batch_tasks = []
                # 限制并发数，避免瞬间发起过多请求导致 429
                semaphore = asyncio.Semaphore(20)

                async def limited_query(our_position, position_index, trader_address, token_id, condition_id):
                    async with semaphore:
                        return await query_single_position_trader(
                            our_position, position_index, trader_address, token_id, condition_id
                        )

                for position_index, our_position in enumerate(our_positions, 1):
                    token_id = our_position.get("asset") or our_position.get("token_id")
                    condition_id = our_position.get("conditionId") or our_position.get("condition_id")

                    if not token_id or not condition_id:
                        continue

                    # 为每个持仓-交易员组合创建查询任务
                    for trader_address in trader_addresses:
                        task = limited_query(
                            our_position, position_index, trader_address, token_id, condition_id
                        )
                        batch_tasks.append(task)

                # 并发执行所有查询 (受信号量限制)
                super_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                # 按持仓分组处理结果
                position_results = {}
                for result in super_results:
                    if isinstance(result, Exception):
                        continue
                    elif result and isinstance(result, dict):
                        pos_key = result["position_key"]
                        if pos_key not in position_results:
                            position_results[pos_key] = {
                                "our_position": result["our_position"],
                                "matching_traders": []
                            }
                        position_results[pos_key]["matching_traders"].append(result["trader_data"])

                return position_results

            # 单个持仓-交易员查询任务
            async def query_single_position_trader(our_position, position_index, trader_address, token_id, condition_id):
                trader_info = await self.check_trader_has_token_api(trader_address, token_id, condition_id)
                if trader_info:
                    return {
                        "position_key": position_index,
                        "our_position": {
                            "title": our_position.get('title', 'N/A'),
                            "outcome": our_position.get('outcome', 'N/A'),
                            "size": float(our_position.get('size', 0)),
                            "current_value": float(our_position.get('currentValue', 0))
                        },
                        "trader_data": {
                            "trader_address": trader_address,
                            "info": trader_info
                        }
                    }
                return None

            # 执行超级批量查询
            position_results = await super_batch_query()

            # 处理结果（快速模式 - 只显示最终结果）
            for result_data in position_results.values():
                # 找到对应的原始持仓数据以获取token_id
                original_position = None
                for pos in our_positions:
                    if (pos.get('title') == result_data["our_position"]["title"] and
                        float(pos.get('size', 0)) == result_data["our_position"]["size"]):
                        original_position = pos
                        break

                if original_position:
                    token_id = original_position.get("asset") or original_position.get("token_id")
                    if token_id:
                        shared_positions_cache[token_id] = {
                            "title": result_data["our_position"]["title"],
                            "outcome": result_data["our_position"]["outcome"],
                            "our_position": {
                                "size": result_data["our_position"]["size"],
                                "current_value": result_data["our_position"]["current_value"]
                            },
                            "matching_traders": result_data["matching_traders"]
                        }
                        total_matches += 1


        except Exception as e:
            print(f"[ERROR] 批量查询失败: {e}")
            import traceback
            traceback.print_exc()

        finally:
            # 保存到内存变量
            self.shared_positions_cache = shared_positions_cache
            if shared_positions_cache and total_matches > 0:
                # 保存共享持仓缓存到内存变量
                cache_data = {
                    "timestamp": datetime.now().isoformat(),
                    "total_markets": len(shared_positions_cache),
                    "total_matches": total_matches,
                    "shared_positions": shared_positions_cache
                }

                self.shared_positions_cache = cache_data

                # 更新交易员持仓缓存
                await self.update_trader_positions_cache_with_shared_positions(shared_positions_cache)


    async def update_balances(self, trader_addresses: List[str], show_log: bool = True) -> Dict[str, BalanceRecord]:
        """
        批量更新余额 - 并发查询所有钱包余额（带重试机制）
        
        重试逻辑：
        - 维护一个待查询地址列表
        - 循环查询直到所有地址都成功或超时
        - 每次只查询尚未成功的地址
        - 失败后等待2秒再重试
        - 最大超时时间：60秒
        """
        # 清空旧数据，只保存当前监控的交易员和我们自己的钱包
        balances = {}
        current_time = datetime.now().isoformat()
        updated = {}

        # 获取所有需要查询的地址
        my_wallet_addresses = self._load_my_wallet_addresses()
        
        # 构建待查询任务列表
        # 格式: {"address": "0x...", "type": "交易员"}
        pending_tasks = []
        for address in trader_addresses:
            pending_tasks.append({"address": address, "type": "交易员"})
        for address in my_wallet_addresses:
            pending_tasks.append({"address": address, "type": "钱包"})
            
        start_time = time.time()
        max_timeout = 60  # 最大超时时间60秒
        retry_delay = 2   # 重试间隔2秒
        
        # 循环重试直到所有任务完成或超时
        while pending_tasks:
            # 检查是否超时
            if time.time() - start_time > max_timeout:
                if show_log:
                    print(f"[警告] 获取余额超时 ({max_timeout}s)，仍有 {len(pending_tasks)} 个地址未获取成功")
                break
                
            # 创建并发查询任务
            async def query_single_balance(task):
                """查询单个地址的余额"""
                address = task["address"]
                address_type = task["type"]
                try:
                    balance = await self.get_trader_balance(address)
                    if balance is not None:
                        return {
                            "address": address,
                            "type": address_type,
                            "balance": balance,
                            "success": True,
                            "task": task
                        }
                    else:
                        return {
                            "address": address,
                            "type": address_type,
                            "balance": None,
                            "success": False,
                            "task": task
                        }
                except Exception as e:
                    return {
                        "address": address,
                        "type": address_type,
                        "balance": None,
                        "success": False,
                        "error": str(e),
                        "task": task
                    }

            # 并发执行本轮查询
            query_tasks = [query_single_balance(task) for task in pending_tasks]
            results = await asyncio.gather(*query_tasks, return_exceptions=True)
            
            # 处理结果
            next_pending_tasks = []
            
            for result in results:
                if isinstance(result, Exception):
                    # 异常情况，保留任务下一轮重试
                    continue
                
                if not isinstance(result, dict):
                    continue

                type_label = "交易员" if result["type"] == "交易员" else "钱包"
                address = result["address"]
                
                if result.get("success"):
                    # 成功获取，保存数据
                    record = BalanceRecord(
                        balance=result["balance"],
                        timestamp=current_time
                    )
                    balances[address] = record
                    updated[address] = record

                    if show_log:
                        # 地址脱敏显示
                        masked_addr = f"{address[:3]}***{address[-3:]}"
                        print(f"[成功] {type_label} {masked_addr}: {result['balance']:.4f} USDC")
                else:
                    # 失败，加入下一轮重试列表
                    next_pending_tasks.append(result["task"])
                    # 仅在最后一轮或特定情况下显示失败日志，避免刷屏
                    # 这里选择不显示中间失败日志，只在最后超时显示警告
            
            # 更新待处理任务列表
            pending_tasks = next_pending_tasks
            
            # 如果还有未完成的任务，等待后重试
            if pending_tasks:
                if show_log:
                    print(f"[提示] 还有 {len(pending_tasks)} 个地址获取失败，{retry_delay}秒后重试...")
                await asyncio.sleep(retry_delay)
            
        # 保存数据：保留旧缓存中未更新的数据，避免网络失败时丢失缓存
        # 先读取现有缓存
        current_cache = self.get_balance_cache()
        
        # 更新成功获取的余额
        for address, record in balances.items():
            current_cache[address] = record
        
        # 保存合并后的数据
        self.save_balances(current_cache)

        return updated

    def print_balance_summary(self):
        """打印余额汇总"""
        balances = self.get_balance_cache()
        if not balances:
            print("没有余额数据")
            return

        print(f"\n===== 余额汇总 ({len(balances)} 个地址) =====")
        for address, record in balances.items():
            print(f"{address[:8]}...: {record.balance:.4f} USDC ({record.timestamp})")
        print("=" * 50)

    # 内存缓存访问器方法
    def get_balance_cache(self) -> Dict[str, BalanceRecord]:
        """获取余额缓存数据"""
        return self.balance_cache

    def get_position_cache(self) -> Dict:
        """获取持仓缓存数据"""
        return self.position_cache

    def get_trader_positions_cache(self) -> Dict[str, List[Dict]]:
        """获取交易员持仓缓存数据"""
        return self.trader_positions_cache

    def get_shared_positions_cache(self) -> Dict:
        """获取共享持仓缓存数据"""
        return self.shared_positions_cache

    def clear_all_caches(self):
        """清空所有内存缓存数据"""
        self.balance_cache = {}
        self.position_cache = {}
        self.trader_positions_cache = {}
        self.shared_positions_cache = {}
        print("所有内存缓存已清空")

    def get_cache_summary(self) -> Dict:
        """获取所有缓存的汇总信息"""
        return {
            "balance_cache_size": len(self.balance_cache),
            "position_cache_exists": bool(self.position_cache),
            "trader_positions_cache_size": len(self.trader_positions_cache),
            "shared_positions_cache_exists": bool(self.shared_positions_cache),
            "position_count": len(self.position_cache.get('positions', [])) if self.position_cache else 0,
            "shared_positions_count": len(self.shared_positions_cache.get('shared_positions', {})) if self.shared_positions_cache else 0
        }

async def main():
    """主函数 - 简化版本，专注核心监控功能"""
    parser = argparse.ArgumentParser(description="Polymarket 监控程序")
    parser.add_argument("--once", action="store_true", help="只运行一次")
    parser.add_argument("--interval", type=int, default=60, help="监控间隔（秒）")
    parser.add_argument("--check-shared", action="store_true", help="检查共享持仓（通过我们持仓token查询交易员相同市场）")
    parser.add_argument("--show-memory", action="store_true", help="显示当前内存变量中的数据")

    args = parser.parse_args()

    monitor = IntegratedMonitor()

    # 如果只是检查共享持仓
    if args.check_shared:
        await monitor.check_shared_positions_by_tokens()
        return

    # 如果只是显示内存变量
    if args.show_memory:
        print("=== 显示当前内存变量中的数据 ===")

        # 先获取数据填充内存变量，然后显示
        print("正在获取最新数据...")

        # 获取交易员地址
        trader_addresses = monitor._load_trader_addresses()
        if trader_addresses:
            # 更新余额
            await monitor.update_balances(trader_addresses)

            # 更新持仓
            await monitor.update_my_positions()

            # 检查共同持仓
            await monitor.check_shared_positions_by_tokens()

            print("数据获取完成！\n")

        # 获取内存变量
        balance_cache = monitor.get_balance_cache()
        position_cache = monitor.get_position_cache()
        trader_positions_cache = monitor.get_trader_positions_cache()
        shared_positions_cache = monitor.get_shared_positions_cache()

        print("\n1. 余额缓存:")
        if balance_cache:
            for addr, record in balance_cache.items():
                print(f"   {addr[:8]}...: {record.balance:.4f} USDC")
        else:
            print("   (空)")

        print("\n2. 我的持仓:")
        if position_cache and position_cache.get('positions'):
            for pos in position_cache['positions']:
                print(f"   {pos.get('title', 'N/A')[:40]}...: {pos.get('size', 0):.2f}股 (${pos.get('currentValue', 0):.2f})")
        else:
            print("   (空)")

        print("\n3. 交易员持仓缓存:")
        if trader_positions_cache:
            for trader_addr, positions in trader_positions_cache.items():
                print(f"   交易员 {trader_addr[:8]}...: {len(positions)}个共同持仓")
        else:
            print("   (空)")

        print("\n4. 共同持仓缓存:")
        if shared_positions_cache and shared_positions_cache.get('shared_positions'):
            for token_id, data in shared_positions_cache['shared_positions'].items():
                our_size = data.get('our_position', {}).get('size', 0)
                trader_count = len(data.get('matching_traders', []))
                print(f"   市场: {data.get('title', 'N/A')[:40]}...")
                print(f"     我的持仓: {our_size}股, {trader_count}个交易员跟随")
        else:
            print("   (空)")

        # 获取缓存摘要
        print("\n5. 缓存摘要:")
        summary = monitor.get_cache_summary()
        for key, value in summary.items():
            print(f"   {key}: {value}")

        return

    print("开始监控...")
    print(f"监控间隔: {args.interval}秒")

    # 获取交易员地址
    trader_addresses = monitor._load_trader_addresses()

    if not trader_addresses:
        print("错误: 未找到交易员地址")
        print("请在.env中配置 TARGET_TRADERS")
        return

    trader_addresses = list(set(trader_addresses))  # 去重
    print(f"监控交易员: {[addr[:8] + '...' for addr in trader_addresses]}")

    if args.once:
        # 只运行一次（同时更新余额、我的持仓和交易员持仓）
        print("\n===== 一次性监控 =====")
        print("1. 更新交易员余额...")
        await monitor.update_balances(trader_addresses)
        monitor.print_balance_summary()

        print("\n2. 更新我的持仓信息...")
        await monitor.update_my_positions()

        print("\n3. 检查交易员持仓信息（按需查询）...")
        await monitor.check_shared_positions_by_tokens()

        print("\n一次性监控完成")
    else:
        # 持续监控
        print(f"\n开始持续监控，间隔 {args.interval} 秒...")

        while True:
            try:
                # 更新交易员余额
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 开始新一轮监控...")
                await monitor.update_balances(trader_addresses)

                # 更新我的持仓
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 更新我的持仓信息...")
                await monitor.update_my_positions()

                # 检查交易员持仓（只查询我们持仓相关的市场）
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 检查交易员持仓信息...")
                await monitor.check_shared_positions_by_tokens()

                print(f"[{datetime.now().strftime('%H:%M:%S')}] 本轮监控完成，等待 {args.interval} 秒...")
                await asyncio.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n监控已停止")
                break

if __name__ == "__main__":
    asyncio.run(main())