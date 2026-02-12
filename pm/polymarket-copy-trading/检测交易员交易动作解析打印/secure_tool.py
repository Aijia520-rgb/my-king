import os
import sys
import argparse
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
import pymysql
from typing import Optional, Dict, Any, List
from db_config import DB_CONFIG


PRIVATE_KEY_FILE = "20251215D.pem"
PUBLIC_KEY_FILE = "20251215E.pem"

class CryptoManager:
    """
    核心加密管理类，封装 RSA 非对称加密算法和数据库操作。
    """
    
    @staticmethod
    def format_address_plain(address: str) -> str:
        """
        将完整地址格式化为前3位+***+后3位的格式。
        
        Args:
            address (str): 完整的地址字符串
            
        Returns:
            str: 格式化后的地址（前3位+***+后3位）
        """
        if not address or len(address) < 6:
            return address
        
        return f"{address[:3]}***{address[-3:]}"
    
    @staticmethod
    def get_db_connection():
        """
        获取数据库连接。
        
        Returns:
            pymysql.Connection: 数据库连接对象
        """
        return pymysql.connect(**DB_CONFIG)
    
    @staticmethod
    def save_wallet_info(market: str, user_name: str, agent_address: str, wallet_address: str,
                        private_key: str) -> bool:
        """
        保存钱包信息到数据库。
        
        Args:
            market (str): 市场类型（pm或者op）
            user_name (str): 用户名称
            agent_address (str): 代理钱包地址（完整地址）
            wallet_address (str): 钱包地址（完整地址）
            private_key (str): 钱包私钥（完整，由用户手动提供）
            
        Returns:
            bool: 保存成功返回True，失败返回False
        """
        try:
            # 加密敏感信息
            agent_address_encrypted = CryptoManager.encrypt(agent_address)
            wallet_address_encrypted = CryptoManager.encrypt(wallet_address)
            private_key_encrypted = CryptoManager.encrypt(private_key)
            
            # 格式化明文显示
            agent_address_plain = CryptoManager.format_address_plain(agent_address)
            wallet_address_plain = CryptoManager.format_address_plain(wallet_address)
            
            # 自动读取公钥文件并格式化
            public_key_plain = CryptoManager._get_formatted_public_key()
            if not public_key_plain:
                print("无法读取公钥文件 20251215E.pem")
                return False
            
            # 连接数据库并插入数据
            conn = CryptoManager.get_db_connection()
            with conn.cursor() as cursor:
                sql = """
                INSERT INTO encrypted_wallets (
                    market, user_name, agent_address_plain, agent_address_encrypted,
                    wallet_address_plain, wallet_address_encrypted, encrypted_private_key, public_key_plain
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """
                # print(sql)
                cursor.execute(sql, (
                    market, user_name, agent_address_plain, agent_address_encrypted,
                    wallet_address_plain, wallet_address_encrypted, private_key_encrypted, public_key_plain
                ))
                
                conn.commit()
                print(f"钱包信息已保存到数据库 - 用户: {user_name}, 市场: {market}")
                return True
                
        except Exception as e:
            print(f"保存钱包信息失败: {e}")
            return False
            
        finally:
            if 'conn' in locals() and conn:
                conn.close()
    
    @staticmethod
    def _get_formatted_public_key(pem_file: str = "20251215E.pem") -> str:
        """
        从PEM文件读取公钥并格式化为前3位+***+后3位。
        
        Args:
            pem_file (str): PEM公钥文件名
            
        Returns:
            str: 格式化后的公钥字符串
        """
        try:
            if not os.path.exists(pem_file):
                print(f"公钥文件 {pem_file} 不存在")
                return ""
            
            with open(pem_file, 'r') as f:
                lines = f.readlines()
            
            # 提取公钥内容（去掉 BEGIN/END 标记）
            public_key_lines = []
            for line in lines:
                line = line.strip()
                if line and not line.startswith('-----'):
                    public_key_lines.append(line)
            
            # 拼接公钥内容
            public_key_content = ''.join(public_key_lines)
            
            # 格式化为前3位+***+后3位
            if len(public_key_content) >= 6:
                return f"{public_key_content[:3]}***{public_key_content[-3:]}"
            else:
                return public_key_content
                
        except Exception as e:
            print(f"读取公钥文件失败: {e}")
            return ""
    
    @staticmethod
    def get_wallet_info(user_name: str) -> Optional[Dict[str, Any]]:
        """
        根据用户名称查询钱包信息。
        
        Args:
            user_name (str): 用户名称
            
        Returns:
            dict: 钱包信息字典，包含加密后的字段，如果未找到返回None
        """
        try:
            conn = CryptoManager.get_db_connection()
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                sql = """
                SELECT market, user_name, agent_address_plain, agent_address_encrypted,
                       wallet_address_plain, wallet_address_encrypted, encrypted_private_key, public_key_plain,
                       created_at, updated_at
                FROM encrypted_wallets
                WHERE user_name = %s
                """
                
                cursor.execute(sql, (user_name,))
                result = cursor.fetchone()
                
                if result:
                    print(f"找到用户 {user_name} 的钱包信息")
                    return result
                else:
                    print(f"未找到用户 {user_name} 的钱包信息")
                    return None
                    
        except Exception as e:
            print(f"查询钱包信息失败: {e}")
            return None
            
        finally:
            if 'conn' in locals() and conn:
                conn.close()
    
    @staticmethod
    def get_all_wallets(market: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        查询所有钱包信息，可按市场筛选。
        
        Args:
            market (str, optional): 市场类型（pm或者op），不传则查询所有
            
        Returns:
            list: 钱包信息列表，每个元素是一个字典
        """
        try:
            conn = CryptoManager.get_db_connection()
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                if market:
                    sql = """
                    SELECT id, market, user_name, agent_address_plain, wallet_address_plain,
                           public_key_plain, created_at, updated_at
                    FROM encrypted_wallets
                    WHERE market = %s
                    ORDER BY created_at DESC
                    """
                    cursor.execute(sql, (market,))
                else:
                    sql = """
                    SELECT id, market, user_name, agent_address_plain, wallet_address_plain,
                           public_key_plain, created_at, updated_at
                    FROM encrypted_wallets
                    ORDER BY created_at DESC
                    """
                    cursor.execute(sql)
                
                results = cursor.fetchall()
                print(f"查询到 {len(results)} 条钱包记录")
                return results
                    
        except Exception as e:
            print(f"查询钱包列表失败: {e}")
            return []
            
        finally:
            if 'conn' in locals() and conn:
                conn.close()
    
    @staticmethod
    def decrypt_wallet_info(encrypted_data: Dict[str, Any]) -> Dict[str, str]:
        """
        解密钱包信息中的加密字段。
        
        Args:
            encrypted_data (dict): 包含加密字段的字典，需要包含：
                - agent_address_encrypted
                - wallet_address_encrypted
                - encrypted_private_key
                
        Returns:
            dict: 解密后的字段字典
        """
        try:
            decrypted = {
                'agent_address': CryptoManager.decrypt(encrypted_data.get('agent_address_encrypted', '')),
                'wallet_address': CryptoManager.decrypt(encrypted_data.get('wallet_address_encrypted', '')),
                'private_key': CryptoManager.decrypt(encrypted_data.get('encrypted_private_key', ''))
            }
            return decrypted
        except Exception as e:
            print(f"解密钱包信息失败: {e}")
            return {}
    
    @staticmethod
    def generate_keys():
        """
        生成 RSA 公钥和私钥对，并保存到文件。
        """
        print("Generating RSA key pair...")
        
        # 1. 生成私钥
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        # 2. 保存私钥
        with open(PRIVATE_KEY_FILE, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            ))
        print(f"Private key saved to: {PRIVATE_KEY_FILE}")
        print("WARNING: Keep private.pem SECRET! Do not commit to Git.")

        # 3. 生成公钥
        public_key = private_key.public_key()

        # 4. 保存公钥
        with open(PUBLIC_KEY_FILE, "wb") as f:
            f.write(public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            ))
        print(f"Public key saved to: {PUBLIC_KEY_FILE}")

    @staticmethod
    def encrypt(data: str, public_key_path: str = PUBLIC_KEY_FILE) -> str:
        """
        使用公钥加密字符串。
        
        Args:
            data (str): 明文字符串。
            public_key_path (str): 公钥文件路径。
            
        Returns:
            str: 加密后的 Base64 字符串。
        """
        if not data:
            return ""
            
        if not os.path.exists(public_key_path):
            raise FileNotFoundError(f"Public key not found at {public_key_path}. Run 'generate-keys' first.")

        with open(public_key_path, "rb") as f:
            public_key = serialization.load_pem_public_key(f.read())

        encrypted = public_key.encrypt(
            data.encode("utf-8"),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        return base64.b64encode(encrypted).decode("utf-8")

    @staticmethod
    def decrypt(token: str, private_key_path: str = PRIVATE_KEY_FILE) -> str:
        """
        使用私钥解密字符串。
        
        Args:
            token (str): 密文字符串 (Base64)。
            private_key_path (str): 私钥文件路径。
            
        Returns:
            str: 解密后的明文字符串。
        """
        if not token:
            return ""
            
        if not os.path.exists(private_key_path):
            raise FileNotFoundError(f"Private key not found at {private_key_path}. Cannot decrypt.")

        with open(private_key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(
                f.read(),
                password=None
            )

        decrypted = private_key.decrypt(
            base64.b64decode(token),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        return decrypted.decode("utf-8")

def handle_generate_keys(args):
    """处理生成密钥命令"""
    try:
        CryptoManager.generate_keys()
    except Exception as e:
        print(f"Error generating keys: {e}", file=sys.stderr)
        sys.exit(1)

def handle_encrypt(args):
    """处理加密命令"""
    try:
        encrypted = CryptoManager.encrypt(args.value)
        print("-" * 60)
        print("ENCRYPTED TOKEN (Save this to your .env):")
        print(f"\n{encrypted}\n")
        print("-" * 60)
    except Exception as e:
        print(f"Error encrypting data: {e}", file=sys.stderr)
        sys.exit(1)

def handle_decrypt(args):
    """处理解密命令"""
    try:
        decrypted = CryptoManager.decrypt(args.value)
        print("-" * 60)
        print("DECRYPTED VALUE:")
        print(f"\n{decrypted}\n")
        print("-" * 60)
    except Exception as e:
        print(f"Error decrypting data: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Local Encryption/Decryption Tool using RSA")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: g (generate-keys)
    subparsers.add_parser("g", help="Generate new RSA Public/Private Key Pair")

    # Command: e (encrypt)
    encrypt_parser = subparsers.add_parser("e", help="Encrypt a string using public.pem")
    encrypt_parser.add_argument("value", help="The plaintext string to encrypt")

    # Command: d (decrypt)
    decrypt_parser = subparsers.add_parser("d", help="Decrypt a string using private.pem")
    decrypt_parser.add_argument("value", help="The encrypted string (token) to decrypt")

    args = parser.parse_args()

    if args.command == "g":
        handle_generate_keys(args)
    elif args.command == "e":
        handle_encrypt(args)
    elif args.command == "d":
        handle_decrypt(args)
    else:
        parser.print_help()

def save_wallet_info():
#     CryptoManager.save_wallet_info(
#     market='pm'.upper(),  # 市场类型：'pm' 或 'op'
#     user_name='马文哲-Test',  # 用户名称
#     agent_address='',  # 代理钱包地址（完整）
#     wallet_address='',  # 钱包地址（完整）
#     private_key=''  # 钱包私钥（完整）
# )
    CryptoManager.save_wallet_info(
    market='pm'.upper(),  # 市场类型：'pm' 或 'op'
    user_name='大白-Test',  # 用户名称
    agent_address='',  # 代理钱包地址（完整）
    wallet_address='',  # 钱包地址（完整）
    private_key=''  # 钱包私钥（完整）
)


if __name__ == "__main__":
    # main()
    save_wallet_info()
