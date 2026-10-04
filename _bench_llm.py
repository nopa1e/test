import json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
URL = "http://172.23.191.167:8000/v1/chat/completions"
PROMPT = ("下面是一次网络事故的证据摘要。\n\n" + "\n".join(
    f"- node{i}: cpu_usage peak_z={3.1*i:.1f} rel=+{i*0.3:.2f}; "
    f"load1 peak_z={2.2*i:.1f}; memory_available_ratio peak_z={1.1*i:.1f}"
    for i in range(1, 10))
    + '\n\n问题：哪一台设备的异常不能被其他设备解释？只输出JSON：'
      '{"root_cause":"<设备名>","reason":"<依据>"}')
def one(i):
    b = json.dumps({"model":"deepseek-r1-14b",
        "messages":[{"role":"user","content":PROMPT}],
        "temperature":0.0,"max_tokens":2048}).encode()
    r = urllib.request.Request(URL, data=b, headers={"Content-Type":"application/json"})
    t = time.time()
    try:
        d = json.loads(urllib.request.urlopen(r, timeout=900).read().decode())
        return time.time()-t, d.get("usage",{}).get("completion_tokens",0), True
    except Exception:
        return time.time()-t, 0, False
import sys
for conc in (8, 24, 48):
    N = conc * 2
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        res = list(ex.map(one, range(N)))
    wall = time.time()-t0
    ok = sum(1 for _,_,o in res if o)
    toks = sum(t for _,t,_ in res)
    lat = sorted(r[0] for r in res)
    print(f"  并发 {conc:3d}: {ok}/{N} 成功  wall {wall:6.1f}s  "
          f"吞吐 {ok/wall:5.2f} 条/秒  延迟中位 {lat[len(lat)//2]:5.1f}s  "
          f"输出 {toks/max(ok,1):.0f} tok/条", flush=True)
    if ok:
        est = 55784 / (ok/wall)
        print(f"          => 全量 55784 条预计 {est/3600:5.1f} 小时", flush=True)
print("BENCH_DONE")
