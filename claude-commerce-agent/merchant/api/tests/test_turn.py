# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Whole conversation turns with a scripted model. ``FakeClient`` plays back one prepared
message per model call, so no API key is involved, while the tool schemas, the executor,
and every gate run for real over the local store. What is checked is what the model sees:
which tools it is offered, what the tool results carry, and which gate stops an apply."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from commerce_common.testing import FakeClient, text_message, tool_use_message
from merchant_agent import MerchantSessionState
from merchant_agent_runtime import MerchantAgent

from merchant.api.agent_config import SKILLS_DIR

from .conftest import APRON, BENCH_DOGS, BROOM, SITE_HOST, STOOL, STOOL_THREE_STEP, STOOL_TWO_STEP

# -- Scripted model messages ------------------------------------------------------------


def reply(text: str):
    return text_message(text)


def search(query: str):
    return tool_use_message("search_listings", {"query": query})


def read_listing(listing_id: str):
    return tool_use_message("get_listing", {"listing_id": listing_id})


def snapshot():
    return tool_use_message("get_business_snapshot", {})


def reprice(listing_id: str, price: float):
    return tool_use_message(
        "stage_price_update", {"items": [{"listing_id": listing_id, "new_price": price}]}
    )


def restock(listing_id: str, quantity: int):
    return tool_use_message(
        "stage_inventory_action",
        {"items": [{"listing_id": listing_id, "action": "restock", "quantity": quantity}]},
    )


def promote(name: str, listing_ids: list[str], pct: float):
    return tool_use_message(
        "stage_promotion",
        {
            "name": name,
            "listing_ids": listing_ids,
            "discount_pct": pct,
            "starts": "2026-10-01",
            "ends": "2026-10-07",
        },
    )


def apply(change_id: str):
    return tool_use_message("apply_change", {"change_id": change_id})


# -- The turn and what it left behind ---------------------------------------------------


@dataclass
class Turn:
    calls: list[dict[str, Any]]
    events: list[Any]
    state: MerchantSessionState

    @property
    def staged_events(self) -> list[Any]:
        return [event for event in self.events if event.type == "change_update"]

    @property
    def tool_names(self) -> set[str]:
        return {tool["name"] for tool in self.calls[0]["tools"]}

    def static_prompt(self, call: int = 0) -> dict[str, Any]:
        return self.calls[call]["system"][0]

    def request_context(self, call: int = 0) -> str:
        return "".join(block["text"] for block in self.calls[call]["system"][1:])

    @property
    def fed_back(self) -> str:
        """Every tool result handed to the model, as one string."""
        return " ".join(self._result_texts())

    def _result_texts(self) -> Iterator[str]:
        for call in self.calls:
            for message in call.get("messages") or []:
                blocks = message.get("content")
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        yield _text_of(block.get("content"))


def _text_of(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return "".join(part.get("text", "") for part in payload or [] if isinstance(part, dict))


@pytest.fixture
def turn(backend, config, session):
    async def run(prompt: str, *script: Any) -> Turn:
        client = FakeClient(list(script))
        agent = MerchantAgent(backend=backend, skills_dir=SKILLS_DIR, config=config, client=client)
        state = MerchantSessionState()
        messages = [{"role": "user", "content": prompt}]
        events = [event async for event in agent.stream_turn(messages, session, state)]
        return Turn(client.calls, events, state)

    return run


# -- What the model is shown ------------------------------------------------------------


async def test_snapshot_result_names_the_currency_and_no_traffic(turn) -> None:
    done = await turn("How are sales this week?", snapshot(), reply("Here you go."))
    assert '"currency": "USD"' in done.fed_back
    assert '"traffic": null' in done.fed_back or '"traffic"' not in done.fed_back


async def test_request_context_states_sources_and_limits(turn) -> None:
    done = await turn("Hello", reply("Ask away."))
    context = done.request_context()
    for expected in (
        "WooCommerce Analytics",
        "records no sessions",
        '"order_history_days": 60',
        "no campaign object",
    ):
        assert expected in context


async def test_campaign_tools_are_not_offered(turn) -> None:
    done = await turn("Hello", reply("Ask away."))
    assert done.tool_names.isdisjoint({"stage_campaign", "get_campaign_performance"})
    assert {
        "stage_price_update",
        "stage_promotion",
        "stage_inventory_action",
        "apply_change",
    } <= done.tool_names


async def test_static_prompt_is_byte_identical_between_calls(turn) -> None:
    done = await turn("How are sales?", snapshot(), reply("Sales are up."))
    assert len(done.calls) == 2
    first, second = done.static_prompt(0), done.static_prompt(1)
    assert first == second
    assert first["cache_control"] == {"type": "ephemeral"}
    # The site's identity lives after the cache breakpoint.
    assert SITE_HOST not in first["text"]
    assert SITE_HOST in done.request_context()


async def test_listing_result_carries_the_variation_ids(turn) -> None:
    done = await turn("Tell me about the step stool", read_listing(STOOL), reply("Two rungs."))
    for expected in (STOOL_TWO_STEP, STOOL_THREE_STEP, "Three-step"):
        assert expected in done.fed_back


async def test_store_owned_text_is_fenced(turn) -> None:
    done = await turn("Show me the apron", read_listing(APRON), reply("Here it is."))
    assert "Canvas tool apron" in done.fed_back
    assert "<merchant_data>" in done.fed_back


# -- The gates, from the model's side ---------------------------------------------------


async def test_staging_emits_one_change_event_and_no_write(turn, store) -> None:
    done = await turn(
        "Raise the apron by 10%",
        search("apron"),
        reprice(APRON, 44.0),
        reply("Queued for your approval; nothing has changed yet."),
    )
    assert store.applied == []
    (staged,) = done.staged_events
    assert staged.data["change"]["status"] == "staged"


async def test_provenance_gate_blocks_an_unread_listing(turn, store) -> None:
    done = await turn(
        "Raise the apron by 10%", reprice(APRON, 44.0), reply("Let me look it up first.")
    )
    assert store.applied == []
    assert done.staged_events == []
    assert "not returned by catalog tools in this session" in done.fed_back


async def test_pricing_a_parent_answers_with_variation_ids(turn, store) -> None:
    done = await turn(
        "Raise the step stool to 70",
        read_listing(STOOL),
        reprice(STOOL, 70.0),
        reply("Which size?"),
    )
    assert store.applied == []
    assert done.staged_events == []
    assert STOOL_TWO_STEP in done.fed_back


async def test_model_cannot_apply_without_the_host_mark(turn, store) -> None:
    done = await turn(
        "Put the apron up 10% and make it live",
        read_listing(APRON),
        reprice(APRON, 44.0),
        apply("chg-0001"),
        reply("I cannot apply that myself; please approve it."),
    )
    assert store.applied == []
    assert "Approve button" in done.fed_back


async def test_oversized_price_move_is_refused_before_staging(turn, store) -> None:
    done = await turn(
        "Double the apron's price",
        read_listing(APRON),
        reprice(APRON, 90.0),
        reply("That is more than one change may move a price."),
    )
    assert store.applied == []
    assert done.staged_events == []
    assert "20" in done.fed_back  # the delta cap, named in the refusal


async def test_untracked_stock_cannot_be_restocked(turn, store) -> None:
    done = await turn(
        "Restock the sawdust broom",
        search("broom"),
        restock(BROOM, 20),
        reply("WooCommerce is not tracking that product's stock."),
    )
    assert store.applied == []
    assert "does not track inventory" in done.fed_back


async def test_promotion_stages_sale_price_items(turn, store) -> None:
    done = await turn(
        "Run 15% off the bench dogs next week",
        read_listing(BENCH_DOGS),
        promote("Bench week", [BENCH_DOGS], 15),
        reply("Staged."),
    )
    (staged,) = done.staged_events
    assert staged.data["change"]["items"][0]["field"] == "sale_price"
    assert store.applied == []
