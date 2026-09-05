---
name: logistics-track
description: >-
  物流小助手。群里发预报 xlsx / 面单并 @物流小助手，后台自动：下载附件 → 聚类录单人 → 查 UPS/DHL 官网轨迹 → 命中节点推群并私聊录单人。
  Use when the user mentions 物流追踪、预报、面单、物流跟踪、物流进度、顺丰单号、国际单号、UPS、DHL、录单人、物流追踪群,
  or attaches a forecast workbook / shipping-label screenshots.
---

# 物流小助手

无 GUI、无 Chrome 插件、无可见窗口：全部后台执行（Agent 工具调用 + python 子进程）。不查 17TRACK。
核心是「读到官网状态词」再通知；查不到不算完成。

## 组件（同目录）

- `tracking-pipeline.py` — 本地管线：预报解析、录单人匹配、台账、轨迹落台、通知组装。台账 `data/shipments.json`；录单人映射 `data/sales_map.json`（自动回填）。
- `ups_track.py` — **UPS 官网抓取通道（已验证 12/12）**：`track_ups("<1Z…>")` 返回 `{ok, stage, status_en, detail, milestones}`。原理：`patchright` 隐身浏览器（有头 + `--window-position=4000,4000`）打开 UPS track 页，拦截 `/track/api/Track/GetStatus` JSON → 状态词。
- `dhl_track.py` — **DHL 官网抓取通道（已验证 5/5）**：`track_dhl("<10位单号>")` 返回 `{ok, stage, status_en, detail}`。流程：打开 `tracking.html` → 点掉 Cookie 横幅 → 填 `input[name=tracking-id]` 回车 → 拦截 `https://www.dhl.com/utapi?trackingNumber=…` JSON（首次 428 安全挑战由页面 JS 自动通过后重试 200）→ `shipments[0].status`。**10 位纯数字单号基本是 DHL**。
- `track_all_ups.py` — 双承运商批量：台账里有国际单号的订单，`1Z` 开头走 UPS、其他走 DHL → `data/ups_results.json`。FedEx 经测返回「查无此单」（页面能开但该单号不在 FedEx 系统）。
- `logi-watcher.py` — 群监听长驻：轮询群消息 → xlsx 走管线 / 图片入 inbox / 文字 `XSD==1Z` 自动配对；有新料后台拉起 `auto-track.py`。
- `auto-track.py` — 自动闭环编排：`track_all_ups.py` → `wire_results.py` → `notify` → `send_dms.py`；`--skip-track` 跳过抓取。
- `track_all_ups.py` — 批量抓 `data/sales_map.json` 里所有 `1Z` 开头单号 → `data/ups_results.json`。
- `track_retry.py` — 只重抓 `ups_results.json` 里 `ok:false` 的单（漏抓/超时补抓），单号取台账，`1Z` 走 UPS 其余走 DHL。
- `send_dms.py` — 只补发私聊（`notify` 已发过的会跳过，靠 `dm_notified_status` 去重）。
- `wire_results.py` — 读 `ups_results.json` 自动 ingest 缺失订单 + track-update 回填台账。

## 官网通道要点（为什么这个能过）

UPS/DHL 封的是 **TLS 指纹**，不是 IP。两个关键依赖（本机已装）：
- `patchright`：打了反检测补丁的 Playwright，有头模式（窗口移到屏幕外）能过 UPS 的 Akamai 指纹检测；拦截内部 `GetStatus` XHR 拿结构化 JSON。
- `curl_cffi`：Chrome TLS 指纹 HTTP 客户端，`cr.get(url, impersonate="chrome131")` 直连 UPS 官网返回 200（页面骨架），但内部 GetStatus API 仍 403，所以**轨迹必须走 patchright**，curl_cffi 只用于探测连通性。

UPS 状态映射（ups_track.py 内置）：`D→签收 I→运输中 P/M→已出国际单 X→海关扣关 O→运输中`；`packageStatusType` 缺失时按 `packageStatus` 文本兜底。

## 关于机器人 agent_enabled（重要）

`im +bots` 返回的 `agent_enabled` **无法由用户开启**：CLI 的 `im +bot-update` 只接受 name/description/avatar_url/webhook_url/is_public，服务端忽略 `agent_enabled`；桌面端源码里也没有该字段。**不要在这上面浪费时间，本技能不依赖它** —— watcher 直接轮询群消息，与机器人是否托管 Agent 无关。

机器人身份：`vbot_EIBezUGncpO8v0QJ`（物流小助手），已加入物流追踪群；仅用于 `+bot-send-user` 私聊署名。群消息用 `im +send` 以本人身份发。

## 触发流（全自动，无需 @）

1. 长驻：`python logi-watcher.py --channel-id <群> --bot-app-id vbot_EIBezUGncpO8v0QJ --interval 30`。
2. 下载附件：`vertu-cli im +history --channel-id <群> --limit 20 --no-json` → `+attachment-download --url <u> --output tmp/<name>`。
3. 预报 xlsx：`tracking-pipeline.py ingest-forecast --file <路径>` → 聚类国内顺丰单 / 承运商 / 收件方 / 品名。
4. **文字配对（优先，无需视觉）**：正文 `XSD…==1Z…` 由 watcher 正则自动提取 → `ingest-pair`。支持 `==` / `=` / `｜` / `|` / 空格分隔，支持多行多单；一行内订单号与单号数相等时顺序配对。
5. 面单图（**自动 OCR，已上线**）：watcher 下载后调 `ocr_label.py` → Qwen 多模态接口（`OCR_BASE_URL/OCR_API_KEY/OCR_MODEL` 在 .env）→ 提取 `XSD==1Z` 配对自动 `ingest-pair`。OCR 失败或没配对才落 inbox 待人工。
6. 录单人（管线自动）：`vertu-cli sales +orders --order-no <XSD> --period this_year --limit 1 --no-json` → 写入 `data/sales_map.json`。
   **CKD 出库单号按订单号查不到**（销售系统无此单）→ `match_sales(order_no, domestic)` 自动用国内顺丰号走 `--logistics-no` 反查真实销售订单号与录单人，并把真实单号记进 `sales_order` / `note`。已内置，无需手工干预。
7. **自动闭环**：watcher 发现新料即后台拉起 `auto-track.py`（抓官网 → 回填台账 → 群通知 → 私聊）。设 `LOGI_NO_AUTOTRACK=1` 可禁用自动拉起。
   **身份规则（用户明确要求）**：所有群消息走 `+agent-notify --target im --agent-slug logistics-track`（专家 bot 身份），私聊走 `+bot-send-user`（物流小助手身份）。`im +send` 只会以付汪阳个人账号发，禁止用于自动通知。管线（tracking-pipeline.py notify / logi-watcher.py ack）已改好。
8. 官网轨迹（UPS 首选）：单票 `python -c "from ups_track import track_ups; import json; print(json.dumps(track_ups('<1Z…>'), ensure_ascii=False))"`；批量 `python track_all_ups.py`（约 30s/单）。
   DHL：同思路用 patchright 拦截其内部 track API（待有真实 DHL 单号时按 ups_track.py 模板实现 `dhl_track.py`）。
9. 落台：`tracking-pipeline.py track-update --order <XSD> --status <阶段> --detail <官网原文摘录>`。
10. 通知：`notify --channel-id <群> --bot-app-id vbot_EIBezUGncpO8v0QJ`（推群 + 私聊，每节点一次）。

## 云端部署（已上线）

服务跑在 `tcp://10.100.0.176:2375`（Docker API，无认证裸连——网段内即 root，**务必尽快加 TLS/防火墙**）的容器 `logistics-track` 里，`--restart unless-stopped`。

- 镜像：`deploy/Dockerfile`（python:3.12-slim via daocloud 镜像站 + xray-core via ghfast.top 镜像 + Chromium；apt/pip 走清华源）
- 入口：`deploy/entrypoint.sh` = Xvfb :99 → xray(SS 出口，主节点 `XRAY_*`、备用节点 `XRAY2_*` 可选，看门狗 `proxy-watchdog.py` 主挂自动切备) → watcher + 定时巡检/对账/组织刷新
  线上已配 s1=`c57s1` / s2=`c57s2`（同端口同密码，`c57s3` 也可解析），切换双向实测通过；UPS 经 s2 抓取正常
- 存活监督：bash 留作 PID 1，`watcher_state.json` 超过 `WATCHER_STALE_MIN`(10) 分钟未更新即杀 watcher 退出，靠 `--restart unless-stopped` 拉起（`kill 1` 对 python 无效，已踩坑）
- 凭据：`deploy/.env`（模板 `deploy/.env.example` 列全了所有变量；`vertu-cli agent env` 生成认证项，**值带单引号要剥掉**；只在本机，别提交）
- 数据卷：`logistics-data:/app/data`；空卷由镜像内 `/app/seed` 自动初始化
- 容器内环境：`UPS_PROXY=socks5://127.0.0.1:10809` + `UPS_DISABLE_HTTP2=1`（**关键**：Linux 容器经 SS 代理时 Akamai 杀 HTTP2，禁用后 8/8 通过；本地 Windows 却要 HTTP2，故做成环境变量开关）
- 运维：`docker -H tcp://10.100.0.176:2375 logs -f logistics-track`；重建 = 改代码后 `docker build -f deploy/Dockerfile -t logistics-track:latest . && docker rm -f logistics-track && docker run ...`（compose 文件 `deploy/docker-compose.yml` 备用）
- 内存红线：宿主机曾 3.7GB 且跑生产容器，浏览器测试容器 shm 2GB 曾把它打到 OOM（TCP 通、服务全不响应）。现容器 `--memory=1536m --shm-size=1g`，测试容器务必 shm 1g。

## 人员解析（user_id）

`resolve_user` 三级兜底：`data/users_map.json` 缓存 → `data/org_people.json` 组织树全量快照 → 实时 `im +users --query`。
**`im +users --query` 姓名索引时常返回空**（连已缓存的姓名也查不到），必须靠 `org_people.json` 兜底。
该快照由爬组织树生成：`im +departments --max-depth 0` 取纯文本树（按缩进解析路径，含 `staff a/b`，只取 b>0 的部门）→ 逐部门 `im +users --department-path <路径>` → 汇总姓名→user_id。当前 433 人。
HR 的 `+personnel-info` 与 `+profile` scope 为 `self`，**查不了别人**，不要走这条路。

## 成功判定与降级

成功 = patchright 拦到 GetStatus JSON 且 stage 命中：已出国际单 / 运输中 / 清关中 / 签收 / 海关扣关 / 退回。
失败 = 拦不到 GetStatus → 内置自动 reload 重试一次 → 仍失败用 `track_retry.py` 补抓；禁止以「官网打不开」交差。
单票失败不影响整批（track_all_ups.py 每单独立 try/except 落盘）。官网只走承运商路由，专线/港车只记单号。

## 私聊推送能力（沉淀）

**三种发送身份，选错就是事故：**

| 命令 | 身份 | 用途 |
|---|---|---|
| `im +bot-send-user --app-id vbot_EIBezUGncpO8v0QJ --user-id <id> --body <单行>` | 物流小助手**机器人** | ✅ 私聊录单人/告警（唯一正确方式） |
| `im +agent-notify --target im --agent-slug logistics-track --agent-name 物流小助手 --bot-name 物流小助手 --channel-id <群>` | 专家 bot | ✅ 群消息（`--user-id <id>` 替代 `--channel-id` 可发私聊） |
| `im +send` / `mail +send` | **付汪阳个人账号** | ❌ 禁止用于自动化，用户明令 |

**user_id 解析**：`data/users_map.json` 缓存 → `data/org_people.json` 全量快照（`im +departments` 树 + 逐部门 `im +users --department-path`，每周日容器内自动刷新）→ 实时 `im +users --query`（索引常空，不可靠）。已知 uid：冯磊 12545、李马特 12708、王逍 13292、刘春梅 12564、李晓悦 14353。

**要点**：
- `+bot-send-user` 正文**必须单行**（换行被截断）
- 子单（-1/-2）**继承父单信息**（录单人/产品/顺丰号），不再报"未匹配"
- OCR 订单号漏前缀（TH/DL/TW）时按**数字尾段**自动匹配台账归位
- `--dry-run` 在部分命令上会真执行（bot-update 踩过坑）

## 通知格式

群（**专家身份，禁止用 im +send 发个人账号消息**）：
`vertu-cli im +agent-notify --target im --agent-slug logistics-track --channel-id <群> --body "<内容>"`
消息以 bot 身份出现（sender_type=bot / agent_slug=logistics-track），不是付汪阳个人账号。
格式：`【物流小助手】<XSD> <旧态>→<新态>｜国际单 <1Z…>｜顺丰 <SF…>｜<产品>｜录单人 <姓名>`
私聊录单人（**正文必须单行**，换行会被截断）：`im +bot-send-user --app-id vbot_EIBezUGncpO8v0QJ --user-id <id> --body "<一行>"`。
姓名必须全等唯一定人（见上节人员解析）。

## 健壮性架构（robust.py）

- **原子写**：台账/缓存 JSON 一律临时文件+`os.replace`，进程中途死不会写坏
- **损坏兜底**：读 JSON 失败自动回退 `.bak`（每次保存前自动备份旧文件）
- **台账互斥锁**：`FileLock(data/.ledger.lock)`——watcher 入库与 auto-track 回填/通知并发时互斥，pid 校验防死锁
- **防注入**：容器内子进程用参数列表（无 shell），Windows 才回退 shell 引号
- **洪泛控制**：群通知 >3 条自动合并为一条消息
- **游标安全**：附件下载失败不推进游标（下轮重试不丢件）；机器人自己的消息跳过（防自循环）
- **OCR 上限**：同一图重试 >6 次标记放弃，不再无限重试
- **崩溃接管**：脏标记+接管循环，容器重启/杀进程后未完成的任务由存活进程补跑

## 规则

- 预报主键顺丰单号；斜杠脏行丢弃；品名向下填充。
- 面单 Invoice `XSD*` 对订单号；一对多/查无 → 待人工，不硬绑。
- 生命周期只向前：已预报 → 已出国际单 → 运输中 → 清关中 → 签收/退回；海关扣关是异常不是签收。
- 台账唯一 `data/shipments.json`；`notify` 只在状态前进时发（`needs_notify`），`send_dms.py` 靠 `dm_notified_status` 去重。
- **后台脚本不要包装 `sys.stdout`**（管道会关，报 `I/O operation on closed file`），且只 print ASCII + 落盘 JSON；中文输出靠读文件拿。
- 改 UTF-8 文件**不要用 `Set-Content`**（PowerShell 5.1 按 GBK 读入会造成双重编码并吞换行），用 write/edit 工具或 python 写。
- **开机自启**：`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\物流小助手-Watcher.vbs`（用 `pythonw.exe` 跑 logi-watcher.py，无窗口）。`schtasks` 因权限被拒，改用 Startup。`run-watcher.bat` 为带重启循环的手动启动备选。watcher 只在 Agent 会话内存活，会话结束即停——靠这个 vbs 保证长期运行。
