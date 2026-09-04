# WordPress

Two things: the bridge plugin, and a disposable local site to run the examples against.

## `claude-commerce-bridge/`

A small WooCommerce plugin (one file, no settings) that does what the public Store API
cannot do on its own:

- **The checkout handoff.** `GET /?claude_commerce_cart=<cart token>` validates the token
  the way the Store API does, re-adds the agent cart's lines to the shopper's browser cart
  through WooCommerce's own cart (so stock and purchasability are checked again), applies
  its coupons, and redirects to the checkout page. An expired token lands on the cart
  page with a notice.
- **The order stamp.** At checkout (classic and block), the order records which agent cart
  it came from.
- **Order read-back.** `GET /wp-json/claude-commerce/v1/orders` with a `Cart-Token` header
  answers the orders placed from that cart: the token that built the cart is the
  credential that reads them. A token that does not validate gets a 403.
- **Brand.** `GET /wp-json/claude-commerce/v1/brand` answers the site's name, tagline,
  custom logo, theme colors (when the global styles are literal hex values), and the
  store's checkout and cart URLs. `GET /wp-json/claude-commerce/v1/` says the plugin is
  there.

Install it like any plugin (copy the directory to `wp-content/plugins/` and activate, or
`wp plugin activate claude-commerce-bridge`). It needs WooCommerce and reads nothing the
public Store API does not already hand out. Extensions that know a shipment's tracking URL
can hook `claude_commerce_order_tracking_url`.

### What it does not do

The plugin is written to be read. It runs against the disposable site below and against a
development store, and a store open to the public would need four things it leaves out.

The handoff is a GET that changes state. Loading `/?claude_commerce_cart=<token>` replaces
whatever is in the visitor's cart, with no nonce and no confirmation step, so the link acts
on anything that follows it. That is fine for a link the shopper's own agent hands them and
wrong for a link that can be pasted anywhere or fetched by a preview crawler. A public store
should land the token on a page that asks the shopper to continue, and post from there.

The token travels in the URL. It reaches server access logs, browser history, and any proxy
in front of the store, and it stays valid for the 48 hours WooCommerce gives a cart token.
A public store should spend the handoff token on first use.

`/orders` has no rate limit. It is guarded only by the cart token, so a token recovered from
a log can be replayed until it expires. WooCommerce ships `RateLimits` for the Store API.

Cart tokens are not bound to a site. WooCommerce signs them with `wp_salt()`, which is
shared across a multisite network, and session keys for signed-in customers are user IDs,
which are also network-wide. On a network running the plugin on two stores, a token from one
validates on the other. The plugin inherits this from the Store API but widens it, because
the token reads orders and not just a cart.

Two smaller choices are deliberate. `/orders` returns every status the store considers real,
including `cancelled` and `failed`, and it returns at most 20 orders with no pagination.

## `local-store/`

WordPress + WooCommerce in Docker, on `http://localhost:8090`, with the bridge plugin
mounted from this repository and the merchant example's seed catalog.

```bash
wordpress/local-store/scripts/setup.sh            # start, install, mint a REST key, seed
wordpress/local-store/scripts/setup.sh --no-images
wordpress/local-store/scripts/reset.sh --force    # remove the containers and data
```

`setup.sh` copies `.env.example` to `.env` (ports, the admin user) on first run, points
`merchant/.env` at the site, and seeds through `merchant/scripts/seed_store.py`, the same
seeder a real store gets. Product images are sideloaded from a placeholder host;
`--no-images` skips them.

Running it again is safe and is the way to repair a half-finished run: every step converges
rather than repeats, so the site ends up the same whether the script has run once or five
times. Nothing is installed twice, no duplicate orders are placed, and the REST API key in
`merchant/.env` is replaced only when it has stopped authenticating -- so a merchant host
already running against the store keeps working. Use `reset.sh --force` to start over.

The plugin directory is bind-mounted into the site, so an edit to the plugin file is live
on the next request. If the `claude-commerce/v1` routes answer 404 after an edit, Docker
Desktop has lost the mount; `docker compose restart wordpress` in `local-store/` brings
it back.

The site is for local development only: the admin password is in `.env.example`, offline
payment methods (bank transfer, cheque, cash on delivery) are enabled so a checkout
completes without charging anything, and `wc/v3` and `wc-analytics` are marked as SSL so
WooCommerce accepts Basic auth over plain HTTP. Never point it at, or copy its settings
to, a production store.
