"""Shared fixtures: a small, fully controlled migration plus the shipped sample."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from migkit.db import connect
from migkit.mapping import MappingSpec, load_mapping

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_MAPPING = ROOT / "examples" / "legacy_crm" / "mapping.yaml"

CUSTOMERS_CSV = """\
Cust No,Company,Country,Revenue,Since,Active
C-1,Halcyra Inc,USA,"$1,000.00",2020-01-15,Y
C-2,Wrenfield,UK,500,15-Jan-2021,N
C-3,HALCYRA Inc.,United States,"1,000",2020-01-15,yes
C-4,,USA,10,2022-03-01,Y
C-5,Ostmere,Valdorra,20,2022-03-01,Y
"""

ORDERS_CSV = """\
Order,Customer,Amount,Currency,Placed
O-1,C-1,100.50,usd,2024-01-01
O-2,C-3,20,USD,2024-01-02
O-3,C-2,"£7.25",GBP,2024-01-03
O-4,C-9,5,USD,2024-01-04
O-5,C-2,oops,GBP,2024-01-05
"""


def mini_mapping_data() -> dict[str, Any]:
    """Customers with a fuzzy duplicate (C-3 of C-1), a blank name and an unknown country;
    orders with an orphan (C-9), an unparseable amount and a child of the duplicate."""
    return {
        "name": "mini",
        "sources": {
            "customers": {"path": "customers.csv"},
            "orders": {"path": "orders.csv"},
        },
        "lookups": {
            "countries": {
                "values": {"usa": "US", "united states": "US", "uk": "GB"},
            }
        },
        "targets": [
            {
                "table": "customers",
                "source": "customers",
                "key": "id",
                "fields": [
                    {"name": "id", "from": "Cust No"},
                    {"name": "name", "from": "Company", "required": True},
                    {"name": "country", "from": "Country", "lookup": {"table": "countries"}},
                    {
                        "name": "revenue",
                        "from": "Revenue",
                        "type": "decimal",
                        "reconcile_sum": True,
                    },
                    {"name": "since", "from": "Since", "type": "date"},
                    {"name": "active", "from": "Active", "type": "boolean"},
                ],
                "dedupe": {
                    "block_on": ["since"],
                    "threshold": 90,
                    "fields": [
                        {"name": "name", "weight": 2, "strip_suffixes": True},
                        {"name": "country", "method": "exact"},
                    ],
                },
            },
            {
                "table": "orders",
                "source": "orders",
                "key": "order_id",
                "reconcile_by": "currency",
                "fields": [
                    {"name": "order_id", "from": "Order"},
                    {
                        "name": "customer_id",
                        "from": "Customer",
                        "references": "customers.id",
                        "required": True,
                    },
                    {
                        "name": "amount",
                        "from": "Amount",
                        "type": "decimal",
                        "reconcile_sum": True,
                    },
                    {"name": "currency", "from": "Currency", "transforms": ["upper"]},
                    {"name": "placed_on", "from": "Placed", "type": "date"},
                ],
            },
        ],
    }


@pytest.fixture
def write_files(tmp_path: Path) -> Callable[..., Path]:
    """Write the mini migration (optionally with changed data) and return the mapping path."""

    def _write(
        data: dict[str, Any] | None = None,
        customers: str = CUSTOMERS_CSV,
        orders: str = ORDERS_CSV,
    ) -> Path:
        (tmp_path / "customers.csv").write_text(customers, encoding="utf-8")
        (tmp_path / "orders.csv").write_text(orders, encoding="utf-8")
        path = tmp_path / "mapping.yaml"
        path.write_text(yaml.safe_dump(data or mini_mapping_data()), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def mini_spec(write_files: Callable[..., Path]) -> MappingSpec:
    return load_mapping(write_files())


@pytest.fixture
def sample_spec() -> MappingSpec:
    return load_mapping(SAMPLE_MAPPING)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'target.db').as_posix()}"


@pytest.fixture
def engine(db_url: str):
    eng = connect(db_url)
    yield eng
    eng.dispose()
