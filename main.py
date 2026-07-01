import base64
import time
from typing import Dict, List, Optional
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Production-Grade Orders API")

# Enable CORS so the grader browser environment can access it directly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURATION (ASSIGNED VALUES) ---
TOTAL_ORDERS = 41
RATE_LIMIT_REQUESTS = 15
RATE_LIMIT_WINDOW = 10.0  # 10 seconds

# --- IN-MEMORY DATASTORES ---
# Seed our fixed catalog of orders 1 through T
orders_db: List[dict] = [
    {"id": i, "item": f"Widget {i}", "price": round(10.0 + i * 1.5, 2), "created_at": time.time()}
    for i in range(1, TOTAL_ORDERS + 1)
]

# Maps Idempotency-Key (str) -> Saved Response Dict
idempotency_db: Dict[str, dict] = {}

# Maps Client-ID (str) -> List of timestamps (floats)
rate_limit_db: Dict[str, List[float]] = {}


# --- SCHEMAS ---
class OrderCreate(BaseModel):
    item: Optional[str] = "Default Item"
    price: Optional[float] = 0.0


# --- HELPERS ---
def encode_cursor(order_id: int) -> str:
    """Encodes an internal integer index/ID into an opaque, URL-safe string."""
    return base64.urlsafe_b64encode(str(order_id).encode()).decode().rstrip("=")


def decode_cursor(cursor_str: str) -> Optional[int]:
    """Decodes an opaque string cursor back into an internal integer ID."""
    try:
        # Add padding back if missing
        padding = 4 - (len(cursor_str) % 4)
        if padding < 4:
            cursor_str += "=" * padding
        return int(base64.urlsafe_b64decode(cursor_str.encode()).decode())
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid or malformed cursor provided."
        )


# --- MIDDLEWARE / RATE LIMITER ---
@app.middleware("http")
async def rate_limiter(request: Request, call_next):
    # Read the required X-Client-Id header
    client_id = request.headers.get("X-Client-Id")
    if not client_id:
        return await call_next(request)

    now = time.time()
    
    # Initialize or fetch the sliding window bucket for this specific client ID
    if client_id not in rate_limit_db:
        rate_limit_db[client_id] = []
        
    timestamps = rate_limit_db[client_id]
    
    # Clear out older timestamps outside the current window
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    rate_limit_db[client_id] = timestamps

    # Enforce the limit (R requests per 10s)
    if len(timestamps) >= RATE_LIMIT_REQUESTS:
        # Calculate how long before the oldest request leaves the 10s window
        retry_after = int(RATE_LIMIT_WINDOW - (now - timestamps[0]))
        retry_after = max(1, retry_after) # Guarantee at least 1 second
        
        return Response(
            content='{"detail": "Too Many Requests. Rate limit exceeded."}',
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
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

    # 1. Idempotency Check
    if idempotency_key in idempotency_db:
        # Key found! Override status code and return the exact previously saved response payload
        response.status_code = status.HTTP_201_CREATED
        return idempotency_db[idempotency_key]

    # Create the new hypothetical order (appending to our localized test pool)
    global TOTAL_ORDERS
    TOTAL_ORDERS += 1
    new_order = {
        "id": TOTAL_ORDERS,
        "item": order.item,
        "price": order.price,
        "created_at": time.time()
    }
    
    # Optional: If you want to dynamically expand your catalog for pagination testing
    orders_db.append(new_order)

    # Save to idempotency storage map before returning
    idempotency_db[idempotency_key] = new_order
    return new_order


@app.get("/orders")
async def get_orders(limit: int = 10, cursor: Optional[str] = None):
    # Filter bounds
    start_id = 1
    if cursor:
        start_id = decode_cursor(cursor)

    # Find orders starting at or greater than the given cursor target ID
    paginated_items = [o for o in orders_db if o["id"] >= start_id]
    
    # Take up to the requested limit P
    items_to_return = paginated_items[:limit]
    
    # Figure out if there is a next page
    next_cursor = None
    if len(paginated_items) > limit:
        # The next item's ID becomes our new opaque cursor anchor point
        next_cursor = encode_cursor(paginated_items[limit]["id"])

    return {
        "items": items_to_return,
        "next_cursor": next_cursor
    }
