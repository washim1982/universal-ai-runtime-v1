"""Create deterministic demo fixtures: a SQLite sales database and a small workspace."""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    fx = ROOT / "examples" / "fixtures"
    fx.mkdir(parents=True, exist_ok=True)
    db = fx / "sales.db"
    if db.exists():
        db.unlink()
    rng = random.Random(42)
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT NOT NULL, unit_price REAL NOT NULL);
        CREATE TABLE sales (id INTEGER PRIMARY KEY, quarter TEXT NOT NULL, region TEXT NOT NULL,
                            sku TEXT NOT NULL REFERENCES products(sku), units INTEGER NOT NULL);
    """)
    products = [("A100", "Aurora Desk Lamp", 49.0), ("B200", "Boreal Chair", 189.0),
                ("C300", "Cirrus Monitor Arm", 79.0), ("D400", "Delta Standing Desk", 549.0)]
    conn.executemany("INSERT INTO products VALUES (?,?,?)", products)
    rows = []
    for q in ("2026-Q1", "2026-Q2"):
        for region in ("North", "South", "East", "West"):
            for sku, _, _ in products:
                rows.append((q, region, sku, rng.randint(20, 400)))
    conn.executemany("INSERT INTO sales (quarter, region, sku, units) VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()

    ws = ROOT / "examples" / "workspace"
    (ws / "reports").mkdir(parents=True, exist_ok=True)
    (ws / "reports" / ".gitkeep").touch()
    (ws / "docs").mkdir(exist_ok=True)
    (ws / "docs" / "product-faq.md").write_text(
        "# Product FAQ\n\n- The Delta Standing Desk supports 120 kg.\n- The Boreal Chair has a 5-year warranty.\n"
        "- Returns are accepted within 30 days.\n", encoding="utf-8", newline="\n")
    print(f"fixtures written: {db.relative_to(ROOT)}, {ws.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
