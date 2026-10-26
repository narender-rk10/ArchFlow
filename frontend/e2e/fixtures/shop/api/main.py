"""Shop API: orders and products."""
import os

import redis
from fastapi import FastAPI
from sqlalchemy import create_engine, text

app = FastAPI()
engine = create_engine(os.environ["DATABASE_URL"])
cache = redis.Redis.from_url(os.environ["REDIS_URL"])


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    cached = cache.get(f"order:{order_id}")
    if cached:
        return {"id": order_id, "cached": True}
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id, total FROM orders WHERE id = :id"), {"id": order_id}).first()
    return {"id": row.id, "total": row.total}


@app.post("/orders")
def create_order(total: float):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO orders (total) VALUES (:t)"), {"t": total})
    return {"ok": True}
