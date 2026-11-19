"""OMNIWALLET - échange stablecoins <-> mobile money / IBAN (modèle Izichange).

5 routes stablecoin, avec une adresse de dépôt FIXE par route (les tiennes, créées dans
TON portefeuille). Paiement du client par MTN, Moov, Celtiis ou IBAN.

  VENTE (SELL) : le client envoie du stablecoin à ton adresse et reçoit des FCFA.
      AWAITING_DEPOSIT -> (DEPOSIT_SUBMITTED) -> PAYOUT_PENDING -> PAID
  ACHAT (BUY)  : le client paie en FCFA et reçoit du stablecoin à son adresse.
      AWAITING_PAYMENT -> PAYMENT_RECEIVED -> COMPLETED
  Fins possibles : CANCELLED, EXPIRED (30 min sans dépôt / paiement).

Au début, les étapes de confirmation sont faites à la main par un administrateur
(après vérification sur l'explorateur blockchain et sur ton compte de paiement).
Une adresse fixe ne dit pas qui a envoyé : on rattache donc chaque dépôt à un ordre
grâce à l'identifiant de transaction (TXID), qui ne peut servir qu'une seule fois.
"""
import hashlib
import hmac
import os
import re
import sqlite3
import time
from decimal import Decimal
from typing import Literal

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field

MICRO = 1_000_000
ORDER_TTL = 30 * 60
MAX_OPEN = 3
METHODS = ("MTN", "Moov", "Celtiis", "IBAN")
AWAITING = ("AWAITING_DEPOSIT", "AWAITING_PAYMENT")

# Les 5 routes de départ (modifiables). L'adresse de chacune vient du fichier .env : ADDR_<ID>.
ROUTES = {
    "USDT_TRC20":   {"asset": "USDT", "network": "TRON (TRC20)",       "kind": "tron"},
    "USDT_BEP20":   {"asset": "USDT", "network": "BNB Chain (BEP20)",  "kind": "evm"},
    "USDC_BEP20":   {"asset": "USDC", "network": "BNB Chain (BEP20)",  "kind": "evm"},
    "USDT_POLYGON": {"asset": "USDT", "network": "Polygon",            "kind": "evm"},
    "USDC_POLYGON": {"asset": "USDC", "network": "Polygon",            "kind": "evm"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, expires INTEGER NOT NULL,
    phone TEXT NOT NULL, side TEXT NOT NULL, route TEXT NOT NULL,
    crypto_micro INTEGER NOT NULL, fcfa INTEGER NOT NULL, rate INTEGER NOT NULL,
    method TEXT NOT NULL, account TEXT NOT NULL, address TEXT NOT NULL,
    ref_in TEXT, ref_out TEXT, status TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_ref_in ON orders(ref_in) WHERE ref_in IS NOT NULL;
"""

_d: dict = {}  # dépendances fournies par main.py via install()


# ---------- Validations ----------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def tron_ok(addr: str) -> bool:
    """Adresse TRON : base58 avec somme de contrôle."""
    try:
        n = 0
        for c in addr:
            n = n * 58 + _B58.index(c)
        raw = n.to_bytes(25, "big")
    except (ValueError, OverflowError):
        return False
    return raw[0] == 0x41 and hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] == raw[21:]


def addr_ok(kind: str, addr: str) -> bool:
    if kind == "tron":
        return tron_ok(addr)
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", addr))  # EVM : format seulement


def txid_ok(kind: str, txid: str) -> bool:
    pattern = r"[0-9a-fA-F]{64}" if kind == "tron" else r"0x[0-9a-fA-F]{64}"
    return bool(re.fullmatch(pattern, txid))


def iban_ok(iban: str) -> bool:
    """Valide le format et la clé de contrôle d'un IBAN (calcul modulo 97, sans API)."""
    s = iban.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def _check_account(method: str, account: str) -> str:
    if method == "IBAN":
        if not iban_ok(account):
            raise HTTPException(400, "IBAN invalide")
        return account.replace(" ", "").upper()
    return _d["norm"](account)


def _deposit_address(route_id: str) -> str | None:
    addr = os.getenv("ADDR_" + route_id, "").strip()
    return addr if addr and addr_ok(ROUTES[route_id]["kind"], addr) else None


# ---------- Accès ----------

def _me(authorization: str = Header(...)) -> str:
    return _d["current_phone"](authorization)


def _admin(x_admin_token: str = Header(...)) -> None:
    expected = os.getenv("ADMIN_TOKEN", "")
    if len(expected) < 32:
        raise HTTPException(503, "ADMIN_TOKEN non configuré (32 caractères minimum)")
    if not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(401, "Accès refusé")


# ---------- Modèles ----------

class OrderIn(BaseModel):
    side: Literal["SELL", "BUY"]
    route: str
    method: Literal["MTN", "Moov", "Celtiis", "IBAN"]
    amount: Decimal = Field(gt=0)      # SELL : quantité de stablecoin ; BUY : montant en FCFA
    account: str                       # SELL : numéro ou IBAN qui REÇOIT les FCFA ; BUY : numéro ou IBAN qui PAIE
    address: str | None = None         # BUY seulement : adresse du client pour recevoir le stablecoin
    pin: str


class TxidIn(BaseModel):
    txid: str


class ActionIn(BaseModel):
    ref: str = Field(min_length=3, max_length=100)


# ---------- Ordres ----------

def _view(r) -> dict:
    out = {
        "id": r["id"], "reference": f"OMNI-ORD-{r['id']}", "side": r["side"], "route": r["route"],
        "asset": ROUTES[r["route"]]["asset"], "network": ROUTES[r["route"]]["network"],
        "crypto_amount": f"{r['crypto_micro'] / MICRO:.6f}", "fcfa": r["fcfa"], "rate": r["rate"],
        "method": r["method"], "status": r["status"], "expires": r["expires"], "ts": r["ts"],
    }
    if r["side"] == "SELL":
        out["send_to_address"] = r["address"]
        out["payout_account"] = r["account"]
    else:
        out["receive_address"] = r["address"]
        out["pay_to"] = os.getenv("PAY_TO_" + r["method"].upper(), "")
    return out


def _load(con, order_id: int, phone: str | None = None):
    r = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if r is None or (phone is not None and r["phone"] != phone):
        raise HTTPException(404, "Ordre introuvable")
    if r["status"] in AWAITING and r["expires"] < time.time():
        con.execute("UPDATE orders SET status = 'EXPIRED' WHERE id = ?", (order_id,))
        r = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return r


def list_routes():
    return {
        "methods": list(METHODS),
        "buy_rate": _d["buy_rate"], "sell_rate": _d["sell_rate"],
        "min_fcfa": int(os.getenv("ORDER_MIN_FCFA", "5000")),
        "max_fcfa": int(os.getenv("ORDER_MAX_FCFA", "2000000")),
        "routes": [
            {"id": rid, "asset": r["asset"], "network": r["network"],
             "sell_enabled": _deposit_address(rid) is not None}
            for rid, r in ROUTES.items()
        ],
    }


def create_order(body: OrderIn, phone: str = Depends(_me)):
    _d["check_pin"](phone, body.pin)
    route = ROUTES.get(body.route)
    if route is None:
        raise HTTPException(404, "Route inconnue")
    account = _check_account(body.method, body.account)

    if body.side == "SELL":
        address = _deposit_address(body.route)
        if address is None:
            raise HTTPException(503, "Cette route n'est pas disponible pour le moment")
        rate = _d["sell_rate"]
        crypto = int((body.amount * MICRO).to_integral_value())
        fcfa = crypto * rate // MICRO
        status = "AWAITING_DEPOSIT"
    else:
        if body.amount != body.amount.to_integral_value():
            raise HTTPException(400, "Le montant en FCFA doit être un nombre entier")
        address = (body.address or "").strip()
        if not addr_ok(route["kind"], address):
            raise HTTPException(400, f"Adresse invalide pour le réseau {route['network']}")
        rate = _d["buy_rate"]
        fcfa = int(body.amount)
        crypto = fcfa * MICRO // rate
        status = "AWAITING_PAYMENT"

    lo, hi = int(os.getenv("ORDER_MIN_FCFA", "5000")), int(os.getenv("ORDER_MAX_FCFA", "2000000"))
    if not lo <= fcfa <= hi:
        raise HTTPException(400, f"Montant hors limites ({lo} à {hi} FCFA)")

    now = int(time.time())
    with _d["tx"]() as con:
        open_n = con.execute(
            "SELECT COUNT(*) FROM orders WHERE phone = ? AND status IN ('AWAITING_DEPOSIT','AWAITING_PAYMENT') AND expires >= ?",
            (phone, now),
        ).fetchone()[0]
        if open_n >= MAX_OPEN:
            raise HTTPException(429, "Trop d'ordres en attente : termine ou annule les précédents")
        cur = con.execute(
            "INSERT INTO orders(ts, expires, phone, side, route, crypto_micro, fcfa, rate, method, account, address, status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, now + ORDER_TTL, phone, body.side, body.route, crypto, fcfa, rate, body.method, account, address, status),
        )
        r = _load(con, cur.lastrowid)
    return _view(r)


def my_orders(phone: str = Depends(_me)):
    with _d["tx"]() as con:
        ids = [x["id"] for x in con.execute(
            "SELECT id FROM orders WHERE phone = ? ORDER BY id DESC LIMIT 50", (phone,)).fetchall()]
        return [_view(_load(con, i)) for i in ids]


def get_order(order_id: int, phone: str = Depends(_me)):
    with _d["tx"]() as con:
        return _view(_load(con, order_id, phone))


def submit_txid(order_id: int, body: TxidIn, phone: str = Depends(_me)):
    """Le client indique le TXID de son envoi (ordre de VENTE)."""
    with _d["tx"]() as con:
        r = _load(con, order_id, phone)
        if r["side"] != "SELL" or r["status"] != "AWAITING_DEPOSIT":
            raise HTTPException(409, "Cet ordre n'attend pas de dépôt")
        txid = body.txid.strip()
        if not txid_ok(ROUTES[r["route"]]["kind"], txid):
            raise HTTPException(400, "TXID invalide pour ce réseau")
        try:
            con.execute("UPDATE orders SET ref_in = ?, status = 'DEPOSIT_SUBMITTED' WHERE id = ?", (txid, order_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Ce TXID est déjà utilisé")
        return _view(_load(con, order_id))


def cancel_order(order_id: int, phone: str = Depends(_me)):
    with _d["tx"]() as con:
        r = _load(con, order_id, phone)
        if r["status"] not in AWAITING:
            raise HTTPException(409, "Cet ordre ne peut plus être annulé")
        con.execute("UPDATE orders SET status = 'CANCELLED' WHERE id = ?", (order_id,))
        return _view(_load(con, order_id))


# ---------- Administration (confirmations manuelles) ----------

# (côté, statut actuel) -> {action: (nouveau statut, colonne qui reçoit la référence)}
TRANSITIONS = {
    ("SELL", "AWAITING_DEPOSIT"):  {"deposit-confirmed": ("PAYOUT_PENDING", "ref_in")},
    ("SELL", "DEPOSIT_SUBMITTED"): {"deposit-confirmed": ("PAYOUT_PENDING", "ref_in")},
    ("SELL", "PAYOUT_PENDING"):    {"payout-done": ("PAID", "ref_out")},
    ("BUY", "AWAITING_PAYMENT"):   {"payment-received": ("PAYMENT_RECEIVED", "ref_in")},
    ("BUY", "PAYMENT_RECEIVED"):   {"crypto-sent": ("COMPLETED", "ref_out")},
}


def admin_orders(status: str | None = None, _: None = Depends(_admin)):
    with _d["tx"]() as con:
        ids = [x["id"] for x in con.execute(
            "SELECT id FROM orders WHERE (? IS NULL OR status = ?) ORDER BY id DESC LIMIT 200", (status, status)
        ).fetchall()]
        out = []
        for i in ids:
            r = _load(con, i)
            out.append({**_view(r), "phone": r["phone"], "ref_in": r["ref_in"], "ref_out": r["ref_out"]})
        return out


def admin_action(order_id: int, action: str, body: ActionIn, _: None = Depends(_admin)):
    """deposit-confirmed (ref = TXID reçu), payout-done (ref = référence du paiement envoyé),
    payment-received (ref = référence du paiement reçu), crypto-sent (ref = TXID envoyé)."""
    ref = body.ref.strip()
    with _d["tx"]() as con:
        r = _load(con, order_id)
        step = TRANSITIONS.get((r["side"], r["status"]), {}).get(action)
        if step is None:
            raise HTTPException(409, f"Action '{action}' impossible : ordre {r['side']} au statut {r['status']}")
        new_status, column = step
        if action in ("deposit-confirmed", "crypto-sent") and not txid_ok(ROUTES[r["route"]]["kind"], ref):
            raise HTTPException(400, "TXID invalide pour ce réseau")
        try:
            con.execute(f"UPDATE orders SET status = ?, {column} = ? WHERE id = ?", (new_status, ref, order_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Cette référence est déjà utilisée par un autre ordre")
        return _view(_load(con, order_id))


# ---------- Branchement sur l'application ----------

def install(app, **deps) -> None:
    """deps : tx, norm, current_phone, check_pin, buy_rate, sell_rate (fournis par main.py)."""
    _d.update(deps)
    app.get("/exchange/routes")(list_routes)
    app.post("/exchange/orders", status_code=201)(create_order)
    app.get("/exchange/orders")(my_orders)
    app.get("/exchange/orders/{order_id}")(get_order)
    app.post("/exchange/orders/{order_id}/txid")(submit_txid)
    app.post("/exchange/orders/{order_id}/cancel")(cancel_order)
    app.get("/admin/orders")(admin_orders)
    app.post("/admin/orders/{order_id}/{action}")(admin_action)
