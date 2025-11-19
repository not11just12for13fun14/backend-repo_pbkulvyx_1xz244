"""
Database Schemas for Laxo Exchange

Each Pydantic model maps to a MongoDB collection using the lowercase class name
(e.g., User -> "user").
"""
from typing import Optional, Literal, Dict
from pydantic import BaseModel, Field
from datetime import datetime

class User(BaseModel):
    name: str
    email: str
    password_hash: str
    account_status: Literal[
        "new",
        "kyc_pending",
        "kyc_verified",
        "funded"
    ] = "new"
    is_admin: bool = False

class KYC(BaseModel):
    user_id: str
    document_type: Literal["passport", "id_card", "driver_license"]
    document_number: str
    full_name: str
    dob: str
    address: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    notes: Optional[str] = None

class Wallet(BaseModel):
    user_id: str
    asset: Literal["BTC", "ETH", "USDT"]
    balance: float = 0.0
    available: float = 0.0

class Deposit(BaseModel):
    user_id: str
    asset: Literal["BTC", "ETH", "USDT", "USD"]
    amount: float
    method: Literal["paypal"] = "paypal"
    destination: str = Field(..., description="Destination account/email for deposit")
    status: Literal["created", "marked_paid", "approved", "rejected"] = "created"

class Withdrawal(BaseModel):
    user_id: str
    asset: Literal["BTC", "ETH", "USDT", "USD"]
    amount: float
    destination: str
    status: Literal["created", "approved", "rejected"] = "created"

class Order(BaseModel):
    user_id: str
    side: Literal["buy", "sell"]
    base_asset: Literal["BTC", "ETH"]
    quote_asset: Literal["USDT"] = "USDT"
    amount: float = Field(..., gt=0, description="Amount of base_asset to buy/sell for market order")
    executed_price_usd: Optional[float] = None
    executed_value_usdt: Optional[float] = None

class PriceSnapshot(BaseModel):
    timestamp: datetime
    prices: Dict[str, float]
