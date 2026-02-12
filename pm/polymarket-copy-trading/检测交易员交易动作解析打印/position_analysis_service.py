"""
持仓分析服务模块
分析交易员持仓情况和卖出占比
"""

import asyncio
import aiohttp
import json
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
import logging

from config import Config, logger

@dataclass
class TraderPosition:
    """交易员持仓信息"""
    token_id: str
    market_name: str
    total_bought: float  # 总买入股数
    total_sold: float    # 总卖出股数
    net_position: float  # 净持仓股数
    total_bought_usdc: float = 0  # 总买入USDC
    total_sold_usdc: float = 0    # 总卖出USDC
    buy_count: int = 0      # 买入次数
    sell_count: int = 0     # 卖出次数

@dataclass
class SellAnalysis:
    """卖出分析结果"""
    trader_address: str
    total_sells: int
    total_sell_amount: float
    total_sell_shares: float
    small_sells: int  # <20%
    medium_sells: int  # 20-50%
    large_sells: int  # ≥50%
    sell_details: List[Dict]

class PositionAnalysisService:
    """持仓分析服务"""

    def __init__(self):
        self.api_base = "https://data-api.polymarket.com"
        self.cache = {}
        self.running = False

    async def start(self):
        """启动持仓分析服务"""
        logger.info("[POSITION] 持仓分析服务启动")
        self.running = True

    async def stop(self):
        """停止持仓分析服务"""
        logger.info("[POSITION] 持仓分析服务停止")
        self.running = False
        self.cache.clear()

    async def get_trader_activity(self, trader_address: str, hours_back: int = 24) -> List[Dict]:
        """获取交易员活动记录"""
        try:
            cache_key = f"{trader_address}_{hours_back}h"
            current_time = time.time()

            # 检查缓存（5分钟有效）
            if cache_key in self.cache:
                cached_data, cache_time = self.cache[cache_key]
                if current_time - cache_time < 300:  # 5分钟缓存
                    return cached_data

            url = f"{self.api_base}/activity"
            params = {
                'user': trader_address,
                'limit': 100,
                'startingAfter': int((datetime.now() - timedelta(hours=hours_back)).timestamp())
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.cache[cache_key] = (data, current_time)
                        logger.info(f"[POSITION] 获取交易员 {trader_address[:10]}... {len(data)} 条记录")
                        return data
                    else:
                        logger.error(f"[POSITION] API请求失败: {response.status}")
                        return []

        except Exception as e:
            logger.error(f"[POSITION] 获取交易员活动失败: {e}")
            return []

    async def analyze_trader_positions(self, trader_address: str, hours_back: int = 24) -> Dict[str, TraderPosition]:
        """分析交易员持仓情况"""
        try:
            activities = await self.get_trader_activity(trader_address, hours_back)
            if not activities:
                return {}

            positions = {}
            activities.sort(key=lambda x: x.get('transactionTimestamp', 0))

            for activity in activities:
                token_id = activity.get('token')
                side = activity.get('side', '').upper()  # BUY/SELL
                size_usdc = float(activity.get('sizeUsd', 0))
                shares = float(activity.get('size', 0))

                # 获取市场信息
                asset = activity.get('asset')
                if isinstance(asset, dict):
                    market_name = asset.get('title', f"Token {token_id}")
                elif isinstance(asset, str):
                    market_name = asset
                else:
                    market_name = f"Token {token_id}"

                # 初始化持仓记录
                if token_id not in positions:
                    positions[token_id] = TraderPosition(
                        token_id=token_id,
                        market_name=market_name,
                        total_bought=0,
                        total_sold=0,
                        net_position=0
                    )

                position = positions[token_id]

                # 更新持仓
                if side == 'BUY':
                    position.total_bought += shares
                    position.total_bought_usdc += size_usdc
                    position.net_position += shares
                    position.buy_count += 1
                elif side == 'SELL':
                    position.total_sold += shares
                    position.total_sold_usdc += size_usdc
                    position.net_position -= shares
                    position.sell_count += 1

            return positions

        except Exception as e:
            logger.error(f"[POSITION] 分析持仓失败: {e}")
            return {}

    async def analyze_sells(self, trader_address: str, hours_back: int = 24) -> Optional[SellAnalysis]:
        """分析卖出占比"""
        try:
            activities = await self.get_trader_activity(trader_address, hours_back)
            if not activities:
                return None

            positions = {}
            sells = []
            activities.sort(key=lambda x: x.get('transactionTimestamp', 0))

            for activity in activities:
                token_id = activity.get('token')
                side = activity.get('side')
                size = float(activity.get('sizeUsd', 0))
                shares = float(activity.get('size', 0))
                timestamp = activity.get('transactionTimestamp', 0)

                # 获取市场信息
                asset = activity.get('asset')
                if isinstance(asset, dict):
                    market_name = asset.get('title', f"Token {token_id}")
                elif isinstance(asset, str):
                    market_name = asset
                else:
                    market_name = f"Token {token_id}"

                # 初始化持仓记录
                if token_id not in positions:
                    positions[token_id] = {
                        'net_position': 0,
                        'market_name': market_name
                    }

                position = positions[token_id]

                if side == 'BUY':
                    position['net_position'] += shares
                elif side == 'SELL':
                    # 记录卖出前的持仓
                    position_before = position['net_position']
                    position['net_position'] -= shares
                    position_after = position['net_position']

                    # 计算卖出占比
                    if position_before > 0:
                        sell_percentage = (shares / position_before) * 100
                    else:
                        sell_percentage = 100  # 清仓

                    # 创建卖出记录
                    sell_time = datetime.fromtimestamp(timestamp / 1000)
                    sell_detail = {
                        'market_name': market_name,
                        'time': sell_time,
                        'amount': size,
                        'shares': shares,
                        'position_before': position_before,
                        'position_after': position_after,
                        'percentage': sell_percentage
                    }
                    sells.append(sell_detail)

            # 分类统计
            small_sells = [s for s in sells if s['percentage'] < 20]
            medium_sells = [s for s in sells if 20 <= s['percentage'] < 50]
            large_sells = [s for s in sells if s['percentage'] >= 50]

            return SellAnalysis(
                trader_address=trader_address,
                total_sells=len(sells),
                total_sell_amount=sum(s['amount'] for s in sells),
                total_sell_shares=sum(s['shares'] for s in sells),
                small_sells=len(small_sells),
                medium_sells=len(medium_sells),
                large_sells=len(large_sells),
                sell_details=sells
            )

        except Exception as e:
            logger.error(f"[POSITION] 分析卖出占比失败: {e}")
            return None

    async def get_current_holdings(self, trader_address: str) -> Dict[str, TraderPosition]:
        """获取当前持仓"""
        positions = await self.analyze_trader_positions(trader_address, 72)  # 3天数据
        return {token_id: pos for token_id, pos in positions.items() if pos.net_position != 0}

    def format_position_report(self, positions: Dict[str, TraderPosition]) -> str:
        """格式化持仓报告"""
        if not positions:
            return "当前无持仓"

        total_shares = sum(abs(pos.net_position) for pos in positions.values())

        report = f"\n[持仓报告] 总持仓: {total_shares:.2f} 股\n"
        report += "-" * 50 + "\n"

        for i, (token_id, pos) in enumerate(positions.items(), 1):
            report += f"{i}. {pos.market_name}\n"
            report += f"   持仓: {pos.net_position:+.2f} 股\n"
            if pos.net_position > 0:
                report += f"   类型: [多头] [UP]\n"
            else:
                report += f"   类型: [空头] {abs(pos.net_position):.2f} 股 [DOWN]\n"
            report += f"   买入: {pos.buy_count}次, 卖出: {pos.sell_count}次\n"
            report += "\n"

        return report

    def format_sell_analysis_report(self, analysis: SellAnalysis) -> str:
        """格式化卖出分析报告"""
        if not analysis:
            return "无卖出数据"

        report = f"\n[卖出分析] {analysis.trader_address[:10]}...\n"
        report += "-" * 50 + "\n"
        report += f"总卖出: {analysis.total_sells} 次\n"
        report += f"卖出总额: ${analysis.total_sell_amount:.2f}\n"
        report += f"卖出总股数: {analysis.total_sell_shares:.2f}\n\n"

        report += "占比分析:\n"
        report += f"  小额卖出 (<20%): {analysis.small_sells} 次\n"
        report += f"  中额卖出 (20-50%): {analysis.medium_sells} 次\n"
        report += f"  大额卖出 (≥50%): {analysis.large_sells} 次\n"

        # 显示最近5次卖出
        if analysis.sell_details:
            report += "\n最近卖出:\n"
            recent_sells = sorted(analysis.sell_details, key=lambda x: x['time'], reverse=True)[:5]
            for i, sell in enumerate(recent_sells, 1):
                report += f"{i}. {sell['market_name'][:30]}...\n"
                report += f"   时间: {sell['time'].strftime('%m-%d %H:%M')}\n"
                report += f"   卖出: {sell['shares']:.1f} 股 (${sell['amount']:.2f})\n"
                report += f"   占比: {sell['percentage']:.1f}%\n\n"

        return report

    async def analyze_trader_comprehensive(self, trader_address: str, hours_back: int = 24) -> str:
        """综合分析交易员"""
        try:
            # 获取持仓情况
            positions = await self.get_current_holdings(trader_address)
            position_report = self.format_position_report(positions)

            # 分析卖出占比
            sell_analysis = await self.analyze_sells(trader_address, hours_back)
            sell_report = self.format_sell_analysis_report(sell_analysis) if sell_analysis else "无卖出记录"

            # 组合报告
            comprehensive_report = f"[综合分析] 交易员: {trader_address[:10]}...\n"
            comprehensive_report += "=" * 60 + "\n"
            comprehensive_report += position_report
            comprehensive_report += sell_report
            comprehensive_report += "=" * 60 + "\n"

            return comprehensive_report

        except Exception as e:
            logger.error(f"[POSITION] 综合分析失败: {e}")
            return f"分析失败: {str(e)}"

# 导入time模块
import time