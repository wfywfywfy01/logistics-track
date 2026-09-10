#!/usr/bin/env bash
# 物流追踪家 容器入口: 起 Xvfb 虚拟屏 -> 并行跑 群监听 + 定时巡检
set -e

echo "[entrypoint] starting Xvfb on :99"
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
sleep 2

export DISPLAY=:99
export PYTHONIOENCODING=utf-8

WATCHER_STALE_MIN="${WATCHER_STALE_MIN:-10}"
case "$WATCHER_STALE_MIN" in
  ''|*[!0-9]*|0) echo "[entrypoint] WATCHER_STALE_MIN must be a positive integer"; exit 1 ;;
esac

# xray 本地代理(SS 节点 -> 海外出口): 密码从环境变量注入, 不落仓库
# s1 = XRAY_*, s2(备用, 可选) = XRAY2_*; 看门狗按 config-s1/config-s2 切换
write_xray_cfg() {  # $1=输出文件 $2=addr $3=port $4=method $5=pass
  cat > "$1" <<EOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [{"port": 10809, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": true}}],
  "outbounds": [{"protocol": "shadowsocks", "settings": {"servers": [{"address": "$2", "port": $3, "method": "$4", "password": "$5"}]}}]}
EOF
}
if [ -z "$XRAY_PASS" ]; then
  echo "[entrypoint] XRAY_PASS missing, proxy disabled (直接抓取会失败)"
else
  mkdir -p /app/deploy/xray
  write_xray_cfg /app/deploy/xray/config-s1.json "${XRAY_ADDR:-c57s1.portablesubmarines.com}" "${XRAY_PORT:-15615}" "${XRAY_METHOD:-aes-256-gcm}" "$XRAY_PASS"
  rm -f /app/deploy/xray/config-s2.json
  [ -n "$XRAY2_PASS" ] && write_xray_cfg /app/deploy/xray/config-s2.json "${XRAY2_ADDR:?XRAY2_ADDR required}" "${XRAY2_PORT:?XRAY2_PORT required}" "${XRAY2_METHOD:-aes-256-gcm}" "$XRAY2_PASS"
  NODE=$(cat /app/data/.active_node 2>/dev/null || echo s1)
  [ -f "/app/deploy/xray/config-$NODE.json" ] || NODE=s1
  cp "/app/deploy/xray/config-$NODE.json" /app/deploy/xray/config.json
  echo "[entrypoint] starting xray proxy on 127.0.0.1:10809 (node=$NODE)"
  xray run -c /app/deploy/xray/config.json >/var/log/xray.log 2>&1 &
fi
sleep 2
export UPS_PROXY="socks5://127.0.0.1:10809"
export UPS_DISABLE_HTTP2=1
echo "[entrypoint] UPS_PROXY=$UPS_PROXY"

# 出口节点看门狗: 5分钟测一次, s1 挂自动切 s2, 双挂超过1小时私信告警
mkdir -p /app/data
[ -f /app/data/.active_node ] || echo s1 > /app/data/.active_node
python proxy-watchdog.py >/var/log/watchdog.log 2>&1 &


CHANNEL_ID="${CHANNEL_ID:?CHANNEL_ID is required}"
BOT_APP_ID="${BOT_APP_ID:-vbot_EIBezUGncpO8v0QJ}"
INTERVAL="${INTERVAL:-30}"
POLL_MINUTES="${POLL_MINUTES:-60}"
RECONCILE_HOUR="${RECONCILE_HOUR:-9}"
RECONCILE_HOUR=$(printf '%02d' "$((10#$RECONCILE_HOUR))")

echo "[entrypoint] channel=$CHANNEL_ID bot=$BOT_APP_ID interval=${INTERVAL}s poll=${POLL_MINUTES}min"

ADMIN_PID=""
if [ -n "$ADMIN_TOKEN" ]; then
  echo "[entrypoint] starting authenticated admin console"
  python admin_server.py --host 0.0.0.0 --port "${ADMIN_PORT:-8080}" >/var/log/admin.log 2>&1 &
  ADMIN_PID=$!
fi

# 每天两轮全量巡检 (09:05 / 15:05, Asia/Shanghai); 群里来新料时 watcher 还会即时触发一轮
TRACK_TIMES="${TRACK_TIMES:-09:05,15:05}"
(
  while true; do
    HM=$(date +%H:%M)
    TODAY=$(date +%F)
    for T in $(echo "$TRACK_TIMES" | tr ',' ' '); do
      if [ "$HM" = "$T" ]; then
        MARK=/app/data/.tracked_$T
        if [ "$(cat "$MARK" 2>/dev/null)" != "$TODAY" ]; then
          echo "[scheduler] $T periodic track run"
          MODE=incremental; [ "$T" = "09:05" ] && MODE=full
          if python auto-track.py --mode "$MODE" --channel-id "$CHANNEL_ID" --bot-app-id "$BOT_APP_ID"; then
            echo "$TODAY" > "$MARK"
          else
            echo "[scheduler] run failed"
          fi
        fi
      fi
    done
    sleep 60
  done
) &

# 每周日 10:00 刷新组织人员快照(新入职录单人自动可私聊)
(
  while true; do
    HM=$(date +%H:%M)
    DOW=$(date +%u)
    TODAY=$(date +%F)
    if [ "$HM" = "10:00" ] && [ "$DOW" = "7" ]; then
      if [ "$(cat /app/data/.org_refreshed 2>/dev/null)" != "$TODAY" ]; then
        echo "[org] weekly refresh"
        if python /app/org_refresh.py; then
          echo "$TODAY" > /app/data/.org_refreshed
        else
          echo "[org] refresh failed"
        fi
      fi
    fi
    sleep 60
  done
) &

# 接管循环：消费持久任务，租约超时后可由下一轮恢复。
(
  while true; do
    python auto-track.py --queued-only --mode incremental --channel-id "$CHANNEL_ID" --bot-app-id "$BOT_APP_ID" || echo "[takeover] queued run failed"
    sleep 120
  done
) &

# 每日对账报告: RECONCILE_HOUR 点发一次(Asia/Shanghai)
(
  while true; do
    NOW_H=$(date +%H)
    TODAY=$(date +%F)
    MARK=/app/data/.reconciled_date
    if [ "$NOW_H" = "$RECONCILE_HOUR" ] && [ "$(cat "$MARK" 2>/dev/null)" != "$TODAY" ]; then
      echo "[reconcile] daily report"
      if python reconcile.py --channel-id "$CHANNEL_ID"; then
        echo "$TODAY" > "$MARK"
      else
        echo "[reconcile] failed"
      fi
    fi
    sleep 900
  done
) &

# 已完成附件按保留期清理；待办、重试、运行中和死信原件保留。
( while true; do python cleanup_evidence.py || true; sleep 86400; done ) &

# 群监听(后台子进程, 本脚本留作 PID 1 做存活监督)
# 注意: 不能 exec python 再 kill 1 —— PID 1 对无 handler 的信号一律忽略, 容器内 kill 不动它
echo "[entrypoint] starting watcher"
touch /app/data/.watcher-heartbeat
python logi-watcher.py --channel-id "$CHANNEL_ID" --bot-app-id "$BOT_APP_ID" --interval "$INTERVAL" &
WATCHER_PID=$!
trap 'echo "[entrypoint] SIGTERM, stopping services"; kill "$WATCHER_PID" $ADMIN_PID 2>/dev/null; wait "$WATCHER_PID"; exit 0' TERM INT

# watcher 假死自愈: 每轮成功轮询都会刷新 heartbeat。
# 容器由 --restart unless-stopped 整体拉起(xray/看门狗/定时循环一起重来)
while kill -0 "$WATCHER_PID" 2>/dev/null; do
  sleep 60 & wait $!   # 用 wait 让 SIGTERM 能立刻打断 sleep 进 trap
  if [ -f /app/data/.watcher-heartbeat ] && [ -n "$(find /app/data/.watcher-heartbeat -mmin +"$WATCHER_STALE_MIN")" ]; then
    echo "[liveness] watcher heartbeat stale > ${WATCHER_STALE_MIN}min, restarting container"
    kill "$WATCHER_PID" 2>/dev/null; sleep 5; kill -9 "$WATCHER_PID" 2>/dev/null
    exit 1
  fi
  if [ -n "$ADMIN_PID" ] && ! kill -0 "$ADMIN_PID" 2>/dev/null; then
    echo "[liveness] admin console exited unexpectedly"
    exit 1
  fi
done
echo "[entrypoint] watcher exited unexpectedly, restarting container"
exit 1
