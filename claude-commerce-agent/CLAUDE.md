# claude-commerce-agent

Notes for agents working in this repository. It holds WooCommerce implementations of both
agents in Anthropic's commerce-agents reference: the shopping agent over the Store API
(`storefront/`), the merchant agent over the REST API (`merchant/`), a bridge plugin and a
Docker site (`wordpress/`), and the reference's example scaffolding under `vendor/`.

## Layout

- `storefront/api/`: `store_client.py`, `woo_backend.py`, `brand.py`, `catalog_warmup.py`, `agent_config.py`, `main.py`; `tests/` with `fake_store.py`.
- `storefront/web/`: the Next.js storefront (port 3006; API 8006).
- `merchant/api/`: `rest_client.py`, `catalog.py`, `orders.py`, `metrics.py`, `alerts.py`, `staging.py`, `woo_backend.py`, `local_store.py`, `store_view.py`, `merchant.py`, `agent_config.py`, `main.py`; `tests/`.
- `merchant/web/`: the Next.js portal (port 3007; API 8007). `merchant/data/`: `seed.json`, `thresholds.json`. `merchant/scripts/`: `seed_store.py`, `smoke_live.py`.
- `wordpress/claude-commerce-bridge/`: the plugin. `wordpress/local-store/`: docker-compose and scripts (site on 8090).
- `vendor/`: `demo_common`, `web-shared`, `skills/` copied from the reference at the commit `requirements.txt` pins. Edit nothing there apart from the two `.env` hints listed in NOTICE.

## Design rules

- The reference's packages install from the pin and are never modified. Everything WooCommerce-specific lives in a `StorefrontBackend` or `MerchantBackend` implementation plus the host wiring around it.
- The shopping agent never calls the Store API's `/checkout`. Checkout is a handoff URL, and orders are read back only through the bridge, with the cart token as the credential.
- No merchant `stage_*` method sends a write. `staging.py` is the only module that mutates a store, and it runs only from `apply_change`.
- Where WooCommerce has no figure (traffic, conversion, unit cost, campaigns), return `None` or disable the feature; never substitute a zero.
- One seed catalog (`merchant/data/seed.json`) feeds the local store, the seeder, and the storefront's Store API fake.
- Prose in READMEs and docstrings: plain declarative sentences, one term per concept, no history.

## Verify

```bash
ruff check . && ruff format --check . && pytest
python storefront/scripts/smoke.py                  # needs a store (wordpress/local-store)
python merchant/scripts/smoke_live.py --read-only   # needs merchant/.env pointed at a store
npm run build                                       # both web apps
```
