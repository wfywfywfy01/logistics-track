# 物流小助手 · 查询接口文档

> 版本 v1 · 2026-09-18 · 分支 `feature/track-api`(基于 master `d52389b`)
> 提供方:物流追踪系统(云容器 `logistics-track`)
> 数据源:UPS / DHL / FedEx 官网快照 + 本地 SQLite 台账

---

## 1. 概览

| 项 | 值 |
|---|---|
| 服务地址 | `http://127.0.0.1:18080`(只绑**云服务器本机**,不对外暴露) |
| 协议 | HTTP/1.1,UTF-8 JSON |
| 鉴权 | HTTP Basic(推荐)或登录会话 Cookie |
| 权限 | 全部只读,不改台账、不触发抓取 |
| 数据新鲜度 | 官网轨迹每天 09:05(全量)、15:05(增量)刷新;新面单入群后约 1 分钟内刷新 |

### 1.1 怎么访问

```bash
# 本机执行:把服务器的 18080 映射到本地
ssh -L 18080:127.0.0.1:18080 root@10.100.0.176
```

然后访问 `http://127.0.0.1:18080`。系统间对接(OA/BI)需要运维按白名单放行该端口或走内网网关。

---

## 2. 鉴权

| 方式 | 用法 | 场景 |
|---|---|---|
| Basic + 管理令牌 | `Authorization: Basic base64("admin:<ADMIN_TOKEN>")` | 服务间调用(推荐) |
| Basic + 员工账号 | `Authorization: Basic base64("<用户名>:<密码>")` | 给同事开的只读账号 |
| 会话 Cookie | 先 `POST /login` 拿 `logistics_session` | 浏览器 |

- 只读接口需要 `viewer` 及以上角色;账号在后台 `/users` 页面创建。
- 未带凭证访问 `/api/*` 返回 **401** + `WWW-Authenticate: Basic`。

---

## 3. 通用约定

### 3.1 状态枚举

| 状态 | 含义 |
|---|---|
| `已预报` | 销售已录单,尚未出国际单 |
| `已出国际单` | 官网显示已揽收/已生成面单 |
| `运输中` | 在途(到达、离港、派送中) |
| `清关中` | 清关处理中 |
| `海关扣关` | 被海关扣留/查验 |
| `部分签收` | 多包裹订单,部分已签收 |
| `签收` | 全部签收(终态) |
| `退回` | 退回发件人(终态) |
| `异常` | 官网异常、地址问题、长时间无更新 |

终态不回退;官网返回无法识别的状态时**不写入台账**,进人工复核队列。

### 3.2 官网事件结构 `latest_event` / `events[]`

```json
{
  "source_time_text": "09/11/2026 14:20",
  "occurred_at_utc": "2026-09-11T06:20:00Z",
  "timezone_offset": "-08:00",
  "location": "ANCHORAGE, AK, US",
  "status": "Arrived at facility",
  "description": "",
  "additional_description": "",
  "code": "",
  "exception_code": "",
  "is_brokerage": false
}
```

- `source_time_text` = 官网原始时间文本(展示用,含承运商当地时间)
- `occurred_at_utc` = 归一化 UTC 时间(排序/对账用),无法换算时为 `"N/A"`
- `events` 为官网返回顺序(一般最新在前),`latest_event` 即最新一条
- 事件条数上限 1000;API 额外给出 `event_count`

### 3.3 承运商代码

`UPS`(1Z 开头 18 位)、`DHL`(10 位)、`FEDEX`(12/15/20/22 位)、`N/A`

### 3.4 错误格式

```json
{ "ok": false, "error": "order not found" }
```

| HTTP | 含义 |
|---|---|
| 200 | 成功 |
| 400 | 参数错误 |
| 401 | 未鉴权 |
| 403 | 无权限 |
| 404 | 订单/单号不存在 |
| 503 | 数据库不可用(/healthz) |

---

## 4. 接口清单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查(免鉴权) |
| GET | `/api/track/{order}` | **按订单号查一票**(含全部包裹与官网轨迹) |
| GET | `/api/track?tracking=<单号>` | **按国际单号反查订单** |
| GET | `/api/shipments` | 台账列表(筛选/分页),批量拉取用 |
| GET | `/api/stats` | 统计(状态分布、缺国际单、有轨迹数) |
| GET | `/api/orders`、`/api/orders/{order}` | 后台订单列表/详情(**含操作与审计**,对接请用上面 4 个) |

---

## 5. GET /api/track/{order} — 查一票

```bash
curl -u admin:<ADMIN_TOKEN> "http://127.0.0.1:18080/api/track/XSD260901141252"
```

**响应 200**

```json
{
  "ok": true,
  "shipment": {
    "order": "XSD260901141252",
    "status": "运输中",
    "salesperson": "周佳丽",
    "salesperson_id": 12545,
    "carrier": "UPS",
    "intl": "1ZC23W53D441751825",
    "alt_intl": "",
    "domestic": "SF5152606658887",
    "products": ["VERTU PHANTOM-深咖色基础款"],
    "status_observed_at": "2026-09-11T06:20:00+00:00",
    "latest_event": {
      "source_time_text": "09/11/2026 14:20",
      "occurred_at_utc": "2026-09-11T06:20:00Z",
      "location": "ANCHORAGE, AK, US",
      "status": "Arrived at facility",
      "description": ""
    },
    "latest_event_text": "09/11/2026 14:20 ANCHORAGE, AK, US Arrived at facility",
    "packages": [
      {
        "tracking": "1ZC23W53D441751825",
        "carrier": "UPS",
        "role": "primary",
        "status": "运输中",
        "binding_version": 1,
        "status_observed_at": "2026-09-11T06:20:00+00:00",
        "official": {
          "source": "ups.com",
          "observed_at": "2026-09-11T06:20:00+00:00",
          "status_en": "In Transit",
          "progress": "60",
          "estimated_delivery": { "local_date_text": "2026-09-13", "local_time_text": "…" },
          "latest_event": { "...": "同上" },
          "events": [ { "...": "官网事件,最新在前" } ],
          "event_count": 12,
          "progress_steps": [
            { "name": "Label Created", "source_time_text": "09/10/2026 09:00",
              "location": "SHANGHAI, CN", "completed": true, "current": false, "future": false }
          ]
        }
      }
    ],
    "history": [
      { "from": "已预报", "to": "运输中", "at": "2026-09-11T06:20:00+00:00",
        "observed_at": "2026-09-11T06:20:00+00:00", "tracking": "1ZC23W53D441751825" }
    ]
  }
}
```

**字段说明**

| 字段 | 说明 |
|---|---|
| `status` | 订单聚合状态(多包裹按"部分签收/异常优先"汇总) |
| `latest_event` | 全订单最新官网事件(取第一个有快照的包裹) |
| `latest_event_text` | 最新事件一行文本,可直接展示/推送 |
| `packages[].official.events` | 该包裹**完整官网轨迹**(最新在前) |
| `packages[].official.event_count` | 轨迹条数;本次升级前入库的历史订单可能为 0 |
| `packages[].official.estimated_delivery` | 官网预计到达(有则给) |
| `packages[].official.progress_steps` | 官网进度步骤(已揽收/运输中/派送中…) |
| `packages[].binding_version` | 换单版本号,换一次 +1(对账追溯) |
| `history` | 状态变化历史(阶段跳变,非全量轨迹) |

**404**:`{"ok": false, "error": "order not found"}`

---

## 6. GET /api/track?tracking= — 按国际单号反查

```bash
curl -u admin:<ADMIN_TOKEN> "http://127.0.0.1:18080/api/track?tracking=1ZC23W53D441751825"
```

```json
{
  "ok": true,
  "tracking": "1ZC23W53D441751825",
  "count": 1,
  "matches": [
    { "order": "XSD260901141252", "status": "运输中", "salesperson": "周佳丽",
      "package": { "tracking": "1ZC23W53D441751825", "carrier": "UPS",
                   "official": { "events": ["..."], "event_count": 12 } } }
  ]
}
```

- 一个单号可能对应多张订单 → `matches` 全量返回
- 查不到返回 **404**:`{"ok": false, "tracking": "...", "count": 0, "matches": [], "error": "tracking number not found"}`
- 也支持 `?order=XSD...`(等价路径写法)

---

## 7. GET /api/shipments — 台账列表

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `q` | string | 空 | 模糊搜索:订单号/国际单号/顺丰单号/录单人 |
| `status` | string | 空 | 精确状态(URL 编码,如 `?status=%E7%AD%BE%E6%94%B6`) |
| `limit` | int | 200 | 1–1000 |
| `offset` | int | 0 | 偏移量 |

```bash
curl -u admin:<ADMIN_TOKEN> "http://127.0.0.1:18080/api/shipments?status=%E7%AD%BE%E6%94%B6&limit=100"
```

```json
{
  "ok": true, "total": 144, "count": 100, "offset": 0, "limit": 100,
  "items": [
    { "order": "XSD260901141252", "status": "运输中", "carrier": "UPS",
      "intl": "1ZC23W53D441751825", "alt_intl": "", "domestic": "SF5152606658887",
      "salesperson": "周佳丽", "product": "VERTU PHANTOM-深咖色基础款",
      "latest_event": { "...": "最新官网事件" },
      "latest_event_text": "09/11/2026 14:20 ANCHORAGE, AK, US Arrived at facility",
      "event_count": 12,
      "status_observed_at": "2026-09-11T06:20:00+00:00" }
  ]
}
```

- `total` 为过滤后总数,`count` 为本页条数
- 列表不含完整 `events`(避免响应过大),要轨迹用单票接口

---

## 8. GET /api/stats — 统计

```json
{
  "ok": true,
  "total": 144,
  "by_status": { "签收": 18, "运输中": 60, "已预报": 57, "异常": 3, "退回": 1 },
  "missing_intl": 97,
  "with_official_events": 41
}
```

| 字段 | 说明 |
|---|---|
| `by_status` | 各状态订单数(键为中文状态) |
| `missing_intl` | 还没有国际单号的订单数(缺面单) |
| `with_official_events` | 已有官网轨迹快照的订单数(覆盖率) |

---

## 9. GET /healthz — 健康检查(免鉴权)

```bash
curl "http://127.0.0.1:18080/healthz"
```

```json
{ "ok": true, "database": "ok" }
```

数据库异常返回 **503** 且 `"database": "error"`。

---

## 10. 调用示例

### PowerShell

```powershell
$pair = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("admin:<ADMIN_TOKEN>"))
$h = @{ Authorization = "Basic $pair" }

# 查一票
$r = Invoke-RestMethod -Headers $h -Uri "http://127.0.0.1:18080/api/track/XSD260901141252"
$r.shipment.status
$r.shipment.latest_event_text

# 只要轨迹
$r.shipment.packages[0].official.events | Select-Object source_time_text, location, status
```

### Python

```python
import requests

BASE, AUTH = "http://127.0.0.1:18080", ("admin", "<ADMIN_TOKEN>")

data = requests.get(f"{BASE}/api/track/XSD260901141252", auth=AUTH, timeout=10).json()
shipment = data["shipment"]
print(shipment["status"], "|", shipment["latest_event_text"])
for event in shipment["packages"][0]["official"]["events"]:
    print(event["occurred_at_utc"], event["location"], event["status"])
```

### 从群消息跳到接口

群通知形如:

> 【物流小助手】XSD260901141252 运输中→签收｜09/11/2026 14:20 ANCHORAGE, AK, US Delivered｜国际单 1ZC23W53D441751825｜顺丰 SF5152606658887｜VERTU PHANTOM｜录单人 周佳丽

取第二段的订单号,直接 `GET /api/track/{订单号}` 拿完整轨迹。

---

## 11. 注意事项

1. **只读**:接口不触发抓取;要立刻刷新某票,走后台 `/orders/{订单}` 或等下一个巡检点。
2. **轨迹为空属正常**:升级前入库的历史订单在下一次巡检后才有 `official.events`(`event_count: 0`)。
3. **状态可回溯官网**:推送与接口里的节点均来自官网字段(`description`/`status` 原文)。
4. **不要高频轮询**:数据 6 小时刷新一次,建议 30 分钟以上间隔;列表一次最多 1000 条。
5. **端口不对外**:当前仅绑服务器回环地址,外部访问走 SSH 隧道或内网网关。

---

## 12. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-09-18 | v1 | 新增 `/api/track`、`/api/shipments`、`/api/stats`(只读,基于 `official_tracking` 官网快照);群通知与私聊带上最新官网节点;云表格新增"轨迹节点数"列,最新节点改为官网事件文本 |
