# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""The write side. Staging any change must leave ``store.applied`` empty; applying one must
send exactly the ``wc/v3`` writes the preview described, with the right verb, path, and
body; a refused write must leave the change staged so it can be approved again; and a change
that was partly written must report what landed and be discarded."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from merchant_agent import (
    ActorKind,
    CampaignDraft,
    ChangeStatus,
    InventoryActionItem,
    PriceUpdateItem,
    PromotionDraft,
    StagedChange,
)
from merchant_agent.changes import ChangeNotApplicable, GuardrailViolation

from merchant.api.local_store import LocalStore

from .conftest import (
    APRON,
    BENCH_DOGS,
    BROOM,
    HOLD_DOWN,
    OPERATOR,
    STOOL,
    STOOL_THREE_STEP,
    STOOL_TWO_STEP,
)

# -- Builders ---------------------------------------------------------------------------


def reprice(listing_id: str, price: float) -> list[PriceUpdateItem]:
    return [PriceUpdateItem(listing_id=listing_id, new_price=price)]


def restock(listing_id: str, quantity: int) -> list[InventoryActionItem]:
    return [InventoryActionItem(listing_id=listing_id, action="restock", quantity=quantity)]


def switch(listing_id: str, action: str) -> list[InventoryActionItem]:
    return [InventoryActionItem(listing_id=listing_id, action=action)]


def sale(name: str, listing_ids: list[str], pct: float) -> PromotionDraft:
    return PromotionDraft(
        name=name, listing_ids=listing_ids, discount_pct=pct, starts="2026-10-01", ends="2026-10-14"
    )


def puts(store: LocalStore) -> list[str]:
    """Paths of every PUT the store has received, in order."""
    return [path for verb, path, _ in store.applied if verb == "PUT"]


def status_of(backend, change: StagedChange) -> ChangeStatus:
    return backend.ledger.get(change.change_id).status


# -- Staging sends nothing --------------------------------------------------------------


async def test_every_stage_tool_leaves_the_store_untouched(backend, session, store) -> None:
    await backend.stage_price_update(session, reprice(APRON, 44.0))
    await backend.stage_listing_update(session, BENCH_DOGS, {"short_description": "Four dogs."})
    await backend.stage_inventory_action(session, restock(BENCH_DOGS, 20))
    await backend.stage_inventory_action(session, switch(APRON, "pause"))
    await backend.stage_promotion(session, sale("Autumn", [STOOL], 10))
    assert len(backend.ledger.pending()) == 5
    assert store.applied == []


Stager = Callable[[Any, Any], Awaitable[Any]]

REFUSED_AT_STAGE: list[tuple[str, Stager, type[Exception], str]] = [
    (
        "single price on a variable parent",
        lambda b, s: b.stage_price_update(s, reprice(STOOL, 70.0)),
        ChangeNotApplicable,
        f"price each variation by its own id: {STOOL_TWO_STEP}, {STOOL_THREE_STEP}",
    ),
    (
        "price move over the delta cap",
        lambda b, s: b.stage_price_update(s, reprice(APRON, 90.0)),
        GuardrailViolation,
        "20%",
    ),
    (
        "field this deployment does not write",
        lambda b, s: b.stage_listing_update(s, APRON, {"seo_title": "Aprons"}),
        ChangeNotApplicable,
        "does not write seo_title",
    ),
    (
        "price smuggled into a listing update",
        lambda b, s: b.stage_listing_update(s, APRON, {"price": 41.0}),
        GuardrailViolation,
        "price update",
    ),
    (
        "content edit aimed at a variation",
        lambda b, s: b.stage_listing_update(s, STOOL_TWO_STEP, {"title": "Two-step stool"}),
        ChangeNotApplicable,
        f"stage the edit against {STOOL}",
    ),
    (
        "restock of a product without stock management",
        lambda b, s: b.stage_inventory_action(s, restock(BROOM, 5)),
        ChangeNotApplicable,
        "does not track inventory",
    ),
    (
        "restock of a variable parent",
        lambda b, s: b.stage_inventory_action(s, restock(STOOL, 5)),
        ChangeNotApplicable,
        f"{STOOL_TWO_STEP}, {STOOL_THREE_STEP}",
    ),
    (
        "discount over the promotion cap",
        lambda b, s: b.stage_promotion(s, sale("Clearance", [APRON], 60)),
        GuardrailViolation,
        "promotion limit",
    ),
    (
        "negative discount",
        lambda b, s: b.stage_promotion(s, sale("Surge", [APRON], -10)),
        ChangeNotApplicable,
        "Stage a price update instead",
    ),
]


@pytest.mark.parametrize(
    ("stager", "error", "message"),
    [case[1:] for case in REFUSED_AT_STAGE],
    ids=[case[0] for case in REFUSED_AT_STAGE],
)
async def test_refusals_happen_at_stage_time(
    backend, session, store, stager: Stager, error, message
) -> None:
    """Each of these would otherwise produce a preview card the operator could approve
    and then watch fail. They are refused before anything reaches the ledger."""
    with pytest.raises(error, match=message):
        await stager(backend, session)
    assert backend.ledger.pending() == []
    assert store.applied == []


# -- regular_price ----------------------------------------------------------------------


async def test_price_update_previews_margin_and_writes_regular_price(
    backend, session, store
) -> None:
    change = await backend.stage_price_update(session, reprice(APRON, 44.0))
    (item,) = change.items
    assert (item.before, item.after) == (40.0, 44.0)
    assert (change.margin_before_pct, change.margin_after_pct) == (75.0, 77.3)
    assert change.currency == "USD"

    applied = await backend.apply_change(session, change.change_id)

    assert applied.status is ChangeStatus.APPLIED
    assert applied.applied_by == OPERATOR
    assert store.applied == [("PUT", f"products/{APRON}", {"regular_price": "44.00"})]
    assert (await backend.get_listing(session, APRON)).price == 44.0


async def test_variation_price_targets_the_variations_route(backend, session, store) -> None:
    change = await backend.stage_price_update(session, reprice(STOOL_TWO_STEP, 66.0))
    await backend.apply_change(session, change.change_id)
    assert puts(store) == [f"products/{STOOL}/variations/{STOOL_TWO_STEP}"]
    stool = await backend.get_listing(session, STOOL)
    # The sibling rung keeps its own price.
    assert [v.price for v in stool.variants] == [66.0, 90.0]


async def test_tightened_config_blocks_the_apply(backend, session, config, store) -> None:
    change = await backend.stage_price_update(session, reprice(APRON, 46.0))
    config.max_price_delta_pct = 5.0  # policy changed between staging and approval
    with pytest.raises(GuardrailViolation):
        await backend.apply_change(session, change.change_id)
    assert store.applied == []
    assert status_of(backend, change) is ChangeStatus.STAGED


async def test_refused_write_leaves_the_change_staged(backend, session, store) -> None:
    change = await backend.stage_price_update(session, reprice(APRON, 44.0))
    store.products = [row for row in store.products if row["id"] != int(APRON)]  # deleted meanwhile
    with pytest.raises(ChangeNotApplicable, match="still staged"):
        await backend.apply_change(session, change.change_id)
    assert status_of(backend, change) is ChangeStatus.STAGED


async def test_price_refuses_when_the_live_price_moved(backend, session, store) -> None:
    """An approved rise becomes a cut if the shop has moved further in the meantime."""
    change = await backend.stage_price_update(session, reprice(APRON, 44.0))
    row = next(row for row in store.products if row["id"] == int(APRON))
    row["regular_price"] = "52.00"  # repriced on the site since staging
    with pytest.raises(ChangeNotApplicable, match="moved since the change was staged"):
        await backend.apply_change(session, change.change_id)
    assert puts(store) == []
    assert status_of(backend, change) is ChangeStatus.STAGED


async def test_a_partly_written_change_reports_what_landed_and_is_discarded(
    backend, session, store
) -> None:
    """A change over two products where the second is refused. The operator has to be told
    which product was already repriced, and the change must not stay approvable: replaying
    it would now be refused on the target it did write, since that value has moved."""
    change = await backend.stage_price_update(session, reprice(APRON, 44.0) + reprice(BROOM, 20.0))
    row = next(row for row in store.products if row["id"] == int(BROOM))
    row["regular_price"] = "19.00"  # repriced on the site since staging

    with pytest.raises(ChangeNotApplicable) as raised:
        await backend.apply_change(session, change.change_id)

    message = str(raised.value)
    assert "moved since the change was staged" in message
    assert "Already updated: " in message
    assert "That write did not reach the store" in message
    assert puts(store) == [f"products/{APRON}"]
    assert status_of(backend, change) is ChangeStatus.DISCARDED


async def test_a_server_error_discards_because_the_write_may_have_landed(
    backend, session, store, monkeypatch
) -> None:
    """A 5xx says nothing about what WooCommerce did, so the change cannot be replayed and
    the operator is told the outcome is unknown rather than that nothing happened."""
    from merchant.api.rest_client import WooApiError

    change = await backend.stage_price_update(session, reprice(APRON, 44.0))
    original = store.request

    async def fail(method, path, **kwargs):
        if method == "PUT":
            raise WooApiError("upstream exploded", code="", status=502)
        return await original(method, path, **kwargs)

    monkeypatch.setattr(store, "request", fail)

    with pytest.raises(ChangeNotApplicable) as raised:
        await backend.apply_change(session, change.change_id)

    assert "may still have reached the store" in str(raised.value)
    assert status_of(backend, change) is ChangeStatus.DISCARDED


# -- Content fields ---------------------------------------------------------------------


async def test_listing_update_writes_text_and_resolves_the_category_id(
    backend, session, store
) -> None:
    edit = {
        "short_description": "Four machined bench dogs for a 19 mm dog hole.",
        "category": "Storage",
    }
    change = await backend.stage_listing_update(session, BENCH_DOGS, edit)
    await backend.apply_change(session, change.change_id)
    verb, path, body = store.applied[-1]
    assert (verb, path) == ("PUT", f"products/{BENCH_DOGS}")
    assert body["short_description"] == edit["short_description"]
    assert body["categories"] == [{"id": 12}]
    assert (await backend.get_listing(session, BENCH_DOGS)).category == "Storage"


async def test_listing_update_refuses_when_the_live_text_moved(backend, session, store) -> None:
    change = await backend.stage_listing_update(
        session, BENCH_DOGS, {"short_description": "Four machined bench dogs."}
    )
    row = next(row for row in store.products if row["id"] == int(BENCH_DOGS))
    row["short_description"] = "Edited by someone else."
    with pytest.raises(ChangeNotApplicable, match="moved since the change was staged"):
        await backend.apply_change(session, change.change_id)
    assert puts(store) == []
    assert status_of(backend, change) is ChangeStatus.STAGED


# -- stock_quantity and status ----------------------------------------------------------


async def test_restock_writes_the_new_absolute_quantity(backend, session, store) -> None:
    change = await backend.stage_inventory_action(session, restock(BENCH_DOGS, 24))
    (item,) = change.items
    assert (item.before, item.after) == (3, 27)
    await backend.apply_change(session, change.change_id)
    assert store.applied[-1] == (
        "PUT",
        f"products/{BENCH_DOGS}",
        {"manage_stock": True, "stock_quantity": 27},
    )


async def test_restock_refuses_when_the_live_count_moved(backend, session, store) -> None:
    change = await backend.stage_inventory_action(session, restock(BENCH_DOGS, 24))
    row = next(row for row in store.products if row["id"] == int(BENCH_DOGS))
    row["stock_quantity"] = 1  # two units sold since staging
    with pytest.raises(ChangeNotApplicable, match="moved since the change was staged"):
        await backend.apply_change(session, change.change_id)
    assert puts(store) == []
    assert status_of(backend, change) is ChangeStatus.STAGED


async def test_pause_and_activate_toggle_the_post_status(backend, session, store) -> None:
    paused = await backend.stage_inventory_action(session, switch(HOLD_DOWN, "pause"))
    assert "draft" in paused.guardrail_notes[0]
    await backend.apply_change(session, paused.change_id)
    assert store.applied[-1] == ("PUT", f"products/{HOLD_DOWN}", {"status": "draft"})
    assert (await backend.get_listing(session, HOLD_DOWN)).status == "paused"

    active = await backend.stage_inventory_action(session, switch(HOLD_DOWN, "activate"))
    await backend.apply_change(session, active.change_id)
    assert store.applied[-1] == ("PUT", f"products/{HOLD_DOWN}", {"status": "publish"})
    assert (await backend.get_listing(session, HOLD_DOWN)).status == "active"


# -- sale_price with a window -----------------------------------------------------------


async def test_promotion_expands_variations_and_schedules_the_sale(backend, session, store) -> None:
    change = await backend.stage_promotion(session, sale("Autumn", [STOOL, APRON], 15))
    assert [(i.target, i.field, i.after) for i in change.items] == [
        (STOOL_TWO_STEP, "sale_price", 51.0),
        (STOOL_THREE_STEP, "sale_price", 76.5),
        (APRON, "sale_price", 34.0),
    ]
    assert store.applied == []

    applied = await backend.apply_change(session, change.change_id)

    assert puts(store) == [
        f"products/{STOOL}/variations/{STOOL_TWO_STEP}",
        f"products/{STOOL}/variations/{STOOL_THREE_STEP}",
        f"products/{APRON}",
    ]
    assert store.applied[-1][2] == {
        "sale_price": "34.00",
        "date_on_sale_from": "2026-10-01T00:00:00",
        "date_on_sale_to": "2026-10-14T23:59:59",
    }
    assert any("scheduled sale price" in note for note in applied.guardrail_notes)


async def test_promotion_refuses_when_the_full_price_moved(backend, session, store) -> None:
    """The discount was computed from the regular price, so a move invalidates the sale
    price the operator approved."""
    change = await backend.stage_promotion(session, sale("Autumn", [APRON], 15))
    row = next(row for row in store.products if row["id"] == int(APRON))
    row["regular_price"] = "60.00"
    with pytest.raises(ChangeNotApplicable, match="moved since the change was staged"):
        await backend.apply_change(session, change.change_id)
    assert puts(store) == []
    assert status_of(backend, change) is ChangeStatus.STAGED


async def test_pause_still_applies_when_the_listing_status_moved(backend, session, store) -> None:
    """Pausing states an absolute intent, so a stale starting point does not block it."""
    change = await backend.stage_inventory_action(session, switch(HOLD_DOWN, "pause"))
    row = next(row for row in store.products if row["id"] == int(HOLD_DOWN))
    row["status"] = "draft"  # already hidden by someone else
    await backend.apply_change(session, change.change_id)
    assert store.applied[-1] == ("PUT", f"products/{HOLD_DOWN}", {"status": "draft"})


# -- Campaigns and discard --------------------------------------------------------------


def test_campaign_tools_stay_on(config) -> None:
    assert config.enable_campaigns is True
    assert {"stage_campaign", "get_campaign_performance"}.isdisjoint(config.absent_tools())


async def test_a_campaign_draft_is_refused_because_woocommerce_cannot_write_one(
    backend, session, store
) -> None:
    with pytest.raises(ChangeNotApplicable, match="no API to write"):
        await backend.stage_campaign(session, CampaignDraft(name="Bench week", budget=200.0))
    assert store.applied == []


async def test_discard_records_who_and_what_kind(backend, session) -> None:
    change = await backend.stage_price_update(session, reprice(APRON, 44.0))
    discarded = await backend.discard_change(session, change.change_id, ActorKind.AGENT)
    assert discarded.status is ChangeStatus.DISCARDED
    assert (discarded.discarded_by, discarded.discarded_by_kind) == (OPERATOR, ActorKind.AGENT)
    with pytest.raises(ChangeNotApplicable):
        await backend.apply_change(session, change.change_id)
