import os
import time
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict
from datetime import datetime, timedelta, timezone
import requests
from database import db, create_document, get_documents
from schemas import User, KYC, Wallet, Deposit, Withdrawal, Order, PriceSnapshot
from bson import ObjectId

app = FastAPI(title="Laxo Exchange API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PAYPAL_EMAIL = "elvisspear046@gmail.com"

# Utilities
class AuthPayload(BaseModel):
    email: str
    password: str

class AuthUser(BaseModel):
    id: str
    email: str
    is_admin: bool = False


def hash_password(pw: str) -> str:
    # Simple hash for demo (not secure) – in real prod use bcrypt
    import hashlib
    return hashlib.sha256(pw.encode()).hexdigest()


def get_user_by_email(email: str) -> Optional[dict]:
    u = db["user"].find_one({"email": email})
    return u


def auth_required(email: str, password: str) -> AuthUser:
    u = get_user_by_email(email)
    if not u:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if u.get("password_hash") != hash_password(password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return AuthUser(id=str(u["_id"]), email=u["email"], is_admin=bool(u.get("is_admin", False)))


# Price feed (simple public API)
PRICE_CACHE: Dict[str, float] = {}
PRICE_CACHE_TS: Optional[datetime] = None


def fetch_prices() -> Dict[str, float]:
    global PRICE_CACHE, PRICE_CACHE_TS
    now = datetime.now(timezone.utc)
    if PRICE_CACHE and PRICE_CACHE_TS and (now - PRICE_CACHE_TS).seconds < 20:
        return PRICE_CACHE
    try:
        # Use CoinGecko simple price
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum,tether", "vs_currencies": "usd"},
            timeout=5,
        )
        data = r.json()
        prices = {
            "BTC": float(data.get("bitcoin", {}).get("usd", 0.0)),
            "ETH": float(data.get("ethereum", {}).get("usd", 0.0)),
            "USDT": 1.0,
        }
        PRICE_CACHE = prices
        PRICE_CACHE_TS = now
        # store snapshot (best-effort)
        try:
            snap = PriceSnapshot(timestamp=now, prices=prices)
            create_document("pricesnapshot", snap)
        except Exception:
            pass
        return prices
    except Exception:
        if PRICE_CACHE:
            return PRICE_CACHE
        return {"BTC": 0.0, "ETH": 0.0, "USDT": 1.0}


# Public routes
@app.get("/")
def root():
    return {"name": "Laxo", "status": "ok"}


@app.post("/auth/register")
def register(payload: AuthPayload):
    if get_user_by_email(payload.email):
        raise HTTPException(status_code=400, detail="Email already registered")
    u = User(
        name=payload.email.split("@")[0],
        email=payload.email,
        password_hash=hash_password(payload.password),
        account_status="new",
        is_admin=False,
    )
    user_id = create_document("user", u)
    # create wallets
    for asset in ["BTC", "ETH", "USDT", "USD"]:
        try:
            create_document("wallet", Wallet(user_id=user_id, asset=asset if asset != "USD" else "USD", balance=0.0, available=0.0))
        except Exception:
            pass
    return {"user_id": user_id}


@app.post("/auth/login")
def login(payload: AuthPayload):
    user = auth_required(payload.email, payload.password)
    return user


# KYC
class KycPayload(BaseModel):
    email: str
    password: str
    document_type: str
    document_number: str
    full_name: str
    dob: str
    address: str


@app.post("/kyc/submit")
def submit_kyc(payload: KycPayload):
    user = auth_required(payload.email, payload.password)
    # create KYC
    kyc = KYC(
        user_id=user.id,
        document_type=payload.document_type, document_number=payload.document_number,
        full_name=payload.full_name, dob=payload.dob, address=payload.address,
        status="pending"
    )
    kyc_id = create_document("kyc", kyc)
    # update account status to kyc_pending
    db["user"].update_one({"_id": ObjectId(user.id)}, {"$set": {"account_status": "kyc_pending"}})

    # Schedule auto-approval after 2 minutes (best-effort naive worker)
    # We store a record that frontend can poll; also try to set a delayed approval timestamp
    approve_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    db["kyc"].update_one({"_id": ObjectId(kyc_id)}, {"$set": {"approve_at": approve_at}})

    return {"kyc_id": kyc_id, "approve_at": approve_at.isoformat()}


@app.get("/kyc/status")
def kyc_status(email: str, password: str):
    user = auth_required(email, password)
    rec = db["kyc"].find_one({"user_id": user.id}, sort=[("created_at", -1)])
    if not rec:
        return {"status": "none"}
    # auto-approve if time passed
    if rec.get("status") == "pending" and rec.get("approve_at") and datetime.now(timezone.utc) >= rec["approve_at"]:
        db["kyc"].update_one({"_id": rec["_id"]}, {"$set": {"status": "approved"}})
        db["user"].update_one({"_id": ObjectId(user.id)}, {"$set": {"account_status": "kyc_verified"}})
        rec = db["kyc"].find_one({"_id": rec["_id"]})
    return {"status": rec.get("status"), "record": {"id": str(rec["_id"]), "approve_at": rec.get("approve_at")}}


# Wallets
@app.get("/wallets")
def list_wallets(email: str, password: str):
    user = auth_required(email, password)
    wallets = list(db["wallet"].find({"user_id": user.id}))
    for w in wallets:
        w["id"] = str(w.pop("_id"))
    return {"wallets": wallets}


# Deposits (PayPal pseudo flow)
class DepositPayload(BaseModel):
    email: str
    password: str
    asset: str
    amount: float


@app.post("/deposits/create")
def create_deposit(payload: DepositPayload):
    user = auth_required(payload.email, payload.password)
    dep = Deposit(user_id=user.id, asset=payload.asset, amount=payload.amount, destination=PAYPAL_EMAIL, status="created")
    dep_id = create_document("deposit", dep)
    # For demo: mark as paid immediately and credit balance
    db["deposit"].update_one({"_id": ObjectId(dep_id)}, {"$set": {"status": "approved"}})
    # credit wallet
    asset = payload.asset
    w = db["wallet"].find_one({"user_id": user.id, "asset": asset})
    if w:
        db["wallet"].update_one({"_id": w["_id"]}, {"$inc": {"balance": payload.amount, "available": payload.amount}})
    # also update account status
    db["user"].update_one({"_id": ObjectId(user.id)}, {"$set": {"account_status": "funded"}})
    return {"deposit_id": dep_id, "destination": PAYPAL_EMAIL, "status": "approved"}


# Withdrawals (admin approval required)
class WithdrawalPayload(BaseModel):
    email: str
    password: str
    asset: str
    amount: float
    destination: str


@app.post("/withdrawals/create")
def create_withdrawal(payload: WithdrawalPayload):
    user = auth_required(payload.email, payload.password)
    w = db["wallet"].find_one({"user_id": user.id, "asset": payload.asset})
    if not w or w.get("available", 0) < payload.amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")
    # lock funds
    db["wallet"].update_one({"_id": w["_id"]}, {"$inc": {"available": -payload.amount}})
    wd = Withdrawal(user_id=user.id, asset=payload.asset, amount=payload.amount, destination=payload.destination)
    wd_id = create_document("withdrawal", wd)
    return {"withdrawal_id": wd_id, "status": "created"}


class AdminApprovePayload(BaseModel):
    email: str
    password: str
    withdrawal_id: str
    approve: bool = True


@app.post("/admin/withdrawals/approve")
def approve_withdrawal(payload: AdminApprovePayload):
    admin = auth_required(payload.email, payload.password)
    if not admin.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        oid = ObjectId(payload.withdrawal_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")
    wd = db["withdrawal"].find_one({"_id": oid})
    if not wd:
        raise HTTPException(status_code=404, detail="Not found")

    if payload.approve:
        # deduct balance and mark approved
        w = db["wallet"].find_one({"user_id": wd["user_id"], "asset": wd["asset"]})
        amount = float(wd["amount"])
        if not w or w.get("balance", 0) < amount:
            raise HTTPException(status_code=400, detail="Insufficient total balance")
        db["wallet"].update_one({"_id": w["_id"]}, {"$inc": {"balance": -amount}})
        db["withdrawal"].update_one({"_id": oid}, {"$set": {"status": "approved"}})
    else:
        # revert lock
        w = db["wallet"].find_one({"user_id": wd["user_id"], "asset": wd["asset"]})
        amount = float(wd["amount"])
        if w:
            db["wallet"].update_one({"_id": w["_id"]}, {"$inc": {"available": amount}})
        db["withdrawal"].update_one({"_id": oid}, {"$set": {"status": "rejected"}})

    return {"ok": True}


# Market orders
class OrderPayload(BaseModel):
    email: str
    password: str
    side: str
    base_asset: str  # BTC/ETH
    amount: float


@app.post("/trade/market")
def market_order(payload: OrderPayload):
    user = auth_required(payload.email, payload.password)
    prices = fetch_prices()
    base = payload.base_asset.upper()
    if base not in ("BTC", "ETH"):
        raise HTTPException(status_code=400, detail="Unsupported market")

    price_usd = prices[base]
    # we trade vs USDT with 1 USDT = 1 USD
    if payload.side == "buy":
        cost = payload.amount * price_usd  # in USDT
        usdt = db["wallet"].find_one({"user_id": user.id, "asset": "USDT"})
        if not usdt or usdt.get("available", 0) < cost:
            raise HTTPException(status_code=400, detail="Insufficient USDT")
        db["wallet"].update_one({"_id": usdt["_id"]}, {"$inc": {"balance": -cost, "available": -cost}})
        base_w = db["wallet"].find_one({"user_id": user.id, "asset": base})
        db["wallet"].update_one({"_id": base_w["_id"]}, {"$inc": {"balance": payload.amount, "available": payload.amount}})
    elif payload.side == "sell":
        base_w = db["wallet"].find_one({"user_id": user.id, "asset": base})
        if not base_w or base_w.get("available", 0) < payload.amount:
            raise HTTPException(status_code=400, detail="Insufficient asset")
        proceeds = payload.amount * price_usd
        db["wallet"].update_one({"_id": base_w["_id"]}, {"$inc": {"balance": -payload.amount, "available": -payload.amount}})
        usdt = db["wallet"].find_one({"user_id": user.id, "asset": "USDT"})
        db["wallet"].update_one({"_id": usdt["_id"]}, {"$inc": {"balance": proceeds, "available": proceeds}})
    else:
        raise HTTPException(status_code=400, detail="Invalid side")

    order = Order(
        user_id=user.id,
        side=payload.side,
        base_asset=base,
        amount=payload.amount,
        executed_price_usd=price_usd,
        executed_value_usdt=payload.amount * price_usd,
    )
    order_id = create_document("order", order)
    return {"order_id": order_id, "price": price_usd}


@app.get("/prices")
def prices():
    return fetch_prices()


# Basic admin creator route for demo
@app.post("/admin/bootstrap")
def bootstrap_admin(email: str, password: str):
    if get_user_by_email(email):
        raise HTTPException(status_code=400, detail="Exists")
    u = User(name="admin", email=email, password_hash=hash_password(password), account_status="kyc_verified", is_admin=True)
    user_id = create_document("user", u)
    for asset in ["BTC", "ETH", "USDT", "USD"]:
        create_document("wallet", Wallet(user_id=user_id, asset=asset if asset != "USD" else "USD", balance=0.0, available=0.0))
    return {"admin_user_id": user_id}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
