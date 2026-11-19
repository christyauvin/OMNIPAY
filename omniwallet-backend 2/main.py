"""OMNIWALLET - squelette backend (FastAPI + SQLite).

Montants stockés en entiers : FCFA en unités, USDT en micro-USDT (6 décimales).
On n'utilise jamais de float pour l'argent.

Lancer : uvicorn main:app --reload
"""
import hashlib
import hmac
import os
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

import auth
import exchange

load_dotenv()

DB = os.getenv("OMNI_DB", "omniwallet.db")
WEBHOOK_SECRET = os.getenv("PAYMENT_WEBHOOK_SECRET", "change-me").encode()
BUY_RATE = int(os.getenv("BUY_RATE", "615"))    # FCFA pour 1 USDT acheté
SELL_RATE = int(os.getenv("SELL_RATE", "595"))  # FCFA pour 1 USDT vendu
WITHDRAW_FEE_PCT = 1
MICRO = 1_000_000
DAILY_LIMIT_FCFA = int(os.getenv("DAILY_LIMIT_FCFA", "500000"))  # sorties max / 24 h (transferts + retraits)
OTP_TTL = 300          # un code SMS vaut 5 minutes
OTP_RESEND_WAIT = 60   # délai minimum entre deux codes
MAX_FAILS = 5          # essais de PIN / de code avant blocage
LOCK_SECONDS = 900     # blocage du PIN pendant 15 minutes

app = FastAPI(title="OMNIWALLET API")


# ---------- Base de données ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
    phone TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    fcfa  INTEGER NOT NULL DEFAULT 0 CHECK(fcfa >= 0),
    usdt  INTEGER NOT NULL DEFAULT 0 CHECK(usdt >= 0)
);
CREATE TABLE IF NOT EXISTS ledger(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, phone TEXT NOT NULL, kind TEXT NOT NULL,
    currency TEXT NOT NULL, delta INTEGER NOT NULL, ref TEXT
);
CREATE TABLE IF NOT EXISTS processed_refs(provider_ref TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS flags(name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credentials(
    phone TEXT PRIMARY KEY, pin_hash TEXT NOT NULL,
    fails INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS otps(
    phone TEXT PRIMARY KEY, code_hash TEXT NOT NULL,
    expires INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payouts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
    phone TEXT NOT NULL, operator TEXT NOT NULL, to_phone TEXT NOT NULL,
    amount INTEGER NOT NULL, fee INTEGER NOT NULL DEFAULT 0,
    provider_ref TEXT, status TEXT NOT NULL DEFAULT 'PENDING'
);
"""


@app.on_event("startup")
def init_db() -> None:
    auth.check_config()
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA + exchange.SCHEMA)
    con.close()


@contextmanager
def tx():
    """Transaction atomique : tout réussit ou rien n'est écrit."""
    con = sqlite3.connect(DB, isolation_level=None)
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


def norm(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 8:
        raise HTTPException(400, "Numéro invalide")
    return digits


def to_units(currency: str, amount: Decimal) -> int:
    if currency == "FCFA":
        if amount != amount.to_integral_value():
            raise HTTPException(400, "Le FCFA n'a pas de décimales")
        return int(amount)
    return int((amount * MICRO).to_integral_value())


def move(con, phone: str, currency: str, delta: int, kind: str, ref: str | None = None):
    """Seul endroit qui modifie un solde. Écrit aussi une ligne d'audit."""
    col = {"FCFA": "fcfa", "USDT": "usdt"}[currency]
    try:
        res = con.execute(
            f"UPDATE accounts SET {col} = {col} + ? WHERE phone = ?", (delta, phone)
        )
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Solde insuffisant")
    if res.rowcount == 0:
        raise HTTPException(404, f"Compte inconnu : {phone}")
    con.execute(
        "INSERT INTO ledger(ts, phone, kind, currency, delta, ref) VALUES(?,?,?,?,?,?)",
        (int(time.time()), phone, kind, currency, delta, ref),
    )


def current_phone(authorization: str = Header(...)) -> str:
    """Numéro de l'utilisateur connecté, d'après l'en-tête 'Authorization: Bearer <jeton>'."""
    phone = auth.read_token(authorization[7:]) if authorization.startswith("Bearer ") else None
    if not phone:
        raise HTTPException(401, "Session invalide ou expirée")
    return phone


# ---------- Comptes ----------

def check_pin(phone: str, pin: str) -> None:
    """Vérifie le PIN. Les échecs sont enregistrés même si la requête échoue ensuite."""
    err = None
    with tx() as con:
        row = con.execute("SELECT * FROM credentials WHERE phone = ?", (phone,)).fetchone()
        now = int(time.time())
        if row is None:
            err = (404, "Compte inconnu")
        elif row["locked_until"] > now:
            err = (429, "Trop d'essais : réessaie plus tard")
        elif auth.verify_pin(pin, row["pin_hash"]):
            con.execute("UPDATE credentials SET fails = 0 WHERE phone = ?", (phone,))
        else:
            fails = row["fails"] + 1
            locked = now + LOCK_SECONDS if fails >= MAX_FAILS else 0
            con.execute(
                "UPDATE credentials SET fails = ?, locked_until = ? WHERE phone = ?",
                (0 if locked else fails, locked, phone),
            )
            err = (401, "PIN incorrect")
    if err:
        raise HTTPException(*err)


def check_otp(phone: str, code: str) -> None:
    """Vérifie un code SMS (usage unique, limité en essais)."""
    err = None
    with tx() as con:
        row = con.execute("SELECT * FROM otps WHERE phone = ?", (phone,)).fetchone()
        if row is None or row["expires"] < time.time():
            err = (400, "Code expiré ou absent")
        elif row["attempts"] >= MAX_FAILS:
            err = (429, "Trop d'essais : demande un nouveau code")
        elif auth.verify_otp(phone, code, row["code_hash"]):
            con.execute("DELETE FROM otps WHERE phone = ?", (phone,))
        else:
            con.execute("UPDATE otps SET attempts = attempts + 1 WHERE phone = ?", (phone,))
            err = (401, "Code incorrect")
    if err:
        raise HTTPException(*err)


def fcfa_equiv(currency: str, units: int) -> int:
    return units if currency == "FCFA" else units * SELL_RATE // MICRO


def check_daily_limit(con, phone: str, fcfa_amount: int) -> None:
    rows = con.execute(
        "SELECT currency, delta FROM ledger WHERE phone = ? AND ts >= ? "
        "AND kind IN ('transfer_out', 'withdraw')", (phone, int(time.time()) - 86400),
    ).fetchall()
    spent = sum(fcfa_equiv(r["currency"], -r["delta"]) for r in rows)
    if spent + fcfa_amount > DAILY_LIMIT_FCFA:
        raise HTTPException(400, "Plafond journalier dépassé")


class OtpIn(BaseModel):
    phone: str


@app.post("/auth/otp", status_code=202)
def send_otp(body: OtpIn):
    phone = norm(body.phone)
    code = auth.new_otp()
    with tx() as con:
        old = con.execute("SELECT expires FROM otps WHERE phone = ?", (phone,)).fetchone()
        if old and old["expires"] - OTP_TTL + OTP_RESEND_WAIT > time.time():
            raise HTTPException(429, "Attends un peu avant de redemander un code")
        con.execute(
            "INSERT INTO otps(phone, code_hash, expires, attempts) VALUES(?,?,?,0) "
            "ON CONFLICT(phone) DO UPDATE SET code_hash = excluded.code_hash, "
            "expires = excluded.expires, attempts = 0",
            (phone, auth.hash_otp(phone, code), int(time.time()) + OTP_TTL),
        )
    auth.send_sms(phone, f"OMNIWALLET : ton code est {code}. Il expire dans 5 minutes.")
    return {"sent": True}


class RegisterIn(BaseModel):
    phone: str
    name: str = Field(min_length=2, max_length=60)
    code: str
    pin: str = Field(pattern=r"^\d{4,6}$")


@app.post("/auth/register", status_code=201)
def register(body: RegisterIn):
    phone = norm(body.phone)
    check_otp(phone, body.code)
    with tx() as con:
        try:
            con.execute("INSERT INTO accounts(phone, name) VALUES(?,?)", (phone, body.name))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Ce numéro a déjà un compte")
        con.execute(
            "INSERT INTO credentials(phone, pin_hash) VALUES(?,?)", (phone, auth.hash_pin(body.pin))
        )
    return {"token": auth.make_token(phone)}


class LoginIn(BaseModel):
    phone: str
    pin: str


@app.post("/auth/login")
def login(body: LoginIn):
    phone = norm(body.phone)
    check_pin(phone, body.pin)
    return {"token": auth.make_token(phone)}


class ResetPinIn(BaseModel):
    phone: str
    code: str
    pin: str = Field(pattern=r"^\d{4,6}$")


@app.post("/auth/reset-pin")
def reset_pin(body: ResetPinIn):
    phone = norm(body.phone)
    check_otp(phone, body.code)
    with tx() as con:
        res = con.execute(
            "UPDATE credentials SET pin_hash = ?, fails = 0, locked_until = 0 WHERE phone = ?",
            (auth.hash_pin(body.pin), phone),
        )
        if res.rowcount == 0:
            raise HTTPException(404, "Compte inconnu")
    return {"ok": True}


@app.get("/balance")
def balance(phone: str = Depends(current_phone)):
    with tx() as con:
        row = con.execute("SELECT * FROM accounts WHERE phone = ?", (phone,)).fetchone()
    if not row:
        raise HTTPException(404, "Compte inconnu")
    return {"fcfa": row["fcfa"], "usdt": row["usdt"] / MICRO}


@app.get("/history")
def history(phone: str = Depends(current_phone), limit: int = 50):
    with tx() as con:
        rows = con.execute(
            "SELECT ts, kind, currency, delta, ref FROM ledger WHERE phone = ? "
            "ORDER BY id DESC LIMIT ?", (phone, min(limit, 200)),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Transfert par numéro ----------

class TransferIn(BaseModel):
    to: str
    currency: Literal["FCFA", "USDT"]
    amount: Decimal = Field(gt=0)
    pin: str


@app.post("/transfer")
def transfer(body: TransferIn, phone: str = Depends(current_phone)):
    sender, recipient = phone, norm(body.to)
    check_pin(sender, body.pin)
    if sender == recipient:
        raise HTTPException(400, "Tu ne peux pas t'envoyer de l'argent")
    units = to_units(body.currency, body.amount)
    with tx() as con:
        check_daily_limit(con, sender, fcfa_equiv(body.currency, units))
        move(con, sender, body.currency, -units, "transfer_out", recipient)
        move(con, recipient, body.currency, units, "transfer_in", sender)
    return {"ok": True}


# ---------- Achat / vente d'USDT (contre le registre interne) ----------

class ConvertIn(BaseModel):
    side: Literal["buy", "sell"]
    amount: Decimal = Field(gt=0)  # FCFA si buy, USDT si sell


@app.post("/convert")
def convert(body: ConvertIn, phone: str = Depends(current_phone)):
    with tx() as con:
        if body.side == "buy":
            halted = con.execute("SELECT value FROM flags WHERE name = 'buy_halted'").fetchone()
            if halted and halted["value"] == "1":
                raise HTTPException(503, "Achat d'USDT temporairement suspendu")
            fcfa = to_units("FCFA", body.amount)
            usdt = fcfa * MICRO // BUY_RATE
            move(con, phone, "FCFA", -fcfa, "buy_usdt")
            move(con, phone, "USDT", usdt, "buy_usdt")
            return {"usdt": usdt / MICRO, "rate": BUY_RATE}
        usdt = to_units("USDT", body.amount)
        fcfa = usdt * SELL_RATE // MICRO
        move(con, phone, "USDT", -usdt, "sell_usdt")
        move(con, phone, "FCFA", fcfa, "sell_usdt")
        return {"fcfa": fcfa, "rate": SELL_RATE}
    # Les réserves USDT sont surveillées par reserve_worker.py, qui peut suspendre les achats.


# ---------- Dépôt : webhook de l'agrégateur de paiement ----------

@app.post("/webhooks/payments")
async def payment_webhook(request: Request, x_signature: str = Header(...)):
    raw = await request.body()
    expected = hmac.new(WEBHOOK_SECRET, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(401, "Signature invalide")
    data = await request.json()  # {"provider_ref": ..., "phone": ..., "amount": 5000, "status": "SUCCESS"}
    if data.get("status") != "SUCCESS":
        return {"ignored": True}
    with tx() as con:
        try:  # idempotence : un même paiement n'est crédité qu'une fois
            con.execute("INSERT INTO processed_refs VALUES(?)", (data["provider_ref"],))
        except sqlite3.IntegrityError:
            return {"duplicate": True}
        move(con, norm(data["phone"]), "FCFA", int(data["amount"]), "deposit", data["provider_ref"])
    return {"credited": True}


# ---------- Retrait vers mobile money ----------

class WithdrawIn(BaseModel):
    operator: Literal["MTN", "Moov", "Celtiis"]
    to: str
    amount: int = Field(ge=1000)
    pin: str


@app.post("/withdrawals", status_code=202)
def withdraw(body: WithdrawIn, phone: str = Depends(current_phone)):
    check_pin(phone, body.pin)
    fee = -(-body.amount * WITHDRAW_FEE_PCT // 100)  # arrondi vers le haut
    with tx() as con:
        check_daily_limit(con, phone, body.amount)
        move(con, phone, "FCFA", -body.amount, "withdraw")
        move(con, phone, "FCFA", -fee, "withdraw_fee")
        con.execute(
            "INSERT INTO payouts(ts, phone, operator, to_phone, amount, fee) VALUES(?,?,?,?,?,?)",
            (int(time.time()), phone, body.operator, norm(body.to), body.amount, fee),
        )
    # payout_worker.py envoie ensuite le retrait et rembourse le client en cas d'échec.
    return {"status": "PENDING", "fee": fee}


# ---------- Prix du marché (API publique Binance, sans clé) ----------

_cache: dict = {"t": 0.0, "data": {}}


@app.get("/prices")
def prices():
    if time.time() - _cache["t"] > 2:
        r = httpx.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbols": '["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"]'},
            timeout=5,
        )
        r.raise_for_status()
        _cache["data"] = {x["symbol"][:-4]: float(x["price"]) for x in r.json()}
        _cache["t"] = time.time()
    return {"usd": _cache["data"], "usdt_buy_fcfa": BUY_RATE, "usdt_sell_fcfa": SELL_RATE}


# ---------- Échange stablecoins <-> mobile money / IBAN (voir exchange.py) ----------

exchange.install(
    app, tx=tx, norm=norm, current_phone=current_phone, check_pin=check_pin,
    buy_rate=BUY_RATE, sell_rate=SELL_RATE,
)
