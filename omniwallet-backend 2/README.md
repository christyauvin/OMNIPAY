# OMNIWALLET - backend (squelette)

API Python (FastAPI + SQLite) : comptes avec PIN et code SMS, transferts par numéro,
achat/vente d'USDT, dépôts et retraits mobile money, prix Binance, surveillance des réserves.

## Installation
```bash
python -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env     # puis remplis AUTH_SECRET (voir le fichier)
export $(grep -v '^#' .env | xargs)   # ou laisse python-dotenv le lire
uvicorn main:app --reload
```
Documentation interactive : http://127.0.0.1:8000/docs
Si tu avais déjà lancé une version précédente, supprime `omniwallet.db` (le schéma a changé).

## Fichiers
| Fichier | Rôle |
|---|---|
| `main.py` | API : authentification, registre, transferts, conversion, dépôts, retraits, prix |
| `auth.py` | PIN haché (scrypt), code SMS à usage unique, jeton de session signé |
| `reserve_worker.py` | Compare l'USDT dû aux clients à l'USDT sur Binance, alerte, suspend les achats si besoin |
| `payout_worker.py` | Envoie les retraits mobile money, rembourse en cas d'échec |
| `exchange.py` | Échange stablecoins <-> MTN / Moov / Celtiis / IBAN (modèle Izichange), 5 routes |

## Essais rapides (curl)
En mode dev, le code SMS s'affiche dans la console du serveur.
```bash
# 1. Inscription : demander un code, puis créer le compte (PIN de 4 à 6 chiffres)
curl -X POST localhost:8000/auth/otp -H "Content-Type: application/json" -d '{"phone":"0197112233"}'
curl -X POST localhost:8000/auth/register -H "Content-Type: application/json" \
  -d '{"phone":"0197112233","name":"Kofi","code":"123456","pin":"1234"}'     # renvoie un token
TOKEN=...   # colle le token reçu

# 2. Dépôt simulé de 20000 FCFA (signature HMAC avec PAYMENT_WEBHOOK_SECRET)
BODY='{"provider_ref":"TX1","phone":"0197112233","amount":20000,"status":"SUCCESS"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$PAYMENT_WEBHOOK_SECRET" | awk '{print $2}')
curl -X POST localhost:8000/webhooks/payments -H "X-Signature: $SIG" -H "Content-Type: application/json" -d "$BODY"

# 3. Acheter de l'USDT, envoyer, retirer (le PIN est redemandé pour envoyer et retirer)
curl -X POST localhost:8000/convert -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"side":"buy","amount":6150}'
curl -X POST localhost:8000/transfer -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"to":"0196445566","currency":"FCFA","amount":2000,"pin":"1234"}'
curl -X POST localhost:8000/withdrawals -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"operator":"MTN","to":"0197000001","amount":5000,"pin":"1234"}'

# 4. Solde, historique, prix
curl localhost:8000/balance -H "Authorization: Bearer $TOKEN"
curl localhost:8000/history -H "Authorization: Bearer $TOKEN"
curl localhost:8000/prices
```

## Échange façon Izichange (5 routes stablecoin)
Routes de départ : USDT TRC20, USDT BEP20, USDC BEP20, USDT Polygon, USDC Polygon (modifiables dans `exchange.py`).
Paiement par MTN, Moov, Celtiis ou IBAN.

**Créer tes 5 adresses** : dans TON portefeuille (idéalement un portefeuille matériel), crée une adresse par réseau,
note la phrase secrète sur papier, hors ligne. Seules les adresses PUBLIQUES vont dans `.env` (`ADDR_...`).
Ne génère jamais de clés dans un chat ou sur le serveur de l'application. Teste d'abord avec un tout petit montant.

Parcours d'un client (la confirmation est manuelle au début, avec `X-Admin-Token`) :
- **Vente** : `POST /exchange/orders` (side=SELL) donne l'adresse et le montant exact à envoyer, puis le client envoie son TXID (`/txid`). L'admin vérifie sur l'explorateur blockchain, puis `deposit-confirmed`, paie le client, puis `payout-done`.
- **Achat** : `POST /exchange/orders` (side=BUY, adresse du client) donne où payer (`PAY_TO_...`) et la référence. L'admin confirme `payment-received`, envoie le stablecoin, puis `crypto-sent` avec le TXID.
- Un ordre expire après 30 minutes. Un TXID ne peut servir qu'une fois. Les IBAN sont vérifiés (format et clé de contrôle).
- Les paiements IBAN sont faits à la main : il faut un prestataire bancaire pour les automatiser.

## Workers (processus séparés)
```bash
python payout_worker.py     # retraits mobile money (PAYOUT_PROVIDER=dev simule ; "aggregator" = à brancher)
python reserve_worker.py    # réserves USDT (nécessite BINANCE_API_KEY / SECRET, lecture seule)
```
- Retraits : PENDING, puis PROCESSING, puis SUCCESS ou FAILED. En cas d'échec, le client est remboursé (montant et frais) une seule fois.
- Réserves : couverture sous 105 % = alerte ; sous 100 % = alerte et achats d'USDT suspendus jusqu'au retour au-dessus de 105 %.

## Sécurité déjà en place
PIN haché (scrypt), blocage après 5 essais, code SMS à usage unique, jeton de session de 12 h,
PIN redemandé pour transférer et retirer, plafond journalier, webhook signé et idempotent.

## Avant de lancer pour de vrai
1. Brancher un vrai fournisseur SMS (`auth.send_sms`) et mettre `SMS_MODE=prod`.
2. Brancher l'agrégateur de paiement dans `AggregatorProvider` (`payout_worker.py`).
3. Passer de SQLite à PostgreSQL, ajouter HTTPS, limitation de débit et sauvegardes.
4. Vérification d'identité (KYC) avec des plafonds selon le niveau.
5. Faire relire le projet par un expert en sécurité et par un juriste (agrément Bénin / UEMOA).
