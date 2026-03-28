"""Tests for local_db_reader — SQLite reading and row-to-route conversion."""

import os
import sqlite3
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uex_client import RouteData
from local_db_reader import _col, _best_location, _row_to_route, read_routes_from_db


# ── Helper: create an in-memory DB with commodity_route schema ───────────────

def _make_db(tmp_path, rows=None):
    """Create a temp SQLite DB with a commodity_route table and return the path."""
    db_path = os.path.join(tmp_path, "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE commodity_route (
            commodity_name TEXT,
            terminal_origin_name TEXT,
            terminal_origin_star_system_name TEXT,
            terminal_origin_outpost_name TEXT,
            terminal_origin_city_name TEXT,
            terminal_origin_space_station_name TEXT,
            terminal_origin_moon_name TEXT,
            terminal_origin_planet_name TEXT,
            terminal_destination_name TEXT,
            terminal_destination_star_system_name TEXT,
            terminal_destination_outpost_name TEXT,
            terminal_destination_city_name TEXT,
            terminal_destination_space_station_name TEXT,
            terminal_destination_moon_name TEXT,
            terminal_destination_planet_name TEXT,
            price_buy REAL,
            price_sell REAL,
            scu_sell_stock INTEGER,
            scu_buy_stock INTEGER,
            profit_margin REAL,
            score REAL,
            is_profitable INTEGER DEFAULT 1
        )
    """)
    if rows:
        for r in rows:
            conn.execute("""
                INSERT INTO commodity_route (
                    commodity_name, terminal_origin_name, terminal_origin_star_system_name,
                    terminal_origin_outpost_name, terminal_destination_name,
                    terminal_destination_star_system_name, terminal_destination_outpost_name,
                    price_buy, price_sell, scu_sell_stock, scu_buy_stock,
                    profit_margin, score, is_profitable
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, r)
    conn.commit()
    conn.close()
    return db_path


# ── _col tests ───────────────────────────────────────────────────────────────

class TestCol:
    def test_existing_column(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE t (name TEXT)")
            conn.execute("INSERT INTO t VALUES ('hello')")
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM t").fetchone()
            assert _col(row, "name") == "hello"

    def test_missing_column(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE t (name TEXT)")
            conn.execute("INSERT INTO t VALUES ('hello')")
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM t").fetchone()
            assert _col(row, "nonexistent") == ""

    def test_null_column(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE t (name TEXT)")
            conn.execute("INSERT INTO t VALUES (NULL)")
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM t").fetchone()
            assert _col(row, "name") == ""


# ── _best_location tests ────────────────────────────────────────────────────

class TestBestLocationDB:
    def _make_row(self, **kwargs):
        cols = [
            "terminal_origin_outpost_name",
            "terminal_origin_city_name",
            "terminal_origin_space_station_name",
            "terminal_origin_moon_name",
            "terminal_origin_planet_name",
            "terminal_origin_star_system_name",
        ]
        conn = sqlite3.connect(":memory:")
        conn.execute(f"CREATE TABLE t ({', '.join(c + ' TEXT' for c in cols)})")
        vals = [kwargs.get(c, "") for c in cols]
        conn.execute(f"INSERT INTO t VALUES ({','.join('?' for _ in cols)})", vals)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM t").fetchone()
        conn.close()
        return row

    def test_outpost_preferred(self):
        row = self._make_row(
            terminal_origin_outpost_name="Benson Mining",
            terminal_origin_city_name="Lorville",
        )
        assert _best_location(row, "origin") == "Benson Mining"

    def test_city_fallback(self):
        row = self._make_row(terminal_origin_city_name="Area 18")
        assert _best_location(row, "origin") == "Area 18"

    def test_empty_when_all_blank(self):
        row = self._make_row()
        assert _best_location(row, "origin") == ""


# ── _row_to_route tests ─────────────────────────────────────────────────────

class TestRowToRoute:
    def _make_full_row(self, **overrides):
        defaults = {
            "commodity_name": "Laranite",
            "terminal_origin_name": "Admin Office",
            "terminal_origin_star_system_name": "Stanton",
            "terminal_origin_outpost_name": "",
            "terminal_origin_city_name": "Lorville",
            "terminal_origin_space_station_name": "",
            "terminal_origin_moon_name": "",
            "terminal_origin_planet_name": "",
            "terminal_destination_name": "TDD Orison",
            "terminal_destination_star_system_name": "Stanton",
            "terminal_destination_outpost_name": "",
            "terminal_destination_city_name": "Orison",
            "terminal_destination_space_station_name": "",
            "terminal_destination_moon_name": "",
            "terminal_destination_planet_name": "",
            "price_buy": 27.0,
            "price_sell": 35.0,
            "scu_sell_stock": 500,
            "scu_buy_stock": 300,
            "profit_margin": 8.0,
            "score": 42.0,
        }
        defaults.update(overrides)
        cols = list(defaults.keys())
        conn = sqlite3.connect(":memory:")
        conn.execute(f"CREATE TABLE t ({', '.join(c + ' TEXT' for c in cols)})")
        conn.execute(f"INSERT INTO t VALUES ({','.join('?' for _ in cols)})",
                     list(defaults.values()))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM t").fetchone()
        conn.close()
        return row

    def test_valid_route(self):
        row = self._make_full_row()
        rd = _row_to_route(row)
        assert rd is not None
        assert rd.commodity == "Laranite"
        assert rd.buy_terminal == "Admin Office"
        assert rd.sell_terminal == "TDD Orison"
        assert rd.price_buy == 27.0
        assert rd.margin == 8.0

    def test_zero_margin_computed(self):
        row = self._make_full_row(profit_margin=0, price_buy=10.0, price_sell=18.0)
        rd = _row_to_route(row)
        assert rd is not None
        assert rd.margin == 8.0

    def test_margin_pct_computed(self):
        row = self._make_full_row(price_buy=100.0, price_sell=125.0, profit_margin=25.0)
        rd = _row_to_route(row)
        assert rd is not None
        assert abs(rd.margin_pct - 25.0) < 0.01

    def test_no_commodity_returns_none(self):
        row = self._make_full_row(commodity_name="")
        rd = _row_to_route(row)
        assert rd is None

    def test_negative_margin_returns_none(self):
        row = self._make_full_row(profit_margin=-5.0, price_buy=20.0, price_sell=15.0)
        rd = _row_to_route(row)
        assert rd is None


# ── read_routes_from_db tests ────────────────────────────────────────────────

class TestReadRoutesFromDB:
    def test_reads_profitable_routes(self, tmp_path):
        db_path = _make_db(tmp_path, rows=[
            ("Laranite", "Admin", "Stanton", "Benson", "TDD", "Stanton", "",
             27.0, 35.0, 500, 300, 8.0, 42.0, 1),
            ("Scrap", "Junkyard", "Stanton", "", "Recycler", "Stanton", "",
             1.0, 5.0, 1000, 800, 4.0, 10.0, 1),
        ])
        routes = read_routes_from_db(db_path)
        assert len(routes) == 2
        assert routes[0].score >= routes[1].score  # sorted by score DESC

    def test_nonexistent_db_returns_empty(self, tmp_path):
        routes = read_routes_from_db(os.path.join(tmp_path, "nope.db"))
        assert routes == []

    def test_corrupt_db_returns_empty(self, tmp_path):
        bad_path = os.path.join(tmp_path, "bad.db")
        with open(bad_path, "w") as f:
            f.write("not a database")
        routes = read_routes_from_db(bad_path)
        assert routes == []

    def test_empty_table_returns_empty(self, tmp_path):
        db_path = _make_db(tmp_path, rows=[])
        routes = read_routes_from_db(db_path)
        assert routes == []


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
