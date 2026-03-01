#!/bin/bash

# 远端日志查看脚本 - 断线自动重连

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

while true; do
    echo "[$(date '+%H:%M:%S')] 正在连接..."

    ssh "$REMOTE_HOST" "tail -$LINES -F $LOG_FILE" 2>/dev/null

    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 130 ]; then
        # Ctrl+C 退出
        echo ""
        echo "用户退出"
        break
    else
        echo ""
        echo "[$(date '+%H:%M:%S')] 连接断开，5秒后重连..."
        sleep 5
        echo ""
    fi
done
