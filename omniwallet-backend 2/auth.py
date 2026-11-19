"""OMNIWALLET - briques d'authentification (bibliothèque standard uniquement).

- PIN : haché avec scrypt, jamais stocké en clair.
- Code SMS (OTP) : 6 chiffres, haché, valable 5 minutes, à usage unique.
- Jeton de session : signé HMAC-SHA256, expire après 12 h.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

log = logging.getLogger("auth")
TOKEN_TTL = 12 * 3600


def _secret() -> bytes:
    s = os.getenv("AUTH_SECRET", "")
    if len(s) < 32:
        raise RuntimeError(
            "AUTH_SECRET manquant ou trop court (32 caractères minimum). "
            "Génère-en un : python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return s.encode()


def check_config() -> None:
    _secret()


# ---------- PIN ----------

def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex() + ":" + dk.hex()


def verify_pin(pin: str, stored: str) -> bool:
    salt_hex, dk_hex = stored.split(":")
    dk = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk.hex(), dk_hex)


# ---------- Code SMS (OTP) ----------

def new_otp() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def hash_otp(phone: str, code: str) -> str:
    return hmac.new(_secret(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()


def verify_otp(phone: str, code: str, stored: str) -> bool:
    return hmac.compare_digest(hash_otp(phone, code), stored)


def send_sms(phone: str, text: str) -> None:
    """Mode dev : affiche le SMS dans le journal. En production, branche ton fournisseur SMS ici."""
    if os.getenv("SMS_MODE", "dev") == "dev":
        log.warning("[SMS DEV] %s : %s", phone, text)
        return
    raise RuntimeError("Branche ton fournisseur SMS dans auth.send_sms()")


# ---------- Jeton de session ----------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_token(phone: str, ttl: int = TOKEN_TTL) -> str:
    body = _b64(json.dumps({"sub": phone, "exp": int(time.time()) + ttl}).encode())
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(token: str) -> str | None:
    """Renvoie le numéro si le jeton est valide et non expiré, sinon None."""
    try:
        body, sig = token.split(".")
        good = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data["sub"] if data["exp"] > time.time() else None
    except (ValueError, KeyError, TypeError):
        return None
