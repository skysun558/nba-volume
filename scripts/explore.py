# -*- coding: utf-8 -*-
"""探测 Kalshi 分时成交（candlesticks）与 Polymarket 成交明细"""
import json
import time
import urllib.request

UA = {"User-Agent": "nba-explore/3.0", "Accept": "application/json"}
K = "https://api.elections.kalshi.com/trade-api/v2"
TICKER = "KXNBAGAME-26OCT11CHIDEN-DEN"
SERIES = "KXNBAGAME"


def g(u):
    req = urllib.request.Request(u, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def probe(name, u, keys=("candlesticks", "trades", "history")):
    try:
        d = g(u)
        print("\n## %s OK" % name)
        print("URL:", u[:150])
        if isinstance(d, dict):
            print("top keys:", list(d.keys())[:10])
            for k in keys:
                if isinstance(d.get(k), list) and d[k]:
                    arr = d[k]
                    print("%s n=%d first=" % (k, len(arr)), json.dumps(arr[0], ensure_ascii=False)[:300])
                    print("last=", json.dumps(arr[-1], ensure_ascii=False)[:300])
                    break
        elif isinstance(d, list):
            print("list n=%d first=" % len(d), json.dumps(d[0], ensure_ascii=False)[:300] if d else "")
    except Exception as ex:
        print("\n## %s FAIL %s %s" % (name, type(ex).__name__, str(ex)[:110]))


now = int(time.time())
week = now - 7 * 86400
month = now - 30 * 86400

probe("kalshi candles 1h 30d",
      "%s/series/%s/markets/%s/candlesticks?start_ts=%d&end_ts=%d&period_interval=60"
      % (K, SERIES, TICKER, month, now))
probe("kalshi candles 1d 60d",
      "%s/series/%s/markets/%s/candlesticks?start_ts=%d&end_ts=%d&period_interval=1440"
      % (K, SERIES, TICKER, month - 30 * 86400, now))
probe("kalshi trades", "%s/markets/trades?ticker=%s&limit=5" % (K, TICKER))
probe("poly data trades", "https://data-api.polymarket.com/trades?market=1356597&limit=3")
