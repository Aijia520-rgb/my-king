"""
Polymarket跟单机器人 - 主程序入口
集成监控和交易功能

版本: 集成版 v1.0
功能:
- Pending交易极速监听
- Log确认监听兜底
- 交易信号解析
- 自动跟单执行

使用方法:
python main.py

配置要求:
- .env 文件配置完整
- abis/ 目录下有必要的合约ABI文件
"""
import sys; print("Python executable:", sys.executable)
print("Python version:", sys.version)

import asyncio
import signal
import sys
import time
import argparse
import aiohttp
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional

from config import Config, logger, get_config
from enrichment_service import EnrichmentService
from monitor_service import MonitorService
from trade_execution_service import TradeExecutionService
from position_analysis_service import PositionAnalysisService
from auto_redeem import AutoRedeemService

TRADER_NICKNAME_CACHE: Dict[str, str] = {}

async def fetch_single_nickname(session, trader):
    """获取单个交易员昵称并缓存"""
    try:
        # 使用 activity 接口获取最近一条活动，其中包含用户信息
        url = f"https://data-api.polymarket.com/activity?user={trader}&limit=1"
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                if data and isinstance(data, list) and len(data) > 0:
                    # 取第一条记录
                    latest_activity = data[0]
                    # 优先取 name (自定义昵称), 其次 pseudonym (系统分配昵称)
                    name = latest_activity.get('name') or latest_activity.get('pseudonym')
                    
                    if name:
                        TRADER_NICKNAME_CACHE[trader.lower()] = name
                        # logger.info(f"[INIT] 获取到昵称: {trader[:6]}... -> {name}")
    except Exception as e:
        logger.warning(f"[INIT] 获取昵称失败 {trader}: {e}")

async def init_trader_nicknames(traders):
    """初始化交易员昵称缓存"""
    if not traders:
        return
    
    logger.info(f"[INIT] 正在获取 {len(traders)} 个交易员的昵称...")
    async with aiohttp.ClientSession() as session:
        tasks = []
        for trader in traders:
            tasks.append(fetch_single_nickname(session, trader))
        await asyncio.gather(*tasks)
    logger.info(f"[INIT] 昵称获取完成，已缓存 {len(TRADER_NICKNAME_CACHE)} 个昵称")

async def position_analysis_mode():
    """持仓分析模式"""
    logger.info("[ANALYSIS] 启动持仓分析模式")

    # 初始化持仓分析服务
    position_analyzer = PositionAnalysisService()
    await position_analyzer.start()

    try:
        # 解析命令行参数
        parser = argparse.ArgumentParser(description='Polymarket持仓分析工具')
        parser.add_argument('trader', help='交易员地址')
        parser.add_argument('--hours', type=int, default=24, help='分析时间范围（小时），默认24小时')
        parser.add_argument('--type', choices=['position', 'sell', 'comprehensive'],
                          default='comprehensive', help='分析类型：持仓/卖出/综合')

        # 从sys.argv中提取参数，跳过脚本名和mode参数
        args = sys.argv[2:]  # 跳过脚本名和mode参数
        parsed_args = parser.parse_args(args)

        trader_address = parsed_args.trader
        hours_back = parsed_args.hours
        analysis_type = parsed_args.type

        # 验证地址格式
        if not trader_address.startswith('0x') or len(trader_address) != 42:
            logger.error("[ANALYSIS] 无效的以太坊地址格式")
            return

        logger.info(f"[ANALYSIS] 分析交易员: {trader_address[:10]}...")
        logger.info(f"[ANALYSIS] 时间范围: {hours_back} 小时")
        logger.info(f"[ANALYSIS] 分析类型: {analysis_type}")

        # 执行分析
        if analysis_type == 'position':
            positions = await position_analyzer.get_current_holdings(trader_address)
            report = position_analyzer.format_position_report(positions)

        elif analysis_type == 'sell':
            sell_analysis = await position_analyzer.analyze_sells(trader_address, hours_back)
            report = position_analyzer.format_sell_analysis_report(sell_analysis)

        else:  # comprehensive
            report = await position_analyzer.analyze_trader_comprehensive(trader_address, hours_back)

        # 输出报告
        print("\n" + "="*80)
        print(report)
        print("="*80)

        logger.info("[ANALYSIS] 分析完成")

    except Exception as e:
        logger.error(f"[ANALYSIS] 分析失败: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await position_analyzer.stop()

async def main():
    """API轮询监控和交易功能的主程序"""
    logger.info("\n" + "="*80)
    logger.info("[START] Polymarket跟单机器人 - API轮询版 v1.0")
    logger.info("="*80)

    # 1. 验证配置
    try:
        # 创建配置实例
        config = get_config()
        
        config.validate()
        logger.info(f"[CONFIG] 基础配置验证通过")
        logger.info(f"[CONFIG] 目标交易员数量: {len(config.target_traders)}")

        # 显示交易配置摘要
        trade_config = config.get_trade_config_summary()
        logger.info(f"[CONFIG] 交易配置摘要:")
        logger.info(f"   - 跟单比例: {trade_config['copy_ratio']*100:.1f}%")
        logger.info(f"   - 订单有效期(GTD): {trade_config['order_expiry_seconds']}秒")
        logger.info(f"   - 信号有效期: {trade_config['signal_expiry']}秒")
        logger.info(f"   - 代理钱包: {'已配置' if trade_config['proxy_wallet_configured'] else '未配置'}")

    except ValueError as e:
        logger.error(f"[CONFIG] 配置错误: {e}")
        return

    # 2. 初始化服务
    logger.info("[INIT] 初始化核心服务...")
    enricher = EnrichmentService()

    # 3. 启动数据增强服务
    logger.info("[START] 启动数据增强服务...")
    await enricher.start()

    # 4. 初始化监控程序（内存变量数据源）
    logger.info("[INIT] 初始化内存监控程序...")
    from balance_monitor import IntegratedMonitor
    memory_monitor = IntegratedMonitor()
    
    # 初始化自动结算服务
    logger.info("[INIT] 初始化自动结算服务...")
    auto_redeem_service = AutoRedeemService(config)
    
    import time

    async def redeem_task():
        """后台自动赎回任务 - 每小时执行一次（完全静默，不阻塞主程序）"""
        await asyncio.sleep(300)  # 启动后等待5分钟，让主程序先稳定运行
        while True:
            try:
                # 静默执行赎回（不输出INFO日志）
                await asyncio.to_thread(auto_redeem_service.execute, silent=True)
            except Exception as e:
                # 只记录错误，不影响主程序
                pass
            # 等待1小时
            await asyncio.sleep(3600)
    
    async def monitor_background():
        """后台监控任务 - 每分钟更新内存变量"""
        trader_addresses = memory_monitor._load_trader_addresses()

        if trader_addresses:
            # 等待60秒后开始定期监控，避免与启动时数据加载重复
            await asyncio.sleep(60)

            while True:
                try:

                    # 更新余额（后台监控不显示日志）
                    await memory_monitor.update_balances(trader_addresses, show_log=False)

                    # 更新持仓（后台监控不显示日志）
                    await memory_monitor.update_my_positions(show_log=False)

                    # 注释：初始化时已经获取过共享持仓，运行时不再重复获取以避免429错误
                    # await memory_monitor.check_shared_positions_by_tokens()

                    await asyncio.sleep(60)  # 每分钟更新一次

                except Exception:
                    await asyncio.sleep(60)

    # 先执行一次初始数据获取
    logger.info("[INIT] 开始获取初始数据...")
    trader_addresses = memory_monitor._load_trader_addresses()
    if trader_addresses:
        logger.info(f"[INIT] 正在获取 {len(trader_addresses)} 个交易员的初始数据...")

        # 1. 并发获取并缓存交易员昵称
        await init_trader_nicknames(trader_addresses)

        # 获取初始余额
        await memory_monitor.update_balances(trader_addresses)
        logger.info("[INIT] 余额数据已加载")

        # 获取初始持仓
        await memory_monitor.update_my_positions(show_log=False)
        logger.info("[INIT] 持仓数据已加载")

        # 检查共同持仓 (增加超时保护)
        logger.info("[INIT] 正在检查共同持仓...")
        try:
            await asyncio.wait_for(memory_monitor.check_shared_positions_by_tokens(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("[INIT] 检查共同持仓超时 (60s)，跳过此步骤继续启动")
        except Exception as e:
            logger.error(f"[INIT] 检查共同持仓失败: {e}")

        # 显示共同持仓统计
        shared_positions_cache = memory_monitor.get_shared_positions_cache()
        if shared_positions_cache and "shared_positions" in shared_positions_cache:
            shared_count = len(shared_positions_cache["shared_positions"])
            logger.info(f"[INIT] 发现 {shared_count} 个共同持仓市场")

            # 统计每个交易员的共同持仓数量
            trader_shared_counts = {}
            for token_id, token_info in shared_positions_cache["shared_positions"].items():
                for trader_match in token_info["matching_traders"]:
                    trader_address = trader_match["trader_address"]
                    if trader_address not in trader_shared_counts:
                        trader_shared_counts[trader_address] = 0
                    trader_shared_counts[trader_address] += 1

            # 显示每个交易员的共同持仓情况
            for trader_address, count in trader_shared_counts.items():
                logger.info(f"[INIT] 交易员 {trader_address[:8]}... 有 {count} 个共同持仓")
        else:
            logger.info("[INIT] 没有共同持仓")

        # 验证数据是否成功加载
        balance_cache = memory_monitor.get_balance_cache()
        position_cache = memory_monitor.get_position_cache()

        if balance_cache:
            our_positions_count = len(position_cache.get('positions', [])) if position_cache else 0
            logger.info(f"[INIT] 初始数据加载完成: {len(balance_cache)} 个地址余额, 我们有 {our_positions_count} 个持仓")
        else:
            logger.warning("[INIT] 初始数据加载失败或为空")

    # 启动后台监控任务（持续更新）
    asyncio.create_task(monitor_background())
    
    # 启动自动赎回后台任务（每小时执行一次，完全静默）
    logger.info("[INIT] 启动自动赎回后台任务（每小时静默执行）...")
    asyncio.create_task(redeem_task())

    # 4. 初始化交易执行服务（使用内存变量）
    logger.info("[INIT] 初始化交易执行服务...")
    trade_executor = TradeExecutionService(config, enricher, memory_monitor)
    await trade_executor.start()

    # 5. 初始化API监控服务
    logger.info("[INIT] 初始化API监控服务...")
    monitor = MonitorService(None, enricher)  # 不需要decoder_service
    monitor.set_trade_executor(trade_executor)

    # 优雅退出处理
    def handle_exit(sig, frame):
        logger.info("\n[SHUTDOWN] 接收到退出信号，正在优雅关闭...")

        # 停止监控
        logger.info("[SHUTDOWN] 停止API监控服务...")
        asyncio.create_task(monitor.stop())

        # 停止交易服务
        logger.info("[SHUTDOWN] 停止交易执行服务...")
        asyncio.create_task(trade_executor.stop())

        # 停止数据增强服务
        logger.info("[SHUTDOWN] 停止数据增强服务...")
        asyncio.create_task(enricher.stop())

        # 显示统计信息
        trade_stats = trade_executor.get_order_statistics()
        monitor_stats = monitor.get_monitor_statistics()
        logger.info(f"[STATS] 订单统计: {trade_stats}")
        logger.info(f"[STATS] 监控统计: {monitor_stats}")

        logger.info("[SHUTDOWN] 程序将在几秒后退出")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # 6. 启动监控服务
    logger.info("[START] 启动API轮询监控服务...")
    logger.info("[MONITOR] API轮询监控 + 智能跟单执行")
    logger.info("="*80)

    try:
        await monitor.start()
    except asyncio.CancelledError:
        logger.info("[SHUTDOWN] 监控服务被取消")
    except Exception as e:
        logger.error(f"[ERROR] 监控服务异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 清理资源
        logger.info("[SHUTDOWN] 正在清理资源...")
        try:
            await monitor.stop()
            await trade_executor.stop()
            await enricher.stop()
        except Exception as e:
            logger.error(f"[SHUTDOWN] 清理资源时出错: {e}")

        logger.info("[SHUTDOWN] 服务关闭完成")

if __name__ == "__main__":
    # 检查命令行模式
    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()

        if mode == "analyze":
            # 持仓分析模式
            asyncio.run(position_analysis_mode())
        elif mode == "monitor":
            # 监控模式（原有功能）
            asyncio.run(main())
        elif mode == "--help" or mode == "-h":
            print("使用方法:")
            print("  python main.py monitor                # 启动监控模式（默认）")
            print("  python main.py analyze <address>      # 持仓分析模式")
            print("")
            print("持仓分析模式参数:")
            print("  <address>                          # 交易员地址（必需）")
            print("  --hours <n>                        # 分析时间范围（小时），默认24")
            print("  --type <position|sell|comprehensive> # 分析类型，默认comprehensive")
            print("")
            print("示例:")
            print("  python main.py analyze 0x1234567890abcdef1234567890abcdef12345678")
            print("  python main.py analyze 0x1234567890abcdef1234567890abcdef12345678 --hours 48")
            print("  python main.py analyze 0x1234567890abcdef1234567890abcdef12345678 --type sell")
        else:
            print(f"未知模式: {mode}")
            print("使用 'python main.py --help' 查看帮助")
    else:
        # 默认启动监控模式
        asyncio.run(main())