# -*- coding: utf-8 -*-
"""在 Actions 里探测两家 API 的真实字段与可用筛选参数"""
import json
import urllib.request

UA = {"User-Agent": "nba-explore/1.0", "Accept": "application/json"}


def g(u):
    req = urllib.request.Request(u, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def show(name, u):
    try:
        st, b = g(u)
        d = json.loads(b.decode("utf-8"))
        print("\n## %s  status=%s bytes=%d" % (name, st, len(b)))
        print("URL:", u)
        if isinstance(d, dict):
            print("top keys:", list(d.keys())[:14])
            items = d.get("markets") or d.get("events") or d.get("series") or []
        else:
            items = d if isinstance(d, list) else []
            print("list n=", len(items))
        if items:
            it = items[0]
            print("item keys:", list(it.keys())[:30])
            vol_keys = [k for k in it.keys() if "vol" in k.lower() or "interest" in k.lower() or "liquid" in k.lower()]
            print("volume-ish keys:", vol_keys)
            print("sample:", json.dumps({k: it.get(k) for k in list(it.keys())[:12]}, ensure_ascii=False)[:400])
            if "series" in name.lower():
                print("all tickers:", [x.get("ticker") for x in items][:40])
    except Exception as ex:
        print("\n## %s FAIL %s %s" % (name, type(ex).__name__, str(ex)[:100]))


K = "https://api.elections.kalshi.com/trade-api/v2"
P = "https://gamma-api.polymarket.com"

show("kalshi markets open", K + "/markets?status=open&limit=2")
show("kalshi markets settled", K + "/markets?status=settled&limit=2")
show("kalshi markets nba series", K + "/markets?status=settled&limit=2&series_ticker=KXNBAGAME")
show("kalshi series sports", K + "/series?category=Sports")
show("poly events tag nba", P + "/events?tag_slug=nba&closed=true&limit=3")
show("poly markets tag nba", P + "/markets?tag_slug=nba&closed=true&limit=3")
