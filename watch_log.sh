#!/bin/bash

# 远端日志查看脚本 - 断线自动重连（带心跳检测）

REMOTE_HOST="mac-m1"
LOG_FILE="~/Prog/flow2api/logs.txt"
LINES=100

echo "========================================"
echo "  远端日志监控 (断线自动重连)"
echo "  主机: $REMOTE_HOST"
echo "  日志: $LOG_FILE"
echo "========================================"
echo "按 Ctrl+C 退出"
echo ""

# SSH 选项：添加心跳检测，5秒发送一次，最多3次失败后断开
SSH_OPTS="-o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o ConnectTimeout=10"

while true; do
    echo "[$(date '+%H:%M:%S')] 正在连接..."

    # 直接运行 SSH 和 tail，依赖 SSH 的心跳机制检测断线
    ssh $SSH_OPTS "$REMOTE_HOST" "tail -n $LINES -F $LOG_FILE" 2>/dev/null

    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 130 ]; then
        # Ctrl+C 退出
        echo ""
        echo "用户退出"
        break
    else
        echo ""
        echo "[$(date '+%H:%M:%S')] 连接断开 (退出码: $EXIT_CODE)，3秒后重连..."
        sleep 3
        echo ""
    fi
done
