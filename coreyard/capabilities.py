"""What this installation is actually configured to do.

Every diagnostic in CoreYard used to assume the original installation: a SQL Server behind
an SMB named pipe, a photo share reached with ``smbclient``, a mapped ``schema.json``, and
an order poller writing to ``out/orders.log``. That assumption is wrong for every other
supported shape of the product. A yard running ``COREYARD_SOURCE=tabular:parts.csv`` has no
SMB credentials to be missing and no schema to map, and ``doctor`` reported it as three
FAIL lines and told the operator to fix an installation that was already correct — which is
the same class of defect as a check that stays lit for something already fixed.

So the question "is this broken" is separated from "is this configured", and only this
module answers the second. It reads configuration; it opens no socket, runs no query and
writes nothing, so it stays usable in the one place a diagnostic must not be slow: deciding
what to check before checking it.

Requirement, not opinion: a capability is ``enabled`` when the operator has configured it,
``missing`` when it is required by something else that *is* enabled, and ``off`` when it is
simply not part of this installation. Only the middle case is a failure. That distinction
is why order booking can stay opt-in without its absence reading
as a broken install (REL-02), and why "unsupported combination" can be reported before any
side effect rather than as a traceback halfway through a run (REL-01).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coreyard.source import SourceTraits
from pathlib import Path

# The three states a capability can be in. `MISSING` is the only one that is anybody's
# fault: something enabled here depends on it and it is not configured.
ON, OFF, MISSING = "on", "off", "missing"


@dataclass(frozen=True)
class Capability:
    """One thing this installation either does or does not do.

    ``detail`` names the setting that decided it, because the next question an operator
    asks after "why is this off" is always "off according to what".
    """

    name: str
    state: str
    detail: str = ""
    # The settings that turn this on, quoted back in `coreyard doctor` and `validate`
    # rather than left for the reader to find in the README.
    keys: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.state == ON

    def __bool__(self) -> bool:
        return self.enabled


@dataclass(frozen=True)
class Capabilities:
    """The whole picture, resolved once so one run uses one consistent snapshot (UX-08)."""

    source_kind: str
    source_argument: str
    items: dict[str, Capability] = field(default_factory=dict)
    #: What the configured source declares it can do. Carried on the snapshot so a caller
    #: that already has the capabilities does not resolve the source a second time — and so
    #: `doctor` and `status` can ask what a source *is* instead of what it is called.
    traits: "SourceTraits | None" = None

    def __getitem__(self, name: str) -> Capability:
        return self.items[name]

    def get(self, name: str) -> Capability:
        return self.items.get(name, Capability(name, OFF, "not configured"))

    def enabled(self, name: str) -> bool:
        return self.get(name).enabled

    @property
    def source_spec(self) -> str:
        """The ``COREYARD_SOURCE`` string this snapshot resolved, ready to hand back to
        :func:`coreyard.source.load`.

        Passing it back matters more than it looks: ``source.load(None)`` re-reads the
        environment, so a diagnostic that resolved a tabular source and then asked for
        "the source" could be handed the database instead — and did, opening a named pipe
        the report had just finished saying this installation does not use.
        """
        return f"{self.source_kind}:{self.source_argument}" if self.source_argument \
            else self.source_kind

    @property
    def tabular(self) -> bool:
        """True when inventory comes from a file rather than the source database.

        Kept because it reads well where the distinction really is "a file, specifically".
        For anything else — does this source need a schema mapping, an SMB server, its own
        photo share, a change cursor — ask :attr:`traits`, which the adapter declares. This
        property could not answer those for a third source kind, and the code that used it
        as though it could was comparing strings in five modules.
        """
        return self.source_kind == "tabular"

    def summary(self) -> list[tuple[str, str, str]]:
        """``(name, state, detail)`` for every capability, in a stable order."""
        return [(cap.name, cap.state, cap.detail) for cap in self.items.values()]


def _text(key: str, default: str = "") -> str:
    from coreyard.config import _get

    return (_get(key, default) or "").strip()


def _schema_path() -> Path:
    """Where ``schema.json`` is, read from the environment already in memory.

    Resolved here rather than by calling ``schema.is_configured()`` with no argument,
    because that helper loads ``.env`` itself. A gate that loads the environment leaks the
    site's real configuration into every unit test that touches it — the same rule the
    render path follows, for the same reason.
    """
    from coreyard.config import _get
    from coreyard.yms.schema import SCHEMA_PATH

    return Path(_get("COREYARD_SCHEMA", str(SCHEMA_PATH)) or str(SCHEMA_PATH))


def _mapping():
    """The loaded source schema, or None when this installation has not mapped one."""
    try:
        from coreyard.yms import schema

        path = _schema_path()
        return schema.load(path) if schema.is_configured(path) else None
    except Exception:
        return None


def detect(load: bool = True) -> Capabilities:
    """Read the configuration and say what this installation can do.

    ``load`` parses ``.env`` first, which is what a command-line diagnostic wants. Render
    and test paths pass ``load=False`` so the site's real ``.env`` cannot leak into them —
    the same rule the render-path config gates follow.
    """
    from coreyard.config import data_path

    if load:
        from coreyard.config import load_env

        load_env()

    spec = _text("COREYARD_SOURCE", "database") or "database"
    kind, _, argument = spec.partition(":")
    kind = kind.strip().lower() or "database"
    argument = argument.strip()

    # What this source can do, declared by its own adapter. Everything below that used to
    # compare `kind` against "database" asks here instead, so registering a third yard
    # system does not mean finding every comparison in this file and the four others that
    # had their own.
    from coreyard.config import source_traits

    traits = source_traits()

    items: dict[str, Capability] = {}

    def add(name: str, state: str, detail: str, keys: tuple[str, ...] = ()) -> None:
        items[name] = Capability(name, state, detail, keys)

    # -- where parts come from ---------------------------------------------------
    # Asked of the adapter, which is the only thing that knows what "reachable" means for
    # its own kind: a file that exists, or credentials that are filled in. This used to be
    # a chain of `elif kind ==` here, so a new yard system had to be added to this function
    # as well as to the registry — and its absence showed up as "unknown source".
    from coreyard import source as source_module

    try:
        state, detail, keys = source_module.probe(spec)
    except ValueError as exc:
        add("source", MISSING, str(exc), ("COREYARD_SOURCE",))
    else:
        add("source", state, detail, keys)

    # `schema.json` maps the source database's tables. A tabular source *is* its own
    # mapping — the column names are Part field names — so demanding one there is asking
    # the operator to write a file nothing will read.
    # Distinct from `source`, and needed because two commands are database-only:
    # `coreyard schema` introspects a live SQL Server, and `coreyard images` reads the SMB
    # share. Both are meaningless against a CSV export, and "the source is configured" is
    # true there — so the coarser capability would wave them through into a confusing
    # failure further in.
    add("database", ON if (traits.needs_schema_mapping and items["source"].enabled) else OFF,
        "source database is the configured source" if traits.needs_schema_mapping
        else f"the configured source is {kind}, not a database")

    mapping = _mapping() if traits.needs_schema_mapping else None
    if traits.needs_schema_mapping:
        if mapping is not None:
            add("schema", ON, f"mapped by {_schema_path().name}", ("COREYARD_SCHEMA",))
        elif _schema_path().exists():
            add("schema", MISSING,
                f"{_schema_path().name} exists but does not load — run `coreyard validate`",
                ("COREYARD_SCHEMA",))
        else:
            add("schema", MISSING,
                "unmapped — run `coreyard schema`, or see README 'Map your database'",
                ("COREYARD_SCHEMA",))
    else:
        add("schema", OFF, f"a {kind} source is its own mapping", ("COREYARD_SCHEMA",))

    # -- photographs -------------------------------------------------------------
    if not traits.carries_own_photos:
        share = _text("SMB_IMAGES_SHARE")
        add("photos", ON if share else OFF,
            f"SMB share {share}" if share else "no SMB_IMAGES_SHARE configured",
            ("SMB_IMAGES_SHARE",))
    else:
        directory = _text("COREYARD_SOURCE_IMAGES")
        if not directory:
            add("photos", OFF, "no COREYARD_SOURCE_IMAGES directory configured",
                ("COREYARD_SOURCE_IMAGES",))
        elif not (data_path(directory) or Path(directory)).is_dir():
            add("photos", MISSING, f"{directory} is not a directory",
                ("COREYARD_SOURCE_IMAGES",))
        else:
            add("photos", ON, f"local directory {directory}",
                ("COREYARD_SOURCE_IMAGES",))

    # Donor fallback is opt-in twice over: it needs the query *and* a database to run it.
    donor = bool(mapping is not None and mapping.supports_donor_images)
    add("donor_photos", ON if donor else OFF,
        "donor_images query mapped" if donor
        else "no donor_images query in schema.json"
             if traits.needs_schema_mapping else f"not available for a {kind} source")

    # -- where parts go ----------------------------------------------------------
    store, token = _text("SHOPIFY_STORE"), _text("SHOPIFY_ADMIN_TOKEN")
    if store and token:
        add("shopify", ON, f"admin token for {store}",
            ("SHOPIFY_STORE", "SHOPIFY_ADMIN_TOKEN"))
    elif store or token:
        add("shopify", MISSING,
            "set both SHOPIFY_STORE and SHOPIFY_ADMIN_TOKEN "
            f"(have {'SHOPIFY_STORE' if store else 'SHOPIFY_ADMIN_TOKEN'})",
            ("SHOPIFY_STORE", "SHOPIFY_ADMIN_TOKEN"))
    else:
        add("shopify", OFF, "no Admin API credentials — the CSV sink still works",
            ("SHOPIFY_STORE", "SHOPIFY_ADMIN_TOKEN"))

    publications = _text("STORE_PUBLICATIONS")
    add("publications", ON if publications else OFF,
        publications or "unset — new products would reach no sales channel",
        ("STORE_PUBLICATIONS",))

    # -- orders ------------------------------------------------------------------
    # This is the *webhook* transport, which is the one with a prerequisite of its own: the
    # receiver verifies a raw-body HMAC, so without a signing secret it cannot accept a
    # delivery at all. Polling is the other transport and needs only the Admin API, so it
    # is deliberately not gated here — a site that never registered a webhook still polls.
    secret = _text("SHOPIFY_WEBHOOK_SECRET") or _text("SHOPIFY_CLIENT_SECRET")
    add("orders", ON if secret else OFF,
        "webhook signing secret configured" if secret
        else "no SHOPIFY_WEBHOOK_SECRET/SHOPIFY_CLIENT_SECRET — webhook receipt is off "
             "(polling needs only the Admin API)",
        ("SHOPIFY_WEBHOOK_SECRET", "SHOPIFY_CLIENT_SECRET"))

    from coreyard.config import flag

    requested = flag("YMS_WRITE_ORDERS", False)
    mapped = mapping is not None and mapping.order_write is not None
    if kind != "database":
        add("order_booking", OFF,
            f"a {kind} source cannot book a work order in a source database",
            ("YMS_WRITE_ORDERS",))
    elif requested and mapped:
        add("order_booking", ON, "YMS_WRITE_ORDERS=1 and order_write is mapped",
            ("YMS_WRITE_ORDERS",))
    elif requested and not mapped:
        add("order_booking", MISSING,
            "YMS_WRITE_ORDERS is set but schema.json has no complete 'order_write' section",
            ("YMS_WRITE_ORDERS",))
    else:
        add("order_booking", OFF,
            "off (read-only); enable with --write-orders or YMS_WRITE_ORDERS=1",
            ("YMS_WRITE_ORDERS",))

    # Promoting a booked order into an invoice is a separate capability, and deliberately
    # not gated on YMS_WRITE_ORDERS: a site can book orders for months and still invoice by
    # hand. Mapping `invoice_write` is the whole opt-in, because supplying that SQL is a
    # decision somebody makes once, on purpose.
    invoiceable = mapping is not None and mapping.invoice_write is not None
    if kind != "database":
        add("order_invoicing", OFF,
            f"a {kind} source cannot raise an invoice in a source database", ())
    elif invoiceable:
        add("order_invoicing", ON,
            "invoice_write is mapped; `orders invoice --apply` promotes shipped orders", ())
    else:
        add("order_invoicing", OFF,
            "off; schema.json has no 'invoice_write' section, so invoicing stays manual",
            ())

    # -- telling somebody ---------------------------------------------------------
    # Deliberately not `missing` when unset. Alerting is opt-in, and a site whose operator
    # watches the pipeline another way has not failed to configure anything. It is still
    # worth a line, because the gap it leaves is invisible by construction: nothing goes
    # wrong until something goes wrong unnoticed.
    notifier = _text("COREYARD_ALERT_COMMAND")
    add("alerting", ON if notifier else OFF,
        "notifier configured" if notifier
        else "no COREYARD_ALERT_COMMAND — `coreyard alert` reports but delivers nothing",
        ("COREYARD_ALERT_COMMAND",))

    # -- source-side capabilities that follow from the mapping -------------------
    delta = bool(traits.supports_delta and mapping is not None and mapping.supports_delta)
    add("delta", ON if delta else OFF,
        "modified-at column mapped" if delta
        else "no delta query in schema.json" if traits.supports_delta
             else f"a {kind} source has no row-level change cursor")

    return Capabilities(source_kind=kind, source_argument=argument, items=items,
                        traits=traits)
