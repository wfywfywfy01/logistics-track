import io, json, subprocess, sys, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
targets = {"王禹","罗锦然","李玉琴","宋依亭","贾梦林","周佳丽","郑丽苹","张心言"}
r = subprocess.run('vertu-cli im +departments --max-depth 0 --no-json', shell=True, capture_output=True)
lines = r.stdout.decode("utf-8", errors="replace").splitlines()
# parse "- Name (#id, staff a/b)" with 2-space indentation levels
paths, stack = [], []
for ln in lines:
    m = re.match(r'^(\s*)- (.+?) \(#\d+, staff ([\d]+)/([\d]+)\)', ln)
    if not m: continue
    indent = len(m.group(1)) // 2
    name = m.group(2).strip()
    total = int(m.group(4))
    stack = stack[:indent] + [name]
    if total > 0:
        paths.append("/".join(stack))
paths = list(dict.fromkeys(paths))
print("depts with staff:", len(paths))
people = {}
for dp in paths:
    cmd = f'vertu-cli im +users --department-path "{dp}" --limit 300 --no-json'
    rr = subprocess.run(cmd, shell=True, capture_output=True)
    txt = rr.stdout.decode("utf-8", errors="replace")
    try: d = json.loads(txt)
    except Exception: continue
    for u in (d.get("users") or []):
        nm, uid = u.get("employee_name"), u.get("user_id")
        if nm and uid: people[nm] = uid
print("people:", len(people))
found = {n: people[n] for n in targets if n in people}
print("FOUND:", json.dumps(found, ensure_ascii=False))
cache = json.load(open("data/users_map.json", encoding="utf-8"))
cache.update(found)
json.dump(cache, open("data/users_map.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)
json.dump(people, open("data/org_people.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)
print("cache:", json.dumps(cache, ensure_ascii=False))
