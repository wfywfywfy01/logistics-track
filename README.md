# logistics-track 物流小助手

无 GUI 的后台服务：监听 IM 群里的预报 xlsx / 面单图片 / 文字配对，自动录单、抓 UPS/DHL 官网轨迹、状态推进时推群并私聊录单人，每日对账、同步云表格、备份台账。

## 工作流

```
群消息 ──► logi-watcher.py ──┬─ xlsx ──► tracking-pipeline.py ingest-forecast
                             ├─ 图片 ──► inbox ──► ocr_label.py(Qwen 多模态) ──► ingest-pair
                             └─ 文字 XSD…==1Z… ──► ingest-pair
                                          │
                                          ▼ 有新料即拉起
                              auto-track.py
                                ├─ track_all_ups.py   官网抓取(1Z→UPS, 其余→DHL) → SQLite 文档
                                ├─ wire_results.py    按运单版本回填台账
                                ├─ alert.py           海关扣关/退回/异常 推群警告
                                ├─ sync_sheet.py      同步云文档表格
                                ├─ backup.py          台账 zip 上传 V盘
                                ├─ tracking-pipeline.py notify   状态前进 → 推群 + 私聊
                                └─ send_dms.py        补发私聊(去重)
```

- 台账唯一：`data/shipments.db`。旧 JSON 在首次启动时幂等迁移。消息、OCR、工作任务和通知均使用持久队列与租约。
- 官网抓取用 `patchright` 有头 Chromium（窗口移出屏幕 / 容器内 Xvfb）拦截内部 JSON 接口；UPS/DHL 封 TLS 指纹不封 IP，容器内需海外 SS 出口 + `UPS_DISABLE_HTTP2=1`。
- 运单绑定带版本，旧运单结果不得覆盖新绑定；承运商未知状态保持未知，不猜测为运输中。

## 文件

| 文件 | 作用 |
|---|---|
| `tracking-pipeline.py` | 核心管线：`ingest-forecast` / `ingest-pair` / `track-update` / `rematch` / `list` / `notify` |
| `logi-watcher.py` | 群监听长驻，拉起 `auto-track.py` |
| `auto-track.py` | 自动闭环编排（`--mode full\|incremental`，`--skip-track`） |
| `ups_track.py` / `dhl_track.py` | 单票官网抓取，返回 `{ok, stage, status_en, detail}` |
| `track_all_ups.py` / `track_retry.py` | 批量抓取 / 只补抓失败单 |
| `ocr_label.py` | 面单 OCR → 配对入库 |
| `reconcile.py` | 每日对账报告，未匹配单自动重新匹配录单人 |
| `org_refresh.py` | 刷新组织人员快照；同名人员不自动私聊 |
| `proxy-watchdog.py` | 出口代理看门狗，主节点挂自动切备节点，双挂告警 |
| `storage.py` / `robust.py` | SQLite 事务、任务租约、系统级文件锁与安全子进程 |
| `backup.py` / `restore_backup.py` | 一致性备份、SHA-256 校验与恢复演练 |
| `deploy/` | Dockerfile、entrypoint、compose、`.env.example` |
| `SKILL.md` | 面向 Agent 的操作手册与踩坑记录 |

## 本地运行

```bash
pip install -r deploy/requirements.txt
python -m patchright install chromium
# vertu-cli 需在 PATH 且已登录
python logi-watcher.py --channel-id <群ID> --bot-app-id <bot> --interval 30
# 单票测试
python ups_track.py 1Z...
python dhl_track.py 9941305430
```

## 部署（Docker）

```bash
cp deploy/.env.example deploy/.env   # 填 Vertu 认证 / XRAY_* 出口节点 / OCR_* 等
docker build -f deploy/Dockerfile -t logistics-track:latest .
docker run -d --name logistics-track --restart unless-stopped \
  --shm-size=1g --memory=1536m --memory-swap=2048m --env-file deploy/.env \
  --log-opt max-size=20m --log-opt max-file=3 \
  -v logistics-data:/app/data -v logistics-tmp:/app/tmp logistics-track:latest
```

自愈：watcher 每轮成功轮询刷新 `data/.watcher-heartbeat`。超过 `WATCHER_STALE_MIN`（默认 10）分钟未更新，入口脚本结束主进程，由 Docker 重启容器；`docker ps` 的 HEALTHCHECK 使用同一判据。

恢复演练必须先停止服务，再运行 `python restore_backup.py <backup.zip> --data-dir data --force`，随后执行 SQLite `PRAGMA integrity_check` 并核对订单计数。

或 `cd deploy && docker compose up -d`。容器入口自动：Xvfb → xray 代理 → 看门狗 → 定时巡检（`TRACK_TIMES`）/ 每日对账（`RECONCILE_HOUR`）/ 每周日组织刷新 → 前台 watcher。

内存红线：宿主机小内存时务必限死 `--memory` 与 `--shm-size`，浏览器并发固定 2。

## 环境变量

见 `deploy/.env.example`，全部变量均有注释。必填：`VERTU_*` 四项、`CHANNEL_ID`、`XRAY_PASS`（无出口代理官网抓取会失败）。
