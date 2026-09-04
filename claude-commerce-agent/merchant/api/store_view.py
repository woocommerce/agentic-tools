# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""``WooStoreView``: the storefront-shaped object the shared merchant router expects.

``build_merchant_router`` in the reference implementation is written for deployments that
host a storefront and a merchant portal in one process, so it takes a storefront object
alongside the backend. Here the storefront is the WooCommerce site (and ``storefront/`` is
a separate service), so this class implements just the three members the merchant routes
actually call: ``store_name``, ``products``, and ``recent_orders``. The cart methods raise
``NotImplementedError`` with an explanation.

``products`` and ``recent_orders`` are synchronous in the router's protocol, so they return
whatever the catalog and order caches currently hold. ``main.py`` fills those caches during
startup.
"""

from __future__ import annotations

from shopping_agent import Cart, Order, OrderItem, OrderStatus, ProductDetails

from .catalog import ProductRecord
from .orders import OrderLine, OrderRecord
from .woo_backend import WooMerchantBackend

# WooCommerce order status -> buyer-side ``OrderStatus``. ``completed`` becomes SHIPPED,
# not DELIVERED: WooCommerce marks an order complete when the merchant is done with it,
# and delivery is not tracked. Anything unlisted reads as PROCESSING.
_BUYER_STATUS = {
    "completed": OrderStatus.SHIPPED,
    "cancelled": OrderStatus.CANCELLED,
    "failed": OrderStatus.CANCELLED,
    "refunded": OrderStatus.REFUNDED,
}
_NO_CART = (
    "{operation} belongs to a storefront; this deployment is a merchant portal over "
    "WooCommerce and has no cart of its own"
)


class WooStoreView:
    """Read-only storefront facade backed by ``WooMerchantBackend``'s caches."""

    def __init__(self, backend: WooMerchantBackend) -> None:
        self._backend = backend

    @property
    def store_name(self) -> str:
        return self._backend.store_name

    @property
    def products(self) -> dict[str, ProductDetails]:
        currency = self._backend.display_currency
        return {
            record.product_id: self._details(record, currency)
            for record in self._backend.catalog.cached()
        }

    def recent_orders(self, limit: int = 6) -> list[Order]:
        currency = self._backend.display_currency
        latest = self._backend.orders.cached()[:limit]
        return [self._order(record, currency) for record in latest]

    def reset_session(self, session_id: str) -> None:
        del session_id

    async def get_cart(self, *args: object, **kwargs: object) -> Cart:
        del args, kwargs
        raise NotImplementedError(_NO_CART.format(operation="get_cart"))

    async def add_to_cart(self, *args: object, **kwargs: object) -> Cart:
        del args, kwargs
        raise NotImplementedError(_NO_CART.format(operation="add_to_cart"))

    # -- Conversions ---------------------------------------------------------------------

    @staticmethod
    def _details(record: ProductRecord, currency: str) -> ProductDetails:
        options = record.options.items() if record.is_family else ()
        return ProductDetails(
            product_id=record.product_id,
            title=record.title,
            price=record.listing_price,
            currency=currency,
            image_url=record.image_url,
            category=record.category,
            attributes=record.attributes(),
            in_stock=record.status != "out_of_stock",
            short_description=record.summary,
            long_description=record.description,
            options={name: list(values) for name, values in options},
        )

    @staticmethod
    def _item(line: OrderLine) -> OrderItem:
        unit_price = round(line.revenue / line.quantity, 2) if line.quantity else 0.0
        return OrderItem(
            product_id=line.variant_id or line.product_id or "",
            title=line.title,
            quantity=line.quantity,
            price=unit_price,
            variant_of=line.product_id if line.variant_id else None,
        )

    @classmethod
    def _order(cls, record: OrderRecord, currency: str) -> Order:
        return Order(
            order_id=record.number,
            status=_BUYER_STATUS.get(record.status, OrderStatus.PROCESSING),
            placed_at=record.created_at,
            items=[cls._item(line) for line in record.lines],
            total=record.total,
            currency=record.currency or currency,
        )
