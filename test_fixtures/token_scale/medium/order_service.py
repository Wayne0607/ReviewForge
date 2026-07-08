"""Medium module - 3-file token benchmark.
File 2/3: Order service with planted bugs.
"""
import os
import sqlite3
from dataclasses import dataclass


@dataclass
class Order:
    id: int
    user_id: int
    product: str
    amount: float


class OrderService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def create_order(self, user_id: int, product: str, amount: float) -> int:
        self.db.execute(
            "INSERT INTO orders (user_id, product, amount) VALUES (?, ?, ?)",
            (user_id, product, amount),
        )
        self.db.commit()
        return self.db.execute("SELECT last_insert_rowid()").fetchone()[0]

    def search_orders(self, keyword: str) -> list[Order]:
        # BUG: SQL injection via f-string
        rows = self.db.execute(
            f"SELECT * FROM orders WHERE product LIKE '%{keyword}%'"
        ).fetchall()
        return [Order(id=r[0], user_id=r[1], product=r[2], amount=r[3]) for r in rows]

    def export_orders(self, output_path: str) -> None:
        # BUG: command injection via os.system
        os.system(f"pg_dump -t orders > {output_path}")
