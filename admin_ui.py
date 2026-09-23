"""HTML views for the logistics operations console.

The UI deliberately renders only data already available from the logistics ledger.
"""
import html
import json
from urllib.parse import quote, urlencode


def h(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def display(value, missing="暂无数据"):
    return h(value) if value not in (None, "", "N/A") else f"<span class='faint'>{h(missing)}</span>"


def badge(value, tone="idle"):
    return f"<span class='badge badge--{tone}'>{h(value)}</span>"


def page(title, subtitle="", actions=""):
    return (f"<header class='page-head'><div><h1 class='h1'>{h(title)}</h1>"
            f"<p class='sub page-head__sub'>{h(subtitle)}</p></div>"
            f"<div class='page-head__actions'>{actions}</div></header>")


def card(title, content, extra=""):
    return (f"<section class='card card--pad {extra}'><h2 class='h2 mb-4'>{h(title)}</h2>"
            f"{content}</section>")


def empty(message):
    return f"<div class='state'><div class='state__title'>{h(message)}</div></div>"


def stat(label, value, href, detail=""):
    return (f"<a class='stat' href='{h(href)}'><span class='stat__label'>{h(label)}</span>"
            f"<strong class='stat__value'>{h(value)}</strong>"
            f"<span class='stat__foot'>{h(detail)} <span class='stat__drill'>查看</span></span></a>")


def shell(body, principal, csrf, section, issue_count=0, notification_count=0):
    role = principal["role"]
    links = [("orders", "/orders", "订单列表", ""),
             ("tasks", "/tasks", "异常待办", issue_count),
             ("notifications", "/notifications", "通知中心", notification_count),
             ("reports", "/reports/daily", "运营日报", "")]
    if role == "admin":
        links.append(("users", "/users", "权限管理", ""))
    navigation = "".join(
        f"<a class='sidenav__item {'sidenav__item--active' if section == key else ''}' "
        f"href='{url}' {'aria-current=page' if section == key else ''}>"
        f"<span>{label}</span>"
        f"{f'<span class=sidenav__badge>{count}</span>' if count else ''}</a>"
        for key, url, label, count in links)
    readonly = ("<div class='readonly-bar'>当前账号为只读角色；可查看，不能修改订单或任务。</div>"
                if role == "viewer" else "")
    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<meta name=csrf content='{h(csrf)}'><title>{h(section)} · 物流运营后台</title>"
            "<link rel='stylesheet' href='/admin.css'></head><body>"
            "<a class='skip-link' href='#main'>跳到主内容</a>"
            "<header class='topbar'><button class='btn btn--ghost mobile-menu' type='button' "
            "id='nav-toggle' aria-label='打开导航' aria-expanded='false'>☰</button>"
            "<div class='topbar__brand'><span class='topbar__mark' aria-hidden='true'>物</span>"
            "<span class='topbar__title'>物流运营后台</span></div>"
            f"<div class='topbar__spacer'></div><span class='who'>当前：{h(principal['username'])}（{h(role)}）</span>"
            f"<form class='logout' method='post' action='/logout'><input type='hidden' name='csrf' value='{h(csrf)}'>"
            "<button class='btn btn--ghost btn--sm' type='submit'>退出</button></form></header>"
            f"{readonly}<div class='shell'><nav class='sidenav' id='admin-nav' aria-label='主导航'>"
            "<div class='sidenav__group'><div class='sidenav__label'>运营</div>"
            f"{navigation}</div></nav><main class='main' id='main'><div class='page'>"
            "<div class='action-feedback' role='status' aria-live='polite' hidden></div>"
            f"{body}</div></main></div><script src='/admin.js' defer></script></body></html>")


def _state(view):
    packages = view.get("packages") or []
    attempts = [p.get("last_tracking_attempt") for p in packages if p.get("last_tracking_attempt")]
    if any(not a.get("ok") for a in attempts):
        return "抓取失败", "bad"
    if any(p.get("tracking_data_stale") is True for p in packages):
        return "数据过期", "warn"
    if any((p.get("official") or {}).get("events") for p in packages):
        return "官网已更新", "ok"
    return "暂无官网数据", "missing"


def orders(views, counts, query="", status="", carrier="", page_number=1, per_page=25):
    all_statuses = sorted(counts.get("by_status", {}))
    filtered = [v for v in views if (not carrier or any(
        str(p.get("carrier") or "").upper() == carrier for p in v.get("packages") or []))]
    total = len(filtered)
    page_number = min(max(1, page_number), max(1, (total + per_page - 1) // per_page))
    shown = filtered[(page_number - 1) * per_page:page_number * per_page]
    body = page("订单列表", "按订单或运单找问题，逐个包裹核对官网状态。")
    body += "<div class='stat-grid mb-5'>" + "".join((
        stat("订单总数", counts.get("total", 0), "/orders"),
        stat("运输中", counts.get("by_status", {}).get("运输中", 0), "/orders?status=" + quote("运输中")),
        stat("已签收", counts.get("by_status", {}).get("签收", 0), "/orders?status=" + quote("签收")),
        stat("缺国际单", counts.get("missing_intl", 0), "/reports/daily"))) + "</div>"
    body += ("<form class='toolbar' method='get' action='/orders'>"
             f"<label>搜索订单、运单或录单人<input class='input' name='q' value='{h(query)}' "
             "placeholder='订单号 / 运单号 / 人员'></label>"
             "<label>状态<select class='select' name='status'><option value=''>全部</option>" +
             "".join(f"<option value='{h(x)}' {'selected' if x == status else ''}>{h(x)}</option>"
                     for x in all_statuses) + "</select></label>"
             "<label>承运商<select class='select' name='carrier'><option value=''>全部</option>" +
             "".join(f"<option {'selected' if x == carrier else ''}>{x}</option>"
                     for x in ("UPS", "DHL", "FEDEX")) +
             "</select></label><button class='btn btn--primary'>筛选</button></form>")
    if not shown:
        return body + empty("没有符合条件的订单")
    body += ("<div class='card table-scroll'><table class='grid'><thead><tr><th>订单</th><th>包裹</th>"
             "<th>承运商</th><th>状态</th><th>官网数据</th><th>最新节点</th></tr></thead><tbody>")
    for view in shown:
        order = str(view.get("order") or "")
        label, tone = _state(view)
        packages = view.get("packages") or []
        carriers = ", ".join(dict.fromkeys(str(p.get("carrier") or "N/A") for p in packages)) or "N/A"
        latest = view.get("latest_event_text") or "暂无官网节点"
        body += (f"<tr data-tone='{tone}'><td data-m='order'><a class='strong mono' href='/orders/{quote(order, safe='')}'>{h(order)}</a>"
                 f"<div class='muted'>{display(view.get('salesperson'))}</div></td>"
                 f"<td data-m='inline' data-label='包裹'>{len(packages)}</td>"
                 f"<td data-m='label' data-label='承运商'>{h(carriers)}</td>"
                 f"<td data-m='label' data-label='状态'>{badge(view.get('status') or 'N/A')}</td>"
                 f"<td data-m='label' data-label='官网数据'>{badge(label, tone)}</td>"
                 f"<td data-m='label' data-label='最新节点'>{h(latest)}</td></tr>")
    body += "</tbody></table></div>"
    base = {"q": query, "status": status, "carrier": carrier}
    pager = []
    for number in range(max(1, page_number - 2), min((total + per_page - 1) // per_page, page_number + 2) + 1):
        pager.append(f"<a class='pager__btn' href='/orders?{h(urlencode({**base, 'page': number}))}' "
                     f"{'aria-current=page' if number == page_number else ''}>{number}</a>")
    body += f"<div class='table-foot'>共 {total} 单 · 第 {page_number} 页<div class='pager'>{''.join(pager)}</div></div>"
    return body


def _package(package, number):
    official = package.get("official") or {}
    attempt = package.get("last_tracking_attempt") or {}
    label, tone = _state({"packages": [package]})
    latest = official.get("latest_event") or {}
    eta = official.get("estimated_delivery")
    if isinstance(eta, dict):
        eta_text = " ".join(str(eta[key]) for key in ("date", "time", "timezone", "text")
                            if eta.get(key)) or json.dumps(eta, ensure_ascii=False)
    else:
        eta_text = json.dumps(eta, ensure_ascii=False) if isinstance(eta, list) else eta
    events = official.get("events") or []
    body = (f"<section class='pkg'><header class='pkg__head'><span class='pkg__index'>{number}</span>"
            f"<strong class='pkg__no'>{display(package.get('tracking'), '无运单号')}</strong>"
            f"<span>{h(package.get('carrier') or '承运商未知')}</span>"
            f"<div class='pkg__head-right'>{badge(label, tone)}</div></header>"
            "<div class='pkg__body'><div class='dl dl--3'>"
            f"<div><span class='label'>当前状态</span><div>{display(latest.get('status') or official.get('status_en') or package.get('status'), '状态未知')}</div></div>"
            f"<div><span class='label'>预计送达</span><div>{display(eta_text)}</div></div>"
            f"<div><span class='label'>最后位置</span><div>{display(latest.get('location'))}</div></div>"
            f"<div><span class='label'>官网来源</span><div>{display(official.get('source'), '非官网数据')}</div></div>"
            f"<div><span class='label'>官网观察时间</span><div>{display(official.get('observed_at'), '从未抓取')}</div></div>"
            f"<div><span class='label'>包裹状态</span><div>{display(package.get('status'))}</div></div></div>")
    if attempt and not attempt.get("ok"):
        body += f"<div class='freshness-panel freshness-panel--failed'>抓取失败：{display(attempt.get('error'), '原因未记录')}</div>"
    if not events:
        body += "<div class='track-empty'><strong class='track-empty__title'>官网暂无轨迹节点</strong><span class='track-empty__desc'>无法据此判断包裹进度。</span></div>"
    else:
        body += "<h3 class='h3'>官网轨迹</h3><div class='table-scroll'><table class='grid'><thead><tr><th>时间</th><th>状态</th><th>地点</th><th>详情</th></tr></thead><tbody>"
        for event in events:
            when = event.get("occurred_at_utc") or event.get("source_time_text") or "N/A"
            body += (f"<tr><td data-m='label' data-label='时间'><time datetime='{h(when)}'>{h(when)}</time></td>"
                     f"<td data-m='label' data-label='状态'>{display(event.get('status'))}</td>"
                     f"<td data-m='label' data-label='地点'>{display(event.get('location'))}</td>"
                     f"<td data-m='label' data-label='详情'>{display(event.get('description'))}</td></tr>")
        body += "</tbody></table></div>"
    return body + "</div></section>"


def _action_form(title, endpoint, fields, button, confirm=""):
    return (f"<details class='action-panel'><summary>{h(title)}</summary>"
            f"<form class=api-form action='{h(endpoint)}'>{fields}"
            "<label>操作原因<input class='input' name='reason' required minlength='2'></label>"
            f"<button class='btn btn--primary' type='submit' data-confirm='{h(confirm)}'>{h(button)}</button>"
            "</form></details>")


def order_detail(shipment, view, evidence, audit, issues, role):
    order = str(shipment.get("orderNo") or "")
    endpoint = "/api/orders/" + quote(order, safe="")
    body = page(order, "按包裹核对官网轨迹、原件和处理记录。",
                "<a class='btn' href='/orders'>返回订单列表</a>")
    body += "<div class='detail-layout'><div class='detail-main'>"
    packages = view.get("packages") or []
    attention = []
    for index, package in enumerate(packages, 1):
        state, _ = _state({"packages": [package]})
        if state in ("抓取失败", "数据过期", "暂无官网数据"):
            attention.append(f"包裹 {index}（{h(package.get('carrier') or 'N/A')}）：{state}")
    delivered = sum(str(package.get("status") or "") == "签收" for package in packages)
    if delivered and delivered < len(packages):
        attention.append(f"{len(packages)} 个包裹中 {delivered} 个已签收，仍有包裹未签收")
    if attention:
        body += ("<section class='focus focus--bad'><h2 class='focus__title'>处理重点</h2>"
                 "<div class='focus__list'>" + "".join(
                     f"<div class='focus__item'>{line}</div>" for line in attention) + "</div></section>")
    body += ("<section class='overview'><div class='overview__top'>"
             f"<div><h2 class='overview__id'>{h(order)}</h2>"
             f"<div class='overview__meta'>{badge(shipment.get('status') or 'N/A')}"
             f"{badge(str(len(view.get('packages') or [])) + ' 个包裹', 'info')}</div></div></div>"
             "<div class='overview__body dl dl--3'>"
             f"<div><span class='label'>录单人</span><div>{display(shipment.get('salesperson'), '未匹配')}</div></div>"
             f"<div><span class='label'>国内单号</span><div>{display(shipment.get('domestic'))}</div></div>"
             f"<div><span class='label'>产品</span><div>{display(', '.join(map(str, shipment.get('products') or [])))}</div></div>"
             "</div></section>")
    if packages:
        body += "<h2 class='h2'>包裹与官网轨迹</h2>" + "".join(_package(p, i) for i, p in enumerate(packages, 1))
    else:
        body += card("包裹与官网轨迹", empty("尚无国际运单号"))
    documents = []
    for item in evidence.get("inbox") or []:
        payload = item.get("payload") or {}
        document = (f"<div class='doc'><div class='doc__body'><strong class='doc__name'>{h(item.get('id'))}</strong>"
                    f"<div class='doc__meta'>状态：{h(item.get('status'))} · {display(item.get('updated_at'))}</div></div>")
        if payload.get("path"):
            document += f"<a class='btn btn--sm' href='/evidence/{quote(str(item['id']), safe='')}' target='_blank' rel='noopener'>查看原件</a>"
        documents.append(document + "</div>")
    for item in evidence.get("messages") or []:
        documents.append(f"<div class='doc'>IM 消息 {h(item.get('message_id'))} · {h(item.get('status'))}</div>")
    body += card("原件与消息", "<div class='doc-list'>" + "".join(documents) + "</div>" if documents else empty("暂无原件或消息"))
    changes = [item for package in shipment.get("packages") or []
               for item in package.get("binding_history") or []]
    body += card("换单历史", "".join(
        f"<div class='waybill-change'><div class='waybill-change__flow'>{display(item.get('from'))} → {display(item.get('to'))}</div>"
        f"<span class='sub'>{display(item.get('at'))} · {display(item.get('reason'), '未记录原因')}</span></div>"
        for item in changes)
                 if changes else empty("暂无换单记录"))
    audit_rows = "".join(
        f"<div class='audit__row'><time class='audit__at'>{h(row.get('created_at'))}</time>"
        f"<span class='audit__by'>{h(row.get('operator'))}</span>"
        f"<span class='audit__what'>{h(row.get('action'))} · {h(row.get('reason'))}</span></div>"
        for row in audit)
    body += card("操作审计", "<div class='audit'>" + audit_rows + "</div>" if audit_rows else empty("暂无操作记录"))
    body += "</div><aside class='detail-side'>"
    linked = [item for item in issues if str((item.get("payload") or {}).get("order") or "") == order]
    issue_content = "".join(f"<a class='doc' href='/tasks?order={quote(order, safe='')}'>{h(item.get('kind'))} · {h(item.get('status'))}</a>" for item in linked)
    body += card("关联待办", issue_content or empty("暂无关联待办"))
    if role in ("admin", "operator"):
        actions = _action_form("补录人员", endpoint + "/salesperson",
            "<label>姓名<input class='input' name='salesperson' required></label>"
            "<label>用户 ID<input class='input' name='user_id' required></label>", "保存")
        actions += _action_form("增加包裹", endpoint + "/packages",
            "<label>国际运单号<input class='input' name='tracking' required></label>"
            "<label>承运商<select class='select' name='carrier'><option>UPS</option><option>DHL</option><option>FEDEX</option></select></label>",
            "增加包裹")
        actions += _action_form("更换运单", endpoint + "/replace-package",
            "<label>原国际单号<input class='input' name='tracking' required></label>"
            "<label>新国际单号<input class='input' name='new_tracking' required></label>",
            "确认换单", "确认更换运单？原运单会保留在历史中。")
        body += card("处理订单", actions)
    return body + "</aside></div>"


KIND_LABELS = {"review": "人工复核", "tracking_failure": "官网抓取失败",
               "tracking_stale": "数据过期", "stalled": "物流停滞", "ocr": "面单 OCR",
               "incoming_message": "消息处理", "notify_group": "群通知", "notify_dm": "私聊通知"}


def tasks(rows, role, filters, page_number=1, per_page=25):
    kind, status, order = (filters.get(k, "").strip() for k in ("kind", "status", "order"))
    rows = [r for r in rows if (not kind or r.get("kind") == kind) and
            (not status or r.get("status") == status) and
            (not order or str((r.get("payload") or {}).get("order") or "") == order)]
    total = len(rows)
    page_number = min(max(1, page_number), max(1, (total + per_page - 1) // per_page))
    shown = rows[(page_number - 1) * per_page:page_number * per_page]
    body = page("异常待办", "先核实依据，再认领、重试或结案；操作原因会进入审计。")
    body += "<form class='toolbar' action='/tasks' method='get'>"
    body += f"<label>订单<input class='input' name='order' value='{h(order)}'></label>"
    body += "<label>类型<select class='select' name='kind'><option value=''>全部</option>" + "".join(
        f"<option value='{h(k)}' {'selected' if k == kind else ''}>{h(v)}</option>" for k, v in KIND_LABELS.items()) + "</select></label>"
    body += "<label>状态<select class='select' name='status'><option value=''>全部</option>" + "".join(
        f"<option {'selected' if k == status else ''}>{k}</option>" for k in ("pending", "retry", "dead", "unknown")) + "</select></label>"
    body += "<button class='btn btn--primary'>筛选</button></form>"
    body += f"<p class='sub mb-4'>符合条件：{total} 条</p>"
    if not shown:
        return body + empty("当前筛选没有待办")
    body += "<div class='todo-list'>"
    for item in shown:
        payload = item.get("payload") or {}
        order_no = str(payload.get("order") or "")
        kind = str(item.get("kind") or "")
        status_value = str(item.get("status") or "")
        tone = "warn" if status_value == "unknown" else "bad" if status_value == "dead" else "info"
        body += (f"<article class='todo' data-tone='{tone}'><header class='todo__head'>"
                 f"<strong class='todo__title'>{h(KIND_LABELS.get(kind, kind))}</strong>"
                 f"<span class='todo__num'>#{h(item.get('source_id') or item.get('id'))}</span>"
                 f"<span class='todo__head-right'>{badge(status_value, tone)}</span></header>"
                 f"<p class='todo__reason'>{display(item.get('last_error') or payload.get('reason'), '暂无原因说明')}</p>"
                 f"<div class='todo__foot'><span>更新：{display(item.get('updated_at'))}</span>"
                 f"<span>负责人：{display(payload.get('owner'), '未认领')}</span>"
                 + (f"<a href='/orders/{quote(order_no, safe='')}'>查看订单 {h(order_no)}</a>" if order_no else "") + "</div>")
        if status_value == "unknown" and kind in ("notify_group", "notify_dm"):
            body += "<div class='uncertain-flag'>发送结果不确定。先人工核实接收情况，避免重复通知。</div>"
        if role in ("admin", "operator") and item.get("source") in ("task", "inbox") and kind not in ("notify_group", "notify_dm"):
            if item.get("source") == "task":
                endpoint = "/api/tasks/" + quote(str(item["id"]), safe="")
                buttons = (f"<button class='btn btn--sm' formaction='{endpoint}/retry' data-confirm='确认重试？'>重试</button>"
                           f"<button class='btn btn--sm' formaction='{endpoint}/claim' data-confirm='确认认领？'>认领</button>")
                buttons += f"<button class='btn btn--sm' formaction='{endpoint}/resolve' data-confirm='确认结案？'>结案</button>"
            else:
                endpoint = "/api/inbox/" + quote(str(item["id"]), safe="") + "/retry"
                buttons = "<button class='btn btn--sm' data-confirm='确认重试 OCR？'>重试</button>"
            body += (f"<form class=api-form action='{endpoint}'><label>操作原因"
                     "<input class='input' name='reason' required minlength='2'></label>" + buttons + "</form>")
        body += "<details><summary>查看原始任务数据</summary><pre>" + h(json.dumps(payload, ensure_ascii=False, indent=2)) + "</pre></details></article>"
    body += "</div>"
    base = {"kind": filters.get("kind", ""), "status": filters.get("status", ""), "order": order}
    pages = (total + per_page - 1) // per_page
    body += "<div class='pager mt-4'>" + "".join(
        f"<a class='pager__btn' href='/tasks?{h(urlencode({**base, 'page': n}))}' {'aria-current=page' if n == page_number else ''}>{n}</a>"
        for n in range(max(1, page_number - 2), min(pages, page_number + 2) + 1)) + "</div>"
    return body


def notifications(rows, filters, page_number=1, per_page=25):
    status = filters.get("status", "").strip()
    rows = [r for r in rows if not status or r.get("status") == status]
    total = len(rows)
    page_number = min(max(1, page_number), max(1, (total + per_page - 1) // per_page))
    shown = rows[(page_number - 1) * per_page:page_number * per_page]
    body = page("通知中心", "查看发送状态与回执；结果不确定时先人工核实。")
    body += "<form class='toolbar' method='get' action='/notifications'><label>状态<select class='select' name='status'><option value=''>全部</option>" + "".join(
        f"<option {'selected' if x == status else ''}>{x}</option>" for x in ("pending", "retry", "running", "unknown", "dead", "succeeded")) + "</select></label><button class='btn'>筛选</button></form>"
    body += f"<p class='sub mb-4'>符合条件：{total} 条</p>"
    if not shown:
        return body + empty("当前筛选没有通知")
    body += "<div class='notify-list'>"
    for item in shown:
        payload = item.get("payload") or {}
        status_value = str(item.get("status") or "")
        order = str(payload.get("order") or "")
        tone = "warn" if status_value == "unknown" else "bad" if status_value == "dead" else "ok" if status_value == "succeeded" else "info"
        recipient = payload.get("channel_id") if item.get("kind") == "notify_group" else (payload.get("name") or payload.get("uid"))
        body += (f"<article class='todo' data-tone='{tone}'><div class='todo__head'>"
                 f"<strong class='todo__title'>{h('群通知' if item.get('kind') == 'notify_group' else '私聊通知')}</strong>"
                 f"{badge(status_value, tone)}</div><div class='todo__foot'>"
                 + (f"<a href='/orders/{quote(order, safe='')}'>{h(order)}</a>" if order else "") +
                 f"<span>对象：{display(recipient)}</span><span>事件：{display(payload.get('status'))}</span>"
                 f"<span>尝试 {h(item.get('attempts'))} 次</span><span>更新：{display(item.get('updated_at'))}</span></div>"
                 f"<p>回执：{display(payload.get('receipt'))}</p>"
                 f"<p>失败原因：{display(item.get('last_error'))}</p>")
        if status_value == "unknown":
            body += "<div class='uncertain-flag'>可能已经送达。先核实接收方，不能直接重发。</div>"
        body += "<details><summary>查看通知详情</summary><pre>" + h(json.dumps(payload, ensure_ascii=False, indent=2)) + "</pre></details></article>"
    body += "</div>"
    pages = (total + per_page - 1) // per_page
    body += "<div class='pager mt-4'>" + "".join(
        f"<a class='pager__btn' href='/notifications?{h(urlencode({'status': status, 'page': n}))}' "
        f"{'aria-current=page' if n == page_number else ''}>{n}</a>"
        for n in range(max(1, page_number - 2), min(pages, page_number + 2) + 1)) + "</div>"
    return body


def report(data):
    def order_links(orders):
        return " ".join(f"<a class='btn btn--sm' href='/orders/{quote(str(order), safe='')}'>{h(order)}</a>" for order in orders) or "无"
    body = page("运营日报 " + str(data.get("date") or ""), "数字来自物流台账与任务记录，可下钻核对。")
    body += "<div class='stat-grid mb-5'>" + "".join((
        stat("订单总数", data.get("denominator", 0), "/orders"),
        stat("缺面单", data["missing_label"]["count"], "/orders", "查看订单"),
        stat("今日签收", data["delivered_today"]["count"], "/orders"),
        stat("未结案", data["unresolved"]["count"], "/tasks"))) + "</div>"
    body += card("缺面单：" + str(data["missing_label"]["count"]), order_links(data["missing_label"]["orders"]))
    body += card("今日签收：" + str(data["delivered_today"]["count"]), order_links(data["delivered_today"]["orders"]))
    body += card("未结案：" + str(data["unresolved"]["count"]), "<a href='/tasks'>进入异常待办</a>")
    rows = ""
    for carrier, item in sorted((data.get("carrier_freshness") or {}).items()):
        rows += (f"<tr><td>{h(carrier)}</td><td>{h(item.get('total'))}</td>"
                 f"<td>{h(item.get('ok'))}</td><td>{h(item.get('with_events'))}</td>"
                 f"<td>{display(item.get('latest_observed_at'))}</td></tr>")
    body += card("承运商数据新鲜度", "<div class='table-scroll'><table class='grid'><thead><tr><th>承运商</th><th>包裹</th><th>成功抓取</th><th>有轨迹</th><th>最后抓取</th></tr></thead><tbody>" + rows + "</tbody></table></div>" if rows else empty("暂无承运商数据"))
    return body + f"<p class='sub mt-4'>数据过期阈值：{h(data.get('tracking_data_max_age_hours', 'N/A'))}</p>"


def users(items, audit):
    body = page("权限管理", "admin 管理账号；operator 处理订单和待办；viewer 只读。")
    fields = ("<label>用户名<input class='input' name='username' required></label>"
              "<label>初始密码（至少 12 位）<input class='input' name='password' type='password' required minlength='12'></label>"
              "<label>角色<select class='select' name='role'><option>viewer</option><option>operator</option><option>admin</option></select></label>"
              "<label>开通原因<input class='input' name='reason' required minlength='2'></label>"
              "<button class='btn btn--primary'>新增账号</button>")
    body += card("新增账号", "<form class=api-form action='/api/users'>" + fields + "</form>")
    rows = ""
    for user in items:
        username = str(user.get("username") or "")
        endpoint = "/api/users/" + quote(username, safe="")
        options = "".join(f"<option {'selected' if user.get('role') == x else ''}>{x}</option>" for x in ("viewer", "operator", "admin"))
        active = "true" if user.get("active") else "false"
        rows += (f"<tr><td>{h(username)}</td><td>{h(user.get('role'))}</td>"
                 f"<td>{h('启用' if user.get('active') else '停用')}</td><td>"
                 f"<form class=api-form action='{endpoint}'><label>新密码<input class='input' name='password' type='password' minlength='12' placeholder='留空不改'></label>"
                 f"<label>角色<select class='select' name='role'>{options}</select></label>"
                 f"<label>状态<select class='select' name='active'><option value='true' {'selected' if active == 'true' else ''}>启用</option>"
                 f"<option value='false' {'selected' if active == 'false' else ''}>停用</option></select></label>"
                 "<label>修改原因<input class='input' name='reason' required minlength='2'></label>"
                 "<button class='btn btn--sm' data-confirm='确认修改账号权限？'>保存</button></form></td></tr>")
    body += card("账号列表", "<div class='table-scroll'><table class='grid'><thead><tr><th>用户名</th><th>角色</th><th>状态</th><th>修改</th></tr></thead><tbody>" + rows + "</tbody></table></div>")
    body += card("权限审计", "<div class='audit'>" + "".join(
        f"<div class='audit__row'><span>{h(item.get('created_at'))}</span><span>{h(item.get('operator'))}</span>"
        f"<span>{h(item.get('action'))} · {h(item.get('reason'))}</span></div>" for item in audit) + "</div>")
    return body
