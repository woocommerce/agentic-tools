# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""``WooStorefrontBackend`` over the fake site: catalog mapping, the cart token's life,
the checkout handoff, bridge order reads, policy pages, shipping quotes, and the executor's
gates applied to WooCommerce ids."""

from __future__ import annotations

import pytest
from commerce_common.skills import SkillRegistry
from shopping_agent import SearchFilters, ShoppingSessionContext, Unavailable
from shopping_agent.executor import ShoppingToolExecutor

from storefront.api.agent_config import shopping_config_for
from storefront.api.woo_backend import CART_HANDOFF_PARAM, PolicyPages, plain_text

from .conftest import Seed
from .fake_store import STORE_URL, FakeWooSite

PRODUCTS_ROUTE = "wc/store/v1/products"


def another_shopper(session_id: str = "s-2") -> ShoppingSessionContext:
    return ShoppingSessionContext(session_id=session_id, user_id="guest")


class TestCatalog:
    async def test_a_search_hit_is_mapped_field_by_field(self, backend, shopper):
        (apron,) = await backend.search_products(shopper, "apron", limit=3)
        assert apron.product_id == Seed.APRON
        assert apron.title == "Canvas tool apron"
        assert (apron.price, apron.currency) == (40.0, "USD")  # "4000" in the record
        assert apron.in_stock is True
        assert apron.category == "Workshop tools"
        assert apron.attributes["sku"] == "ACME-AP-1"
        assert (apron.rating, apron.review_count) == (4.5, 2)
        assert apron.image_url.startswith("https://images.acme-supply.invalid/")

    async def test_a_variable_product_searches_as_one_family(self, backend, shopper):
        stool = (await backend.search_products(shopper, "stool", limit=3))[0]
        assert stool.product_id == Seed.STOOL
        assert stool.has_options
        assert stool.options == {"Size": ["Two-step", "Three-step"]}
        assert stool.price == 60.0  # the cheapest variation

    async def test_details_list_each_variation_under_its_own_id(self, backend, shopper):
        details = await backend.get_product_details(shopper, Seed.STOOL)
        assert details is not None and details.long_description
        assert [(v.product_id, v.option_values, v.price, v.in_stock) for v in details.variants] == [
            (Seed.STOOL_TWO_STEP, {"Size": "Two-step"}, 60.0, True),
            (Seed.STOOL_THREE_STEP, {"Size": "Three-step"}, 90.0, True),
        ]
        assert {v.variant_of for v in details.variants} == {Seed.STOOL}

    async def test_a_variation_id_resolves_to_that_variation(self, backend, shopper):
        variation = await backend.get_product_details(shopper, Seed.STOOL_THREE_STEP)
        assert variation is not None
        assert variation.product_id == Seed.STOOL_THREE_STEP
        assert variation.title == "Folding step stool — Three-step"
        assert variation.variant_of == Seed.STOOL
        # The host's grid lookup finds it too, inside its cached family.
        assert backend.product(Seed.STOOL_THREE_STEP).product_id == Seed.STOOL_THREE_STEP

    @pytest.mark.parametrize("product_id", [Seed.UNKNOWN, Seed.DRAFT])
    async def test_ids_the_store_will_not_serve_are_none(self, backend, shopper, product_id):
        assert await backend.get_product_details(shopper, product_id) is None

    async def test_price_filters_travel_in_minor_units(self, backend, shopper, site: FakeWooSite):
        filters = SearchFilters(min_price=10.0, max_price=30.0, sort="price_asc")
        found = await backend.search_products(shopper, "", filters, limit=10)
        sent = site.sent(PRODUCTS_ROUTE)[0]
        assert (sent["min_price"], sent["max_price"]) == ("1000", "3000")
        assert (sent["orderby"], sent["order"]) == ("price", "asc")
        assert [p.title for p in found] == ["Sawdust broom", "Bench dog set"]

    async def test_a_category_name_becomes_its_term_id(self, backend, shopper):
        found = await backend.search_products(
            shopper, "", SearchFilters(category="Storage"), limit=10
        )
        assert [p.title for p in found] == ["Cast iron hold-down"]

    async def test_a_phrase_with_no_literal_hit_is_widened_word_by_word(
        self, backend, shopper, site: FakeWooSite
    ):
        """WooCommerce matches search text literally. A miss is retried one word at a time,
        and when that also misses but a filter narrows the catalog, the popular products
        inside the filter come back instead of nothing."""
        found = await backend.search_products(
            shopper, "a woodworking hobbyist present", SearchFilters(max_price=50.0), limit=8
        )
        assert found and all(p.price <= 50.0 for p in found)
        assert {p.product_id for p in found} >= {Seed.APRON, Seed.BENCH_DOGS}
        searches = [sent.get("search") for sent in site.sent(PRODUCTS_ROUTE)]
        assert "a woodworking hobbyist present" in searches
        assert "woodworking" in searches

    async def test_the_tightest_text_match_leads(self, backend, shopper):
        found = await backend.search_products(shopper, "a woodworking bench", None, limit=8)
        assert found[0].product_id == Seed.BENCH_DOGS
        found = await backend.search_products(shopper, "waxed canvas apron", None, limit=8)
        assert found[0].product_id == Seed.APRON

    async def test_one_unmatched_word_without_a_filter_is_simply_empty(self, backend, shopper):
        assert await backend.search_products(shopper, "quantum", None, limit=8) == []


class TestCart:
    async def test_a_token_is_issued_on_the_first_write_and_kept(
        self, backend, shopper, site: FakeWooSite
    ):
        assert (await backend.get_cart(shopper)).items == []
        assert not [entry for entry in site.log if "cart" in entry[1]]  # no token, no request

        await backend.search_products(shopper, "apron", limit=1)
        cart = await backend.add_to_cart(shopper, Seed.APRON, 2)
        assert [(line.product_id, line.quantity, line.price) for line in cart.items] == [
            (Seed.APRON, 2, 40.0)
        ]
        token = backend.cart_token_for(shopper.session_id)
        assert token in site.carts

        assert (await backend.get_cart(shopper)).item_count == 2
        assert (await backend.update_cart_item(shopper, Seed.APRON, 1)).item_count == 1
        assert (await backend.remove_from_cart(shopper, Seed.APRON)).items == []
        assert backend.cart_token_for(shopper.session_id) == token

    async def test_a_line_is_the_variation_with_its_option_values(self, backend, shopper):
        await backend.get_product_details(shopper, Seed.STOOL)
        (line,) = (await backend.add_to_cart(shopper, Seed.STOOL_TWO_STEP, 1)).items
        assert line.product_id == Seed.STOOL_TWO_STEP
        assert line.option_values == {"Size": "Two-step"}
        assert line.variant_of == Seed.STOOL
        assert line.title == "Folding step stool - Two-step"

    async def test_adding_twice_grows_one_line(self, backend, shopper):
        await backend.add_to_cart(shopper, Seed.APRON, 1)
        cart = await backend.add_to_cart(shopper, Seed.APRON, 2)
        assert [(line.product_id, line.quantity) for line in cart.items] == [(Seed.APRON, 3)]

    async def test_a_family_id_adds_its_fallback_variation(self, backend, shopper):
        # The web app's button may send a family id; the first in-stock variation goes in.
        await backend.get_product_details(shopper, Seed.STOOL)
        cart = await backend.add_to_cart(shopper, Seed.STOOL, 1)
        assert cart.items[0].product_id == Seed.STOOL_TWO_STEP

    async def test_updating_a_line_the_cart_lacks_changes_nothing(self, backend, shopper):
        await backend.add_to_cart(shopper, Seed.APRON, 1)
        assert (await backend.update_cart_item(shopper, Seed.UNKNOWN, 3)).item_count == 1

    async def test_a_sold_out_variation_is_unavailable_with_in_stock_siblings_named(
        self, backend, shopper, site: FakeWooSite
    ):
        site.store.variations[int(Seed.STOOL)][1]["stock_quantity"] = 0  # the three-step
        await backend.get_product_details(shopper, Seed.STOOL)
        with pytest.raises(Unavailable) as refused:
            await backend.add_to_cart(shopper, Seed.STOOL_THREE_STEP, 1)
        assert Seed.STOOL_THREE_STEP in str(refused.value)
        assert Seed.STOOL_TWO_STEP in str(refused.value)
        assert (await backend.get_cart(shopper)).items == []

    async def test_sessions_do_not_share_a_cart(self, backend, shopper):
        await backend.add_to_cart(shopper, Seed.APRON, 1)
        assert (await backend.get_cart(another_shopper())).items == []

    async def test_reset_drops_the_token(self, backend, shopper):
        await backend.add_to_cart(shopper, Seed.APRON, 1)
        backend.reset_session(shopper.session_id)
        assert (await backend.get_cart(shopper)).items == []
        assert backend.checkout_url_for(shopper.session_id) is None

    async def test_a_rejected_token_reads_empty_and_the_next_add_opens_a_new_cart(
        self, backend, shopper, site: FakeWooSite
    ):
        await backend.add_to_cart(shopper, Seed.APRON, 1)
        site.expire_cart(backend.cart_token_for(shopper.session_id))
        assert (await backend.get_cart(shopper)).items == []
        assert backend.cart_token_for(shopper.session_id) is None
        cart = await backend.add_to_cart(shopper, Seed.APRON, 1)
        assert cart.item_count == 1
        assert backend.cart_token_for(shopper.session_id) in site.carts

    async def test_a_token_rejected_mid_write_is_retried_into_a_fresh_cart(
        self, backend, shopper, site: FakeWooSite
    ):
        await backend.add_to_cart(shopper, Seed.APRON, 1)
        stale = backend.cart_token_for(shopper.session_id)
        site.expire_cart(stale)
        cart = await backend.add_to_cart(shopper, Seed.BENCH_DOGS, 1)  # the POST itself is refused
        assert [line.product_id for line in cart.items] == [Seed.BENCH_DOGS]
        assert backend.cart_token_for(shopper.session_id) not in (None, stale)

    async def test_attach_adopts_a_token_from_elsewhere(self, backend, site: FakeWooSite):
        first = another_shopper("s-1")
        await backend.add_to_cart(first, Seed.APRON, 1)
        token = backend.cart_token_for(first.session_id)
        joined = await backend.attach_cart("s-2", token)
        assert joined is not None and joined.item_count == 1
        assert backend.cart_token_for("s-2") == token
        assert await backend.attach_cart("s-3", "tok-unknown") is None


class TestCheckoutHandoff:
    async def test_with_the_bridge_the_url_carries_the_token(self, backend, shopper):
        assert backend.checkout_url_for(shopper.session_id) is None
        await backend.add_to_cart(shopper, Seed.APRON, 1)
        await backend.add_to_cart(shopper, Seed.BENCH_DOGS, 1)
        handoffs = await backend.checkout_handoff(shopper, await backend.get_cart(shopper))
        token = backend.cart_token_for(shopper.session_id)
        assert [h.url for h in handoffs] == [f"{STORE_URL}/?{CART_HANDOFF_PARAM}={token}"]
        assert handoffs[0].label == "Check out at ACME Supply Co."

    async def test_without_the_bridge_only_a_single_line_cart_has_a_link(
        self, make_backend, shopper
    ):
        backend = await make_backend(FakeWooSite(bridge=False))
        assert backend.bridge_available is False
        await backend.add_to_cart(shopper, Seed.APRON, 2)
        assert (
            backend.checkout_url_for(shopper.session_id)
            == f"{STORE_URL}/checkout/?add-to-cart={Seed.APRON}&quantity=2"
        )
        await backend.add_to_cart(shopper, Seed.BENCH_DOGS, 1)
        assert backend.checkout_url_for(shopper.session_id) is None
        assert await backend.checkout_handoff(shopper, await backend.get_cart(shopper)) == []


class TestOrders:
    async def test_orders_are_read_with_this_cart_s_token_only(
        self, backend, shopper, site: FakeWooSite
    ):
        assert await backend.get_orders(shopper) == []  # no cart yet, so no credential
        await backend.add_to_cart(shopper, Seed.STOOL_TWO_STEP, 1)
        assert await backend.get_orders(shopper) == []
        placed = site.place_order(backend.cart_token_for(shopper.session_id))

        (order,) = await backend.get_orders(shopper)
        assert order.order_id == f"#{placed['number']}"
        assert (order.items[0].product_id, order.items[0].variant_of) == (
            Seed.STOOL_TWO_STEP,
            Seed.STOOL,
        )
        assert (order.total, order.status) == (60.0, "processing")
        assert (await backend.get_order(shopper, placed["number"])).order_id == order.order_id
        assert await backend.get_order(shopper, "1") is None

        other = another_shopper()
        await backend.add_to_cart(other, Seed.APRON, 1)
        assert await backend.get_orders(other) == []

    async def test_without_the_bridge_there_are_no_order_reads(self, make_backend, shopper):
        site = FakeWooSite(bridge=False)
        backend = await make_backend(site)
        await backend.add_to_cart(shopper, Seed.APRON, 1)
        site.place_order(backend.cart_token_for(shopper.session_id))
        assert await backend.get_orders(shopper) == []

    async def test_preferences_are_a_guest_profile(self, backend, shopper):
        preferences = await backend.get_preferences(shopper)
        assert preferences.display_name == "Guest"
        assert preferences.user_id == shopper.user_id
        assert backend.recent_orders() == []


class TestPoliciesAndShipping:
    async def test_policy_search_reads_published_pages(self, backend, shopper):
        policies = await backend.search_policies(shopper, "what is your return policy")
        assert [p.policy_id for p in policies] == ["returns-and-refunds"]
        assert "30 days" in policies[0].content
        assert policies[0].category == "page"

    async def test_a_miss_is_retried_with_synonyms(self, backend, shopper):
        assert PolicyPages.queries_for("can I exchange something")[:3] == [
            "can I exchange something",
            "return",
            "refund",
        ]
        widened = await backend.search_policies(shopper, "can I exchange something")
        assert [p.policy_id for p in widened] == ["returns-and-refunds"]
        assert await backend.search_policies(shopper, "quantum") == []

    async def test_shipping_rates_come_from_a_scratch_cart(
        self, backend, shopper, site: FakeWooSite
    ):
        await backend.add_to_cart(shopper, Seed.APRON, 1)
        own_token = backend.cart_token_for(shopper.session_id)
        options = await backend.get_fulfillment_options(shopper, [Seed.BENCH_DOGS])
        assert [(o.method, o.eta, o.fee, o.location) for o in options] == [
            ("shipping", "3-5 business days", 6.0, None),
            ("pickup", "Pick up at the workshop", 0.0, "12 Bench Lane, Toronto"),
        ]
        assert [line["id"] for line in site.carts[own_token]] == [int(Seed.APRON)]
        assert await backend.get_fulfillment_options(shopper, [Seed.UNKNOWN]) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<p>Steel &amp; oak</p>", "Steel & oak"),
        ("<p>one</p>\n<p>two&nbsp;three</p>", "one two three"),
        ("", None),
        (None, None),
    ],
)
def test_plain_text_flattens_rendered_html(raw, expected):
    assert plain_text(raw) == expected


class TestExecutorGates:
    @staticmethod
    def executor(backend, shopper, state) -> ShoppingToolExecutor:
        return ShoppingToolExecutor(
            backend=backend,
            config=shopping_config_for("ACME Supply Co.", bridge=True),
            skills=SkillRegistry([]),
            session=shopper,
            state=state,
        )

    async def test_cart_writes_wait_for_provenance(self, backend, shopper, state):
        run = self.executor(backend, shopper, state).execute
        too_early = await run("add_to_cart", {"product_id": Seed.APRON, "quantity": 1})
        assert too_early.blocked
        await run("search_products", {"query": "apron"})
        assert Seed.APRON in state.seen_products
        for tool, arguments in [
            ("add_to_cart", {"product_id": Seed.APRON, "quantity": 1}),
            ("update_cart_item", {"product_id": Seed.APRON, "quantity": 2}),
            ("remove_from_cart", {"product_id": Seed.APRON}),
        ]:
            assert not (await run(tool, arguments)).refused, tool

    async def test_a_family_add_is_held_and_the_option_named(self, backend, shopper, state):
        run = self.executor(backend, shopper, state).execute
        await run("get_product_details", {"product_id": Seed.STOOL})
        assert Seed.STOOL_TWO_STEP in state.seen_products
        family_add = await run("add_to_cart", {"product_id": Seed.STOOL, "quantity": 1})
        assert family_add.blocked == "options"
        assert "Size" in family_add.result_text
        variation_add = await run("add_to_cart", {"product_id": Seed.STOOL_TWO_STEP, "quantity": 1})
        assert not variation_add.refused
        assert (await backend.get_cart(shopper)).items[0].product_id == Seed.STOOL_TWO_STEP

    async def test_a_stock_refusal_reaches_the_model_as_text(
        self, backend, shopper, state, site: FakeWooSite
    ):
        site.store.products[2]["stock_quantity"] = 0  # the bench dog set
        executor = self.executor(backend, shopper, state)
        await executor.execute("search_products", {"query": "bench"})
        result = await executor.execute(
            "add_to_cart", {"product_id": Seed.BENCH_DOGS, "quantity": 1}
        )
        assert result.is_error and "Nothing was added" in result.result_text
