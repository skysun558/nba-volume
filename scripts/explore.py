# -*- coding: utf-8 -*-
"""探测 Polymarket 如何取到「最近的」NBA 已结算事件"""
import json
import urllib.request

UA = {"User-Agent": "nba-explore/2.0", "Accept": "application/json"}
P = "https://gamma-api.polymarket.com"


def g(u):
    req = urllib.request.Request(u, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def dates(name, u):
    try:
        d = g(u)
        if not isinstance(d, list):
            print("\n##", name, "not list:", type(d).__name__, list(d.keys())[:8] if isinstance(d, dict) else "")
            return
        eds = [str(x.get("endDate")) for x in d]
        print("\n##", name, "n=", len(d))
        print("   first endDate:", eds[0] if eds else None, "| last:", eds[-1] if eds else None)
        if d:
            print("   first title:", str(d[0].get("title"))[:70], "| vol:", d[0].get("volume"),
                  "| markets:", len(d[0].get("markets") or []))
    except Exception as ex:
        print("\n##", name, "FAIL", type(ex).__name__, str(ex)[:90])


dates("closed, default order", P + "/events?tag_slug=nba&closed=true&limit=5")
dates("closed, order=endDate desc", P + "/events?tag_slug=nba&closed=true&limit=5&order=endDate&ascending=false")
dates("closed, start_date_min", P + "/events?tag_slug=nba&closed=true&limit=5&end_date_min=2026-10-01T00:00:00Z")
try:
    t = g(P + "/tags?slug=nba")
    print("\n## tag nba:", json.dumps(t, ensure_ascii=False)[:200])
    tid = (t[0] if isinstance(t, list) and t else {}).get("id")
    if tid:
        dates("markets by tag_id desc", P + "/markets?tag_id=%s&closed=true&limit=5&order=endDate&ascending=false" % tid)
except Exception as ex:
    print("\n## tag nba FAIL", type(ex).__name__, str(ex)[:80])
