"""GS Bridge — syncs closed freqtrade dry-run trades to GS Supabase.

Reads freqtrade's SQLite database (tradesv3.sqlite), finds closed trades
not yet pushed, and upserts them into the same `trades` and
`regime_snapshots` tables used by the GS Alpaca agent.

This gives gs_model.py a bigger, multi-source training set:
  - GS Alpaca live trades (ETH/SOL/LTC on Alpaca)
  - Freqtrade dry-run trades (ETH/SOL/LTC on Bybit)

Run manually:
  python user_data/scripts/gs_bridge.py
  python user_data/scripts/gs_bridge.py --dry-run    # print without pushing

Scheduled: add to the same Task Scheduler job as gs_retrain_check.py,
or run standalone after market hours.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE       = Path(__file__).parent.parent          # user_data/
REPO_ROOT  = HERE.parent                           # freqtrade/
DB_PATH    = HERE / "tradesv3.sqlite"
STATE_PATH = HERE / "scripts" / "bridge_state.json"

GS_ROOT   = Path("C:/Users/adebi/.openclaw")
ENV_PATH  = GS_ROOT / ".env"

SOURCE_TAG = "freqtrade-dryrun"


# ── Env / credentials ─────────────────────────────────────────────────────────

def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


# ── Supabase REST ─────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }


def _post(table: str, rows: list[dict]) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY or not rows:
        return bool(rows) is False
    url  = f"{SUPABASE_URL}/rest/v1/{table}"
    body = json.dumps(rows).encode("utf-8")
    req  = urllib.request.Request(url, data=body, headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 201, 204)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return True
        print(f"[bridge] {table}: HTTP {e.code} - {e.read().decode()[:200]}")
        return False
    except Exception as e:
        print(f"[bridge] {table}: {e}")
        return False


# ── SQLite reader ─────────────────────────────────────────────────────────────

def read_closed_trades(since_trade_id: int = 0) -> list[dict]:
    """Read closed trades from freqtrade SQLite DB."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT
            id, pair, open_date, close_date,
            open_rate, close_rate, stake_amount,
            profit_ratio, profit_abs,
            is_open, sell_reason,
            stop_loss, initial_stop_loss,
            max_rate, min_rate,
            open_order_id, strategy, timeframe
        FROM trades
        WHERE is_open = 0
          AND id > ?
        ORDER BY id
    """, (since_trade_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ── Trade mapper ──────────────────────────────────────────────────────────────

def _map_trade(ft: dict, num_offset: int) -> dict:
    """Convert a freqtrade trade row to GS trades table schema."""
    profit = ft.get("profit_ratio") or 0.0
    outcome = "WIN" if profit > 0.005 else ("LOSS" if profit < -0.005 else "BE")

    pair      = ft.get("pair", "")                  # e.g. "ETH/USDT"
    instrument = pair.replace("USDT", "USD")        # normalise to GS naming

    entry_ts = ft.get("open_date", "")
    exit_ts  = ft.get("close_date", "")

    # Approx R: profit_ratio / abs(stoploss_ratio)
    stop_dist = abs((ft.get("open_rate", 1) - ft.get("stop_loss", 0))
                    / max(ft.get("open_rate", 1), 1e-9))
    r = round(profit / stop_dist, 3) if stop_dist > 0 else 0.0

    return {
        "num":          num_offset + ft["id"],
        "instrument":   instrument,
        "direction":    "long",
        "entry":        ft.get("open_rate"),
        "stop":         ft.get("stop_loss"),
        "target":       ft.get("max_rate"),
        "size":         ft.get("stake_amount"),
        "r":            r,
        "outcome":      outcome,
        "fill_type":    "real",
        "simulated":    True,
        "source":       SOURCE_TAG,
        "mode":         "paper",
        "entry_time":   entry_ts,
        "exit_time":    exit_ts,
        "exit_price":   ft.get("close_rate"),
        "exit_reason":  ft.get("sell_reason"),
        "rule_cited":   ft.get("strategy"),
    }


def _map_snapshot(ft: dict, num_offset: int) -> dict:
    """Minimal regime_snapshot row — features will be filled on next gs_dataset run."""
    pair = ft.get("pair", "")
    instrument = pair.replace("USDT", "USD")
    profit = ft.get("profit_ratio") or 0.0
    outcome = "WIN" if profit > 0.005 else ("LOSS" if profit < -0.005 else "BE")
    return {
        "trade_num":   num_offset + ft["id"],
        "instrument":  instrument,
        "ts":          ft.get("open_date"),
        "regime":      None,
        "outcome":     outcome,
        "r":           round(profit, 4),
    }


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_id": 0, "pushed": 0, "last_run": None}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    dry_run = "--dry-run" in sys.argv
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    state     = load_state()
    last_id   = state.get("last_id", 0)
    num_offset = 10000  # offset so FT trade IDs don't collide with GS trade nums

    print(f"[bridge] {ts}  Checking {DB_PATH.name} for trades > id {last_id} ...")

    trades = read_closed_trades(since_trade_id=last_id)
    if not trades:
        print(f"[bridge] No new closed trades found.")
        return

    print(f"[bridge] Found {len(trades)} new closed trade(s)")

    trade_rows = [_map_trade(t, num_offset) for t in trades]
    snap_rows  = [_map_snapshot(t, num_offset) for t in trades]

    for t in trade_rows:
        outcome = t["outcome"]
        r       = t["r"]
        print(f"  [{t['instrument']}] #{t['num']}  {outcome}  {r:+.2f}R  "
              f"{t['entry_time'][:16] if t['entry_time'] else '?'}")

    if dry_run:
        print("[bridge] Dry run — not pushing to Supabase.")
        return

    ok_trades = _post("trades", trade_rows)
    ok_snaps  = _post("regime_snapshots", snap_rows)

    if ok_trades and ok_snaps:
        new_last = max(t["id"] for t in trades)
        state["last_id"]  = new_last
        state["pushed"]   = state.get("pushed", 0) + len(trades)
        state["last_run"] = ts
        save_state(state)
        print(f"[bridge] Pushed {len(trades)} trade(s) + snapshots to Supabase. "
              f"Total pushed: {state['pushed']}")
    else:
        print("[bridge] Push failed — state not updated, will retry next run.")


if __name__ == "__main__":
    main()
