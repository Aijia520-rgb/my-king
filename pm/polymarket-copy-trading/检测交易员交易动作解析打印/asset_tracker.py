"""
资产跟踪模块
功能：
- 使用官方API获取总资产（USDC余额 + 持仓价值）
- 保存到JSON文件
- 每小时检测一次
- 亏损达到20%时自动停止程序
"""

import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger("AssetTracker")

class AssetTracker:
    """资产跟踪器"""
    
    def __init__(self, memory_monitor, config, clob_client=None):
        self.memory_monitor = memory_monitor
        self.config = config
        self.clob_client = clob_client
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.asset_file = os.path.join(script_dir, "data", "asset_tracking.json")
        self.initial_assets = None
        self.current_assets = 0.0
        self.stop_loss_threshold = 0.2  # 20% 亏损阈值
        self.running = False
        
        # 确保data目录存在
        os.makedirs(os.path.dirname(self.asset_file), exist_ok=True)
    
    async def calculate_total_assets(self) -> float:
        """使用官方API计算总资产（USDC余额 + 持仓价值）"""
        try:
            if not self.clob_client:
                logger.error("[ASSET] ClobClient未初始化，无法获取资产")
                return 0.0
            
            # 1. 获取USDC余额（COLLATERAL类型）
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            
            try:
                balance_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                balance_result = self.clob_client.get_balance_allowance(balance_params)
                
                logger.debug(f"[ASSET] USDC余额API返回: {balance_result}")
                
                # 处理不同的返回格式
                usdc_balance = 0.0
                if isinstance(balance_result, dict):
                    if 'balances' in balance_result and balance_result['balances']:
                        usdc_balance = float(balance_result['balances'][0].get('balance', 0))
                    elif 'balance' in balance_result:
                        usdc_balance = float(balance_result['balance'])
                    elif 'amount' in balance_result:
                        usdc_balance = float(balance_result['amount'])
                elif isinstance(balance_result, list) and len(balance_result) > 0:
                    usdc_balance = float(balance_result[0].get('balance', 0))
                
                # USDC有6位小数，需要除以1000000转换为USDC单位
                usdc_balance = usdc_balance / 1000000
                    
                logger.info(f"[ASSET] USDC余额: {usdc_balance:.2f}")
            except Exception as e:
                logger.warning(f"[ASSET] 获取USDC余额失败: {e}")
                import traceback
                logger.debug(f"[ASSET] 堆栈: {traceback.format_exc()}")
                usdc_balance = 0.0
            
            # 2. 使用官方API获取持仓总价值（直接返回总价值，无需累加）
            position_value = 0.0
            
            try:
                import aiohttp
                wallet_address = self.config.proxy_wallet_address or self.config.local_wallet_address
                if not wallet_address:
                    logger.warning("[ASSET] 未配置钱包地址，无法获取持仓价值")
                else:
                    # 使用官方API: https://data-api.polymarket.com/value?user=<wallet_address>
                    # 这个API直接返回持仓总价值，无需累加
                    api_url = f"https://data-api.polymarket.com/value?user={wallet_address}"
                    
                    logger.debug(f"[ASSET] 持仓总价值API请求: {api_url}")
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.get(api_url) as response:
                            if response.status == 200:
                                result = await response.json()
                                logger.debug(f"[ASSET] 持仓总价值API返回: {result}")
                                
                                if isinstance(result, list) and len(result) > 0:
                                    # 直接获取持仓总价值
                                    position_value = float(result[0].get('value', 0))
                                    logger.info(f"[ASSET] 持仓总价值: {position_value:.2f} USDC")
                                else:
                                    logger.info(f"[ASSET] 没有持仓")
                            else:
                                logger.warning(f"[ASSET] 持仓总价值API请求失败: HTTP {response.status}")
            except Exception as e:
                logger.warning(f"[ASSET] 获取持仓总价值失败: {e}")
                import traceback
                logger.debug(f"[ASSET] 堆栈: {traceback.format_exc()}")
                position_value = 0.0
            
            # 3. 总资产
            total_assets = usdc_balance + position_value
            
            logger.info(f"[ASSET] 总资产: {total_assets:.2f} USDC")
            
            return total_assets
            
        except Exception as e:
            logger.error(f"[ASSET] 计算总资产失败: {e}")
            import traceback
            logger.debug(f"[ASSET] 堆栈: {traceback.format_exc()}")
            return 0.0
    
    def load_initial_assets(self) -> Optional[float]:
        """从文件加载初始资产"""
        try:
            if os.path.exists(self.asset_file):
                with open(self.asset_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if 'initial_assets' in data:
                        initial = float(data['initial_assets'])
                        logger.info(f"[ASSET] 加载初始资产: {initial:.2f} USDC")
                        return initial
        except Exception as e:
            logger.warning(f"[ASSET] 加载初始资产失败: {e}")
        return None
    
    def save_assets(self, initial: bool = False, current: bool = True):
        """保存资产到文件"""
        try:
            data = {}
            
            if os.path.exists(self.asset_file):
                with open(self.asset_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            
            if initial:
                data['initial_assets'] = self.current_assets
                data['initial_timestamp'] = datetime.now().isoformat()
                logger.info(f"[ASSET] 保存初始资产: {self.current_assets:.2f} USDC")
            
            if current:
                data['current_assets'] = self.current_assets
                data['current_timestamp'] = datetime.now().isoformat()
                
                # 计算亏损率
                if 'initial_assets' in data and data['initial_assets'] > 0:
                    loss_rate = (data['initial_assets'] - self.current_assets) / data['initial_assets']
                    data['loss_rate'] = loss_rate
                    logger.info(f"[ASSET] 当前资产: {self.current_assets:.2f}, 亏损率: {loss_rate*100:.2f}%")
            
            with open(self.asset_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            logger.error(f"[ASSET] 保存资产失败: {e}")
    
    async def check_loss_and_stop(self) -> bool:
        """检查亏损率，如果达到阈值则返回True（需要停止）"""
        try:
            if not os.path.exists(self.asset_file):
                return False
            
            with open(self.asset_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if 'initial_assets' not in data or 'current_assets' not in data:
                return False
            
            initial = float(data['initial_assets'])
            current = float(data['current_assets'])
            
            if initial <= 0:
                return False
            
            loss_rate = (initial - current) / initial
            
            logger.info(f"[ASSET] 亏损检查: 初始 {initial:.2f}, 当前 {current:.2f}, 亏损率 {loss_rate*100:.2f}%")
            
            if loss_rate >= self.stop_loss_threshold:
                logger.error(f"[ASSET] 亏损率 {loss_rate*100:.2f}% 达到阈值 {self.stop_loss_threshold*100:.2f}%，触发止损停止程序！")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"[ASSET] 检查亏损失败: {e}")
            return False
    
    async def start_tracking(self):
        """启动资产跟踪"""
        self.running = True
        logger.info("[ASSET] 资产跟踪已启动")
        
        # 1. 初始化：每次启动都获取并保存初始资产
        self.current_assets = await self.calculate_total_assets()
        self.save_assets(initial=True, current=False)
        self.initial_assets = self.current_assets
        logger.info(f"[ASSET] 初始资产已设置: {self.initial_assets:.2f} USDC")
        
        # 2. 每小时检测一次
        while self.running:
            try:
                await asyncio.sleep(3600)  # 1小时
                
                # 更新当前资产
                self.current_assets = await self.calculate_total_assets()
                self.save_assets(initial=False, current=True)
                
                # 检查是否需要止损
                should_stop = await self.check_loss_and_stop()
                if should_stop:
                    logger.error("[ASSET] 触发止损，程序将在5秒后退出...")
                    await asyncio.sleep(5)
                    import sys
                    sys.exit(1)
                    
            except asyncio.CancelledError:
                logger.info("[ASSET] 资产跟踪已停止")
                break
            except Exception as e:
                logger.error(f"[ASSET] 资产跟踪错误: {e}")
                await asyncio.sleep(60)  # 错误后等待1分钟继续
    
    def stop(self):
        """停止资产跟踪"""
        self.running = False
        logger.info("[ASSET] 资产跟踪已停止")
