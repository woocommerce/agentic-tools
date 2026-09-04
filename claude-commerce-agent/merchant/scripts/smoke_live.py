# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Run the merchant backend against a real WooCommerce site: every read, the approval
gate, and one write that is undone before the script exits.

    python merchant/scripts/smoke_live.py [--read-only]

The test suite runs over ``LocalStore``, so a green suite says the Python is right and
nothing about an actual WooCommerce install. This script covers that gap. Pointed at the
site in ``merchant/.env`` it confirms what only a live server can: the REST key has the
capabilities each read needs, the ``wc/v3`` responses carry the fields the backend
expects, and a ``PUT`` changes what it should.

No model is called and ``ANTHROPIC_API_KEY`` is not needed. Staging and applying go through
``MerchantToolExecutor`` exactly as a conversation would, so the host-approval gate is the
real one.

The write is a five percent increase on one simple product, applied with the host's
approval mark and reverted by a second approved change. If the revert fails the script
prints the product, its id, and the price to set it back to, so nothing is left quietly
changed. ``--read-only`` runs the reads and the gate check but discards the staged change
instead of applying it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve()
EXAMPLE_DIR = HERE.parents[1]
sys.path.insert(0, str(HERE.parents[2]))

from commerce_common.skills import SkillRegistry  # noqa: E402
from demo_common import load_demo_env  # noqa: E402
from merchant.api.agent_config import (  # noqa: E402
    SKILLS_DIR,
    MissingCredentials,
    build_merchant_config,
    load_settings,
)
from merchant.api.rest_client import WooApiError, WooRestClient  # noqa: E402
from merchant.api.woo_backend import WooMerchantBackend  # noqa: E402
from merchant_agent import (  # noqa: E402
    ActorKind,
    ChangeNotApplicable,
    Listing,
    MerchantAgentConfig,
    MerchantSessionContext,
    MerchantSessionState,
    PriceUpdateItem,
    StagedChange,
)
from merchant_agent.executor import MerchantToolExecutor  # noqa: E402

load_demo_env(EXAMPLE_DIR)

PRICE_BUMP = 1.05


class Verdict(StrEnum):
    OK = "ok"
    FAIL = "FAIL"
    SKIP = "skip"


@dataclass
class Outcome:
    name: str
    verdict: Verdict
    detail: str = ""

    def line(self) -> str:
        return f"[{self.verdict.value:^6}] {self.name}" + (
            f" — {self.detail}" if self.detail else ""
        )


class CheckFailed(Exception):
    """Raised inside a check to fail it; the message becomes the detail column."""


class NotOffered(Exception):
    """Raised inside a check when this deployment does not offer the read. Recorded as a
    skip, not a failure."""


Check = Callable[[], Awaitable[str | None]]


class Checklist:
    """Runs named checks in sequence and keeps every outcome, so one run reports all of
    them and exits once."""

    def __init__(self, echo: Callable[[str], None] = print) -> None:
        self.outcomes: list[Outcome] = []
        self._echo = echo

    async def run(self, name: str, check: Check) -> bool:
        try:
            detail = await check()
        except CheckFailed as failure:
            outcome = Outcome(name, Verdict.FAIL, str(failure))
        except NotOffered as reason:
            outcome = Outcome(name, Verdict.SKIP, str(reason))
        else:
            outcome = Outcome(name, Verdict.OK, detail or "")
        self.outcomes.append(outcome)
        self._echo(outcome.line())
        return outcome.verdict is Verdict.OK

    def note(self, text: str) -> None:
        self._echo(f"         {text}")

    def _named(self, verdict: Verdict) -> list[str]:
        return [outcome.name for outcome in self.outcomes if outcome.verdict is verdict]

    @property
    def failed(self) -> list[str]:
        return self._named(Verdict.FAIL)

    @property
    def skipped(self) -> list[str]:
        return self._named(Verdict.SKIP)

    def summary(self) -> str:
        if self.failed:
            return f"{len(self.failed)} failed: " + "; ".join(self.failed)
        if self.skipped:
            return (
                f"all checks passed; {len(self.skipped)} not offered by this deployment: "
                + "; ".join(self.skipped)
            )
        return "all checks passed"


class SmokeRun:
    """Every backend read, then the approval gate, against one site."""

    def __init__(
        self,
        backend: WooMerchantBackend,
        config: MerchantAgentConfig,
        session: MerchantSessionContext,
        *,
        read_only: bool,
        checks: Checklist | None = None,
    ) -> None:
        self.backend = backend
        self.config = config
        self.session = session
        self.read_only = read_only
        self.checks = checks or Checklist()
        self.state = MerchantSessionState()
        self._skills = SkillRegistry.from_dir(SKILLS_DIR)

    # -- Reads -------------------------------------------------------------------------

    async def reads(self) -> None:
        """Call each backend read once and confirm it answers in the expected shape. The
        values are whatever this site holds, so the checks are deliberately loose."""
        await self.checks.run("store profile", self._check_profile)
        if not await self.checks.run("catalog", self._check_catalog):
            self.checks.note("Nothing to read further; run merchant/scripts/seed_store.py first.")
            return
        subject = self.backend.all_listings()[0]
        reads: list[tuple[str, Check]] = [
            ("get_listing", partial(self._check_listing, subject)),
            ("search_listings", partial(self._check_search, subject)),
            ("get_business_snapshot", self._check_snapshot),
            ("query_metrics", self._check_metrics),
            ("get_inventory_alerts", self._check_alerts),
            ("get_order_issues", self._check_issues),
            ("get_pricing_context", partial(self._check_pricing, subject)),
            ("get_campaign_performance", self._check_campaigns),
            ("get_merchant_context", self._check_context),
        ]
        for name, check in reads:
            await self.checks.run(name, check)

    async def _check_profile(self) -> str:
        profile = await self.backend.profile()
        if not profile.name:
            raise CheckFailed("the site returned no name")
        return f"{profile.name} · {profile.currency}"

    async def _check_catalog(self) -> str:
        listings = self.backend.all_listings()
        if not listings:
            raise CheckFailed("no products found")
        return f"{len(listings)} listings"

    async def _check_listing(self, subject: Listing) -> str:
        if await self.backend.get_listing(self.session, subject.listing_id) is None:
            raise CheckFailed(f"{subject.listing_id} did not resolve")
        return subject.listing_id

    async def _check_search(self, subject: Listing) -> str:
        word = subject.title.split()[0]
        found = await self.backend.search_listings(self.session, word, None, 10)
        if not found:
            raise CheckFailed(f"no match for {word!r}, a word from a listing title")
        return f"{len(found)} matches for {word!r}"

    async def _check_snapshot(self) -> str:
        snapshot = await self.backend.get_business_snapshot(self.session, None)
        if not snapshot.period:
            raise CheckFailed("snapshot has no period")
        if snapshot.sales == 0:
            self.checks.note(
                "Zero sales in the window: the snapshot excludes today, so a site seeded "
                "moments ago reports none."
            )
        return (
            f"{snapshot.period}: {snapshot.currency} {snapshot.sales} over {snapshot.orders} orders"
        )

    async def _check_metrics(self) -> str:
        series = await self.backend.query_metrics(self.session, "sales")
        if not series.points:
            raise CheckFailed("sales series is empty")
        return f"{len(series.points)} points"

    async def _check_alerts(self) -> str:
        alerts = await self.backend.get_inventory_alerts(self.session)
        return f"{len(alerts)} alerts"

    async def _check_issues(self) -> str:
        issues = await self.backend.get_order_issues(self.session)
        return f"{len(issues)} issues"

    async def _check_pricing(self, subject: Listing) -> str:
        pricing = await self.backend.get_pricing_context(self.session, subject.listing_id)
        if pricing is None:
            raise CheckFailed(f"no pricing context for {subject.listing_id}")
        return f"floor {pricing.min_price}"

    async def _check_campaigns(self) -> str:
        try:
            campaigns = await self.backend.get_campaign_performance(self.session, None)
        except ChangeNotApplicable as reason:
            raise NotOffered(str(reason)) from reason
        return f"{len(campaigns)} campaigns"

    async def _check_context(self) -> str:
        context = await self.backend.get_merchant_context(self.session)
        if not context:
            raise CheckFailed("merchant context is empty")
        for key, value in context.items():
            self.checks.note(f"{key}: {value}")
        return ""

    # -- The approval gate ---------------------------------------------------------------

    async def gate(self) -> None:
        """In order: a staged price is not on the site yet, an apply without the host's
        mark is held, and (unless read-only) an apply carrying the mark lands and is then
        reverted."""
        subject = self._pick_subject()
        if subject is None:
            await self.checks.run("a simple priced product", partial(_fail, "none in the catalog"))
            return
        original = subject.price
        target = round(original * PRICE_BUMP, 2)

        staged = await self._stage_price(subject, target)
        await self.checks.run(
            "stage_price_update returns a change",
            partial(_ok, f"{staged.change_id}: {subject.title} {original} → {target}"),
        )
        await self.checks.run(
            "staging leaves the site's price alone", partial(self._expect_price, subject, original)
        )
        await self.checks.run(
            "apply_change without the host's mark is held", partial(self._expect_held, staged)
        )
        await self.checks.run(
            "the held apply wrote nothing", partial(self._expect_price, subject, original)
        )

        if self.read_only:
            await self.backend.discard_change(self.session, staged.change_id, ActorKind.OPERATOR)
            self.checks.note("--read-only: the staged change was discarded; no PUT was sent.")
            return

        async with self._price_moved(subject, staged, back_to=original) as applied:
            await self.checks.run(
                "a host-approved apply goes through", partial(self._expect_applied, applied)
            )
            await self.checks.run(
                "the site reports the new price", partial(self._expect_price, subject, target)
            )

    def _pick_subject(self) -> Listing | None:
        """A simple product with a real price. Variable parents are skipped: their price is
        the cheapest variation's and cannot be set in one write."""
        for entry in self.backend.all_listings():
            if entry.price > 1 and not entry.options:
                return entry
        return None

    def _tool_executor(self) -> MerchantToolExecutor:
        return MerchantToolExecutor(
            backend=self.backend,
            config=self.config,
            skills=self._skills,
            session=self.session,
            state=self.state,
        )

    async def _stage_price(self, subject: Listing, price: float) -> StagedChange:
        change = await self.backend.stage_price_update(
            self.session, [PriceUpdateItem(listing_id=subject.listing_id, new_price=price)]
        )
        self.state.remember_change(change)
        return change

    async def _apply_with_host_mark(self, change: StagedChange):
        """What the portal's Approve button does: mark the id, run the executor, unmark."""
        self.state.approved_change_ids.add(change.change_id)
        try:
            return await self._tool_executor().execute(
                "apply_change", {"change_id": change.change_id}
            )
        finally:
            self.state.approved_change_ids.discard(change.change_id)

    @asynccontextmanager
    async def _price_moved(
        self, subject: Listing, staged: StagedChange, *, back_to: float
    ) -> AsyncIterator[object]:
        """Apply ``staged`` with the host's mark, hand the outcome to the body, then restore
        ``back_to`` through a second approved change. The restore runs even if a check in
        the body raises; if it fails, the warning names what to fix by hand."""
        applied = await self._apply_with_host_mark(staged)
        try:
            yield applied
        finally:
            revert = await self._stage_price(subject, back_to)
            reverted = await self._apply_with_host_mark(revert)
            restored = await self.checks.run(
                "the price was put back",
                partial(self._expect_price, subject, back_to, after=reverted),
            )
            if not restored:
                print()
                print(
                    f"!!! {subject.title} ({subject.listing_id}) may still carry the test "
                    f"price. Set its regular price back to {back_to}."
                )

    async def _expect_price(self, subject: Listing, wanted: float, after=None) -> str:
        if after is not None and (after.is_error or after.blocked is not None):
            raise CheckFailed(after.blocked or after.result_text[:120])
        reread = await self.backend.get_listing(self.session, subject.listing_id)
        if reread is None:
            raise CheckFailed(f"{subject.listing_id} no longer resolves")
        if reread.price != wanted:
            raise CheckFailed(f"{reread.price} (wanted {wanted})")
        return f"{reread.price}"

    async def _expect_held(self, staged: StagedChange) -> str:
        outcome = await self._tool_executor().execute(
            "apply_change", {"change_id": staged.change_id}
        )
        if outcome.blocked is None:
            raise CheckFailed("the apply went through without the host's mark")
        return outcome.blocked

    @staticmethod
    async def _expect_applied(applied) -> str:
        if applied.is_error or applied.blocked is not None:
            raise CheckFailed(applied.blocked or applied.result_text[:120])
        return applied.result_text[:120]


async def _ok(detail: str) -> str:
    return detail


async def _fail(detail: str) -> str:
    raise CheckFailed(detail)


# -- Command line ------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test the merchant backend on a live site.")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="run the reads and the gate check, then discard the staged change; write nothing",
    )
    return parser.parse_args(argv)


async def run(read_only: bool) -> int:
    try:
        settings = load_settings()
    except MissingCredentials as error:
        print(error)
        return 2
    if settings.local_store:
        print("WOOCOMMERCE_LOCAL_STORE is set; this script needs a real site. Unset it first.")
        return 2

    client = WooRestClient(
        settings.store_url,
        consumer_key=settings.consumer_key,
        consumer_secret=settings.consumer_secret,
    )
    config = build_merchant_config(settings.store_name or settings.merchant_id)
    backend = WooMerchantBackend(client, settings, config)
    session = MerchantSessionContext(
        session_id="smoke", merchant_id=settings.merchant_id, operator=settings.operator
    )
    smoke = SmokeRun(backend, config, session, read_only=read_only)

    print(f"{settings.store_url} · wc/v3")
    print()
    try:
        await backend.warm()
        await smoke.reads()
        print()
        await smoke.gate()
    except WooApiError as error:
        print()
        print(f"[ FAIL ] the REST API refused a request: {error}")
        return 1
    finally:
        await client.aclose()

    print()
    print(smoke.checks.summary())
    return 1 if smoke.checks.failed else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv).read_only))


if __name__ == "__main__":
    sys.exit(main())
