from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from collections import defaultdict, deque
import time
import uuid
import base64

app = FastAPI()

# ----------------------------------------------------
# CORS
# ----------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------
# Constants
# ----------------------------------------------------
TOTAL_ORDERS = 41
RATE_LIMIT = 15
WINDOW = 10  # seconds

# ----------------------------------------------------
# Fixed catalog for pagination
# ----------------------------------------------------
catalog = [
    {
        "id": i,
        "item": f"Product {i}",
        "price": i * 10,
    }
    for i in range(1, TOTAL_ORDERS + 1)
]

# ----------------------------------------------------
# In-memory stores
# ----------------------------------------------------
idempotency_store = {}
client_requests = defaultdict(deque)


class OrderRequest(BaseModel):
    item: Optional[str] = "Widget"
    quantity: Optional[int] = 1


# ----------------------------------------------------
# Cursor helpers
# ----------------------------------------------------
def encode_cursor(index: int):
    return base64.urlsafe_b64encode(str(index).encode()).decode()


def decode_cursor(cursor: Optional[str]):
    if not cursor:
        return 0
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        return 0


# ----------------------------------------------------
# Rate limiting
# ----------------------------------------------------
@app.middleware("http")
async def rate_limit(request, call_next):
    client = request.headers.get("X-Client-Id", "anonymous")

    now = time.time()
    bucket = client_requests[client]

    while bucket and now - bucket[0] >= WINDOW:
        bucket.popleft()

    if len(bucket) >= RATE_LIMIT:
        retry_after = max(1, int(WINDOW - (now - bucket[0])))
        return Response(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    bucket.append(now)
    return await call_next(request)


# ----------------------------------------------------
# Idempotent order creation
# ----------------------------------------------------
@app.post("/orders", status_code=201)
def create_order(
    order: OrderRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    if idempotency_key in idempotency_store:
        return idempotency_store[idempotency_key]

    order_obj = {
        "id": str(uuid.uuid4()),
        "item": order.item,
        "quantity": order.quantity,
    }

    idempotency_store[idempotency_key] = order_obj
    return order_obj


# ----------------------------------------------------
# Cursor pagination
# ----------------------------------------------------
@app.get("/orders")
def list_orders(limit: int = 10, cursor: Optional[str] = None):
    start = decode_cursor(cursor)
    end = min(start + limit, TOTAL_ORDERS)

    items = catalog[start:end]

    next_cursor = None
    if end < TOTAL_ORDERS:
        next_cursor = encode_cursor(end)

    return {
        "items": items,
        "next_cursor": next_cursor,
    }


@app.get("/")
def root():
    return {"status": "ok"}
