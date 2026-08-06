#!/usr/bin/env python3
"""
Jenkins job: list ACTIVE CoinRoutes orders across the 902 accounts, post a
Slack alert, and flag any order >= NEAR_COMPLETE_PCT filled.
Deps: requests only.
"""

import os
import sys
import json
import requests

# ------------------------------ CONFIG -----------------------------------
HOST          = os.environ["CR_HOST"]                 # required
TOKEN         = os.environ["CR_TOKEN"]                # required (Jenkins credential)
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")   # optional; skips Slack if unset
NEAR_PCT      = float(os.environ.get("NEAR_COMPLETE_PCT", "90"))
REQ_TIMEOUT   = 20

CLIENT_IDS = [
    "fe966b48-6dcb-4274-9d14-0704fa9debd5",  # binance         binance_902
    "b4cd051b-e148-44a7-a03e-8c17ff55823e",  # binancefutures  binance_902_futures
    "1fbd2f44-91e8-49ef-98da-d034f5364bc6",  # bybit           bybit_902
    "61bfac6a-82a2-4fea-b157-dc4a543746fb",  # hyperliquid     902_Hyperliquid
    "ec577682-95c0-4887-bd3c-892f930300ef",  # lighter         902_Lighter
    "ef9e7712-b144-49aa-99ee-14686664f85d",  # okex            okx_902
]

TERMINAL = {"closed", "cancelled", "canceled", "finished",
            "error", "rejected", "expired", "filled"}
COLS = ["account", "exchange", "currency_pair", "side", "client_order_id",
        "created_at", "quantity", "avg_price", "pct_executed",
        "realized_net_spread_pct", "pause_offset", "spread_offset", "risk_quantity"]
# -------------------------------------------------------------------------

S = requests.Session()
S.headers.update({"Authorization": f"Token {TOKEN}", "Accept": "application/json"})


def get(path, params=None):
    r = S.get(f"{HOST.rstrip('/')}{path}", params=params, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    return r.json()


def paginate_orders(client_id):
    params = {"client_id": client_id, "ordering": "-created_at", "limit": 200}
    offset, out = 0, []
    for _ in range(50):
        params["offset"] = offset
        page = get("/api/client_orders/", params)
        if isinstance(page, list):
            return page
        rows = page.get("results", [])
        out.extend(rows)
        if not page.get("next") or not rows:
            break
        offset += len(rows)
    return out


def as_dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}


def coalesce(d, keys):
    for k in keys:
        if d.get(k) not in (None, "", "null"):
            return d[k]
    return None


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_active(o):
    return not o.get("finished_at") and str(o.get("order_status", "")).lower() not in TERMINAL


def extract(o, name, exch):
    ap, rs = as_dict(o.get("cade_algo_params")), as_dict(o.get("realized_spread"))
    return {
        "account": name, "exchange": exch,
        "currency_pair": o.get("currency_pair"), "side": o.get("side"),
        "client_order_id": o.get("client_order_id"), "created_at": o.get("created_at"),
        "quantity": o.get("quantity"), "avg_price": o.get("avg_price"),
        "pct_executed": o.get("pct_executed"),
        "realized_net_spread_pct": rs.get("avg_exec_price_net_pct"),
        "pause_offset": coalesce(ap, ["pause_offset_pct", "pause_offset_value", "pause_offset_ratio"]),
        "spread_offset": coalesce(ap, ["spread_offset_pct", "spread_offset_value"]),
        "risk_quantity": coalesce(ap, ["risk_quantity", "risk_quantity_notional_usd"]),
    }


def print_table(rows):
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in COLS} if rows \
        else {c: len(c) for c in COLS}
    print("  ".join(c.ljust(w[c]) for c in COLS))
    for r in rows:
        print("  ".join(str(r.get(c) if r.get(c) is not None else "").ljust(w[c]) for c in COLS))


def slack_post(active, near):
    def line(r):
        pe = to_float(r["pct_executed"])
        nsp = to_float(r["realized_net_spread_pct"])
        return (f"{r['exchange']}/{r['account']} {r['currency_pair']} {r['side']} "
                f"qty={r['quantity']} px={r['avg_price']} "
                f"{pe:.1f}% netSpread={nsp:.4f}%" if pe is not None and nsp is not None
                else f"{r['exchange']}/{r['account']} {r['currency_pair']} {r['side']} qty={r['quantity']}")
    parts = [f":bar_chart: CoinRoutes 902 active orders: *{len(active)}*"]
    if near:
        parts.append(f":rotating_light: *Near complete (>= {NEAR_PCT:.0f}%): {len(near)}*")
        parts += [f"• {line(r)}" for r in near]
    else:
        parts.append(f"_No orders at or above {NEAR_PCT:.0f}% fill._")
    parts.append("*All active:*")
    parts += [f"• {line(r)}" for r in active[:40]]
    if len(active) > 40:
        parts.append(f"_...and {len(active) - 40} more (truncated)_")
    r = S.post(SLACK_WEBHOOK, json={"text": "\n".join(parts)}, timeout=REQ_TIMEOUT)
    r.raise_for_status()


# ------------------------------ RUN --------------------------------------
try:
    meta = {a["client_id"]: (a.get("name", ""), a.get("exchange", "")) for a in get("/api/exchange_accounts/")}
except requests.RequestException as e:
    print(f"ERROR: fetching accounts: {e}", file=sys.stderr); sys.exit(3)

active = []
for cid in CLIENT_IDS:
    name, exch = meta.get(cid, (cid, "?"))
    try:
        active += [extract(o, name, exch) for o in paginate_orders(cid) if is_active(o)]
    except requests.RequestException as e:
        print(f"ERROR: orders for {name} ({cid}): {e}", file=sys.stderr); sys.exit(3)

active.sort(key=lambda r: r.get("created_at") or "", reverse=True)
near = [r for r in active if (to_float(r["pct_executed"]) or 0) >= NEAR_PCT]

print(f"ACTIVE: {len(active)} | NEAR COMPLETE (>= {NEAR_PCT:.0f}%): {len(near)}\n")
print_table(active)

if SLACK_WEBHOOK:
    try:
        slack_post(active, near); print("slack: posted")
    except requests.RequestException as e:
        print(f"WARN: slack post failed: {e}", file=sys.stderr)
else:
    print("slack: SLACK_WEBHOOK_URL not set, skipping")
