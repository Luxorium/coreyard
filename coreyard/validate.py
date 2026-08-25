"""Validate the external configuration files CoreYard consumes.

CoreYard reads four site-supplied files, and each is a contract: get one wrong and the
failure shows up as a wrong claim on a listing, a freight part quoting free ground, or an
order fulfilled out from under the shipping app. Those are expensive places to find a typo.

So the schemas are checkable on their own, without a database, a store, credentials, or
even a ``.env``:

    bin/coreyard validate                       # everything this installation configures
    bin/coreyard validate --shipping path.json  # one file, by path
    bin/coreyard validate --profile p.json --orders o.json --weights w.json

That last form is the point of this module: the *storefront* repository can validate its own
files against the backend's schemas in CI, by checking this repository out, without either
application importing the other at runtime.

Cross-file consistency is checked too, because most real mistakes live between files rather
than inside one — an order policy naming a shipping group that no longer exists, a shipping
group whose tag another group already claimed.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Optional

from coreyard.orders.policy import OrderPolicy, OrderPolicyError
from coreyard.orders.policy import load as load_order_policy
from coreyard.profile import CatalogProfile, ProfileError
from coreyard.profile import load as load_profile
from coreyard.transform.shipping import ShippingPolicy, ShippingPolicyError
from coreyard.transform.shipping import load as load_shipping
from coreyard.transform.weights import WeightRules, WeightRulesError
from coreyard.transform.weights import load as load_weights


@dataclass
class Result:
    """What validation found, as text a person and a CI log can both read."""

    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def fail(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def note(self, message: str) -> None:
        self.notes.append(message)

    def report(self, out=None) -> int:
        # Resolved at call time, not bound at definition: a default of sys.stdout captures
        # the stream this module was imported with, which ignores any later redirect.
        out = out if out is not None else sys.stdout
        for line in self.notes:
            print(f"  {line}", file=out)
        if self.errors:
            print("\nInvalid configuration:", file=out)
            for line in self.errors:
                print(f"  ✗ {line}", file=out)
            return 1
        print("\nConfiguration is valid.", file=out)
        return 0


def check_profile(path, result: Result) -> Optional[CatalogProfile]:
    try:
        profile = load_profile(path)
    except ProfileError as exc:
        result.fail("catalog profile", str(exc))
        return None
    if path:
        namespace = profile.metafield_namespace
        if not namespace.replace("_", "").isalnum():
            result.fail("catalog profile",
                        f"metafield_namespace {namespace!r} must be alphanumeric or "
                        f"underscores; Shopify rejects anything else")
        result.note(f"catalog profile: condition {profile.condition!r}, "
                    f"metafields in {namespace!r}, {len(profile.part_types)} part-type "
                    f"override(s)")
    return profile


def check_weights(path, result: Result) -> Optional[WeightRules]:
    try:
        rules = load_weights(path)
    except WeightRulesError as exc:
        result.fail("weight rules", str(exc))
        return None
    if path:
        if rules.default is None:
            result.note("weight rules: no default — parts matching no rule publish with "
                        "no weight, and carrier-calculated rates quote them as empty boxes")
        result.note(f"weight rules: {len(rules.rules)} rule(s) in {rules.unit}, "
                    f"default {rules.default}, safety x{rules.safety_multiplier}")
    return rules


def check_shipping(path, result: Result) -> Optional[ShippingPolicy]:
    try:
        policy = load_shipping(path)
    except ShippingPolicyError as exc:
        result.fail("shipping policy", str(exc))
        return None
    if not path:
        return policy
    unmatched = [g.id for g in policy.groups if not g.match and not g.default]
    if unmatched:
        result.note(f"shipping policy: group(s) {', '.join(unmatched)} have no 'match' "
                    f"patterns, so nothing is ever classified into them")
    without_owner = [g.id for g in policy.groups if not g.fulfillment]
    if without_owner:
        result.note(f"shipping policy: group(s) {', '.join(without_owner)} do not say who "
                    f"ships them ('fulfillment'), so order status sync cannot derive it")
    result.note(f"shipping policy: {len(policy.groups)} group(s) "
                f"({', '.join(g.tag for g in policy.groups)}), default "
                f"{policy.default_group.id if policy.default_group else '?'}, owns "
                f"{', '.join(policy.owned_prefixes) or 'nothing'}")
    return policy


def check_orders(path, result: Result,
                 shipping: Optional[ShippingPolicy] = None) -> Optional[OrderPolicy]:
    try:
        policy = load_order_policy(path)
    except OrderPolicyError as exc:
        result.fail("order policy", str(exc))
        return None
    if not path:
        return policy
    if policy.explicit_fulfillment and shipping and shipping.configured:
        # Both files describe the same thing; the copies are free to drift, and the one
        # that drifts decides whether a parcel order gets closed before it ships.
        known = {t.lower() for t in shipping.tags}
        for label, names in (("groups", policy.fulfillment.groups),
                             ("defer_groups", policy.fulfillment.defer_groups),
                             ("default_group", (policy.fulfillment.default_group,))):
            for name in names:
                if name and name.lower() not in known:
                    result.fail("order policy",
                                f"fulfillment.{label} names {name!r}, which is not a tag in "
                                f"the shipping policy ({', '.join(shipping.tags)})")
        result.note("order policy: declares its own fulfillment block, so the shipping "
                    "policy's 'fulfillment' keys are ignored — delete it to derive them")
    elif shipping and shipping.configured:
        derived = policy.with_shipping(shipping).fulfillment
        result.note(f"order policy: fulfillment derived from the shipping policy — defers "
                    f"{', '.join(derived.defer_groups) or 'nothing'}")
    elif not policy.fulfillment.enabled:
        result.note("order policy: no fulfillment rules and no shipping policy, so order "
                    "status sync will only tag and note")
    result.note(f"order policy: tags {list(policy.tags_for('123'))}")
    return policy


def validate(profile=None, weights=None, orders=None, shipping=None) -> Result:
    """Validate the given config paths. Any left as None is simply not checked."""
    result = Result()
    check_profile(profile, result)
    check_weights(weights, result)
    policy = check_shipping(shipping, result)
    check_orders(orders, result, policy)
    return result


def _configured() -> dict:
    from coreyard.config import _get, load_env

    load_env()
    return {
        "profile": _get("STORE_PROFILE_FILE", "") or None,
        "weights": _get("STORE_WEIGHT_RULES_FILE", "") or None,
        "orders": _get("STORE_ORDER_POLICY_FILE", "") or None,
        "shipping": _get("STORE_SHIPPING_POLICY_FILE", "") or None,
    }


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard validate",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default=None, help="catalog profile JSON")
    ap.add_argument("--weights", default=None, help="weight rules JSON")
    ap.add_argument("--orders", default=None, help="order policy JSON")
    ap.add_argument("--shipping", default=None, help="shipping policy JSON")
    args = ap.parse_args(argv)

    paths = {"profile": args.profile, "weights": args.weights,
             "orders": args.orders, "shipping": args.shipping}
    if not any(paths.values()):
        # No paths given: check whatever this installation has configured. Reading .env is
        # deliberate here and only here — it is the one command whose job is to answer
        # "is my configuration right".
        paths = _configured()
        if not any(paths.values()):
            print("No configuration files are set and none were given. Nothing to check.\n"
                  "Pass --profile/--weights/--orders/--shipping, or set the STORE_*_FILE "
                  "variables.")
            return 0
        print("Validating this installation's configured files:")
    else:
        print("Validating:")
    for name, path in paths.items():
        if path:
            print(f"  {name:9s} {path}")
    print()
    return validate(**paths).report()


if __name__ == "__main__":
    raise SystemExit(main())
