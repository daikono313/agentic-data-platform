"""EC 売上ドメインの合成サンプルデータを DuckDB の raw スキーマに生成する。

7W（Who / What / When / Where / How many / Why / How）がすべて揃うように設計している。
乱数シードを固定しているので、同じ引数なら同じデータになる。

    python scripts/generate_sample_data.py --db data/dev.duckdb --orders 30000 --seed 42
"""

from __future__ import annotations

import argparse
import csv
import random
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

START = date(2024, 1, 1)
END = date(2025, 12, 31)

REGIONS = {
    "関東": ["東京都", "神奈川県", "埼玉県", "千葉県"],
    "関西": ["大阪府", "京都府", "兵庫県"],
    "中部": ["愛知県", "静岡県"],
    "九州": ["福岡県"],
    "北海道": ["北海道"],
}
MEMBERSHIP_RANKS = [("bronze", 0.6), ("silver", 0.25), ("gold", 0.12), ("platinum", 0.03)]
CATEGORIES = {
    "食品": (["お菓子", "飲料", "調味料"], (150, 1500)),
    "日用品": (["洗剤", "ティッシュ", "キッチン用品"], (200, 3000)),
    "家電": (["調理家電", "生活家電", "オーディオ"], (3000, 60000)),
    "衣料": (["トップス", "ボトムス", "アウター"], (1500, 20000)),
    "書籍": (["ビジネス", "コミック", "実用書"], (500, 3000)),
}
BRANDS = ["あおば", "さくら", "みなと", "ひかり", "こだま", "つばさ"]
CHANNELS = [("store", 0.55), ("online", 0.30), ("app", 0.15)]
PAYMENT_METHODS = [("credit_card", 0.5), ("cash", 0.2), ("e_money", 0.2), ("bank_transfer", 0.1)]
SHIPPING_METHODS = [("standard", 0.7), ("express", 0.2), ("store_pickup", 0.1)]
RETURN_REASONS = ["初期不良", "サイズ違い", "イメージ違い", "誤配送"]
LAST_NAMES = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤"]
FIRST_NAMES = ["翔", "蓮", "陽菜", "結衣", "大翔", "さくら", "湊", "葵", "悠真", "凛"]


def weighted(rng: random.Random, choices: list[tuple[str, float]]) -> str:
    return rng.choices([c for c, _ in choices], weights=[w for _, w in choices])[0]


def rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


@dataclass
class Tables:
    customers: list[tuple[object, ...]]
    products: list[tuple[object, ...]]
    stores: list[tuple[object, ...]]
    promotions: list[tuple[object, ...]]
    orders: list[tuple[object, ...]]
    order_items: list[tuple[object, ...]]
    payments: list[tuple[object, ...]]
    shipments: list[tuple[object, ...]]
    returns: list[tuple[object, ...]]


def generate(n_orders: int, seed: int) -> Tables:
    rng = random.Random(seed)
    n_customers = max(50, n_orders // 6)
    prefectures = [(r, p) for r, ps in REGIONS.items() for p in ps]

    customers = []
    for i in range(1, n_customers + 1):
        region, pref = rng.choice(prefectures)
        signup = rand_date(rng, date(2020, 1, 1), END)
        customers.append(
            (
                i,
                f"{rng.choice(LAST_NAMES)} {rng.choice(FIRST_NAMES)}",
                rng.choice(["male", "female", "other"]),
                rand_date(rng, date(1955, 1, 1), date(2006, 12, 31)),
                pref,
                region,
                weighted(rng, MEMBERSHIP_RANKS),
                signup,
                datetime.combine(signup, datetime.min.time()),
            )
        )

    products = []
    pid = 0
    for category, (subcats, (lo, hi)) in CATEGORIES.items():
        for sub in subcats:
            for _ in range(rng.randint(6, 12)):
                pid += 1
                price = round(rng.randint(lo, hi), -1)
                products.append(
                    (
                        pid,
                        f"{rng.choice(BRANDS)} {sub} {pid:04d}",
                        category,
                        sub,
                        rng.choice(BRANDS),
                        price,
                        round(price * rng.uniform(0.4, 0.75)),
                        rand_date(rng, date(2019, 1, 1), date(2025, 6, 30)),
                    )
                )

    stores = []
    for i in range(1, 21):
        channel = "store" if i <= 16 else ("online" if i <= 18 else "app")
        region, pref = rng.choice(prefectures)
        name = f"{pref}{i:02d}号店" if channel == "store" else f"{channel.upper()}ストア{i:02d}"
        stores.append(
            (
                i,
                name,
                channel,
                pref if channel == "store" else None,
                region if channel == "store" else None,
                rand_date(rng, date(2015, 1, 1), date(2023, 12, 31)),
            )
        )
    stores_by_channel = {c: [s for s in stores if s[2] == c] for c, _ in CHANNELS}

    promotions = []
    for i in range(1, 25):
        s = rand_date(rng, START, END - timedelta(days=30))
        ptype = rng.choice(["percent_off", "point_up", "bundle"])
        promotions.append(
            (
                i,
                f"キャンペーン{i:02d}",
                ptype,
                round(rng.choice([0.05, 0.1, 0.15, 0.2]), 2) if ptype == "percent_off" else 0.0,
                s,
                s + timedelta(days=rng.randint(7, 30)),
            )
        )

    orders, items, payments, shipments, returns = [], [], [], [], []
    item_id = pay_id = ship_id = ret_id = 0
    for oid in range(1, n_orders + 1):
        cust = rng.choice(customers)
        channel = weighted(rng, CHANNELS)
        store = rng.choice(stores_by_channel[channel])
        d = rand_date(rng, max(START, cust[7]), END)
        ordered_at = datetime.combine(d, datetime.min.time()) + timedelta(
            hours=rng.randint(9, 21), minutes=rng.randint(0, 59)
        )
        active = [p for p in promotions if p[4] <= d <= p[5]]
        promo = rng.choice(active) if active and rng.random() < 0.35 else None
        status = weighted(rng, [("completed", 0.92), ("cancelled", 0.05), ("returned", 0.03)])
        method = weighted(rng, PAYMENT_METHODS)
        if channel != "store" and method == "cash":
            method = "credit_card"
        orders.append(
            (
                oid,
                cust[0],
                store[0],
                ordered_at,
                promo[0] if promo else None,
                status,
                method,
                ordered_at,
            )
        )

        total = 0
        for prod in rng.sample(products, rng.randint(1, 4)):
            item_id += 1
            qty = rng.choices([1, 2, 3, 5], weights=[70, 20, 8, 2])[0]
            unit_price = int(prod[5])
            rate = promo[3] if promo else 0.0
            discount = int(unit_price * qty * float(rate))
            items.append((item_id, oid, prod[0], qty, unit_price, discount))
            total += unit_price * qty - discount
            if status == "returned" and rng.random() < 0.7:
                ret_id += 1
                returns.append(
                    (
                        ret_id,
                        item_id,
                        ordered_at + timedelta(days=rng.randint(3, 20)),
                        rng.randint(1, qty),
                        rng.choice(RETURN_REASONS),
                    )
                )

        if status != "cancelled":
            pay_id += 1
            payments.append(
                (pay_id, oid, method, total, ordered_at + timedelta(minutes=rng.randint(0, 120)))
            )
            if channel != "store":
                ship_id += 1
                smethod = weighted(rng, SHIPPING_METHODS)
                shipped = ordered_at + timedelta(days=rng.randint(0, 3))
                delivered = shipped + timedelta(
                    days=1 if smethod == "express" else rng.randint(1, 5)
                )
                shipments.append((ship_id, oid, smethod, shipped, delivered))

    return Tables(
        customers, products, stores, promotions, orders, items, payments, shipments, returns
    )


DDL = {
    "customers": """customer_id INTEGER, customer_name VARCHAR, gender VARCHAR, birth_date DATE,
        prefecture VARCHAR, region VARCHAR, membership_rank VARCHAR, signup_date DATE,
        updated_at TIMESTAMP""",
    "products": """product_id INTEGER, product_name VARCHAR, category VARCHAR,
        subcategory VARCHAR, brand VARCHAR, list_price INTEGER, unit_cost INTEGER,
        launched_on DATE""",
    "stores": """store_id INTEGER, store_name VARCHAR, channel VARCHAR, prefecture VARCHAR,
        region VARCHAR, opened_on DATE""",
    "promotions": """promotion_id INTEGER, promotion_name VARCHAR, promotion_type VARCHAR,
        discount_rate DOUBLE, start_date DATE, end_date DATE""",
    "orders": """order_id INTEGER, customer_id INTEGER, store_id INTEGER, ordered_at TIMESTAMP,
        promotion_id INTEGER, order_status VARCHAR, payment_method VARCHAR,
        updated_at TIMESTAMP""",
    "order_items": """order_item_id INTEGER, order_id INTEGER, product_id INTEGER,
        quantity INTEGER, unit_price INTEGER, discount_amount INTEGER""",
    "payments": """payment_id INTEGER, order_id INTEGER, payment_method VARCHAR,
        amount INTEGER, paid_at TIMESTAMP""",
    "shipments": """shipment_id INTEGER, order_id INTEGER, shipping_method VARCHAR,
        shipped_at TIMESTAMP, delivered_at TIMESTAMP""",
    "returns": """return_id INTEGER, order_item_id INTEGER, returned_at TIMESTAMP,
        quantity INTEGER, return_reason VARCHAR""",
}


def load(db_path: Path, tables: Tables) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        with tempfile.TemporaryDirectory() as tmp:
            for name, ddl in DDL.items():
                con.execute(f"CREATE OR REPLACE TABLE raw.{name} ({ddl})")
                path = Path(tmp) / f"{name}.csv"
                with path.open("w", newline="") as f:
                    csv.writer(f).writerows(getattr(tables, name))
                con.execute(f"COPY raw.{name} FROM '{path}' (FORMAT csv, HEADER false, NULLSTR '')")
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/dev.duckdb"))
    parser.add_argument("--orders", type=int, default=30000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    tables = generate(args.orders, args.seed)
    load(args.db, tables)
    counts = {k: len(getattr(tables, k)) for k in DDL}
    print(f"generated into {args.db}: {counts}")


if __name__ == "__main__":
    main()
