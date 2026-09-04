# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

import json
from datetime import date, datetime, timedelta

from demo_common.storefront_fixtures import (
    SessionCarts,
    find_by_id,
    find_product,
    keyword_score,
    load_catalog,
    option_text,
    rank_products,
    redate_in_flight_orders,
    search_help,
    within_price_and_rating,
)
from shopping_agent import Policy, ProductDetails, SearchFilters


def product(
    product_id: str, title: str, price: float, rating: float, **attributes: str
) -> ProductDetails:
    return ProductDetails(
        product_id=product_id,
        title=title,
        price=price,
        currency="USD",
        rating=rating,
        category="camping",
        attributes=attributes,
    )


TENT = product("P-1", "Family camping tent", 180.0, 4.2, capacity="4 people")
LANTERN = product("P-2", "Camping lantern", 25.0, 4.8)
SOFA = product("P-3", "Sofa", 900.0, 4.9)
CATALOG = [TENT, LANTERN, SOFA]
WEIGHTS = {"title": 3.0, "attributes": 1.0}


def score(item: ProductDetails, query_tokens: list[str]) -> float:
    fields = {
        "title": item.title,
        "attributes": " ".join(f"{k} {v}" for k, v in item.attributes.items()),
    }
    return keyword_score(fields, WEIGHTS, query_tokens, {"couch": ["sofa"]})


def soft(item: ProductDetails, filters: SearchFilters) -> bool:
    return all(str(v) in item.attributes.get(k, "") for k, v in filters.attributes.items())


def rank(query: str, filters: SearchFilters | None = None) -> list[str]:
    ranked = rank_products(
        CATALOG,
        query,
        filters,
        8,
        score=score,
        hard_filter=within_price_and_rating,
        soft_filter=soft,
    )
    return [item.product_id for item in ranked]


def test_synonyms_stems_and_field_weights_drive_the_score():
    assert score(SOFA, ["couch"]) == 3.0
    assert score(TENT, ["tents", "people"]) == 4.0
    assert score(LANTERN, ["sofa"]) == 0.0


def test_ranking_cuts_faint_matches_relaxes_empty_soft_filters_and_sorts():
    assert rank("camping tent") == ["P-1", "P-2"]  # relevance, then rating breaks ties
    assert rank("camping") == ["P-2", "P-1"]  # equal scores: the better rated first
    assert rank("camping", SearchFilters(sort="price_desc")) == ["P-1", "P-2"]
    assert rank("camping", SearchFilters(max_price=100)) == ["P-2"]
    assert rank("camping", SearchFilters(attributes={"capacity": "4"})) == ["P-1"]
    assert rank("camping", SearchFilters(attributes={"capacity": "9"})) == [
        "P-2",
        "P-1",
    ]  # nothing matches, so relaxed
    assert rank("") == []


def test_help_search_ignores_function_words_and_weights_headings():
    returns = Policy(policy_id="h-1", title="Returns", category="orders", content="Thirty days.")
    guide = Policy(
        policy_id="h-2",
        title="Buying guide",
        category="guides",
        content="How do I choose a tent? What is the return on a good tent? Returns matter.",
    )
    assert search_help([guide, returns], "how do returns work") == [returns, guide]
    assert search_help([guide, returns], "the a of") == []


def test_ids_resolve_case_insensitively():
    items = {"AR-1601": object()}
    assert find_by_id(items, "AR-1601") == "AR-1601"
    assert find_by_id(items, "ar-1601") == "AR-1601"
    assert find_by_id(items, "AR-9999") is None


def test_in_flight_orders_shift_with_the_anchor_and_finished_orders_do_not():
    anchor = date.today() - timedelta(days=30)
    raw = {
        "dates_anchored_to": anchor.isoformat(),
        "orders": [
            {
                "status": "delayed",
                "placed_at": f"{(anchor - timedelta(days=3)).isoformat()}T10:00:00Z",
                "estimated_delivery": f"{(anchor + timedelta(days=4)).isoformat()} (was {anchor.isoformat()})",
            },
            {
                "status": "delivered",
                "placed_at": "2026-01-01T10:00:00Z",
                "estimated_delivery": "2026-01-04",
            },
        ],
    }
    delayed, delivered = redate_in_flight_orders(raw)["orders"]
    today = datetime.now().date()
    assert delayed["placed_at"] == f"{(today - timedelta(days=3)).isoformat()}T10:00:00Z"
    assert (
        delayed["estimated_delivery"]
        == f"{(today + timedelta(days=4)).isoformat()} (was {today.isoformat()})"
    )
    assert delivered["estimated_delivery"] == "2026-01-04"
    assert redate_in_flight_orders({"orders": []}) == {"orders": []}


def test_session_carts_keep_sessions_apart():
    carts = SessionCarts()
    carts.put("s-1", TENT, 2)
    assert carts.cart("s-2").items == []
    assert carts.set_quantity("s-1", "P-1", 3).items[0].quantity == 3
    assert carts.set_quantity("s-1", "P-9", 3).item_count == 3  # unknown lines are ignored
    assert carts.remove("s-1", "P-1").items == []
    carts.put("s-1", LANTERN, 1)
    carts.reset("s-1")
    assert carts.cart("s-1").items == []


def test_a_family_fixture_fills_its_variants_and_derives_its_options(tmp_path):
    catalog = {
        "store_name": "ACME",
        "products": [
            {"product_id": "P-1", "title": "Lantern", "price": 25.0},
            {
                "product_id": "P-2",
                "title": "Pad",
                "brand": "ACME Basecamp",
                "price": 59.0,
                "category": "camping",
                "attributes": {"fill": "foam"},
                "long_description": "Self-inflating.",
                "variants": [
                    {"product_id": "P-2-R", "option_values": {"length": "regular"}},
                    {
                        "product_id": "P-2-L",
                        "option_values": {"length": "long"},
                        "price": 69.0,
                        "in_stock": False,
                        "attributes": {"packed": "34 cm"},
                    },
                ],
            },
        ],
    }
    (tmp_path / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    raw, listings, variants = load_catalog(tmp_path)

    assert raw["store_name"] == "ACME" and list(listings) == ["P-1", "P-2"]
    assert list(variants) == ["P-2-R", "P-2-L"]
    family = listings["P-2"]
    assert family.options == {"length": ["regular", "long"]}
    assert family.in_stock is True  # any variant in stock
    assert [v.product_id for v in family.variants] == ["P-2-R", "P-2-L"]
    assert not listings["P-1"].options and not listings["P-1"].variants

    long = variants["P-2-L"]
    assert (long.title, long.brand, long.category, long.price) == (
        "Pad",
        "ACME Basecamp",
        "camping",
        69.0,
    )
    assert long.variant_of == "P-2" and long.option_values == {"length": "long"}
    assert long.attributes == {"fill": "foam", "packed": "34 cm"}
    assert long.long_description == "Self-inflating." and long.in_stock is False
    assert variants["P-2-R"].price == 59.0  # inherited

    assert find_product(listings, variants, "p-2-l") is long
    assert find_product(listings, variants, "P-2") is family
    assert find_product(listings, variants, "P-9") is None
    assert option_text(family) == "length regular long" and option_text(long) == ""

    carts = SessionCarts()
    [line] = carts.put("s1", long, 1).items
    assert line.option_values == {"length": "long"} and line.variant_of == "P-2"
