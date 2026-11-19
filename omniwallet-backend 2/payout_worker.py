"""OMNIWALLET - worker des retraits mobile money.

Lit les retraits 'PENDING' créés par /withdrawals et les envoie via l'agrégateur.
  PENDING -> PROCESSING -> SUCCESS
                        -> FAILED (le client est remboursé, montant + frais, une seule fois)

Sécurité contre les doubles paiements : chaque retrait est marqué PROCESSING (et
enregistré) AVANT l'appel à l'agrégateur, avec une référence unique 'OMNI-<id>'.
Si le worker plante ou si le réseau coupe, le retrait reste PROCESSING : on demande
son statut à l'agrégateur, on ne le renvoie jamais.

    python payout_worker.py          # tourne en continu
    python payout_worker.py --once   # un seul passage
"""
import argparse
import logging
import os
import sqlite3
import time
from contextlib import contextmanager

import httpx  # noqa: F401  (utilisé par AggregatorProvider)
from dotenv import load_dotenv

load_dotenv()

DB = os.getenv("OMNI_DB", "omniwallet.db")
INTERVAL = int(os.getenv("PAYOUT_INTERVAL", "5"))
BATCH = 20
STUCK_AFTER = 3600  # alerte si un retrait reste PROCESSING plus d'une heure

log = logging.getLogger("payout_worker")


# ---------- Fournisseur de paiement ----------

class DevProvider:
    """Simule un agrégateur. Les numéros finissant par 0 échouent (pour tester le remboursement)."""

    def send(self, ref: str, operator: str, to_phone: str, amount: int) -> str:
        return "FAILED" if to_phone.endswith("0") else "SUCCESS"

    def status(self, ref: str) -> str:
        return "UNKNOWN"


class AggregatorProvider:
    """À brancher sur l'API de ton agrégateur (FedaPay, KKiaPay, CinetPay...).

    send()   : envoie le paiement en passant `ref` comme clé d'idempotence, et renvoie
               "SUCCESS", "FAILED" ou "PENDING".
    status() : interroge le statut d'un paiement déjà envoyé avec cette `ref`.
    """

    def send(self, ref: str, operator: str, to_phone: str, amount: int) -> str:
        raise NotImplementedError("Implémente l'appel de paiement de ton agrégateur")

    def status(self, ref: str) -> str:
        raise NotImplementedError("Implémente la lecture de statut de ton agrégateur")


def get_provider():
    return AggregatorProvider() if os.getenv("PAYOUT_PROVIDER", "dev") == "aggregator" else DevProvider()


# ---------- Base de données ----------

@contextmanager
def tx():
    con = sqlite3.connect(DB, isolation_level=None, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def claim() -> list[sqlite3.Row]:
    """Passe un lot de retraits de PENDING à PROCESSING."""
    with tx() as con:
        rows = con.execute(
            "SELECT * FROM payouts WHERE status = 'PENDING' ORDER BY id LIMIT ?", (BATCH,)
        ).fetchall()
        for r in rows:
            con.execute(
                "UPDATE payouts SET status = 'PROCESSING', provider_ref = ? WHERE id = ?",
                (f"OMNI-{r['id']}", r["id"]),
            )
    return rows


def finish(payout_id: int, ok: bool) -> None:
    """Clôture un retrait PROCESSING. L'échec rembourse le client, une seule fois."""
    with tx() as con:
        r = con.execute(
            "SELECT * FROM payouts WHERE id = ? AND status = 'PROCESSING'", (payout_id,)
        ).fetchone()
        if r is None:
            return  # déjà traité
        con.execute(
            "UPDATE payouts SET status = ? WHERE id = ?", ("SUCCESS" if ok else "FAILED", payout_id)
        )
        if not ok:
            for amount, kind in ((r["amount"], "withdraw_refund"), (r["fee"], "withdraw_fee_refund")):
                if amount:
                    con.execute("UPDATE accounts SET fcfa = fcfa + ? WHERE phone = ?", (amount, r["phone"]))
                    con.execute(
                        "INSERT INTO ledger(ts, phone, kind, currency, delta, ref) VALUES(?,?,?,?,?,?)",
                        (int(time.time()), r["phone"], kind, "FCFA", amount, f"OMNI-{payout_id}"),
                    )
    log.info("Retrait %s : %s", payout_id, "SUCCESS" if ok else "FAILED (client remboursé)")


# ---------- Boucle ----------

def run_once(provider) -> None:
    # 1. Envoi des nouveaux retraits
    for r in claim():
        ref = f"OMNI-{r['id']}"
        try:
            result = provider.send(ref, r["operator"], r["to_phone"], r["amount"])
        except Exception:
            log.exception("Envoi incertain pour %s : on ne renvoie pas, on vérifiera le statut", ref)
            continue
        if result in ("SUCCESS", "FAILED"):
            finish(r["id"], result == "SUCCESS")

    # 2. Rapprochement des retraits restés PROCESSING
    with tx() as con:
        pending = con.execute("SELECT * FROM payouts WHERE status = 'PROCESSING'").fetchall()
    for r in pending:
        try:
            state = provider.status(r["provider_ref"])
        except Exception:
            log.exception("Statut illisible pour %s", r["provider_ref"])
            continue
        if state in ("SUCCESS", "FAILED"):
            finish(r["id"], state == "SUCCESS")
        elif time.time() - r["ts"] > STUCK_AFTER:
            log.error("Retrait %s bloqué depuis plus d'une heure : vérification manuelle", r["provider_ref"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    provider = get_provider()
    while True:
        try:
            run_once(provider)
        except Exception:
            log.exception("Passage échoué")
        if args.once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
