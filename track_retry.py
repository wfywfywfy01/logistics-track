import sys, json
sys.path.insert(0, ".")
from ups_track import track_ups
sales = json.load(open("data/sales_map.json", encoding="utf-8"))
prev = json.load(open("data/ups_results.json", encoding="utf-8"))
retry = [k for k,v in prev.items() if not v.get("ok")]
print("retry", retry)
for order in retry:
    tn = sales[order]["intl"]
    try:
        r = track_ups(tn)
    except Exception as e:
        r = {"tracking": tn, "ok": False, "error": str(e)[:150]}
    r["order"] = order
    r["salesperson"] = sales[order].get("salesperson","")
    prev[order] = r
    print(f"{order} {tn} -> {r.get('stage') or ('ERR:'+r.get('error','')[:60])}")
    json.dump(prev, open("data/ups_results.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)
print("DONE")
