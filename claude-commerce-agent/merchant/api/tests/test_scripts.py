# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Offline checks for ``scripts/seed_store.py`` and ``scripts/smoke_live.py``. The seeder is
run against an empty ``LocalStore`` and must reproduce the fixture catalog; the smoke
script's reads and its ``--read-only`` gate pass must leave ``applied`` empty. The scripts
directory is not a package, so it is put on ``sys.path`` and the files imported by name."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest
from merchant_agent import ChangeStatus

from merchant.api.local_store import TRASHED_SUFFIX, LocalStore

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

FIXTURE_SLUGS = [
    "canvas-tool-apron",
    "folding-step-stool",
    "bench-dog-set",
    "shop-stool-cushion",
    "layout-square",
    "sawdust-broom",
    "cast-iron-hold-down",
    "discontinued-mallet",
]


def import_script(name: str) -> ModuleType:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(name)


@pytest.fixture(scope="module")
def seeder() -> ModuleType:
    return import_script("seed_store")


@pytest.fixture(scope="module")
def smoke() -> ModuleType:
    return import_script("smoke_live")


@pytest.fixture
def seed_spec(seeder):
    return seeder.load_seed()


def by_slug(store: LocalStore, slug: str) -> dict:
    """Trashing renames the slug, so a product is found by the name the seed file uses."""
    return next(row for row in store.products if row["slug"].removesuffix(TRASHED_SUFFIX) == slug)


# -- seed_store -------------------------------------------------------------------------


def test_seed_file_lists_the_fixture_catalog(seed_spec) -> None:
    assert [entry["slug"] for entry in seed_spec["products"]] == FIXTURE_SLUGS
    status = {entry["slug"]: entry.get("status") for entry in seed_spec["products"]}
    assert status["shop-stool-cushion"] == "draft"
    assert status["discontinued-mallet"] == "trash"


async def test_seeder_fills_an_empty_store_from_the_fixture(seeder, seed_spec) -> None:
    store = LocalStore.empty()
    outcome = await seeder.Seeder(store, seed_spec, images=False).run()

    assert set(outcome.products) == set(FIXTURE_SLUGS)
    assert set(outcome.variations) == {"ACME-SS-2", "ACME-SS-3"}
    assert len(outcome.orders) == len(seed_spec["orders"])
    assert by_slug(store, "discontinued-mallet")["status"] == "trash"
    assert by_slug(store, "sawdust-broom")["manage_stock"] is False
    stool_id = by_slug(store, "folding-step-stool")["id"]
    assert [row["regular_price"] for row in store.variations[stool_id]] == ["60.00", "90.00"]
    refunds = [body for _, path, body in store.applied if path.endswith("/refunds")]
    assert len(refunds) == 2
    assert all(body["api_refund"] is False for body in refunds)


async def test_second_run_skips_slugs_already_present(seeder, seed_spec) -> None:
    store = LocalStore.empty()
    first = await seeder.Seeder(store, seed_spec, images=False, orders=False).run()
    again = await seeder.Seeder(store, seed_spec, images=False, orders=False).run()
    assert again.products == first.products
    assert len(store.products) == len(FIXTURE_SLUGS)
    creates = [path for verb, path, _ in store.applied if (verb, path) == ("POST", "products")]
    assert len(creates) == len(FIXTURE_SLUGS)


async def test_second_run_matches_the_trashed_product_despite_its_renamed_slug(
    seeder, seed_spec
) -> None:
    """WordPress appends ``__trashed`` to a trashed post's slug. Matching on the raw slug
    makes the fixture's trashed product look absent, and creating it again collides on its
    SKU, which is a hard failure rather than a duplicate."""
    store = LocalStore.empty()
    await seeder.Seeder(store, seed_spec, images=False, orders=False).run()
    trashed = by_slug(store, "discontinued-mallet")
    assert trashed["slug"].endswith(TRASHED_SUFFIX)

    again = await seeder.Seeder(store, seed_spec, images=False, orders=False).run()

    assert again.products["discontinued-mallet"] == trashed["id"]
    assert len(store.products) == len(FIXTURE_SLUGS)


async def test_second_run_places_no_orders_and_writes_nothing(seeder, seed_spec) -> None:
    """Re-running setup.sh must not double the store's revenue: every seeded order carries
    its ref, and a ref already on the site is not placed again."""
    store = LocalStore.empty()
    first = await seeder.Seeder(store, seed_spec, images=False).run()
    assert len(first.orders) == len(seed_spec["orders"])
    settled = len(store.orders)

    store.applied.clear()
    again = await seeder.Seeder(store, seed_spec, images=False).run()

    assert again.orders == []
    assert again.products == first.products
    assert len(store.orders) == settled
    assert [verb for verb, _, _ in store.applied if verb != "GET"] == []


def test_every_seed_order_carries_a_unique_ref(seed_spec) -> None:
    refs = [entry["ref"] for entry in seed_spec["orders"]]
    assert len(set(refs)) == len(refs)


async def test_dry_run_transport_records_and_sends_nothing(seeder, seed_spec) -> None:
    transport = seeder.RecordingTransport()
    await seeder.Seeder(transport, seed_spec, images=False).run()
    posted = [path for verb, path, _ in transport.requests if verb == "POST"]
    assert posted.count("wc/v3/products") == len(seed_spec["products"])
    assert posted.count("wc/v3/orders") == len(seed_spec["orders"])


def test_dry_run_flag_exits_cleanly(seeder, capsys) -> None:
    assert seeder.main(["--dry-run", "--no-images"]) == 0
    assert "POST" in capsys.readouterr().out


# -- smoke_live -------------------------------------------------------------------------


async def test_smoke_reads_pass_and_only_campaigns_are_skipped(
    smoke, backend, config, session, store
) -> None:
    await backend.warm()
    run = smoke.SmokeRun(backend, config, session, read_only=True)
    await run.reads()
    assert run.checks.failed == []
    assert run.checks.skipped == ["get_campaign_performance"]
    assert store.applied == []


async def test_read_only_gate_pass_discards_instead_of_writing(
    smoke, backend, config, session, store
) -> None:
    await backend.warm()
    run = smoke.SmokeRun(backend, config, session, read_only=True)
    await run.gate()
    assert run.checks.failed == []
    assert store.applied == []
    assert backend.ledger.pending() == []
    assert [change.status for change in backend.ledger.resolved()] == [ChangeStatus.DISCARDED]
