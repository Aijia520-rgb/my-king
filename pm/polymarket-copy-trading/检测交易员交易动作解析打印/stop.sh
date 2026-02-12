#!/bin/bash

cd /root/follow
pkill -f "python3 main.py"

sleep 3
echo "脚本已经停止.stopped"
ps aux | grep "[p]ython3"