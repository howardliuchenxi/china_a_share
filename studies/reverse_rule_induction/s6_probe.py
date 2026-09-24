"""RRI-2 probe: verify top_list (dragon-tiger) and moneyflow endpoints."""
import os
import sys

from dotenv import load_dotenv

load_dotenv("/Users/lcx/workspace/china_a_share/.env")
TOKEN = os.environ.get("TUSHARE_TOKEN", "")
if not TOKEN:
    sys.exit("FATAL: TUSHARE_TOKEN missing")

import tushare as ts  # noqa: E402

pro = ts.pro_api(TOKEN)


def probe(name, fn):
    try:
        df = fn()
        print(f"[ok] {name}: {len(df)} rows, cols={list(df.columns)}")
        return df
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name}: {type(exc).__name__}: {str(exc)[:200]}")
        return None


# dragon-tiger list detail (per trade_date)
tl = probe("top_list 20240105", lambda: pro.top_list(trade_date="20240105"))
if tl is not None and len(tl):
    print(tl.head(3).to_string())

# daily moneyflow per stock
mf = probe("moneyflow 20240105", lambda: pro.moneyflow(trade_date="20240105"))
if mf is not None and len(mf):
    print(mf.head(3).to_string())

# top_inst: institutional seat detail for the dragon-tiger list
ti = probe("top_inst 20240105", lambda: pro.top_inst(trade_date="20240105"))
if ti is not None and len(ti):
    print(ti.head(3).to_string())
