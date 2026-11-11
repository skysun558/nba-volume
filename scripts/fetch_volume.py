#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_volume.py — Kalshi / Polymarket NBA 成交量采集器（GitHub Actions 运行）
==============================================================================
实测校准（2026-11-11）：
  · Kalshi 成交量字段为 volume_fp / open_interest_fp（定点字符串），无 volume 键
  · Kalshi 需按系列过滤：仅保留比赛级系列 KXNBAGAME* / KXNBASERIES
    （其余 280 个 NBA 系列几乎全是球员数据盘，不是我们的标的）
  · Polymarket 的 /markets?tag_slug= 被忽略；/events?tag_slug=nba 可行，
    但必须加 order=endDate&ascending=false 才能拿到最近场次，且需按标题 "vs" 过滤比赛盘

模式：
  快照（默认）      未平仓市场 → data/volume.json
  回补（--backfill）已结算市场 → data/history.json（按 (src,id) 去重合并）

用法：
  python scripts/fetch_volume.py
  python scripts/fetch_volume.py --backfill --days 60
  python scripts/fetch_volume.py --self-test
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY = "https://gamma-api.polymarket.com"
UA = "nba-volume-bridge/2.1"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(HERE, "data")

GAME_SERIES_PREFIX = ("KXNBAGAME", "KXNBASERIES")
MAX_PAGES_PER_SERIES = 6
MAX_POLY_PAGES = 10
WINDOW_DAYS = 30
SERIES_CACHE = os.path.join(OUT_DIR, "kalshi_nba_series.json")
VS_RE = re.compile(r"\bvs\.?\b", re.I)


def get_json(url, timeout=40, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as ex:
            last = ex
            if i + 1 < tries:
                time.sleep(2 * (i + 1))
    raise last


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_game_market(rec):
    """只要比赛级市场：Kalshi 按系列前缀，Polymarket 按标题含 vs。"""
    if rec.get("src") == "kalshi":
        return str(rec.get("id", "")).startswith(GAME_SERIES_PREFIX)
    return bool(VS_RE.search(str(rec.get("title") or "")))


def nba_series(force=False):
    """Kalshi 中比赛级 NBA 系列。"""
    if not force and os.path.exists(SERIES_CACHE):
        try:
            with open(SERIES_CACHE, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("tickers"):
                return d["tickers"]
        except Exception:
            pass
    d = get_json(KALSHI + "/series?category=Sports", timeout=90)
    tickers = sorted({s.get("ticker") for s in (d.get("series") or [])
                      if str(s.get("ticker", "")).startswith(GAME_SERIES_PREFIX)})
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(SERIES_CACHE, "w", encoding="utf-8") as f:
        json.dump({"tickers": tickers}, f, ensure_ascii=False, indent=1)
    return tickers


def norm_kalshi(m, settled=False):
    return {
        "src": "kalshi", "id": m.get("ticker"), "event": m.get("event_ticker"),
        "title": m.get("title") or m.get("sub_title") or m.get("ticker"),
        "settled": settled, "close": m.get("close_time"),
        "volume": _f(m.get("volume_fp")) if m.get("volume_fp") is not None else _f(m.get("volume")),
        "oi": _f(m.get("open_interest_fp")) if m.get("open_interest_fp") is not None else _f(m.get("open_interest")),
        "last": _f(m.get("last_price")), "bid": _f(m.get("yes_bid")), "ask": _f(m.get("yes_ask")),
        "result": m.get("result"),
    }


def norm_poly_event(e):
    vols = [_f(m.get("volume")) for m in (e.get("markets") or [])]
    vols = [v for v in vols if v is not None]
    return {
        "src": "polymarket", "id": str(e.get("id") or e.get("slug")), "event": e.get("slug"),
        "title": e.get("title"), "settled": True, "close": e.get("endDate"),
        "volume": _f(e.get("volume")) if e.get("volume") is not None else (sum(vols) if vols else None),
        "oi": _f(e.get("liquidity")), "last": None, "bid": None, "ask": None, "result": None,
    }


def snapshot():
    series = nba_series()
    items = []
    for s in series:
        try:
            d = get_json(KALSHI + "/markets?" + urllib.parse.urlencode(
                {"status": "open", "limit": 200, "series_ticker": s}))
        except Exception as ex:
            print("  ! %s 快照失败 %s" % (s, type(ex).__name__))
            continue
        items.extend(norm_kalshi(m) for m in (d.get("markets") or []))
    poly = []
    try:
        evs = get_json(POLY + "/events?" + urllib.parse.urlencode(
            {"tag_slug": "nba", "closed": "false", "limit": 100,
             "order": "endDate", "ascending": "false"}))
        for e in evs if isinstance(evs, list) else []:
            rec = norm_poly_event(e)
            rec["settled"] = False
            if is_game_market(rec):
                poly.append(rec)
    except Exception as ex:
        print("  ! Polymarket 快照失败 %s" % type(ex).__name__)
    return {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "counts": {"kalshi_series": len(series), "kalshi_nba": len(items),
                       "poly_nba": len(poly)},
            "markets": items + poly}


def _windows(days):
    now = datetime.now(timezone.utc)
    cur, out = now - timedelta(days=days), []
    while cur < now:
        nxt = min(cur + timedelta(days=WINDOW_DAYS), now)
        out.append((cur, nxt))
        cur = nxt
    return out


def backfill(days):
    series = nba_series()
    items, truncated = [], False
    for s in series:
        for a, b in _windows(days):
            cursor, pages = "", 0
            while pages < MAX_PAGES_PER_SERIES:
                q = {"status": "settled", "limit": 200, "series_ticker": s,
                     "min_close_ts": int(a.timestamp()), "max_close_ts": int(b.timestamp())}
                if cursor:
                    q["cursor"] = cursor
                try:
                    d = get_json(KALSHI + "/markets?" + urllib.parse.urlencode(q))
                except Exception as ex:
                    print("  ! %s %s 拉取失败 %s" % (s, a.date(), type(ex).__name__))
                    break
                batch = d.get("markets") or []
                items.extend(norm_kalshi(m, settled=True) for m in batch)
                pages += 1
                cursor = d.get("cursor") or ""
                if not cursor or not batch:
                    break
            else:
                truncated = True
                print("  ! %s 触到每系列翻页上限" % s)

    lo = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    poly = []
    for page in range(MAX_POLY_PAGES):
        try:
            evs = get_json(POLY + "/events?" + urllib.parse.urlencode(
                {"tag_slug": "nba", "closed": "true", "limit": 100, "offset": page * 100,
                 "order": "endDate", "ascending": "false", "end_date_min": lo}))
        except Exception as ex:
            print("  ! Polymarket 回补失败 %s" % type(ex).__name__)
            break
        if not isinstance(evs, list) or not evs:
            break
        for e in evs:
            rec = norm_poly_event(e)
            if is_game_market(rec):
                poly.append(rec)
        if len(evs) < 100:
            break

    old = []
    path = os.path.join(OUT_DIR, "history.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f).get("markets") or []
        except Exception:
            old = []
    dropped = sum(1 for r in old if not is_game_market(r))
    merged = {(r["src"], r["id"]): r for r in old if r.get("id") and is_game_market(r)}
    added = 0
    for i in items + poly:
        key = (i["src"], i["id"])
        if key not in merged:
            added += 1
        merged[key] = i
    out = list(merged.values())
    meta = {"days": days, "series": len(series), "kalshi_n": len(items),
            "poly_n": len(poly), "added": added, "dropped_non_game": dropped,
            "total": len(out), "truncated": truncated}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "meta": meta, "markets": out}, f, ensure_ascii=False, indent=1)
    print("[backfill] %d 天 | %d 个比赛级系列 | Kalshi %d | Polymarket %d | 新增 %d | 清理非比赛盘 %d | 合计 %d | 截断=%s"
          % (days, len(series), len(items), len(poly), added, dropped, len(out), truncated))
    for label, arr in (("kalshi", items), ("poly", poly)):
        if arr:
            print("%s sample:" % label, json.dumps(arr[0], ensure_ascii=False)[:280])


def self_test():
    k = {"ticker": "KXNBAGAME-26NOV10LACPHX-PHX", "event_ticker": "KXNBAGAME-26NOV10LACPHX",
         "title": "Will Phoenix win?", "volume_fp": "1234.50", "open_interest_fp": "321.00"}
    r = norm_kalshi(k, settled=True)
    assert r["volume"] == 1234.5 and r["oi"] == 321.0, r
    e = {"id": "99", "slug": "nba-lal-bos", "title": "Lakers vs. Celtics",
         "volume": "555.5", "liquidity": "12.0", "endDate": "2026-11-11T03:00:00Z", "markets": []}
    r2 = norm_poly_event(e)
    assert r2["volume"] == 555.5 and r2["oi"] == 12.0, r2
    assert is_game_market(r) and is_game_market(r2)
    assert not is_game_market({"src": "kalshi", "id": "KXNBAPTS-26NOV10LACPHX-LEBRON-30"})
    assert not is_game_market({"src": "polymarket", "title": "Will LeBron retire before next NBA season?"})
    assert len(_windows(90)) == 3
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    elif "--backfill" in sys.argv:
        days = 60
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        backfill(days)
    else:
        d = snapshot()
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "volume.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        print("[snapshot] %s | series=%d kalshi=%d poly=%d"
              % (d["ts"], d["counts"]["kalshi_series"], d["counts"]["kalshi_nba"], d["counts"]["poly_nba"]))
