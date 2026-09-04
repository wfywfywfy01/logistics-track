import sys, io, json, subprocess

res = json.load(open("data/ups_results.json", encoding="utf-8"))
sales = json.load(open("data/sales_map.json", encoding="utf-8"))
led = json.load(open("data/shipments.json", encoding="utf-8"))

def cli(args):
    cmd = "python tracking-pipeline.py " + " ".join(f'"{a}"' for a in args)
    r = subprocess.run(cmd, shell=True, capture_output=True)
    return r.stdout.decode("utf-8", errors="replace").strip()

# 1) ingest missing orders (intl 一律从台账拿, 台账没有才看 sales_map 缓存)
for order in res:
    tn = res[order].get("intl") or led.get(order, {}).get("intl") or (sales.get(order) or {}).get("intl")
    if order not in led and tn:
        out = cli(["ingest-pair", "--order", order, "--intl", tn])
        print("ingest:", order, "->", out[:100])

# 2) track-update with real UPS data
for order, r in res.items():
    if not r.get("ok"): 
        print("skip (not ok):", order); continue
    stage = r["stage"]; detail = r.get("detail") or r.get("status_en","")
    out = cli(["track-update", "--order", order, "--status", stage, "--detail", detail])
    print("update:", order, stage, "->", out[:120])
print("ALL DONE")
