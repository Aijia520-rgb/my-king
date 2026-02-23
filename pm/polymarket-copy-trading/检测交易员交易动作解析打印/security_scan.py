#!/usr/bin/env python3
"""
安全扫描脚本 - 检查项目中的可疑代码
"""

import os
import sys
import ast
import re
from pathlib import Path

class SecurityScanner:
    def __init__(self, project_path):
        self.project_path = Path(project_path)
        self.suspicious_patterns = {
            'network_exfiltration': [
                r'post\s*\(\s*.*key',
                r'send\s*\(\s*.*key', 
                r'upload\s*\(\s*.*key',
                r'requests\.post.*private',
                r'http.*private',
                r'telegra',
                r'discord',
                r'slack',
                r'webhook',
            ],
            'code_execution': [
                r'exec\s*\(',
                r'eval\s*\(',
                r'compile\s*\(',
                r'__import__\s*\(',
                r'subprocess\.',
                r'os\.system',
                r'os\.popen',
            ],
            'data_serialization': [
                r'pickle\.loads',
                r'marshal\.loads',
                r'yaml\.load(?!_safe)',
            ],
            'clipboard_keyboard': [
                r'pyperclip',
                r'clipboard',
                r'keyboard',
                r'pynput',
                r'keylogger',
            ],
            'obfuscation': [
                r'base64\.b64decode.*exec',
                r'base64\.b64decode.*eval',
                r'chr\s*\(\s*\d+\s*\)',
                r'\\x[0-9a-fA-F]{2}',
            ],
            'file_operations': [
                r'open\s*\(\s*.*[\'"]w',
                r'write\s*\(\s*.*key',
                r'write\s*\(\s*.*private',
            ]
        }
        self.findings = []
        
    def scan_file(self, file_path):
        """扫描单个文件"""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                lines = content.split('\n')
                
            for line_num, line in enumerate(lines, 1):
                for category, patterns in self.suspicious_patterns.items():
                    for pattern in patterns:
                        if re.search(pattern, line, re.IGNORECASE):
                            # 排除注释行
                            stripped = line.strip()
                            if stripped.startswith('#') or stripped.startswith('"""') or stripped.startswith("'''"):
                                continue
                            
                            self.findings.append({
                                'file': str(file_path.relative_to(self.project_path)),
                                'line': line_num,
                                'category': category,
                                'pattern': pattern,
                                'content': line.strip()[:100]
                            })
        except Exception as e:
            print(f"无法扫描文件 {file_path}: {e}")
    
    def scan_project(self):
        """扫描整个项目"""
        python_files = list(self.project_path.rglob('*.py'))
        
        # 排除 py_clob_client (这是官方库)
        python_files = [f for f in python_files if 'py_clob_client' not in str(f)]
        
        print(f"正在扫描 {len(python_files)} 个 Python 文件...\n")
        
        for file_path in python_files:
            if '__pycache__' in str(file_path):
                continue
            self.scan_file(file_path)
        
        return self.findings
    
    def print_report(self):
        """打印扫描报告"""
        print("="*80)
        print("安全扫描报告")
        print("="*80)
        
        if not self.findings:
            print("\n✅ 未发现可疑代码！")
            print("项目看起来是安全的。")
            return
        
        # 按类别分组
        by_category = {}
        for finding in self.findings:
            cat = finding['category']
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(finding)
        
        print(f"\n⚠️  发现 {len(self.findings)} 个可疑代码片段\n")
        
        for category, findings in by_category.items():
            print(f"\n【{category.upper()}】 - {len(findings)} 处")
            print("-"*80)
            for finding in findings[:5]:  # 只显示前5个
                print(f"  文件: {finding['file']}:{finding['line']}")
                print(f"  代码: {finding['content']}")
                print()
            if len(findings) > 5:
                print(f"  ... 还有 {len(findings) - 5} 处 ...\n")
        
        print("\n" + "="*80)
        print("建议:")
        print("1. 仔细检查标记的代码")
        print("2. 确认没有将私钥发送到远程服务器")
        print("3. 确认没有执行恶意代码")
        print("="*80)


def check_imports(project_path):
    """检查可疑的导入"""
    print("\n" + "="*80)
    print("检查第三方依赖...")
    print("="*80)
    
    suspicious_modules = [
        'telebot', 'python-telegram-bot', 'discord', 'slack-sdk',
        'keylogger', 'pynput', 'keyboard', 'pyperclip',
        'pycryptodome', 'cryptography'  # 可能是正常的
    ]
    
    req_file = Path(project_path) / 'requirements.txt'
    if req_file.exists():
        with open(req_file, 'r') as f:
            content = f.read()
            for module in suspicious_modules:
                if module in content.lower():
                    print(f"⚠️  发现依赖: {module}")
    
    print("\n✅ 依赖检查完成")


if __name__ == "__main__":
    project_path = Path(__file__).parent
    
    scanner = SecurityScanner(project_path)
    scanner.scan_project()
    scanner.print_report()
    
    check_imports(project_path)
    
    print("\n" + "="*80)
    print("扫描完成！")
    print("="*80)
