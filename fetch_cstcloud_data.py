#!/usr/bin/env python3
"""从 CSTCloud 网盘分享抓取 2026 AIOps 挑战赛数据集（第一/第二批）。

网盘是 JS 渲染页面，但底层是三个 JSON 接口，可直接调用：
  POST /s/api/shareGetInfo            分享元信息
  POST /s/api/shareDirList   {shareId, fid}         列目录
  POST /s/api/shareDownloadRequest {shareId, fid}   取带时效直链（age=86400）

用法：
  python3 fetch_cstcloud_data.py --list
  python3 fetch_cstcloud_data.py --group 第二批数据 --dir /path/to/dest
  python3 fetch_cstcloud_data.py --group 第一批数据 --only README
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

SHARE = "r9YmfWbeTG4"
API = "https://pan.cstcloud.cn/s/api/"
ROOT_FID = 2428666566988412


def post(ep: str, body: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        API + ep, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def listdir(fid: int):
    j = post("shareDirList", {"shareId": SHARE, "fid": fid})
    return [
        (r["name"], r["fid"], bool(r.get("dir")), int(r.get("size") or 0))
        for r in (j.get("rows") or [])
    ]


def dl_url(fid: int) -> str:
    return post("shareDownloadRequest", {"shareId": SHARE, "fid": fid})["downloadUrl"]


def fetch(url: str, dest: str) -> int:
    tmp = dest + ".part"
    n = 0
    with urllib.request.urlopen(url, timeout=600) as r, open(tmp, "wb") as fh:
        while True:
            buf = r.read(1 << 20)
            if not buf:
                break
            fh.write(buf)
            n += len(buf)
    os.replace(tmp, dest)
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/202531630503/lyt/workspace/data_stage2")
    ap.add_argument("--group", default="第二批数据", help="根目录下的分组名")
    ap.add_argument("--only", default=None, help="只抓名称含该子串的文件")
    ap.add_argument("--list", action="store_true", help="只列清单不下载")
    a = ap.parse_args()

    root = listdir(ROOT_FID)
    if a.list:
        for name, fid, isdir, size in root:
            print(f"[DIR ] {name}  fid={fid}")
            if isdir:
                for n2, f2, d2, s2 in listdir(fid):
                    print(f"        {n2:<60} {s2/1048576:9.1f} MB  fid={f2}")
        return

    groups = [x for x in root if x[0] == a.group and x[2]]
    if not groups:
        print(f"找不到分组 {a.group}；现有：{[x[0] for x in root]}")
        sys.exit(1)
    items = [x for x in listdir(groups[0][1]) if not x[2]]
    if a.only:
        items = [x for x in items if a.only in x[0]]

    os.makedirs(a.dir, exist_ok=True)
    total = sum(x[3] for x in items)
    print(f"分组={a.group}  文件数={len(items)}  合计={total/1048576:.1f} MB  -> {a.dir}")
    done = 0
    for name, fid, _, size in items:
        dest = os.path.join(a.dir, name)
        if os.path.exists(dest) and os.path.getsize(dest) == size:
            print(f"  [skip] {name} 已存在且大小一致")
            done += size
            continue
        print(f"  [get ] {name}  {size/1048576:.1f} MB ...", flush=True)
        try:
            got = fetch(dl_url(fid), dest)
        except Exception as exc:
            print(f"        失败: {type(exc).__name__}: {exc}")
            continue
        flag = "OK" if got == size else f"大小不符({got} vs {size})"
        print(f"        {flag}")
        done += got
    print(f"完成 {done/1048576:.1f} / {total/1048576:.1f} MB")


if __name__ == "__main__":
    main()
