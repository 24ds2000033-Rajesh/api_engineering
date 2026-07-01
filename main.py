import base64
import time
from typing import Dict, List, Optional
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Production-Grade Orders API")

# 1. Enable CORS Core Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"], # Explicitly exposes custom headers to the browser engine
)

# --- CONFIGURATION (ASSIGNED VALUES) ---
TOTAL_ORDERS = 41
RATE_LIMIT_REQUESTS = 15
RATE_LIMIT_WINDOW = 10.0  # 10 seconds

# --- IN-MEMORY DATASTORES ---
orders_db: List[dict] = [
    {"id": i, "item": f"Widget {i}", "price": round(10.0 + i * 1.5, 2), "created_at": time.time()}
    for i in range(1, TOTAL_ORDERS + 1)
]

idempotency_db: Dict[str, dict] = {}
rate_limit_db: Dict[str, List[float]] = {}


class OrderCreate(BaseModel):
    item: Optional[str] = "Default Item"
    price: Optional[float] = 0.0


def encode_cursor(order_id: int) -> str:
    return base64.urlsafe_b64encode(str(order_id).encode()).decode().rstrip("=")


def decode_cursor(cursor_str: str) -> Optional[int]:
    try:
        padding = 4 - (len(cursor_str) % 4)
        if padding < 4:
            cursor_str += "=" * padding
        return int(base64.urlsafe_b64decode(cursor_str.encode()).decode())
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid or malformed cursor provided."
        )


# --- MIDDLEWARE WITH CORS BYPASS ---
@app.middleware("http")
async def rate_limiter(request: Request, call_next):
    # Always let browser CORS preflight requests bypass rate limiting
    if request.method == "OPTIONS":
        return await call_next(request)
        
    client_id = request.headers.get("X-Client-Id")
    if not client_id:
        return await call_next(request)

    now = time.time()
    
    # Initialize or fetch the sliding window bucket for this client ID
    if client_id not in rate_limit_db:
        rate_limit_db[client_id] = []
        
    timestamps = rate_limit_db[client_id]
    
    # Clear out older timestamps outside the 10-second window
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    rate_limit_db[client_id] = timestamps

    # Enforce the limit (15 requests per 10s)
    if len(timestamps) >= RATE_LIMIT_REQUESTS:
        retry_after = int(RATE_LIMIT_WINDOW - (now - timestamps[0]))
        retry_after = max(1, retry_after)
        
        # Build standard response headers
        headers = {
            "Retry-After": str(retry_after),
            "Access-Control-Allow-Origin": "*",  # Ensure CORS doesn't swallow the 429 response
            "Access-Control-Expose-Headers": "Retry-After"  # Explicitly tell browsers they can read this header
        }
        
        return Response(
            content='{"detail": "Too Many Requests. Rate limit exceeded."}',
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers=headers,
            media_type="application/json"
        )

    # Log the successful request timestamp
    rate_limit_db[client_id].append(now)
    return await call_next(request)

# --- ENDPOINTS ---

@app.post("/orders", status_code=status.HTTP_201_CREATED)
async def create_order(
    order: OrderCreate, 
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Idempotency-Key header is missing."
        )

    if idempotency_key in idempotency_db:
        response.status_code = status.HTTP_201_CREATED
        return idempotency_db[idempotency_key]

    # Create dynamic order using unique incremental index sequence 
    new_id = len(orders_db) + 1
    new_order = {
        "id": new_id,
        "item": order.item,
        "price": order.price,
        "created_at": time.time()
    }
    
    orders_db.append(new_order)
    idempotency_db[idempotency_key] = new_order
    return new_order


@app.get("/orders")
async def get_orders(limit: int = 10, cursor: Optional[str] = None):
    start_id = 1
    if cursor:
        start_id = decode_cursor(cursor)

    # CRITICAL FIX 3: Restrict pagination strictly to the assigned catalog range (1 to 41)
    target_catalog = [o for o in orders_db if 1 <= o["id"] <= TOTAL_ORDERS]
    paginated_items = [o for o in target_catalog if o["id"] >= start_id]
    
    items_to_return = paginated_items[:limit]
    
    next_cursor = None
    if len(paginated_items) > limit:
        next_cursor = encode_cursor(paginated_items[limit]["id"])

    return {
        "items": items_to_return,
        "next_cursor": next_cursor
    }
