#!/bin/bash
# flow2api 启动包装脚本（部署在 mac-m1，由 pm2 调用）
# 用法：pm2 start start_flow2api.sh --name flow2api --cwd ~/Prog/flow2api --interpreter bash
#
# 流程：先从 aime 增量同步最新 profile（保持 Google 登录态新鲜），再启动服务。
# 这样 flow2api 的 playwright 打码 profile 永远跟随 aime（aime 持续在线、登录态新鲜），
# 无需手动 cp 续期。
set -e

AIME_PROFILE="$HOME/Prog/aime/chrome_bot_profile"
FLOW_PROFILE="$HOME/Prog/flow2api/browser_data_playwright"
PYTHON="/Users/leslielu/miniconda3/envs/flow/bin/python"

# 1. 增量同步 aime profile → flow2api（排除 Chrome 锁文件，避免占用冲突）
if [ -d "$AIME_PROFILE" ]; then
    echo "[start_flow2api] 同步 aime profile → flow2api ..."
    rsync -a --delete \
        --exclude='SingletonLock' \
        --exclude='SingletonSocket' \
        --exclude='SingletonCookie' \
        --exclude='lockfile' \
        "$AIME_PROFILE/" "$FLOW_PROFILE/"
    # 清理可能残留的锁文件，确保 Playwright launch_persistent_context 不被占用
    rm -f "$FLOW_PROFILE"/SingletonLock "$FLOW_PROFILE"/SingletonSocket \
          "$FLOW_PROFILE"/SingletonCookie "$FLOW_PROFILE"/lockfile 2>/dev/null || true
    echo "[start_flow2api] profile 同步完成"
else
    echo "[start_flow2api] ⚠️ aime profile 不存在 ($AIME_PROFILE)，跳过同步，沿用现有 profile"
fi

# 2. 启动服务（沿用原有 conda python + main.py）
cd "$HOME/Prog/flow2api"
exec "$PYTHON" main.py
