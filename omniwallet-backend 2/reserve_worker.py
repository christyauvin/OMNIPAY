"""OMNIWALLET - worker de réserves.

Compare l'USDT dû aux clients (registre interne) à l'USDT réellement détenu
sur Binance. Si la couverture baisse trop, il alerte, et sous 100 % il suspend
les achats d'USDT (drapeau 'buy_halted' lu par main.py) jusqu'au retour à la normale.

    python reserve_worker.py          # tourne en continu
    python reserve_worker.py --once   # une seule vérification
"""
import argparse
import hashlib
import hmac
import logging
import os
import sqlite3
import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

load_dotenv()

DB = os.getenv("OMNI_DB", "omniwallet.db")
KEY = os.getenv("BINANCE_API_KEY", "")
SECRET = os.getenv("BINANCE_API_SECRET", "")
MIN_COVERAGE = Decimal(os.getenv("MIN_COVERAGE", "1.05"))  # alerte sous 105 %
INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))           # secondes
ALERT_COOLDOWN = 30 * 60                                    # une alerte / 30 min
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
MICRO = 1_000_000
BASE = "https://api.binance.com"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
    phone TEXT PRIMARY KEY, name TEXT NOT NULL,
    fcfa INTEGER NOT NULL DEFAULT 0 CHECK(fcfa >= 0),
    usdt INTEGER NOT NULL DEFAULT 0 CHECK(usdt >= 0)
);
CREATE TABLE IF NOT EXISTS flags(name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reserve_snapshots(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
    held_micro INTEGER NOT NULL, owed_micro INTEGER NOT NULL, status TEXT NOT NULL
);
"""

log = logging.getLogger("reserve_worker")
_last_alert = 0.0


def binance_usdt() -> Decimal:
    """Solde USDT (libre + bloqué) du compte Binance. Clé en lecture seule suffisante."""
    qs = urlencode({"timestamp": int(time.time() * 1000), "recvWindow": 5000})
    sig = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    r = httpx.get(
        f"{BASE}/api/v3/account?{qs}&signature={sig}",
        headers={"X-MBX-APIKEY": KEY},
        timeout=10,
    )
    r.raise_for_status()
    for b in r.json()["balances"]:
        if b["asset"] == "USDT":
            return Decimal(b["free"]) + Decimal(b["locked"])
    return Decimal(0)


def notify(msg: str) -> None:
    global _last_alert
    log.warning(msg)
    if time.time() - _last_alert < ALERT_COOLDOWN:
        return
    _last_alert = time.time()
    if TG_TOKEN and TG_CHAT:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg},
                timeout=10,
            )
        except httpx.HTTPError:
            log.exception("Envoi Telegram impossible")


def check() -> str:
    held = binance_usdt()
    con = sqlite3.connect(DB, timeout=10)
    try:
        con.executescript(SCHEMA)
        owed_micro = con.execute("SELECT COALESCE(SUM(usdt), 0) FROM accounts").fetchone()[0]
        owed = Decimal(owed_micro) / MICRO
        coverage = held / owed if owed > 0 else None

        if coverage is None or coverage >= MIN_COVERAGE:
            status = "OK"
        elif coverage >= 1:
            status = "FAIBLE"
        else:
            status = "CRITIQUE"

        # CRITIQUE : on suspend les achats. OK : on les rouvre. FAIBLE : on ne change rien.
        halt = {"CRITIQUE": "1", "OK": "0"}.get(status)
        if halt is not None:
            con.execute(
                "INSERT INTO flags(name, value) VALUES('buy_halted', ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (halt,),
            )
        con.execute(
            "INSERT INTO reserve_snapshots(ts, held_micro, owed_micro, status) VALUES(?,?,?,?)",
            (int(time.time()), int(held * MICRO), owed_micro, status),
        )
        con.commit()
    finally:
        con.close()

    pct = f"{coverage * 100:.1f} %" if coverage is not None else "n/a"
    log.info("USDT Binance : %s | dû aux clients : %s | couverture : %s | %s", held, owed, pct, status)
    if status != "OK":
        manque = owed * MIN_COVERAGE - held
        notify(
            f"OMNIWALLET - réserves {status} : couverture {pct}. "
            f"Il manque environ {manque:.2f} USDT pour atteindre {MIN_COVERAGE * 100:.0f} %."
            + (" Achats d'USDT suspendus." if status == "CRITIQUE" else "")
        )
    return status


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if not KEY or not SECRET:
        raise SystemExit("Renseigne BINANCE_API_KEY et BINANCE_API_SECRET dans .env")
    while True:
        try:
            check()
        except Exception:
            log.exception("Vérification échouée")
        if args.once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
