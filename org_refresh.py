#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""刷新组织人员快照 data/org_people.json (姓名 -> user_id), 供 resolve_user 兜底。
im +departments 树按缩进解析部门路径(只取有人的部门) -> 逐部门 im +users。每周日容器内自动跑。"""
import json, re
import robust
from storage import Storage

rc, out, _ = robust.cli_run(["im", "+departments", "--max-depth", "0", "--no-json"], timeout=300)
if rc != 0:
    raise SystemExit("department refresh failed")
# parse "- Name (#id, staff a/b)" with 2-space indentation levels
paths, stack = [], []
for ln in out.splitlines():
    m = re.match(r'^(\s*)- (.+?) \(#\d+, staff ([\d]+)/([\d]+)\)', ln)
    if not m: continue
    indent = len(m.group(1)) // 2
    stack = stack[:indent] + [m.group(2).strip()]
    if int(m.group(4)) > 0:
        paths.append("/".join(stack))
paths = list(dict.fromkeys(paths))
print("depts with staff:", len(paths), flush=True)
people = {}
for dp in paths:
    users_rc, txt, _ = robust.cli_run(["im", "+users", "--department-path", dp, "--limit", "300", "--no-json"], timeout=300)
    if users_rc != 0:
        raise SystemExit("user refresh failed for one department")
    try: d = json.loads(txt)
    except Exception: continue
    for u in (d.get("users") or []):
        nm, uid = u.get("employee_name"), u.get("user_id")
        if nm and uid:
            people.setdefault(nm, [])
            if uid not in people[nm]: people[nm].append(uid)
print("people:", len(people), flush=True)
if not people:
    raise SystemExit("organization refresh returned no people")
Storage().put_document("org_people", people)
