# Storefront (WooCommerce shopping agent)

The shopping agent from Anthropic's commerce-agents reference, running over a WooCommerce
store's public Store API. Setup and running instructions live in the root README; this
file describes what is particular to the storefront.

## Modules

| Module | Role |
|---|---|
| `api/store_client.py` | HTTP client for the site's unauthenticated endpoints: Store API, WordPress pages, the root index, and the bridge plugin. Turns Store API error bodies into exceptions, with a rejected cart token as its own type |
| `api/woo_backend.py` | `WooStorefrontBackend`, the `StorefrontBackend` implementation: search, families and variations, one cart token per session, the checkout handoff, order reads through the bridge, policy pages, shipping quotes from a throwaway cart |
| `api/brand.py` | Site branding for the web app: name, tagline, and logo from the root index, theme colors from the bridge, with a contrast check on the colors |
| `api/catalog_warmup.py` | At startup, loads the store's best sellers into the display cache behind the grid; never touches a session |
| `api/agent_config.py` | Prompt facts for this deployment: what a family means here, that checkout is a handoff, and whether orders can be looked up |
| `api/main.py` | The reference's shared storefront routes plus `/api/cart/attach` and `/api/brand`; a startup probe learns the site's name and whether the bridge is installed, then rebuilds the agent |
| `web/` | The branded storefront (Next.js) |
| `scripts/smoke.py` | Hands-on check against a running store |

## How WooCommerce concepts map

- A product id is a WordPress post id. A variable product is a family: it appears in
  search with its options, `get_product_details` lists each variation under its own id,
  and cart lines are variation ids.
- Store API prices arrive as strings in minor units and are converted using the
  currency's minor unit. A family's price is that of its cheapest in-stock variation.
- The cart is a Store API cart identified by a `Cart-Token`, one per session. Each write
  sends the token; if the store rejects it, the session forgets it and the write is tried
  once more against a new cart. Adding an item already in the cart increases that line's
  quantity, as the Store API itself does.
- Checkout is a handoff, described under "How checkout and orders work" in the root
  README. The Store API's `/checkout` route is never called.
- Policies come from the site's published pages, found by a page search on the question
  and, if that finds nothing, on a round of synonyms (returns becomes refunds, and so on).
- Shipping quotes come from a throwaway cart holding the products in question, priced at
  the store's default customer location. The session's own cart is left alone.
- Orders are the ones placed from this session's cart, read through the bridge using the
  cart token as the credential. Without the bridge the order tools return nothing.

## Tests

```bash
pytest storefront/api/tests -q
```

| File | Scope |
|---|---|
| `fake_store.py` | An in-process Store API over the merchant side's local store: tokens, rejections, variations, pages, the bridge |
| `test_woo_backend.py` | Product mapping, cart token handling, the handoff, orders, policies, shipping quotes, executor gates |
| `test_host_app.py` | The assembled app: startup probe, cart response extras, attach, direct add, branding |
| `test_brand.py` | The brand payload and the contrast check |
