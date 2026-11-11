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
CUT_HOURS = 3          # 赛前量口径：close_time 前 3 小时
MAX_PAGES_PER_SERIES = 6
MAX_POLY_PAGES = 10
WINDOW_DAYS = 30
SERIES_CACHE = os.path.join(OUT_DIR, "kalshi_nba_series.json")
SERIES_CACHE_V = 2
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
            if d.get("v") == SERIES_CACHE_V and d.get("tickers"):
                return d["tickers"]
        except Exception:
            pass
    d = get_json(KALSHI + "/series?category=Sports", timeout=90)
    tickers = sorted({s.get("ticker") for s in (d.get("series") or [])
                      if str(s.get("ticker", "")).startswith(GAME_SERIES_PREFIX)})
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(SERIES_CACHE, "w", encoding="utf-8") as f:
        json.dump({"v": SERIES_CACHE_V, "tickers": tickers}, f, ensure_ascii=False, indent=1)
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
    items = [r for r in items if is_game_market(r)]
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


def backfill(days, with_pregame=True):
    series = nba_series()
    items, truncated = [], False
    games = {}
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
                items.extend(norm_kalshi(m, settled=True) for m in batch if is_game_market({"src": "kalshi", "id": m.get("ticker")}))
                if with_pregame:
                    for m in batch:
                        tk, ev, ct = m.get("ticker"), m.get("event_ticker"), m.get("close_time")
                        if not (tk and ev and ct) or not is_game_market({"src": "kalshi", "id": tk}):
                            continue
                        try:
                            close_ts = int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
                        except ValueError:
                            continue
                        try:
                            pv, nc = _candle_sum(tk, s, close_ts - CUT_HOURS * 3600)
                        except Exception:
                            continue
                        g = games.setdefault(ev, {"event": ev, "close": ct, "pregame_volume": 0.0,
                                                  "total_volume": 0.0, "n_markets": 0, "candles": 0})
                        g["pregame_volume"] += pv
                        g["total_volume"] += float(m.get("volume_fp") or 0)
                        g["n_markets"] += 1
                        g["candles"] += nc
                        time.sleep(0.03)
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
    if with_pregame:
        gl = sorted(games.values(), key=lambda x: x["event"])
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "pregame.json"), "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "meta": {"days": days, "cut_hours": CUT_HOURS, "games": len(gl)},
                       "games": gl}, f, ensure_ascii=False, indent=1)
        print("[pregame] %d 场 | 口径 close-%dh" % (len(gl), CUT_HOURS))
    for label, arr in (("kalshi", items), ("poly", poly)):
        if arr:
            print("%s sample:" % label, json.dumps(arr[0], ensure_ascii=False)[:280])


def _candle_sum(ticker, series, cut_ts, hours=10):
    """赛前累计成交量：cut 之前 hours 小时内的 1 分钟 K 线成交量求和。"""
    url = ("%s/series/%s/markets/%s/candlesticks?start_ts=%d&end_ts=%d&period_interval=1"
           % (KALSHI, series, ticker, cut_ts - hours * 3600, cut_ts))
    d = get_json(url)
    cs = d.get("candlesticks") or []
    return sum(float(c.get("volume_fp") or 0) for c in cs), len(cs)


def pregame(days):
    """用 1 分钟 K 线还原每场赛前累计成交量，避开结算后成交量。"""
    series = nba_series()
    games, n_mkt = {}, 0
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
                    print("  ! %s 列表失败 %s" % (s, type(ex).__name__))
                    break
                batch = d.get("markets") or []
                for m in batch:
                    tk, ev, ct = m.get("ticker"), m.get("event_ticker"), m.get("close_time")
                    if not (tk and ev and ct) or not is_game_market({"src": "kalshi", "id": tk}):
                        continue
                    try:
                        close_ts = int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        continue
                    cut = close_ts - CUT_HOURS * 3600
                    try:
                        pv, nc = _candle_sum(tk, s, cut)
                    except Exception as ex:
                        print("  ! %s K线失败 %s" % (tk, type(ex).__name__))
                        continue
                    g = games.setdefault(ev, {"event": ev, "close": ct, "pregame_volume": 0.0,
                                              "total_volume": 0.0, "n_markets": 0, "candles": 0})
                    g["pregame_volume"] += pv
                    g["total_volume"] += float(m.get("volume_fp") or 0)
                    g["n_markets"] += 1
                    g["candles"] += nc
                    n_mkt += 1
                    time.sleep(0.05)
                pages += 1
                cursor = d.get("cursor") or ""
                if not cursor or not batch:
                    break
    out = sorted(games.values(), key=lambda x: x["event"])
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "pregame.json"), "w", encoding="utf-8") as f:
        json.dump({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "meta": {"days": days, "cut_hours": CUT_HOURS, "markets": n_mkt,
                            "games": len(out)}, "games": out}, f, ensure_ascii=False, indent=1)
    print("[pregame] %d 天 | %d 个市场 | %d 场 | 口径 close-%dh"
          % (days, n_mkt, len(out), CUT_HOURS))
    for g in out[-3:]:
        print("  ", json.dumps(g, ensure_ascii=False)[:200])


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
    elif "--pregame" in sys.argv:
        days = 60
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        pregame(days)
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
