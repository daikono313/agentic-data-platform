"""EC 売上ドメインの合成サンプルデータを DuckDB の raw スキーマに生成する。

7W（Who / What / When / Where / How many / Why / How）がすべて揃うように設計している。
乱数シードを固定しているので、同じ引数なら同じデータになる。

    python scripts/generate_sample_data.py --db data/dev.duckdb --orders 30000 --seed 42
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

START = date(2024, 1, 1)
END = date(2025, 12, 31)  # 注文日の範囲。配達・返品は END 以降にずれ込むことがある

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
    inventory: list[tuple[object, ...]]
    """在庫の入出庫（実店舗）。後から追加された源泉の例（#69。いまはオントロジーに取り込み済み）。"""
    reviews: list[tuple[object, ...]]
    """商品レビュー（明細の一部にだけある）。オントロジーにも dbt の sources にも無い、
    新しく届いた源泉の例（design-doc の S5。#118）。自由記述の列は持たない。"""


# カテゴリごとの返品しやすさ（明細単位の確率）と返品理由の重み
RETURN_PROFILE: dict[str, tuple[float, list[tuple[str, float]]]] = {
    "衣料": (0.12, [("サイズ違い", 0.6), ("イメージ違い", 0.3), ("誤配送", 0.1)]),
    "家電": (0.06, [("初期不良", 0.7), ("イメージ違い", 0.2), ("誤配送", 0.1)]),
    "日用品": (0.02, [("イメージ違い", 0.5), ("誤配送", 0.5)]),
    "書籍": (0.02, [("誤配送", 0.6), ("イメージ違い", 0.4)]),
    "食品": (0.01, [("誤配送", 0.7), ("イメージ違い", 0.3)]),
}


def _rng(seed: int, table: str) -> random.Random:
    """テーブルごとに独立した乱数列。--orders を変えてもマスタは変わらない。"""
    return random.Random(f"{seed}:{table}")


def _customers(seed: int, n: int) -> list[tuple[object, ...]]:
    rng = _rng(seed, "customers")
    prefectures = [(r, p) for r, ps in REGIONS.items() for p in ps]
    rows = []
    for i in range(1, n + 1):
        region, pref = rng.choice(prefectures)
        signup = rand_date(rng, date(2020, 1, 1), END)
        rows.append(
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
    return rows


def _products(seed: int) -> list[tuple[object, ...]]:
    rng = _rng(seed, "products")
    rows: list[tuple[object, ...]] = []
    for category, (subcats, (lo, hi)) in CATEGORIES.items():
        for sub in subcats:
            for _ in range(rng.randint(6, 12)):
                pid = len(rows) + 1
                price = round(rng.randint(lo, hi), -1)
                # 6 割は期間開始前から販売、残りは期間中に発売
                launched = (
                    rand_date(rng, date(2019, 1, 1), START - timedelta(days=1))
                    if rng.random() < 0.6
                    else rand_date(rng, START, date(2025, 6, 30))
                )
                rows.append(
                    (
                        pid,
                        f"{rng.choice(BRANDS)} {sub} {pid:04d}",
                        category,
                        sub,
                        rng.choice(BRANDS),
                        price,
                        round(price * rng.uniform(0.4, 0.75)),
                        launched,
                    )
                )
    return rows


def _stores(seed: int) -> list[tuple[object, ...]]:
    rng = _rng(seed, "stores")
    rows: list[tuple[object, ...]] = []
    # 実店舗は各地方に最低 1 店舗
    store_prefs = [(r, ps[0]) for r, ps in REGIONS.items()]
    store_prefs += [(r, p) for r, ps in REGIONS.items() for p in ps]
    for i in range(1, 21):
        opened = rand_date(rng, date(2015, 1, 1), date(2023, 12, 31))
        if i <= 16:
            region, pref = store_prefs[(i - 1) % len(store_prefs)]
            rows.append((i, f"{pref}{i:02d}号店", "store", pref, region, opened))
        else:
            channel = "online" if i <= 18 else "app"
            rows.append((i, f"{channel.upper()}ストア{i:02d}", channel, None, None, opened))
    return rows


def _promotions(seed: int) -> list[tuple[object, ...]]:
    rng = _rng(seed, "promotions")
    rows: list[tuple[object, ...]] = []
    for i in range(1, 25):
        s = rand_date(rng, START, END - timedelta(days=30))
        ptype = rng.choice(["percent_off", "point_up", "bundle"])
        # point_up は値引きなし（ポイント付与）、bundle はまとめ買い割引
        rate = {"percent_off": rng.choice([0.05, 0.1, 0.15, 0.2]), "bundle": 0.1}.get(ptype, 0.0)
        rows.append(
            (i, f"キャンペーン{i:02d}", ptype, rate, s, s + timedelta(days=rng.randint(7, 30)))
        )
    return rows


def generate(n_orders: int, seed: int) -> Tables:
    customers = _customers(seed, max(50, n_orders // 6))
    products = _products(seed)
    stores = _stores(seed)
    promotions = _promotions(seed)
    rng = _rng(seed, "orders")
    stores_by_channel = {c: [s for s in stores if s[2] == c] for c, _ in CHANNELS}

    orders, items, payments, shipments, returns = [], [], [], [], []
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
        method = weighted(rng, PAYMENT_METHODS)
        if channel != "store" and method == "cash":
            method = "credit_card"
        cancelled = rng.random() < 0.05

        on_sale = [p for p in products if p[7] <= d]
        lines = []
        for prod in rng.sample(on_sale, min(len(on_sale), rng.randint(1, 4))):
            qty = rng.choices([1, 2, 3, 5], weights=[70, 20, 8, 2])[0]
            unit_price = int(prod[5])
            rate = float(promo[3]) if promo else 0.0
            if promo and promo[2] == "bundle" and qty < 2:
                rate = 0.0
            discount = int(unit_price * qty * rate)
            lines.append((len(items) + len(lines) + 1, oid, prod, qty, unit_price, discount))

        # 受け取り日時（店舗は購入時、配送は配達時）
        received_at = ordered_at
        shipment = None
        if not cancelled and channel != "store":
            smethod = weighted(rng, SHIPPING_METHODS)
            shipped = ordered_at + timedelta(days=rng.randint(0, 3))
            delivered = shipped + timedelta(days=1 if smethod == "express" else rng.randint(1, 5))
            shipment = (len(shipments) + 1, oid, smethod, shipped, delivered)
            received_at = delivered

        # 返品はカテゴリごとの確率で明細単位に決め、1 件でもあれば注文は returned
        order_returns = []
        if not cancelled:
            for line_id, _, prod, qty, _, _ in lines:
                p_return, reasons = RETURN_PROFILE[str(prod[2])]
                if rng.random() < p_return:
                    order_returns.append(
                        (
                            len(returns) + len(order_returns) + 1,
                            line_id,
                            received_at
                            + timedelta(days=rng.randint(1, 14), hours=rng.randint(0, 8)),
                            rng.randint(1, qty),
                            weighted(rng, reasons),
                        )
                    )

        status = "cancelled" if cancelled else ("returned" if order_returns else "completed")
        if cancelled:
            updated_at = ordered_at + timedelta(hours=rng.randint(1, 48))
        elif order_returns:
            updated_at = max(r[2] for r in order_returns)
        else:
            updated_at = received_at
        orders.append(
            (
                oid,
                cust[0],
                store[0],
                ordered_at,
                promo[0] if promo else None,
                status,
                method,
                updated_at,
            )
        )
        items.extend((lid, o, prod[0], q, up, disc) for lid, o, prod, q, up, disc in lines)
        returns.extend(order_returns)
        if shipment:
            shipments.append(shipment)
        if not cancelled:
            total = sum(q * up - disc for _, _, _, q, up, disc in lines)
            payments.append(
                (
                    len(payments) + 1,
                    oid,
                    method,
                    total,
                    ordered_at + timedelta(minutes=rng.randint(0, 120)),
                )
            )

    inventory = _inventory(seed, max(100, n_orders // 3), products, stores)
    reviews = _reviews(seed, orders, items, products)
    return Tables(
        customers,
        products,
        stores,
        promotions,
        orders,
        items,
        payments,
        shipments,
        returns,
        inventory,
        reviews,
    )


# カテゴリごとの評価（1〜5）の重み
RATING_WEIGHTS: dict[str, list[float]] = {
    "食品": [0.03, 0.05, 0.17, 0.40, 0.35],
    "日用品": [0.04, 0.08, 0.28, 0.38, 0.22],
    "家電": [0.08, 0.10, 0.20, 0.32, 0.30],
    "衣料": [0.10, 0.14, 0.26, 0.30, 0.20],
    "書籍": [0.02, 0.05, 0.18, 0.35, 0.40],
}
REVIEW_RATE = 0.12
"""レビューが書かれる明細の割合（キャンセルされた注文の明細には書かれない）。"""


def _reviews(
    seed: int,
    orders: list[tuple[object, ...]],
    items: list[tuple[object, ...]],
    products: list[tuple[object, ...]],
) -> list[tuple[object, ...]]:
    """商品レビュー（review_id, order_item_id, product_id, customer_id, rating, reviewed_at）。"""
    rng = _rng(seed, "reviews")
    order_by_id = {o[0]: o for o in orders}
    category = {p[0]: str(p[2]) for p in products}
    rows: list[tuple[object, ...]] = []
    for item in items:
        order = order_by_id[item[1]]
        if order[5] == "cancelled" or rng.random() >= REVIEW_RATE:
            continue
        ordered_at = order[3]
        assert isinstance(ordered_at, datetime)
        rating = rng.choices([1, 2, 3, 4, 5], weights=RATING_WEIGHTS[category[item[2]]])[0]
        reviewed_at = ordered_at + timedelta(days=rng.randint(2, 30), hours=rng.randint(0, 12))
        rows.append((len(rows) + 1, item[0], item[2], order[1], rating, reviewed_at))
    return rows


MOVEMENT_TYPES = [("receipt", 0.55), ("transfer_out", 0.15), ("disposal", 0.05), ("count", 0.25)]
COUNT_DISCREPANCY = (-3, 3)
"""棚卸で数えた数と帳簿の在庫数のずれ（紛失・数え間違いなど）。"""


def _inventory(
    seed: int, n: int, products: list[tuple[object, ...]], stores: list[tuple[object, ...]]
) -> list[tuple[object, ...]]:
    """実店舗の在庫の入出庫（入庫・他店への移動出庫・廃棄・棚卸）。

    quantity の意味は movement_type で決まる:
    - receipt（入庫）: 増えた数（プラス）
    - transfer_out（移動出庫）・disposal（廃棄）: 減った数（マイナス）
    - count（棚卸）: 数えた在庫数そのもの（実数。0 以上）。増減ではないので、増減と足さない

    店舗 × 商品ごとに、時刻の順に帳簿の在庫数をたどる（期首の在庫は記録の外にある）。在庫が
    0 より少なくならないよう、出庫・廃棄は在庫の範囲にし、在庫が無ければ入庫にする。棚卸は
    帳簿の在庫数に小さなずれを足した数（0 以上）で、棚卸の後は帳簿をその数に合わせる。
    """
    rng = _rng(seed, "inventory")
    shops = [s[0] for s in stores if s[2] == "store"]
    events: list[tuple[datetime, object, object, str]] = []
    for _ in range(n):
        product = rng.choice(products)
        moved_at = datetime.combine(rand_date(rng, START, END), datetime.min.time()) + timedelta(
            hours=rng.randint(7, 21), minutes=rng.choice([0, 15, 30, 45])
        )
        events.append((moved_at, rng.choice(shops), product[0], weighted(rng, MOVEMENT_TYPES)))
    events.sort(key=lambda e: (e[0], str(e[1]), str(e[2])))

    book: dict[tuple[object, object], int] = {}
    rows: list[tuple[object, ...]] = []
    for moved_at, shop, product_id, kind in events:
        key = (shop, product_id)
        if key not in book:
            book[key] = rng.randint(0, 30)  # 期首の在庫
        stock = book[key]
        if kind in ("transfer_out", "disposal") and stock == 0:
            kind = "receipt"  # 在庫が無ければ出せない
        if kind == "receipt":
            qty = rng.randint(5, 60)
            stock += qty
        elif kind == "transfer_out":
            qty = -min(stock, rng.randint(1, 10))
            stock += qty
        elif kind == "disposal":
            qty = -min(stock, rng.randint(1, 5))
            stock += qty
        else:  # count: 数えた在庫数（実数）。帳簿をこれに合わせる
            qty = max(0, stock + rng.randint(*COUNT_DISCREPANCY))
            stock = qty
        book[key] = stock
        rows.append(
            (len(rows) + 1, shop, product_id, moved_at, kind, qty, moved_at + timedelta(hours=1))
        )
    return rows


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
    "inventory": """inventory_id INTEGER, store_id INTEGER, product_id INTEGER,
        moved_at TIMESTAMP, movement_type VARCHAR, quantity INTEGER, updated_at TIMESTAMP""",
    "reviews": """review_id INTEGER, order_item_id INTEGER, product_id INTEGER,
        customer_id INTEGER, rating INTEGER, reviewed_at TIMESTAMP""",
}


def load(db_path: Path, tables: Tables) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        # 属性の履歴の snapshot（dbt の schema「snapshots」。adp の生成器の
        # HISTORY_SNAPSHOT_SCHEMA）は源泉の版なので、源泉を作り直すときに消す（ADR 0009）
        snapshot_schemas = con.execute(
            "SELECT schema_name FROM information_schema.schemata"
            " WHERE schema_name LIKE '%snapshots'"
        ).fetchall()
        for (schema,) in snapshot_schemas:
            con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
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
    parser.add_argument(
        "--db", type=Path, default=Path(os.environ.get("ADP_DUCKDB_PATH", "data/dev.duckdb"))
    )
    parser.add_argument("--orders", type=int, default=30000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    tables = generate(args.orders, args.seed)
    load(args.db, tables)
    counts = {k: len(getattr(tables, k)) for k in DDL}
    print(f"generated into {args.db}: {counts}")


if __name__ == "__main__":
    main()
