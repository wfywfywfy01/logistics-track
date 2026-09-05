#!/usr/bin/env bash
# 物流追踪家 容器入口: 起 Xvfb 虚拟屏 -> 并行跑 群监听 + 定时巡检
set -e

echo "[entrypoint] starting Xvfb on :99"
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
sleep 2

export DISPLAY=:99
export PYTHONIOENCODING=utf-8

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


# 首次启动: 宿主挂载空 data 目录时, 用镜像内种子数据初始化(台账/人员映射)
if [ -z "$(ls -A /app/data 2>/dev/null)" ]; then
  echo "[entrypoint] initializing /app/data from seed"
  mkdir -p /app/data
  cp -r /app/seed/. /app/data/ 2>/dev/null || true
fi
CHANNEL_ID="${CHANNEL_ID:?CHANNEL_ID is required}"
BOT_APP_ID="${BOT_APP_ID:-vbot_EIBezUGncpO8v0QJ}"
INTERVAL="${INTERVAL:-30}"
POLL_MINUTES="${POLL_MINUTES:-60}"
RECONCILE_HOUR="${RECONCILE_HOUR:-9}"

echo "[entrypoint] channel=$CHANNEL_ID bot=$BOT_APP_ID interval=${INTERVAL}s poll=${POLL_MINUTES}min"

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
          python auto-track.py --mode "$MODE" --channel-id "$CHANNEL_ID" --bot-app-id "$BOT_APP_ID" || echo "[scheduler] run failed"
          echo "$TODAY" > "$MARK"
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
        python /app/org_refresh.py || echo "[org] refresh failed"
        echo "$TODAY" > /app/data/.org_refreshed
      fi
    fi
    sleep 60
  done
) &

# 接管循环(崩溃安全): 有新料(dirty)且上次没跑完, 由这个循环兜底执行抓取
(
  while true; do
    if [ -f /app/data/.dirty ]; then
      DIRTY=$(cat /app/data/.dirty)
      LAST=$(cat /app/data/.tracked_dirty 2>/dev/null || echo 0)
      if [ "$DIRTY" != "$LAST" ]; then
        echo "[takeover] dirty material, incremental track"
        if python auto-track.py --mode incremental --channel-id "$CHANNEL_ID" --bot-app-id "$BOT_APP_ID"; then
          echo "$DIRTY" > /app/data/.tracked_dirty
        fi
      fi
    fi
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
      python reconcile.py --channel-id "$CHANNEL_ID" || echo "[reconcile] failed"
      echo "$TODAY" > "$MARK"
    fi
    sleep 900
  done
) &

# watcher 假死自愈: 每轮轮询都会重写 watcher_state.json, 超过 WATCHER_STALE_MIN 分钟没动 -> 结束 PID 1(watcher),
# 容器退出后由 --restart unless-stopped 整体拉起(xray/看门狗/定时循环一起重来)
WATCHER_STALE_MIN="${WATCHER_STALE_MIN:-10}"
(
  sleep 300
  while true; do
    if [ -f /app/data/watcher_state.json ] && [ -n "$(find /app/data/watcher_state.json -mmin +"$WATCHER_STALE_MIN")" ]; then
      echo "[liveness] watcher_state.json stale > ${WATCHER_STALE_MIN}min, exit for restart"
      kill 1
    fi
    sleep 60
  done
) &

# 下载附件缓存清理: 超过 30 天的 tmp 文件删掉, 防止 logistics-tmp 卷无限涨
( while true; do find /app/tmp -type f -mtime +30 -delete 2>/dev/null; sleep 86400; done ) &

# 群监听(前台)
echo "[entrypoint] starting watcher"
exec python logi-watcher.py --channel-id "$CHANNEL_ID" --bot-app-id "$BOT_APP_ID" --interval "$INTERVAL"