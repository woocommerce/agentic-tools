# Claude commerce agent for WooCommerce

WooCommerce implementations of the two agents in
[Anthropic's commerce-agents reference](https://github.com/anthropics/commerce-agents),
the code behind
[The anatomy of effective commerce agents](https://claude.com/blog/the-anatomy-of-effective-commerce-agents).
`storefront/` puts the shopping agent in front of a WooCommerce store, talking to the
store's public Store API. `merchant/` puts the merchant agent behind the counter of the
same store, talking to the WooCommerce REST API. The two halves are independent, each
with an API host, a web app, fixtures, and a test suite of its own. In common they have
`vendor/`, where the reference's example scaffolding and skills are kept (see
[NOTICE](NOTICE)), and the reference's own packages, which `requirements.txt` installs
from Anthropic's repository at a pinned commit.

Neither agent completes a sale or edits a live store by itself. The shopping agent sends
the shopper to the store's own checkout page and never places an order or takes payment.
The merchant agent proposes changes; a person approves each one before anything is
written.

## Security and production limitations

This repository is an experimental reference implementation, not a production deployment.
The merchant API has no operator login: it answers only to loopback host names, and that
standing in for authentication is the whole of its access control, so `DEMO_ALLOWED_HOSTS`
widens the surface without adding any. A change that writes several targets can partially
apply without automatic rollback. Before each write the value being replaced is compared
against what the site holds and the write is refused when the two differ, with status and
category left out of that comparison.

Cart tokens and any checkout or handoff URLs containing them are bearer capabilities: do
not share or log them. Smoke and setup status output avoids printing those values or
generated credentials, but production observability needs equivalent filtering. The local
merchant store, session state, ledgers, and related demo data are in memory and disappear
on restart. Retry handling is intentionally small: a Store API read gets one retry on a
rate limit or a server error, while a cart write is retried only on a rate limit, because a
server error leaves it unknown whether the mutation landed. Production deployments need
persistence, authentication, concurrency and recovery policies suited to their environment.

## Quick start

You need Python 3.11 or newer, Node 22, and Docker Desktop for the local store.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && npm install      # Python packages, then the two web apps
cp .env.example .env                                # then set ANTHROPIC_API_KEY in .env
wordpress/local-store/scripts/setup.sh              # WordPress + WooCommerce in Docker on :8090, seeded
```

Run those in order: the setup script seeds the store through this repository's own Python,
so the packages have to be installed before it. It ends by printing the site's address and
its admin login. Open <http://localhost:8090> and the shop page lists six products. The
seed catalog holds eight: one is a draft, which the merchant agent reports as paused, and
one is in the trash, which neither side lists.

Each example then runs in two terminals, one for the Python host and one for the web app.
A new terminal does not carry the virtual environment, so the host's terminal activates it
first.

Storefront:

```bash
source .venv/bin/activate && uvicorn storefront.api.main:app --port 8006
npm run dev -w storefront/web                       # http://localhost:3006
```

Merchant:

```bash
source .venv/bin/activate && uvicorn merchant.api.main:app --port 8007
npm run dev -w merchant/web                         # http://localhost:3007
```

That leaves five addresses:

| Address | What answers there |
|---|---|
| <http://localhost:3006> | Storefront web app, the page a shopper uses |
| <http://localhost:8006> | Storefront host, the shopping agent's API |
| <http://localhost:3007> | Merchant portal, where a proposed change is approved |
| <http://localhost:8007> | Merchant host, the merchant agent's API |
| <http://localhost:8090> | The WooCommerce store; `/wp-admin` takes the login the setup script printed |

The merchant side can also run with no WooCommerce site at all. Start it with
`WOOCOMMERCE_LOCAL_STORE=1` in the environment and the REST transport is replaced by
`merchant/api/local_store.py`, a store that lives inside the process and is loaded from
`merchant/data/seed.json`.

## Storefront

Set `WOOCOMMERCE_STORE_URL` to any WooCommerce site whose Store API is reachable (it is,
on every store, unless a plugin has closed it) and the shopping agent works against that
site's live catalog. It searches products, walks a variable product down to its
variations, keeps a real Store API cart under a `Cart-Token`, quotes the store's shipping
rates, answers policy questions from the site's own pages, and hands the shopper over to
the store's checkout. The web app takes its name, tagline, logo, and colors from the site,
so one codebase is a branded storefront for whichever store it points at.

`.env.example` lists every setting; with nothing set, the storefront targets the local
Docker store. If the page already has a cart, open the storefront with
`?cart=<cart token>` or call `POST /api/cart/attach`; the agent then works in that cart,
and because every write re-reads the cart first, lines added elsewhere survive.

### Try

1. I need a present for a woodworker, budget $50. Any ideas?
2. Put the first suggestion in my cart.
3. How do returns work here?
4. What sizes does the folding step stool come in? I'll take the tall one.

As the conversation goes on, the web app's grid, cart drawer, and checkout button update
to match it; the checkout button leads to the store's own checkout page.

### How checkout and orders work

The agent's cart belongs to a WooCommerce session that the Store API identifies by its
`Cart-Token`. The shopper's browser has a session of its own, so the two have to be
connected. The [bridge plugin](wordpress/claude-commerce-bridge/) does that: the
`checkout_url` on every cart response carries the token to the store, and the plugin
adopts the agent's cart into the browser's session by adding each line again through
WooCommerce's own cart, which re-checks stock and purchasability, and then forwards the
shopper to the checkout page. When the order is placed, the plugin records which cart it
came from, and `GET /wp-json/claude-commerce/v1/orders`, called with that same token,
returns the orders placed from that cart. That endpoint is what the agent's order tools
read, and only when the shopper asks.

Without the plugin, a one-line cart hands off through WooCommerce's `add-to-cart` URL, a
cart with more lines has no handoff, and the order tools return nothing; the agent is
told as much in its config. In every configuration the Store API's `/checkout` route goes
uncalled: nothing in this repository places an order or takes payment.

## Merchant

The merchant agent reads a store's products, orders, stock levels, and revenue through
`wc/v3` and the `wc-analytics` reports, proposes changes, and writes back only what the
operator approves.

For a real store, put `WOOCOMMERCE_STORE_URL`, `WOOCOMMERCE_CONSUMER_KEY`, and
`WOOCOMMERCE_CONSUMER_SECRET` in `merchant/.env`. The Docker setup script writes these for
the local store; [merchant/README.md](merchant/README.md) explains what a real store
needs. Then `merchant/scripts/seed_store.py` loads the seed catalog and
`merchant/scripts/smoke_live.py` checks the connection.

The approval path is the part worth reading. The agent's `stage_*` tools never send a
REST request: staging records a proposed change in a ledger, and the change reaches
WooCommerce only when someone calls `POST /api/merchant/changes/{id}/apply`. The REST
credential is loaded a single time, by `merchant/api/agent_config.py`, and goes straight
into the transport; the model, the routes, and the logs never see it.

### Try

1. Anything I should look at before the shop opens today?
2. We're down to the last few bench dog sets and the listing is thin. Work out a restock
   quantity that lasts about a month at recent sales, and draft a better description.
   Stage both; I want to review them first.
3. Approve the restock.

Four of the reference's five merchant flows map onto WooCommerce objects: prices
(`regular_price` on a product or variation), listing content, stock and status, and
promotions, which WooCommerce models natively as a scheduled `sale_price`. Campaigns are
read-only: WooCommerce lists what the store's marketing extensions report and has no API to
create one, so a campaign draft is refused with that reason.

## Layout

| Path | Contents |
|---|---|
| `storefront/api/` | Storefront host (FastAPI). `store_client.py` talks to the Store API, `woo_backend.py` implements `StorefrontBackend` on top of it, `brand.py` reads site branding, `catalog_warmup.py` pre-fills the grid, `agent_config.py` holds the prompt facts |
| `storefront/web/` | Shopper-facing web app (Next.js) with the product grid, cart drawer, and assistant rail |
| `storefront/scripts/` | `smoke.py`, a hands-on check against a running store |
| `merchant/api/` | Merchant host (FastAPI). `rest_client.py` is the REST transport; `catalog.py`, `orders.py`, `metrics.py`, and `alerts.py` read the store; `woo_backend.py` implements `MerchantBackend`; `staging.py` is the only writer; `local_store.py` is the in-process store |
| `merchant/web/` | Operator portal (Next.js) |
| `merchant/scripts/`, `merchant/data/` | `seed_store.py` and `smoke_live.py`; `seed.json` and `thresholds.json` |
| `wordpress/claude-commerce-bridge/` | The bridge plugin: cart adoption at checkout, order read-back by cart token, branding endpoint |
| `wordpress/local-store/` | Docker Compose site and its setup scripts |
| `vendor/` | Example scaffolding (`demo_common`, `web-shared`) and skills (five per agent) copied from the reference implementation; see [NOTICE](NOTICE) |

## Tests

```bash
pytest                                              # unit and integration tests over in-process fakes
python storefront/scripts/smoke.py                  # storefront backend against a running store
python merchant/scripts/smoke_live.py --read-only   # merchant backend against a running store; without the flag it also applies one change
```

The storefront suite runs the real backend against a Store API fake
(`storefront/api/tests/fake_store.py`) that sits on top of the merchant side's local
store, so both suites describe a single catalog. The merchant suite runs the real modules
against that local store, which applies whatever an approved change writes. None of
these, the smoke scripts included, needs an Anthropic API key.

## License

Apache-2.0. Parts of this repository are copied or adapted from Anthropic's
[commerce-agents repository](https://github.com/anthropics/commerce-agents), Copyright
Anthropic PBC; [NOTICE](NOTICE) has the details.
