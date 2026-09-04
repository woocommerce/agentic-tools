# Agentic Tools

A collection of agentic and AI-related projects, code snippets, and tools.

## Projects

### [claude-commerce-agent](claude-commerce-agent/)

WooCommerce implementations of the two agents in
[Anthropic's commerce-agents reference](https://github.com/anthropics/commerce-agents), the
code behind [The anatomy of effective commerce agents](https://claude.com/blog/the-anatomy-of-effective-commerce-agents).
The reference's own packages are installed unmodified at a pinned commit; everything
WooCommerce-specific lives in a backend implementation for each agent plus the host wiring
around it.

**Storefront: the shopping agent.** Sits in front of a WooCommerce store and talks to its
public Store API, so it works against any store whose Store API is reachable. It searches
the catalog, walks a variable product down to its variations, keeps a real Store API cart,
quotes the store's shipping rates, answers policy questions from the site's own pages, and
hands the shopper over to the store's checkout. The Next.js web app takes its name, logo,
and colors from the site, so one codebase is a branded storefront for whichever store it
points at. The agent never places an order or takes payment.

**Merchant: the operator agent.** Sits behind the counter of the same store and talks to
the WooCommerce REST API and the analytics reports. It reads products, orders, stock
levels, and revenue, flags what needs attention, and proposes changes to prices, listing
content, stock and status, and scheduled promotions. Every proposal lands in a ledger; a
person approves it in the Next.js operator portal before anything is written. The REST
credential is loaded once and goes straight into the transport, so the model, the routes,
and the logs never see it. The merchant side can also run against an in-process store with
no WooCommerce site at all.

**What ships with it:**

- `storefront/` and `merchant/`: a FastAPI host, a Next.js web app, fixtures, and a test
  suite for each agent.
- `wordpress/claude-commerce-bridge/`: a plugin that adopts the agent's cart into the
  shopper's browser session at checkout, reads orders back by cart token, and exposes site
  branding.
- `wordpress/local-store/`: a Docker Compose WordPress and WooCommerce site with a setup
  script that seeds it from a shared catalog.
- Smoke scripts for both agents against a running store. Neither the unit tests nor the
  smoke scripts need an Anthropic API key.

**Getting started.** Needs Python 3.11 or newer, Node 22, and Docker Desktop. The project's
[README](claude-commerce-agent/README.md) has the full quick start, a scripted
conversation to try on each side, and a description of how checkout and order read-back
work. The project's [merchant README](claude-commerce-agent/merchant/README.md) explains
what a real store needs.

**Status.** An experimental reference implementation, not a production deployment. The
merchant API has no operator login and relies on answering only to loopback host names,
demo state is in memory and disappears on restart, and a multi-target change can partially
apply without rollback. The project's README lists these limitations in full. Licensed
Apache-2.0, with parts copied or adapted from Anthropic's repository as recorded in its
[NOTICE](claude-commerce-agent/NOTICE).

## Code snippets

- **[agentic-product-visits](agentic-product-visits/)**: single-file WordPress/WooCommerce snippet that detects AI-agent visits to product pages (Web Bot Auth headers, User-Agent signatures, AI referrals) and counts them per brand in post meta. Read via the `wcus_agentic_product_visits` and `wcus_agentic_visits_totals` filters.
