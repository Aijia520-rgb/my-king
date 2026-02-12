#!/bin/bash

# 进入工作目录
cd /root/follow

# 检查是否已经在运行
if pgrep -f "python3 main.py" > /dev/null; then
    echo "程序已经在运行中 (Process is already running)."
    ps aux | grep "[p]ython3 main.py"
    exit 1
fi

# 启动程序
# > nohup.out 2>&1 : 将标准输出和错误输出都重定向到 nohup.out
# & : 在后台运行
nohup python3 main.py > nohup.out 2>&1 &

# 获取最近一个后台进程的 PID
PID=$!

echo "正在启动... PID: $PID"
sleep 3

# 检查进程是否还在运行
if ps -p $PID > /dev/null; then
    echo "脚本已经运行.running..."
    echo "----------------------------------------"
    ps aux | grep "[p]ython3 main.py"
    echo "----------------------------------------"
    echo "最新日志 (tail -n 5 nohup.out):"
    tail -n 5 nohup.out
else
    echo "脚本启动失败! 请查看完整日志:"
    cat nohup.out
fi