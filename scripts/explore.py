# -*- coding: utf-8 -*-
"""验证 1 分钟 K 线能否还原「开赛前」累计成交量"""
import json
import urllib.request
from datetime import datetime, timezone

UA = {"User-Agent": "nba-explore/4.0", "Accept": "application/json"}
K = "https://api.elections.kalshi.com/trade-api/v2"
TICKERS = [
    "KXNBAGAME-26OCT11CHIDEN-DEN",
    "KXNBAGAME-26NOV10LACPHX-PHX",
]


def g(u):
    req = urllib.request.Request(u, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


for tk in TICKERS:
    try:
        m = g(K + "/markets/" + tk).get("market") or {}
        occ = m.get("occurrence_datetime") or m.get("open_time")
        print("\n## %s occurrence=%s close=%s vol=%s" % (tk, occ, m.get("close_time"), m.get("volume_fp")))
        if not occ:
            continue
        t0 = int(datetime.fromisoformat(occ.replace("Z", "+00:00")).timestamp())
        d = g("%s/series/KXNBAGAME/markets/%s/candlesticks?start_ts=%d&end_ts=%d&period_interval=1"
              % (K, tk, t0 - 12 * 3600, t0))
        cs = d.get("candlesticks") or []
        tot = sum(float(c.get("volume_fp") or 0) for c in cs)
        last = cs[-1] if cs else {}
        print("##   1m candles n=%d  pregame_sum=%.2f  last_ts=%s last_vol=%s"
              % (len(cs), tot,
                 datetime.fromtimestamp(last.get("end_period_ts", 0), timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                 if last else "-", last.get("volume_fp")))
        print("##   first3:", json.dumps(cs[:3], ensure_ascii=False)[:260])
    except Exception as ex:
        print("\n## %s FAIL %s %s" % (tk, type(ex).__name__, str(ex)[:110]))
