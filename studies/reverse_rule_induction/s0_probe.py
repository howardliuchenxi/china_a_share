"""S0 probe: validate tushare token, endpoint shapes, delisted-stock access.

Run first; prints findings. Never prints the token.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv("/Users/lcx/workspace/china_a_share/.env")
TOKEN = os.environ.get("TUSHARE_TOKEN", "")
if not TOKEN:
    sys.exit("FATAL: TUSHARE_TOKEN missing from repo .env")

import tushare as ts  # noqa: E402

pro = ts.pro_api(TOKEN)


def probe(name, fn):
    try:
        df = fn()
        print(f"[ok] {name}: {len(df)} rows, cols={list(df.columns)[:12]}")
        return df
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name}: {type(exc).__name__}: {str(exc)[:220]}")
        return None


cal = probe("trade_cal", lambda: pro.trade_cal(
    exchange="SSE", start_date="20151201", end_date="20160110"))
basic = probe("stock_basic L", lambda: pro.stock_basic(
    exchange="", list_status="L", fields="ts_code,symbol,name,market,list_date"))
basic_d = probe("stock_basic D", lambda: pro.stock_basic(
    exchange="", list_status="D", fields="ts_code,symbol,name,market,list_date"))
probe("stock_basic P", lambda: pro.stock_basic(
    exchange="", list_status="P", fields="ts_code,symbol,name"))

# One trading day of full-market daily (small call, proves per-date strategy works)
probe("daily 20160104", lambda: pro.daily(trade_date="20160104"))
probe("adj_factor 20160104", lambda: pro.adj_factor(trade_date="20160104"))
probe("daily_basic 20160104", lambda: pro.daily_basic(
    trade_date="20160104", fields="ts_code,trade_date,turnover_rate,turnover_rate_f,"
    "volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,"
    "free_share,total_mv,circ_mv"))

# ST endpoints: which exists?
st = probe("stock_st", lambda: pro.stock_st())
nc = probe("namechange bulk p1", lambda: pro.namechange(
    ts_code="", start_date="", end_date="", limit=1000, offset=0,
    fields="ts_code,name,start_date,end_date,ann_date,change_reason"))
if nc is not None:
    probe("namechange bulk p2", lambda: pro.namechange(
        ts_code="", start_date="", end_date="", limit=1000, offset=1000,
        fields="ts_code,name,start_date,end_date,ann_date,change_reason"))

# Delisted-stock accessibility: 601558.SH (delisted 2020) while listed
probe("delisted 601558.SH daily", lambda: pro.daily(
    ts_code="601558.SH", start_date="20190101", end_date="20190601"))
# B-share shape check (should be excluded from universe)
probe("B-share 900901.SH daily", lambda: pro.daily(
    ts_code="900901.SH", start_date="20160104", end_date="20160108"))

if basic is not None:
    print("market values:", basic["market"].value_counts().to_dict())
    print("total listed:", len(basic))
if basic_d is not None:
    print("delisted count:", len(basic_d))
