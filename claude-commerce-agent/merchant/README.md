# WooCommerce merchant example (ACME Supply Co.)

This directory is Automattic's WooCommerce implementation of the merchant agent from
Anthropic's commerce-agents reference. `WooMerchantBackend` fulfils the reference's
`MerchantBackend` interface by talking to a single WooCommerce site through its REST API
(`wc/v3` for products, orders, refunds and reviews; `wc-analytics` for the revenue report).
When the operator approves a change, the same REST API is what receives the write.

You can run it in either of two configurations:

- **Local store.** `api/local_store.py` is an in-process fake of one site's REST API, loaded
  from `data/seed.json`. Set `WOOCOMMERCE_LOCAL_STORE=1` and you have a working merchant
  agent with no WordPress install, no credentials and no network. Every layer above the
  transport is the production code, so a change you approve here genuinely updates the
  catalog the routes serve back.
- **A real WooCommerce site.** Point the example at any WooCommerce store with a REST API
  key, or use the Docker site in `wordpress/local-store/`. This is the configuration that
  tells you whether the mapping and the permissions actually hold up.

The local store is the quickest way to see the thing work; a real site is how you find out
whether it works *for WooCommerce*.

## What is verified

The pytest suite runs the real modules against the local store and covers:

- every abstract method on `MerchantBackend` (sixteen of them) plus `get_merchant_context`;
- that no `stage_*` call sends a REST write;
- that each `apply_change` sends exactly the expected `PUT`s, with the expected bodies;
- the host-approval gate: an apply triggered from chat is held, the portal's apply route
  goes through, and the approval mark is dropped again immediately afterwards;
- the reference's shared merchant HTTP routes mounted over this backend;
- complete agent turns with a scripted model, so each guardrail is hit from the model's
  side and the cacheable system prompt is byte-identical between calls;
- a full local-store loop: list the catalog over HTTP, stage a price, approve it through
  the apply route, list again and see the new price.

Beyond pytest, `scripts/smoke_live.py` was pointed at the Docker site (WooCommerce 11.0)
and passed: every read, both write-safety checks, and the reversible price change.

## Running it

### Option A: local store only

```sh
WOOCOMMERCE_LOCAL_STORE=1 uvicorn --port 8007 merchant.api.main:app
```

That single variable is the entire configuration. `GET /api/merchant/overview` returns a
`store_kind` field (`"local"` or `"woocommerce"`) so you can always tell which mode is
answering. If you prefer a file, copy `merchant/.env.example` to `merchant/.env`; it ships
with `WOOCOMMERCE_LOCAL_STORE=1` already set.

The local store contains the eight seed products and roughly two months of generated
order history, so period-over-period comparisons have something to compare. Reads work
with no API key at all; chat requires `ANTHROPIC_API_KEY`.

Bear in mind what the fake is not: it implements only the parts of the REST API this
backend uses, it does not validate request bodies the way WordPress does, and it answers
only the revenue statistics half of `wc-analytics`.

### Option B: a WooCommerce site

Fastest route: `wordpress/local-store/scripts/setup.sh` brings up the Docker site on
`http://localhost:8090`, installs WooCommerce plus the bridge plugin, writes a REST API key
into `merchant/.env`, and runs the seeder.

For a site of your own:

1. Create a REST API key (WooCommerce → Settings → Advanced → REST API) with
   **Read/Write** permissions, owned by a user who holds the `manage_woocommerce`
   capability. Alternatively, generate a WordPress application password for that user and
   use the username/password pair in the same two variables.
2. In `merchant/.env` set `WOOCOMMERCE_LOCAL_STORE=0`, then fill in
   `WOOCOMMERCE_STORE_URL` (including `https://`), `WOOCOMMERCE_CONSUMER_KEY` and
   `WOOCOMMERCE_CONSUMER_SECRET`.

WooCommerce only honours HTTP Basic authentication over TLS. The Docker site flags its
`wc/v3`, `wc-analytics`, and `wc-admin` routes as SSL-equivalent so that plain
`http://localhost` works during development; a production site should be on HTTPS.

| Requirement | Why |
|---|---|
| Key permission `read_write` | Needed for `PUT` writes; `read` suffices for reads only |
| `manage_woocommerce` capability | Products, variations, orders, refunds and reviews endpoints, and the `wc-admin` marketing campaigns list |
| `view_woocommerce_reports` capability | The `wc-analytics` revenue report. If the user lacks it (or `WOOCOMMERCE_DISABLE_ANALYTICS=1` is set) every metric is computed from the trailing order scan instead |

Treat the consumer secret like an admin password: it can edit the whole store. Only
`api/agent_config.py` reads it from the environment, and only `api/rest_client.py` uses
it; it is never placed in a prompt, returned from a route, or logged.

**Seeding.** A new WooCommerce install has nothing in it, and an empty store gives the
agent nothing to reason about.

```sh
python merchant/scripts/seed_store.py --dry-run   # list the requests without sending them
python merchant/scripts/seed_store.py
```

The seeder is idempotent, so re-running it changes nothing. A product whose slug is
already on the site is left alone, prices and stock included, and an order is stamped with
its `ref` from `data/seed.json` under a private meta key, which is how a second run knows
not to place it again. Because WooCommerce stamps every API-created order with the current
time, a just-seeded site shows revenue only in the most recent window and nothing in the
comparison window; the agent reports the span it measured. Every entry in `data/seed.json`
carries a `note` explaining which behaviour it is there to exercise.

**Smoke test.**

```sh
python merchant/scripts/smoke_live.py --read-only   # reads plus the approval gate, no writes
python merchant/scripts/smoke_live.py               # also one price change, then reverted
```

## Try it

Send these through `/api/merchant/chat` in either mode:

1. "Anything I should deal with first thing today?"
2. "The bench dog set is almost sold out and its listing is bare. Restock enough for a
   month at the current rate and draft a description that answers the obvious buyer
   questions. Show me both before anything changes."
3. "Looks good, approve the restock."
4. "Put the step stool on 15% off for the first two weeks of next month."

Turn 3 changes nothing on its own. With `MERCHANT_REQUIRE_HOST_APPROVAL` enabled (the
default) the agent's `apply_change` call is held at the approval gate; the change goes live
only after the host's `POST /changes/{id}/apply` request, which is what the Approve
button on the preview card sends. Turn 4 shows variation handling: the stool has two
variations, and the promotion is staged as one scheduled sale price for each.

## Modules

| Module | Role |
|---|---|
| `api/rest_client.py` | HTTP transport for the REST API: Basic auth from the key pair, WordPress error envelopes raised as `WooApiError`, a single retry on 429 or 5xx, and `X-WP-TotalPages` pagination. Exposes the `WooExecutor` protocol, which `LocalStore` also implements |
| `api/catalog.py` | Cached product catalog: paged `wc/v3/products` plus each variable product's variations, the `ProductRecord`/`VariantRecord` types, the local relevance scorer behind search, and conversion to the reference's `Listing` |
| `api/orders.py` | The trailing-window order scan that feeds the metric fallback, the alerts and the order issues |
| `api/metrics.py` | `MetricsSource`: prefers the `wc-analytics` revenue report, falls back to the order scan, and records which one answered |
| `api/alerts.py` | Return spikes, delayed orders, slow movers, low stock and customer notes, derived from the catalog and order scan using `data/thresholds.json` |
| `api/staging.py` | Translates one staged change into one or more `wc/v3` writes and phrases the partial-failure message |
| `api/woo_backend.py` | `WooMerchantBackend`: the `MerchantBackend` implementation, plus the KPI trends and insight cards the portal displays |
| `api/store_view.py` | The three storefront-side reads the shared router expects, answered from the store itself |
| `api/merchant.py` | `create_merchant_portal`: mounts the reference's merchant router over this backend |
| `api/agent_config.py` | Sole reader of environment variables; `WOOCOMMERCE_LOCAL_STORE` selects the transport here |
| `api/local_store.py` | The in-process REST API fake, seeded from `data/seed.json`, which applies the writes it receives |
| `scripts/` | The seeder and the live smoke test described above |

## How WooCommerce concepts map onto the interface

Where the reference's model and WooCommerce's differ, this example narrows rather than
fabricates:

- **Listing ids** are WordPress post ids. A variable product is a parent listing whose
  variations are its variants, each addressable by its own id; price and stock are written
  per variation, never on the parent.
- **Price** is `regular_price`. An active `sale_price` is reported alongside it, not in
  place of it.
- **Status:** a `draft` post is reported as `paused`; products in the trash are excluded
  from the catalog altogether.
- **Unit cost** comes from WooCommerce's cost-of-goods field (or the `_wc_cog_cost` meta
  key written by cost-of-goods extensions). Where the store records none, cost and margin
  are `None` rather than guessed.
- **Traffic and conversion rate** are always `None`. WooCommerce core does not record
  sessions; the merchant context and the snapshot note both say so.
- **Promotions** become scheduled sale prices (`sale_price` with `date_on_sale_from`/`to`).
  A negative discount — a temporary price rise — has no scheduled equivalent and is
  refused.
- **Campaigns** are read-only. `wc-admin/marketing/campaigns` lists the campaigns the
  store's marketing extensions report (core has none of its own), with spend and sales
  where the channel supplies them and no budget, dates, or status. WooCommerce has no API
  to create one, so `stage_campaign` refuses and says why.
- **`buyer_message` issues** are the `customer_note` field shoppers fill in at checkout,
  surfaced for orders still in `processing`.

## The approval path

With `MERCHANT_REQUIRE_HOST_APPROVAL` on (the default):

1. The model calls a `stage_*` tool. The backend fetches the current product, builds the
   before/after preview, evaluates the guardrails and records a `staged` entry in the
   ledger. Nothing is sent to WooCommerce.
2. The host renders the preview. When the operator clicks Approve, the portal's
   `/changes/{id}/apply` route flags that id as approved by the host, executes
   `apply_change` via the tool executor, and clears the flag before returning.
3. `apply_change` re-runs the guardrails against the current config, issues the REST
   writes, and only then flips the ledger entry to `applied`. A change WooCommerce refused
   outright stays `staged` and can be approved again once the cause is fixed; one that was
   partly written is discarded, and has to be staged again from the store's new values.

Two safeguards are worth calling out. Every write compares the value it is replacing
against what the site holds at apply time, and refuses when the two differ, so an edit made
on the store between approval and apply is not silently overwritten. The operator approved a
move away from a particular value, and a stale starting point can invert that decision: an
approved rise from 19.99 to 24.99 is a cut once the shop has already moved to 30.00. Two
fields are left out: status, because pausing or activating states an intent a stale starting
point cannot invert, and category, whose live value is a list of names rather than the single
one a staged change carries. And any field this backend cannot write is rejected during
staging, so the operator is never shown an Approve button for something that could not be
applied.

## What it does not do

Two limits are deliberate, and a store with more than one operator would have to answer
both.

**A change covering several products can end up half applied.** WooCommerce writes one
product per request and `wc/v3` has no transaction across them, so a failure partway leaves
the earlier products changed. Nothing rolls them back: rollback would mean compensating
writes that can themselves fail, and that would overwrite whatever else had happened in the
meantime, so reporting is the safer half of the trade. The error therefore names the targets
already written, and says whether the one that failed was refused outright or may have
reached the store anyway, which a 5xx cannot tell you.

Such a change is then discarded rather than left approvable. Its `before` values no longer
describe the store, so replaying it would be refused on every target it did write, and the
operator has to stage a new change from what the store now holds. That is the whole reason
the check above exists: it is what makes a half-applied change fail loudly on the second
attempt instead of quietly writing something nobody approved.

**There is no operator login.** The portal answers to `localhost` and `127.0.0.1` only,
which is what stands in for authorization here: anyone who can reach the port can start a
session and approve a change. `DEMO_ALLOWED_HOSTS` widens that, and it is the setting to be
careful with, because it opens the API to other hosts without adding any authentication.
A deployment that uses it has to put its own authentication in front.

## Tests

```sh
pytest merchant/api/tests -q
```

| File | Scope |
|---|---|
| `test_reads.py` | All backend reads, including those that must report "unknown" rather than zero |
| `test_staging.py` | Staging sends nothing; each apply sends exactly the right writes |
| `test_portal.py` | The shared HTTP routes over this backend, especially apply and discard |
| `test_turn.py` | Complete agent turns with a scripted model |
| `test_local_store.py` | `LocalStore`'s REST shapes and the end-to-end price change |
| `test_scripts.py` | The seeder against an empty local store, and `smoke_live --read-only` writing nothing |
