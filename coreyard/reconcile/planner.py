"""Decide what reconciliation should do. Pure functions, no network, no database.

Split out from the driver so the rules that decide whether a catalogue gets emptied can be
tested exhaustively offline. Everything here is a function of two sets — what the yard says
is listable, what the store says it holds — plus the site's policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

ACTIVE = "ACTIVE"
DRAFT = "DRAFT"
ARCHIVED = "ARCHIVED"


@dataclass(frozen=True)
class ShopProduct:
    """One product as the store currently holds it."""

    r_number: str
    product_id: str
    status: str
    published: bool = False           # visible on at least one sales channel
    quantity: int = 0
    inventory_item_id: str = ""
    tracked: bool = False
    handle: str = ""
    title: str = ""


@dataclass
class StateDrift:
    """Where the sync's snapshot disagrees with the store.

    ``stale`` is the damaging half: the snapshot claims a part was published and the store
    has no such product, so every future incremental run classifies it as "unchanged" and it
    is never created. Forgetting those entries is what lets the next sync build them.
    """

    stale: list[str] = field(default_factory=list)      # in the state file, not on the store
    untracked: list[str] = field(default_factory=list)  # on the store, not in the state file

    def summary(self) -> str:
        return f"state_stale={len(self.stale)} state_untracked={len(self.untracked)}"


@dataclass
class Actions:
    """What reconciliation intends to do, before anything is written."""

    create: list[str] = field(default_factory=list)
    activate: list[str] = field(default_factory=list)
    revive: list[tuple[str, str]] = field(default_factory=list)   # (R#, status to restore)
    retire: list[str] = field(default_factory=list)
    publish: list[str] = field(default_factory=list)
    drift: StateDrift = field(default_factory=StateDrift)
    refused: str = ""

    @property
    def writes(self) -> int:
        return (len(self.activate) + len(self.revive) + len(self.retire)
                + len(self.publish))

    def summary(self) -> str:
        return (f"create={len(self.create)} activate={len(self.activate)} "
                f"revive={len(self.revive)} retire={len(self.retire)} "
                f"publish={len(self.publish)} " + self.drift.summary())


def plan(
    listable: Iterable[str],
    shop: Mapping[str, ShopProduct],
    *,
    state: Optional[Iterable[str]] = None,
    remembered: Optional[Mapping[str, str]] = None,
    activate: bool = False,
    retire: bool = True,
    publish_channels: bool = False,
    max_retire_fraction: float = 0.10,
    force_retire: bool = False,
) -> Actions:
    """Work out the safe difference between the yard and the store.

    ``activate`` is the site's publishing policy: whether a listable draft may be promoted
    without a person looking at it. It stays off by default because the mistake is not
    symmetrical — a draft that should be live is one click away, while a catalogue that went
    live unreviewed is already in front of customers and in search results.

    ``remembered`` is the sync state's pre-archive status memory, so a part that comes back
    to the yard returns as whatever it was rather than as whatever this run's policy is.
    """
    wanted = {str(r).strip() for r in listable if str(r).strip()}
    actions = Actions()

    for r_number in sorted(wanted):
        product = shop.get(r_number)
        if product is None:
            actions.create.append(r_number)
        elif product.status == DRAFT:
            if activate:
                actions.activate.append(r_number)
        elif product.status == ARCHIVED:
            # A hand-set status can only be inferred for a product that was archived by a
            # retirement this tool recorded; without that memory the site's own publishing
            # policy decides, and the conservative answer is the holding state.
            restore = (remembered or {}).get(r_number) or (ACTIVE if activate else DRAFT)
            actions.revive.append((r_number, restore))
        elif publish_channels and not product.published:
            # ACTIVE and on no channel: the product exists, has stock and a price, and still
            # returns 404 to every shopper and to Google. Status alone never made it visible.
            actions.publish.append(r_number)

    if retire:
        candidates = sorted(r for r, p in shop.items()
                            if p.status == ACTIVE and r not in wanted)
        active_now = sum(1 for p in shop.values() if p.status == ACTIVE)
        if candidates and active_now:
            share = len(candidates) / active_now
            if share > max_retire_fraction and not force_retire:
                actions.refused = (
                    f"REFUSING to retire {len(candidates)} of {active_now} active product(s) "
                    f"— {share:.1%}, over the {max_retire_fraction:.0%} limit. A yard "
                    f"database that answered partially looks exactly like this. Re-run with "
                    f"--force-retire if it is real."
                )
                candidates = []
        actions.retire = candidates

    if state is not None:
        keys = {str(r).strip() for r in state if str(r).strip()}
        actions.drift.stale = sorted(keys - set(shop))
        actions.drift.untracked = sorted(set(shop) - keys)

    # Activating a draft that is not on a channel leaves it just as invisible, so the two
    # repairs have to happen together rather than one run apart.
    if publish_channels:
        for r_number in actions.activate + [r for r, _ in actions.revive]:
            if r_number not in actions.publish and not shop[r_number].published:
                actions.publish.append(r_number)
        actions.publish.sort()
    return actions
