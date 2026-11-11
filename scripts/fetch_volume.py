#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_volume.py — Kalshi / Polymarket 成交量采集器（在 GitHub Actions 里运行）
================================================================================
两种模式：
  快照（默认）      抓未平仓市场 → data/volume.json（覆盖写）
  回补（--backfill）抓已结算/已关闭市场 → data/history.json（按 id 去重合并）

为什么放在 GitHub Actions：本机到两家 API 被按 SNI 阻断，Actions 出口在美国可直连；
抓完提交进仓库，本地再用 ghproxy / jsDelivr 读。

接口（公开只读，无需 key；字段名以首次真实返回为准）：
  Kalshi      快照  /trade-api/v2/markets?status=open&limit=1000
              回补  /trade-api/v2/markets?status=settled&min_close_ts=..&max_close_ts=..&cursor=..
  Polymarket  快照  /markets?closed=false&limit=500
              回补  /markets?closed=true&limit=500&offset=..

用法：
  python scripts/fetch_volume.py                    # 快照
  python scripts/fetch_volume.py --backfill --days 60
  python scripts/fetch_volume.py --self-test        # 离线自检，不发网络请求
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
POLY = "https://gamma-api.polymarket.com/markets"
UA = "nba-volume-bridge/1.0 (+https://github.com)"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(HERE, "data")

MAX_KALSHI_PAGES = 40      # 每个时间窗最多翻 40 页（每页 1000 条）
MAX_POLY_PAGES = 40        # 最多翻 40 页（每页 500 条）
WINDOW_DAYS = 30

NBA_TEAMS = (
    "hawks", "celtics", "nets", "hornets", "bulls", "cavaliers", "mavericks", "nuggets",
    "pistons", "warriors", "rockets", "pacers", "clippers", "lakers", "grizzlies", "heat",
    "bucks", "timberwolves", "pelicans", "knicks", "thunder", "magic", "76ers", "sixers",
    "suns", "blazers", "trail blazers", "kings", "spurs", "raptors", "jazz", "wizards",
)


def get_json(url, timeout=30, tries=3):
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


def is_nba(text):
    t = (text or "").lower()
    if "nba" in t or "basketball" in t:
        return True
    return any(team in t for team in NBA_TEAMS)


def norm_kalshi(items, limit=None, settled=False):
    out = []
    for m in items or []:
        ticker = m.get("ticker") or ""
        title = m.get("title") or ""
        event = m.get("event_ticker") or ""
        if not is_nba(" ".join([ticker, title, event, m.get("subtitle") or ""])):
            continue
        out.append({
            "src": "kalshi", "id": ticker, "event": event, "title": title,
            "settled": settled,
            "close": m.get("close_time"), "volume": _f(m.get("volume")),
            "oi": _f(m.get("open_interest")), "last": _f(m.get("last_price")),
            "bid": _f(m.get("yes_bid")), "ask": _f(m.get("yes_ask")),
            "result": m.get("result"),
        })
        if limit and len(out) >= limit:
            break
    return out


def norm_poly(items, limit=None, settled=False):
    out = []
    for m in items or []:
        q = m.get("question") or ""
        slug = m.get("slug") or ""
        if not is_nba(" ".join([q, slug])):
            continue
        out.append({
            "src": "polymarket", "id": str(m.get("id") or slug), "event": slug,
            "title": q, "settled": settled, "close": m.get("endDate"),
            "volume": _f(m.get("volume")), "oi": _f(m.get("liquidity")),
            "last": None, "bid": None, "ask": None, "result": None,
        })
        if limit and len(out) >= limit:
            break
    return out


def build_snapshot(kalshi_items, poly_items, now=None):
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    k = norm_kalshi(kalshi_items)
    p = norm_poly(poly_items)
    return {
        "ts": ts,
        "counts": {"kalshi_raw": len(kalshi_items or []), "poly_raw": len(poly_items or []),
                   "kalshi_nba": len(k), "poly_nba": len(p)},
        "markets": k + p,
        "samples": {"kalshi": (kalshi_items or [{}])[0], "polymarket": (poly_items or [{}])[0]},
    }


def merge_history(old_items, new_items):
    """按 (src, id) 去重，新数据覆盖旧数据（已结算的量更完整）。"""
    m = {}
    for i in old_items or []:
        if i.get("src") and i.get("id"):
            m[(i["src"], i["id"])] = i
    added = 0
    for i in new_items or []:
        key = (i.get("src"), i.get("id"))
        if key not in m:
            added += 1
        m[key] = i
    return list(m.values()), added


def _windows(days, now=None):
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    spans = []
    cur = start
    while cur < now:
        nxt = min(cur + timedelta(days=WINDOW_DAYS), now)
        spans.append((cur, nxt))
        cur = nxt
    return spans


def backfill_kalshi(days, log=print):
    items, pages, truncated = [], 0, False
    for a, b in _windows(days):
        cursor = ""
        for _ in range(MAX_KALSHI_PAGES):
            q = {"status": "settled", "limit": 1000,
                 "min_close_ts": int(a.timestamp()), "max_close_ts": int(b.timestamp())}
            if cursor:
                q["cursor"] = cursor
            d = get_json(KALSHI + "?" + urllib.parse.urlencode(q))
            batch = d.get("markets") or []
            items.extend(norm_kalshi(batch, settled=True))
            pages += 1
            cursor = d.get("cursor") or ""
            if not cursor or not batch:
                break
        else:
            truncated = True
            log("  ! Kalshi 窗口 %s~%s 触到翻页上限，可能不全"
                % (a.date(), b.date()))
    return items, pages, truncated


def backfill_poly(days, log=print):
    lo = datetime.now(timezone.utc) - timedelta(days=days)
    items, pages, truncated = [], 0, False
    for _ in range(MAX_POLY_PAGES):
        q = {"closed": "true", "limit": 500, "offset": pages * 500}
        d = get_json(POLY + "?" + urllib.parse.urlencode(q))
        batch = d if isinstance(d, list) else (d.get("markets") or [])
        if not batch:
            return items, pages, truncated
        keep = []
        for m in batch:
            end = str(m.get("endDate") or "")
            try:
                dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt >= lo:
                keep.append(m)
        items.extend(norm_poly(keep, settled=True))
        pages += 1
        if len(batch) < 500:
            return items, pages, truncated
    truncated = True
    log("  ! Polymarket 触到翻页上限 %d 页，可能不全" % MAX_POLY_PAGES)
    return items, pages, truncated


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_outputs(payload, out_dir=None):
    out_dir = out_dir or OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "volume.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    hist = os.path.join(out_dir, "volume_history.jsonl")
    lines = []
    if os.path.exists(hist):
        with open(hist, encoding="utf-8") as f:
            lines = f.read().splitlines()[-1999:]
    lines.append(json.dumps({"ts": payload["ts"], "counts": payload["counts"]}, ensure_ascii=False))
    with open(hist, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return {"volume": os.path.join(out_dir, "volume.json"), "history_lines": len(lines)}


def write_history(items, meta, out_dir=None):
    out_dir = out_dir or OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "history.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "meta": meta, "markets": items}, f, ensure_ascii=False, indent=1)
    return path


def run_backfill(days, out_dir=None):
    log = print
    k, kp, kt = backfill_kalshi(days, log)
    p, pp, pt = backfill_poly(days, log)
    old = (load_json(os.path.join(out_dir or OUT_DIR, "history.json"), {}) or {}).get("markets") or []
    merged, added = merge_history(old, k + p)
    meta = {"days": days, "kalshi_pages": kp, "poly_pages": pp,
            "kalshi_truncated": kt, "poly_truncated": pt,
            "added": added, "total": len(merged)}
    path = write_history(merged, meta, out_dir)
    log("[backfill] %d 天 | Kalshi %d 页 %d 条（截断=%s） | Polymarket %d 页 %d 条（截断=%s）"
        % (days, kp, len(k), kt, pp, len(p), pt))
    log("[backfill] 新增 %d，累计 %d → %s" % (added, len(merged), path))
    return {"meta": meta, "path": path}


def self_test():
    k = [{"ticker": "KXNBAGAME-26NOV11NYKMIL-NYK", "event_ticker": "KXNBAGAME-26NOV11NYKMIL",
          "title": "Will the Knicks beat the Bucks?", "close_time": "2026-11-11T03:00:00Z",
          "volume": 1200, "open_interest": 340, "last_price": 62, "result": "yes"},
         {"ticker": "KXCPI-26NOV", "event_ticker": "KXCPI-26NOV", "title": "CPI above 3%?",
          "volume": 99}]
    p = [{"id": "512345", "question": "Will the Knicks win on 2026-11-11?",
          "slug": "nba-nyk-mil-2026-11-11", "endDate": "2026-11-11T03:00:00Z",
          "volume": "8221.5", "liquidity": "310.2"},
         {"id": "999", "question": "Will Bitcoin close above 100k?", "slug": "btc-100k",
          "volume": "5000"}]
    d = build_snapshot(k, p)
    assert d["counts"]["kalshi_nba"] == 1 and d["counts"]["poly_nba"] == 1, d["counts"]
    assert d["markets"][0]["volume"] == 1200.0 and d["markets"][0]["oi"] == 340.0
    pm = [x for x in d["markets"] if x["src"] == "polymarket"][0]
    assert pm["volume"] == 8221.5 and pm["oi"] == 310.2

    # 去重合并：同 id 覆盖，新 id 计入新增
    old = [{"src": "kalshi", "id": "A", "volume": 10}]
    new = [{"src": "kalshi", "id": "A", "volume": 20}, {"src": "kalshi", "id": "B", "volume": 5}]
    merged, added = merge_history(old, new)
    assert added == 1 and len(merged) == 2
    assert [x for x in merged if x["id"] == "A"][0]["volume"] == 20

    # 时间窗：90 天应切 3 段
    spans = _windows(90, now=datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert len(spans) == 3, len(spans)

    # 收盘时间解析过滤（Polymarket 客户端筛选）
    assert datetime.fromisoformat("2026-11-11T03:00:00Z".replace("Z", "+00:00")).year == 2026
    print("self-test ok:", d["counts"], "windows", len(spans))


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        self_test()
        return
    if "--backfill" in args:
        days = 60
        if "--days" in args:
            try:
                days = int(args[args.index("--days") + 1])
            except (ValueError, IndexError):
                print("--days 需要一个整数")
                sys.exit(2)
        run_backfill(days)
        return
    payload = build_snapshot(get_json(KALSHI + "?limit=1000&status=open").get("markets") or [],
                             get_json(POLY + "?closed=false&limit=500") or [])
    info = write_outputs(payload)
    print("[volume] %s  kalshi %d/%d  poly %d/%d  -> %s"
          % (payload["ts"], payload["counts"]["kalshi_nba"], payload["counts"]["kalshi_raw"],
             payload["counts"]["poly_nba"], payload["counts"]["poly_raw"], info["volume"]))


if __name__ == "__main__":
    main()
