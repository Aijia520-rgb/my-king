"""
自动结算服务 (Auto-Redeem Service)

功能：
1. 检查当前账户的所有持仓。
2. 识别已决议且盈利的持仓 (Redeemable)。
3. 调用 ConditionalTokens 合约执行赎回操作，将持仓转换为 USDC。

重构说明：
- 封装为 AutoRedeemService 类，支持配置复用。
- 修复 Gas 估算缓冲不足的问题。
- 优化日志配置，避免冲突。
- [新增] 智能持仓检测：不依赖 API 的 outcomeIndex，直接查询链上余额确定要赎回的 Index。
"""

import json
import time
import requests
import logging
import sys
from web3 import Web3
from hexbytes import HexBytes
import warnings
from config import Config, logger



# 常量定义
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Gnosis Conditional Tokens (Polygon)
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174" # USDC (Polygon)
PARENT_COLLECTION_ID = "0x0000000000000000000000000000000000000000000000000000000000000000"

class AutoRedeemService:
    """自动结算服务类"""

    def __init__(self, config: Config):
        """
        初始化自动结算服务
        
        Args:
            config: 全局配置实例
        """
        self.config = config
        self.w3, self.account = config.get_web3_and_account()
        self.ctf_contract = self._load_contract()
        self.safe_contract = self._load_safe_contract()
        
        if not self.w3 or not self.account:
            logger.error("无法初始化 Web3 或账户，自动结算服务不可用")

    def _load_contract(self):
        """加载 ConditionalTokens 合约"""
        try:
            with open('abis/ConditionalTokens.json', 'r') as f:
                ctf_abi = json.load(f)
            
            if not self.w3:
                return None
                
            return self.w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=ctf_abi
            )
        except Exception as e:
            logger.error(f"加载合约 ABI 失败: {e}")
            return None

    def _load_safe_contract(self):
        """加载 Gnosis Safe 合约 (用于代理钱包模式)"""
        if not self.config.proxy_wallet_address:
            return None
            
        try:
            with open('abis/GnosisSafe.json', 'r') as f:
                safe_abi = json.load(f)
            
            if not self.w3:
                return None
                
            return self.w3.eth.contract(
                address=Web3.to_checksum_address(self.config.proxy_wallet_address),
                abi=safe_abi
            )
        except Exception as e:
            logger.warning(f"加载 Gnosis Safe ABI 失败 (如果是代理钱包模式可能会出错): {e}")
            return None

    def get_redeemable_positions(self, wallet_address):
        """从 Polymarket Data API 获取可赎回的持仓"""
        api_url = "https://data-api.polymarket.com/positions"
        all_positions = []
        offset = 0
        limit = 100  # API单次查询限制
        
        try:
            while True:
                params = {
                    'user': wallet_address,
                    'limit': limit,
                    'offset': offset
                }
                
                response = requests.get(api_url, params=params)
                response.raise_for_status()
                positions = response.json()
                
                if not positions:
                    break  # 没有更多数据了
                
                all_positions.extend(positions)
                
                # 如果返回的持仓数量少于limit，说明已经获取完所有数据
                if len(positions) < limit:
                    break
                
                offset += limit
            
            # 筛选可赎回的持仓
            redeemable = [p for p in all_positions if p.get('redeemable', False) 
                          and p.get('currentValue', 0) > 0]
            
            return redeemable
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return []

    def _calculate_position_id(self, collateral_token, collection_id):
        """计算 Position ID"""
        # positionId = keccak256(abi.encodePacked(collateralToken, collectionId))
        return Web3.solidity_keccak(
            ['address', 'bytes32'],
            [Web3.to_checksum_address(collateral_token), collection_id]
        )

    def _calculate_collection_id(self, parent_collection_id, condition_id, index_set):
        """计算 Collection ID"""
        # collectionId = keccak256(abi.encodePacked(parentCollectionId, conditionId, indexSet))
        # 注意：indexSet 是 uint256
        return Web3.solidity_keccak(
            ['bytes32', 'bytes32', 'uint256'],
            [parent_collection_id, condition_id, index_set]
        )

    def redeem_position(self, position):
        """执行单笔赎回操作"""
        if not self.w3 or not self.account or not self.ctf_contract:
            logger.error("服务未正确初始化，无法执行赎回")
            return False

        try:
            condition_id = position.get('conditionId')
            api_outcome_index = int(position.get('outcomeIndex', 0))
            market_slug = position.get('slug', 'Unknown')
            size = float(position.get('size', 0))
            
            logger.info(f"正在赎回: {market_slug}")
            logger.info(f"  - Condition: {condition_id[:8]}...")
            logger.info(f"  - API Index: {api_outcome_index}")
            logger.info(f"  - Size: {size}")
            
            # ---------------------------------------------------------
            # [智能诊断与修正]
            # 不盲目信任 API 的 outcomeIndex，而是检查链上余额
            # ---------------------------------------------------------
            
            # 1. 检查 Payout Numerators (确认合约判定的获胜方)
            try:
                payout0 = self.ctf_contract.functions.payoutNumerators(condition_id, 0).call()
                payout1 = self.ctf_contract.functions.payoutNumerators(condition_id, 1).call()
                logger.info(f"  - [Debug] Payout Numerators: Index 0 = {payout0}, Index 1 = {payout1}")
            except Exception as e:
                logger.warning(f"  ⚠️ [Debug] 无法查询 Payout Numerators: {e}")
                payout0 = 0
                payout1 = 0

            # 2. 检查链上余额 (Balance Check)
            # 我们检查 Index 0 (Set 1) 和 Index 1 (Set 2) 的余额
            wallet_address = self.config.proxy_wallet_address or self.account.address
            
            # Index 0
            index_set_0 = 1
            collection_id_0 = self._calculate_collection_id(PARENT_COLLECTION_ID, condition_id, index_set_0)
            position_id_0 = self._calculate_position_id(USDC_ADDRESS, collection_id_0)
            # 将 bytes 转换为 int (uint256)
            position_id_int_0 = int.from_bytes(position_id_0, byteorder='big')
            balance_0 = self.ctf_contract.functions.balanceOf(wallet_address, position_id_int_0).call()
            
            # Index 1
            index_set_1 = 2
            collection_id_1 = self._calculate_collection_id(PARENT_COLLECTION_ID, condition_id, index_set_1)
            position_id_1 = self._calculate_position_id(USDC_ADDRESS, collection_id_1)
            # 将 bytes 转换为 int (uint256)
            position_id_int_1 = int.from_bytes(position_id_1, byteorder='big')
            balance_1 = self.ctf_contract.functions.balanceOf(wallet_address, position_id_int_1).call()
            
            logger.info(f"  - [Debug] On-Chain Balance Index 0: {balance_0}")
            logger.info(f"  - [Debug] On-Chain Balance Index 1: {balance_1}")
            
            # 3. 确定要赎回的 Index Set
            target_index_sets = []
            
            if balance_0 > 0:
                logger.info(f"  ✅ 检测到 Index 0 持仓 (余额: {balance_0})，加入赎回列表。")
                target_index_sets.append(index_set_0)
                if payout0 == 0:
                    logger.warning("  ⚠️ 警告: 您持有 Index 0，但合约显示 Index 0 赔付为 0 (输了)。赎回将获得 0 USDC。")
                else:
                    logger.info("  🎉 恭喜: 您持有 Index 0，且 Index 0 获胜！")

            if balance_1 > 0:
                logger.info(f"  ✅ 检测到 Index 1 持仓 (余额: {balance_1})，加入赎回列表。")
                target_index_sets.append(index_set_1)
                if payout1 == 0:
                    logger.warning("  ⚠️ 警告: 您持有 Index 1，但合约显示 Index 1 赔付为 0 (输了)。赎回将获得 0 USDC。")
                else:
                    logger.info("  🎉 恭喜: 您持有 Index 1，且 Index 1 获胜！")
            
            if not target_index_sets:
                logger.warning("  ⚠️ [Critical] 链上未检测到任何余额！API 可能显示了过时数据，或者您持有的是 ERC20 包装代币而非 CTF 原生代币。")
                # 即使没有余额，如果 API 坚持说有，我们也可以尝试用 API 的 Index (死马当活马医)，但通常没用
                logger.info(f"  尝试使用 API 提供的 Index: {api_outcome_index}")
                target_index_sets.append(1 << api_outcome_index)
            
            # ---------------------------------------------------------
            
            # 1. 准备 redeemPositions 的调用数据 (Calldata)
            func = self.ctf_contract.functions.redeemPositions(
                USDC_ADDRESS,
                PARENT_COLLECTION_ID,
                condition_id,
                target_index_sets
            )
            
            tx_preview = func.build_transaction({
                'chainId': 137, # Polygon Mainnet
                'gas': 0,
                'gasPrice': 0,
                'nonce': 0,
                'from': self.account.address,
                'value': 0
            })
            redeem_calldata = tx_preview['data']

            # 2. 判断是否使用代理钱包 (Gnosis Safe)
            use_proxy = (
                bool(self.config.proxy_wallet_address) and
                self.config.proxy_wallet_address.lower() != self.account.address.lower()
            )

            if use_proxy:
                return self._redeem_via_proxy(redeem_calldata)
            else:
                return self._redeem_direct(redeem_calldata)
                
        except Exception as e:
            logger.error(f"赎回操作异常: {e}", exc_info=True)
            return False

    def _redeem_via_proxy(self, calldata):
        """通过 Gnosis Safe 代理钱包执行赎回"""
        if not self.safe_contract:
            logger.error("Gnosis Safe 合约未加载，无法执行代理交易")
            return False
            
        logger.info(f"使用代理钱包执行赎回: {self.config.proxy_wallet_address}")
        
        try:
            # Gnosis Safe execTransaction 参数
            to = CTF_ADDRESS
            value = 0
            # 确保 data 是 bytes 类型，Gnosis Safe 合约要求 data 为 bytes
            if isinstance(calldata, str):
                data = HexBytes(calldata)
            else:
                data = calldata
            
            operation = 0 # 0 = Call, 1 = DelegateCall
            safe_tx_gas = 0
            base_gas = 0
            gas_price = 0
            gas_token = "0x0000000000000000000000000000000000000000"
            refund_receiver = "0x0000000000000000000000000000000000000000"
            
            # 获取 Safe 的 nonce
            try:
                nonce = self.safe_contract.functions.nonce().call()
                logger.info(f"[Debug] Safe Nonce: {nonce}")
            except Exception as e:
                logger.error(f"[Debug] 获取 Safe Nonce 失败: {e}")
                raise

            # 1. 获取交易哈希 (Transaction Hash)
            # 这是 Gnosis Safe 需要签名的内容
            try:
                logger.info(f"[Debug] getTransactionHash Params: to={to}, value={value}, data={data[:10]}... (len={len(data)}), op={operation}, safeTxGas={safe_tx_gas}, baseGas={base_gas}, gasPrice={gas_price}, gasToken={gas_token}, refundReceiver={refund_receiver}, nonce={nonce}")
                
                tx_hash_bytes = self.safe_contract.functions.getTransactionHash(
                    to, value, data, operation, safe_tx_gas, base_gas, gas_price, gas_token, refund_receiver, nonce
                ).call()
                logger.info(f"[Debug] Safe Tx Hash: {tx_hash_bytes.hex()}")
            except Exception as e:
                logger.error(f"[Debug] getTransactionHash 调用失败: {e}")
                raise
            
            # 2. 签名 (EOA Owner 签名)
            # Gnosis Safe 要求直接对交易哈希进行签名 (不加 Ethereum Signed Message 前缀)
            # Web3.py v6 / eth-account v0.9+ 使用 unsafe_sign_hash
            if hasattr(self.w3.eth.account, 'unsafe_sign_hash'):
                signed_hash = self.w3.eth.account.unsafe_sign_hash(tx_hash_bytes, private_key=self.account.key)
            elif hasattr(self.w3.eth.account, 'sign_hash'):
                signed_hash = self.w3.eth.account.sign_hash(tx_hash_bytes, private_key=self.account.key)
            else:
                # 兼容旧版本
                signed_hash = self.w3.eth.account.signHash(tx_hash_bytes, private_key=self.account.key)
            signature = signed_hash.signature # 65 bytes (r, s, v)
            logger.info(f"[Debug] Signature: {signature.hex()}")
            
            # 3. 构建 execTransaction 交易
            # 注意：这里是发送给 Safe 合约的交易，由 EOA 发起
            func = self.safe_contract.functions.execTransaction(
                to, value, data, operation, safe_tx_gas, base_gas, gas_price, gas_token, refund_receiver, signature
            )
            
            # 估算 Gas (外层交易)
            try:
                logger.info("[Debug] 开始估算 execTransaction Gas...")
                gas_estimate = func.estimate_gas({'from': self.account.address})
                logger.info(f"[Debug] Gas 估算成功: {gas_estimate}")
                gas_limit = int(gas_estimate * 1.2)
            except Exception as e:
                logger.warning(f"代理交易 Gas 估算失败: {e}")
                # 打印更详细的错误信息以便调试
                if hasattr(e, 'args'):
                    logger.warning(f"[Debug] Gas 估算错误详情: {e.args}")
                gas_limit = 500000 # 代理交易通常更贵，给高一点
            
            # 发送交易
            return self._send_transaction(func, gas_limit)
            
        except Exception as e:
            logger.error(f"代理钱包赎回失败: {e}", exc_info=True)
            return False

    def _redeem_direct(self, calldata):
        """直接通过 EOA 赎回 (非代理模式)"""
        logger.info("使用本地钱包直接执行赎回")
        
        try:
            gas_price = self.w3.eth.gas_price
            
            tx_params = {
                'to': CTF_ADDRESS,
                'data': calldata,
                'from': self.account.address,
                'gasPrice': gas_price,
                'nonce': self.w3.eth.get_transaction_count(self.account.address),
                'value': 0
            }
            
            # 估算 Gas
            try:
                gas_estimate = self.w3.eth.estimate_gas(tx_params)
                tx_params['gas'] = int(gas_estimate * 1.2)
            except Exception as e:
                logger.warning(f"Gas 估算失败: {e}")
                tx_params['gas'] = 300000
            
            # 签名并发送
            signed_tx = self.w3.eth.account.sign_transaction(tx_params, private_key=self.account.key)
            raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
            
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            logger.info(f"赎回交易已发送: {tx_hash.hex()}")
            
            return self._wait_for_receipt(tx_hash)
            
        except Exception as e:
            logger.error(f"直接赎回失败: {e}")
            return False

    def _send_transaction(self, func, gas_limit):
        """通用交易发送辅助函数"""
        try:
            gas_price = self.w3.eth.gas_price
            
            tx_params = {
                'from': self.account.address,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'nonce': self.w3.eth.get_transaction_count(self.account.address),
            }
            
            tx = func.build_transaction(tx_params)
            signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.account.key)
            
            raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            logger.info(f"交易已发送: {tx_hash.hex()}")
            
            return self._wait_for_receipt(tx_hash)
        except Exception as e:
            logger.error(f"发送交易失败: {e}")
            return False

    def _wait_for_receipt(self, tx_hash):
        """等待交易确认"""
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            # 兼容 AttributeDict 和 dict 访问方式
            status = receipt.get('status') if isinstance(receipt, dict) else receipt.status
            
            if status == 1:
                logger.info(f"✅ 交易上链成功! TX: {tx_hash.hex()}")
                
                # 抑制 MismatchedABI 警告
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    
                    # 1. 如果是代理模式，检查 Safe 的执行结果
                    if self.safe_contract:
                        try:
                            # 检查 ExecutionFailure
                            failures = self.safe_contract.events.ExecutionFailure().process_receipt(receipt)
                            if failures:
                                logger.error(f"❌ 代理交易内部执行失败 (ExecutionFailure)! TX: {tx_hash.hex()}")
                                for log in failures:
                                    logger.error(f"  - Failure Log: {log}")
                                return False # 标记为失败

                            # 检查 ExecutionSuccess
                            successes = self.safe_contract.events.ExecutionSuccess().process_receipt(receipt)
                            if successes:
                                logger.info(f"✅ 代理交易内部执行成功 (ExecutionSuccess).")
                            else:
                                # 既没成功也没失败（可能是非 execTransaction 交易，或者解析不到）
                                logger.warning("⚠️ 未检测到 Safe 执行事件，请确认是否为 Safe 交易。")
                                
                        except Exception as e:
                            logger.warning(f"解析 Safe 事件时出错: {e}")

                    # 2. 检查 CTF 的 PayoutRedemption 事件 (最终确认赎回成功)
                    if self.ctf_contract:
                        try:
                            redemptions = self.ctf_contract.events.PayoutRedemption().process_receipt(receipt)
                            if redemptions:
                                logger.info(f"✅ 赎回资金到账确认! 检测到 {len(redemptions)} 笔 PayoutRedemption 事件。")
                                for log in redemptions:
                                    payout = log['args'].get('payout', 0)
                                    logger.info(f"  - 赎回金额 (USDC units): {payout}")
                                    if payout == 0:
                                        logger.warning(f"⚠️ 警告: 赎回金额为 0。这意味着合约判定该持仓为输 (Losing Outcome)。")
                                        logger.warning(f"  请核对: 您持有的 Index 是否确实是获胜方？(通常 0=No, 1=Yes)")
                            else:
                                logger.warning("⚠️ 未检测到 PayoutRedemption 事件 (可能未产生赎回金额或解析失败)")
                        except Exception as e:
                            logger.warning(f"解析 CTF 事件失败: {e}")

                return True
            else:
                logger.error(f"❌ 交易失败 (Reverted). TX: {tx_hash.hex()}")
                return False
        except Exception as e:
            logger.error(f"等待交易确认超时或出错: {e}")
            return False

    def execute(self):
        """执行自动结算流程"""
        logger.info("启动自动结算流程...")
        
        if not self.w3 or not self.account:
            logger.error("Web3 或账户未初始化，无法执行")
            return

        # 获取钱包地址 (用于查询持仓)
        # 如果配置了代理钱包，则查询代理钱包的持仓；否则查询本地钱包
        wallet_address = self.config.proxy_wallet_address or self.account.address
        logger.info(f"当前查询钱包: {wallet_address}")
        
        # 获取可赎回持仓
        logger.info("正在查询可赎回持仓...")
        redeemable_positions = self.get_redeemable_positions(wallet_address)
        
        if not redeemable_positions:
            logger.info("当前没有可赎回的持仓。")
            return
            
        logger.info(f"发现 {len(redeemable_positions)} 个可赎回持仓。")
        
        # 执行赎回
        success_count = 0
        for pos in redeemable_positions:
            if self.redeem_position(pos):
                success_count += 1
            # 简单的防速率限制
            time.sleep(1)
            
        logger.info(f"结算完成。成功: {success_count}/{len(redeemable_positions)}")

# 兼容旧代码的独立执行入口
def execute_auto_redeem():
    """
    兼容旧接口的独立执行函数
    注意：此方式会重新创建 Config 实例，建议在主程序中使用 AutoRedeemService
    """
    config = Config()
    service = AutoRedeemService(config)
    service.execute()

if __name__ == "__main__":
    execute_auto_redeem()