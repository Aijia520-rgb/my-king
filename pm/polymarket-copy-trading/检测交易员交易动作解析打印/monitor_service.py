import asyncio
import json
import time
import aiohttp
from datetime import datetime
from config import get_config, logger
from ding import get_trader_display_info
from rate_limiter import global_rate_limiter

class MonitorService:
    def __init__(self, decoder_service, enrichment_service):
        self.config = get_config()
        self.decoder = decoder_service
        self.enricher = enrichment_service
        self.processed_hashes = set()  # 去重集合: tx_hash
        self.last_fetch_time = {}      # 最后获取时间: trader -> timestamp
        self.running = False
        self.trade_executor = None     # 交易执行器
        self.trade_processing_lock = False  # 交易处理锁
        self.trade_queue = asyncio.Queue()  # 交易队列
        self.queue_processor_task = None   # 队列处理任务

        # Polymarket API配置
        self.api_url = "https://data-api.polymarket.com/activity"
        
        # API频率限制配置 (10秒190次 => 19次/秒)
        self.rate_limit_10s = 190
        self.safety_margin = 0.9   # 降低频率：从 1.0 降至 0.5，避免 429
        self.target_total_rps = (self.rate_limit_10s / 10.0) * self.safety_margin
        
        self.fetch_interval = 1       # 初始值，start()中会动态计算
        self.aggregation_window = 300 # 秒，与frontrun-bot一致

    def set_trade_executor(self, trade_executor):
        """设置交易执行器"""
        self.trade_executor = trade_executor
        logger.info("[MONITOR] 交易执行器已集成到监控服务")

        # 启动交易队列处理器
        self.queue_processor_task = asyncio.create_task(self.process_trade_queue())
        logger.info("[MONITOR] 交易队列处理器已启动")

    async def process_trade_queue(self):
        """处理交易队列，确保交易按顺序执行"""

        while True:
            try:
                # 等待队列中的交易
                trade_package = await self.trade_queue.get()

                if trade_package is None:  # 停止信号
                    break

                # 提取实际的trade_data
                actual_trade_data = trade_package['trade_data']
                market_info = trade_package['market_info']

                # 输出交易详情（现在才输出，避免打断其他订单处理）
                #
                # 注意：这里的 price 使用 `:.2f` 仅用于“人类可读展示”，并不参与任何下单计算。
                # 当 price 接近 1（例如 0.999）时，`:.2f` 会显示成 1.00，容易误判为“交易员价格=1.0”。
                # 下单侧会使用更高精度（建议 >= 6 位小数）打印并严格 clamp 到服务端允许区间。
                logger.info(f"\n[检测到新交易] {actual_trade_data['side']} {actual_trade_data['sizeUsd']:.2f} USD")
                logger.info(f"   交易员: {get_trader_display_info(actual_trade_data['trader'])}")
                logger.info(f"   代币ID: {actual_trade_data['tokenId']}")
                logger.info(f"   市场ID: {actual_trade_data['marketId']}")
                logger.info(f"   预测结果: {market_info['outcome']}")
                logger.info(f"   价格: {actual_trade_data['price']:.2f}")
                logger.info(f"   交易哈希: {actual_trade_data['transactionHash']}")
                logger.info(f"   时间: {datetime.fromtimestamp(actual_trade_data['timestamp']/1000)}")
                logger.info(f"   市场名称: {market_info['market_slug']}")
                logger.info(f"   问题描述: {market_info['question'][:80]}...")

                logger.info(f"[QUEUE] 开始处理队列中的交易: {actual_trade_data['transactionHash'][:8]}...")

                # 设置处理锁
                self.trade_processing_lock = True

                try:
                    # 处理交易
                    await self.execute_trade(trade_package)

                except Exception as e:
                    logger.error(f"[QUEUE] 处理交易时发生错误: {e}")

                finally:
                    # 释放处理锁
                    self.trade_processing_lock = False
                    logger.info(f"[QUEUE] 交易处理完成: {actual_trade_data['transactionHash'][:8]}...")

                # 标记队列任务完成
                self.trade_queue.task_done()

            except Exception as e:
                logger.error(f"[QUEUE] 队列处理器错误: {e}")
                # 确保释放锁
                self.trade_processing_lock = False
                await asyncio.sleep(1)  # 短暂等待后继续

    async def start(self):
        """启动API轮询监控"""
        self.running = True
        
        # 动态计算最优轮询间隔
        trader_count = len(self.config.target_traders)
        # 使用全局限流器控制频率，这里不再需要复杂的计算
        # 只要保证有足够的并发任务即可，限流器会控制总速率
        self.fetch_interval = 0.1 # 极小间隔，完全由限流器控制

        # 打印详细的监控信息
        logger.info("\n" + "="*80)
        logger.info("[MONITOR] API轮询监控服务已启动")
        logger.info(f"[MONITOR] 目标交易员数量: {trader_count}")
        logger.info(f"[MONITOR] 全局限流: 启用 (180 req/10s)")
        logger.info(f"[MONITOR] 聚合窗口: {self.aggregation_window}秒")
        logger.info(f"[MONITOR] API端点: {self.api_url}")

        logger.info("[MONITOR] 监控地址列表:")
        for i, addr in enumerate(self.config.target_traders, 1):
            logger.info(f"   {i}. {get_trader_display_info(addr)}")
        logger.info("="*80 + "\n")

        # 为每个交易员创建监控任务
        tasks = []
        # 计算错峰启动的延迟步长，使请求均匀分布在时间轴上
        delay_step = self.fetch_interval / trader_count if trader_count > 0 else 0
        
        for i, trader in enumerate(self.config.target_traders):
            initial_delay = i * delay_step
            task = asyncio.create_task(self.monitor_trader(trader, initial_delay))
            tasks.append(task)

        await asyncio.gather(*tasks)

    async def execute_trade(self, trade_package):
        """
        从队列中执行单个交易（把监控到的 trade_data 转成下单侧的 [`TradeSignal`](trade_execution_service.py:51)）。

        运行逻辑：
        1) 从队列包中取出：
           - trade_data：来自 [`MonitorService._process_trade()`](monitor_service.py:353) 组装的交易活动字段
           - market_info：用于日志/下单侧辅助展示的市场信息
        2) 构造交易信号 [`TradeSignal`](trade_execution_service.py:51)：
           - signal.price = trade_data['price']（交易员成交价/每股价格）
           - signal.shares = sizeUsd / price（仅当 price > 0 时推导；否则为 None）
        3) 调用交易执行器执行下单：
           - [`TradeExecutionService.execute_copy_trade()`](trade_execution_service.py:181)

        重要说明（与当前问题现象对齐）：
        - 这里的 price 直接来自 data-api 的 `activity.price`（见 [`MonitorService._process_trade()`](monitor_service.py:353) 的 trade_data['price']）。
        - shares 是用 sizeUsd/price 推导得到，属于“推算值”，用于下单侧：
          - 定价兜底：当 signal.price 缺失时可用 amount_usdc/shares 推导 trader_price
          - 日志解释：明确 signal.price/signal.shares/amount_usdc 的一致性来源
        - 由于部分日志用 `:.2f` 打印，0.999 可能显示为 1.00，容易误判；后续 Phase 2 会统一提升价格日志精度。
        """
        trade_data = trade_package['trade_data']
        market_info = trade_package['market_info']

        # 导入TradeSignal
        from trade_execution_service import TradeSignal

        # 计算股数和获取价格
        #
        # 说明：
        # - trader_price 来自 data-api 的 activity.price（每股价格），会直接写入 signal.price。
        # - shares 这里用 sizeUsd / price 推导，属于“推算值”：
        #   - 主要用于下单侧日志解释（price/shares/amount_usdc 三者关系）
        #   - 以及当 signal.price 缺失时的兜底推导（amount_usdc / shares）
        # - 本项目当前 Phase 1 仅补注释：不改变推导公式与精度格式。
        trader_price = trade_data.get('price', 0)
        shares = 0

        if trader_price and trader_price > 0:
            shares = trade_data['sizeUsd'] / trader_price

        trade_signal = TradeSignal(
            source_address=trade_data['trader'],
            original_tx_hash=trade_data['transactionHash'],
            detection_source="API_POLLING",
            token_id=trade_data['tokenId'],
            side=trade_data['side'],
            amount_usdc=trade_data['sizeUsd'],
            price=trader_price,     # 添加交易员价格
            shares=shares,          # 添加计算出的股数
            market_info=market_info,
            detected_at=time.time()
        )

        # 执行跟单
        try:
            logger.info(f"[EXECUTE] 开始执行跟单: {trade_data['transactionHash'][:8]}...")
            ok = await self.trade_executor.execute_copy_trade(trade_signal)

            if ok:
                logger.info(f"[EXECUTE] 跟单成功提交订单: {trade_data['transactionHash'][:8]}...")

                # 异步精准更新相关持仓，不阻塞交易队列
                asyncio.create_task(
                    self.update_specific_positions(
                        trade_data['tokenId'],
                        trade_data['trader']
                    )
                )
            else:
                # 关键：当下单侧主动跳过（例如“溢价后价格>0.99”返回 None）时，这里必须明确标记为“未下单”
                logger.info(f"[EXECUTE] 跟单跳过/未下单: {trade_data['transactionHash'][:8]}...")

        except Exception as e:
            logger.error(f"[EXECUTE] 执行跟单时发生错误: {e}")

    async def update_specific_positions(self, token_id: str, trader_address: str):
        """精准更新相关持仓信息（我们的+目标交易员的）"""
        try:
            # 获取内存监控器
            if hasattr(self.trade_executor, 'memory_monitor') and self.trade_executor.memory_monitor:
                monitor = self.trade_executor.memory_monitor

                # 1. 更新我们在该token的持仓
                await monitor.update_my_positions()  # 这里可以优化为只更新特定token

                # 2. 更新目标交易员在该token的持仓
                await self.update_trader_specific_position(trader_address, token_id)
            else:
                logger.warning("[POSITION] 无法访问内存监控器，跳过持仓更新")

        except Exception as e:
            logger.error(f"[POSITION] 精准更新持仓时发生错误: {e}")

    async def update_trader_specific_position(self, trader_address: str, token_id: str):
        """更新特定交易员的特定token持仓"""
        try:
            # 使用精确查询获取该交易员在该token的持仓
            from balance_monitor import IntegratedMonitor
            monitor = IntegratedMonitor()

            # 获取该token的condition_id（从现有持仓缓存中查找）
            condition_id = await self.get_condition_id_for_token(token_id)
            if condition_id:
                # 精确查询该交易员在这个token的持仓
                position_info = await monitor.check_trader_has_token_api(trader_address, token_id, condition_id)

                if position_info:
                    # 这里可以更新交易员持仓缓存中的具体记录
                    pass  # 保留空块以避免语法错误
                else:
                    logger.info(f"[POSITION] 交易员 {trader_address[:8]} 在 {token_id[:8]} 无持仓")
            else:
                logger.warning(f"[POSITION] 无法找到token {token_id[:8]} 的condition_id")

        except Exception as e:
            logger.error(f"[POSITION] 更新交易员特定持仓时发生错误: {e}")

    async def get_condition_id_for_token(self, token_id: str):
        """从我们的持仓缓存中查找token对应的condition_id"""
        try:
            if hasattr(self.trade_executor, 'memory_monitor') and self.trade_executor.memory_monitor:
                position_cache = self.trade_executor.memory_monitor.get_position_cache()
                if position_cache and 'positions' in position_cache:
                    for position in position_cache['positions']:
                        asset_id = position.get('asset') or position.get('token_id')
                        if asset_id == token_id:
                            return position.get('conditionId') or position.get('condition_id')
            return None
        except Exception as e:
            logger.error(f"[POSITION] 查找condition_id时发生错误: {e}")
            return None

    async def stop(self):
        """停止监控"""
        logger.info("[MONITOR] 正在停止API监控服务...")
        self.running = False

        # 停止队列处理器
        if self.queue_processor_task:
            await self.trade_queue.put(None)  # 发送停止信号
            await self.queue_processor_task

        logger.info("[MONITOR] API监控服务已停止")

    async def monitor_trader(self, trader, initial_delay=0.0):
        """监控单个交易员"""
        
        # 错峰启动
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)

        while self.running:
            try:
                # 获取活动 (内部已包含限流等待)
                await self.fetch_trader_activities(trader)
                
                # 极短休眠让出CPU
                await asyncio.sleep(0.01)
                
            except Exception as e:
                logger.error(f"[MONITOR] 交易员 {trader[:10]}... 监控失败: {e}")
                await asyncio.sleep(5)  # 错误时等待5秒

    async def fetch_trader_activities(self, trader):
        """获取交易员活动 - 完全按照frontrun-bot的逻辑"""
        try:
            url = f"{self.api_url}?user={trader}"

            # 获取令牌 (限流)
            await global_rate_limiter.acquire()

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        activities = await response.json()

                        now = int(datetime.now().timestamp())
                        cutoff_time = now - self.aggregation_window

                        # 统计变量
                        trade_count = 0
                        skipped_old = 0
                        skipped_processed = 0
                        skipped_before_last = 0

                        for activity in activities:
                            if activity.get('type') != 'TRADE':
                                continue
                            trade_count += 1

                            # 处理时间戳
                            activity_time = self._get_timestamp(activity)

                            if activity_time < cutoff_time:
                                skipped_old += 1
                                continue

                            # 检查是否已处理
                            tx_hash = activity.get('transactionHash')
                            if tx_hash in self.processed_hashes:
                                skipped_processed += 1
                                continue

                            # 检查是否在最后获取时间之前
                            last_time = self.last_fetch_time.get(trader, 0)
                            if activity_time <= last_time:
                                skipped_before_last += 1
                                continue

                            # 检测到新交易
                            await self._process_trade(activity, trader)

                            # 记录处理状态
                            self.processed_hashes.add(tx_hash)
                            self.last_fetch_time[trader] = max(
                                self.last_fetch_time.get(trader, 0),
                                activity_time
                            )

                        # 只在有新交易时显示状态
                        new_trades = trade_count - skipped_old - skipped_processed - skipped_before_last
                        if new_trades > 0:
                            logger.info(f"[MONITOR] {trader[:10]}...: 发现 {new_trades} 笔新交易 "
                                      f"(总计: {trade_count}, 跳过: {skipped_old + skipped_processed + skipped_before_last})")

                    elif response.status == 404:
                        logger.info(f"[MONITOR] 交易员 {trader[:10]}... 无活动记录 (404)")
                    elif response.status == 429:
                        logger.warning(f"[MONITOR] 触发API频率限制 (429)，暂停 5 秒...")
                        await asyncio.sleep(5)
                    else:
                        logger.warning(f"[MONITOR] API请求失败，状态码: {response.status}")

        except asyncio.TimeoutError:
            logger.warning(f"[MONITOR] 请求超时: {trader[:10]}...")
        except Exception as e:
            # 404错误是正常的
            if "404" in str(e):
                logger.info(f"[MONITOR] 交易员 {trader[:10]}... 无活动记录 (404)")
                return
            logger.error(f"[MONITOR] 获取交易员 {trader[:10]}... 活动失败: {e}")

    def _get_timestamp(self, activity):
        """获取活动时间戳"""
        timestamp = activity.get('timestamp')
        if isinstance(timestamp, (int, float)):
            return int(timestamp)
        elif isinstance(timestamp, str):
            try:
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                return int(dt.timestamp())
            except:
                return 0
        return 0

    async def _process_trade(self, activity, trader):
        """
        处理交易活动（把 data-api 的 activity 规范化为内部 trade_data，并入队等待执行）。

        数据流位置：
        - 本函数处于“监控侧”->“执行侧”的边界：
          - 监控侧：轮询 data-api 获取 activity
          - 执行侧：由队列处理器调用 [`MonitorService.execute_trade()`](monitor_service.py:116)，再调用下单侧

        运行逻辑：
        1) 将 activity 字段映射成 trade_data（尽量统一字段名）：
           - tokenId: activity.asset
           - side: activity.side (upper)
           - sizeUsd: 优先 activity.usdcSize；否则用 size * price 推导
           - price: activity.price（交易员成交价/每股价格）
        2) 构建 market_info（此处不调用 enricher，避免 API 不一致与额外延迟）：
           - market_slug/title/description/outcome 等直接来自 activity
        3) 入队：将 trade_package 放入 `self.trade_queue`，由队列处理器串行执行，避免并发下单互相打断日志

        与当前问题的关联点：
        - “交易员价格首次没打印/显示成 1.00”：
          - price 在这里写入 trade_data['price']，后续日志若仅保留 2 位小数会把 0.999 显示成 1.00；
            这属于日志精度问题（Phase 2 会统一调整）。
        """
        start_time = time.time()

        # 构建交易活动数据，包含完整的市场信息
        #
        # 字段约定（Monitor -> Executor 的边界）：
        # - tokenId: data-api 的 activity.asset
        # - marketId: data-api 的 activity.conditionId（注意：这里是 market/condition 维度，不是 token）
        # - side: BUY/SELL（统一大写）
        # - sizeUsd:
        #   - 优先使用 activity.usdcSize（data-api 已给出美元/USDC 口径）
        #   - 若缺失：用 size * price 推导（此处的 size 与 price 均来自 activity；属于兜底推算）
        # - price: activity.price（交易员成交价/每股价格，原始值）
        # - timestamp: 内部统一用“毫秒时间戳”传递，便于展示与对齐链上/数据源
        trade_data = {
            'trader': trader,
            'marketId': activity.get('conditionId', ''),
            'tokenId': activity.get('asset', ''),
            'title': activity.get('title', ''),  # 添加正确的市场标题
            'description': activity.get('description', ''),  # 添加市场描述
            'outcome': activity.get('outcome', ''),  # 直接使用API返回的outcome字段
            'side': activity.get('side', '').upper(),
            'sizeUsd': activity.get('usdcSize', 0) or (activity.get('size', 0) * activity.get('price', 0)),
            'price': activity.get('price', 0),
            'timestamp': self._get_timestamp(activity) * 1000,
            'transactionHash': activity.get('transactionHash', '')
        }

        # 1. 使用原始交易数据中的正确市场信息，避免API不一致问题
        # 直接从API返回的数据中获取市场信息
        market_name = trade_data.get('title', 'Unknown Market')
        question = trade_data.get('description', 'No description available')
        outcome = trade_data.get('outcome', 'UNKNOWN')

        
        # 构建完整的market_info供交易使用
        complete_market_info = {
            'market_slug': market_name,
            'question': question,
            'outcome': outcome,
            'price': trade_data.get('price', 0)  # 添加价格信息
        }

        # 2. 将交易加入队列排队处理（如果有订单正在处理，延迟日志输出）
        if self.trade_executor and trade_data['side'] in ['BUY', 'SELL']:
            try:
                # 构建交易数据包
                trade_package = {
                    'trade_data': trade_data,
                    'market_info': complete_market_info
                }

                # 将交易加入队列
                await self.trade_queue.put(trade_package)

                logger.info(f"[QUEUE] 新交易已加入队列: {trade_data['transactionHash'][:8]}... (队列长度: {self.trade_queue.qsize()})")

            except Exception as e:
                logger.error(f"[ERROR] 将交易加入队列时发生错误: {e}")

        latency = (time.time() - start_time) * 1000
        logger.info(f"   处理延迟: {latency:.2f}ms")

    def get_monitor_statistics(self):
        """获取监控统计信息"""
        return {
            'processed_transactions': len(self.processed_hashes),
            'monitored_traders': len(self.config.target_traders),
            'last_fetch_times': dict(self.last_fetch_time)
        }

    async def _handle_detected_action(self, decoded_data, tx_hash, source):
        """
        处理已识别的动作：数据增强 + 日志输出
        """
        # 注意：对于 batchFillOrders，同一个 tx_hash 会有多次调用，不能简单用 tx_hash 去重
        # 应该结合 tx_hash + log_index (如果是 log) 或 tx_hash + index (如果是 batch)
        # 这里为了简化，暂时允许同一 tx 的多次输出，或者使用更复杂的去重键
        # 如果是 PENDING，通常只处理一次，但 batch 会循环调用此函数
        
        # 简单去重策略：如果是 PENDING，我们只记录 tx_hash，防止重复处理整个交易
        # 但这里是处理具体的 action，所以不需要在这里对 tx_hash 去重，
        # 而是在 _process_pending_tx 入口处做去重 (目前代码逻辑是在入口处没做，这里做)
        # 修改策略：允许同一 tx_hash 进入，但在日志中区分
        
        # if tx_hash in self.processed_txs:
        #     return
        # self.processed_txs.add(tx_hash)

        start_time = time.time()
        
        # 1. 数据增强
        token_id = decoded_data.get('tokenId')
        market_info = await self.enricher.get_market_info(token_id)
        
        if not market_info:
            market_info = {"market_slug": "Unknown", "outcome": "Unknown", "question": "Unknown"}

        # 2. 格式化输出
        # 确定方向:
        # side=0 (BUY), side=1 (SELL)
        side_str = "UNKNOWN"
        side_raw = decoded_data.get('side')
        if side_raw == 0: side_str = "BUY"
        elif side_raw == 1: side_str = "SELL"
        
        # 计算金额
        amount_wei = decoded_data.get('fillAmount', 0) # fillOrders
        if not amount_wei:
            amount_wei = decoded_data.get('takerAmount', 0) # OrderFilled (近似)
            
        amount_usdc = self.enricher.format_amount(amount_wei)
        
        latency = (time.time() - start_time) * 1000

        # 3. 打印日志
        log_msg = (
            f"\n[INFO] [{source}] [ALERT] 监测到目标交易员动作!\n"
            f"--------------------------------------------------\n"
            f"[TRADER] 交易员  : {decoded_data.get('maker') or decoded_data.get('taker') or 'Unknown'}\n"
            f"[HASH] 交易哈希: {tx_hash}\n"
            f"[MARKET] 市场名称: {market_info['market_slug']}\n"
            f"[DESC] 问题描述: {market_info['question'][:50]}...\n"
            f"[OUTCOME] 预测结果: {market_info['outcome']}\n"
            f"[DIRECTION] 交易方向: {side_str}\n"
            f"[AMOUNT] 交易金额: {amount_usdc:,.2f} USDC\n"
            f"--------------------------------------------------\n"
            f"[TIME] 处理延迟: {latency:.2f}ms"
        )
        logger.info(log_msg)