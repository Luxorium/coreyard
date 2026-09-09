"""The original source: the site's SQL Server, over the SMB named pipe.

This is an adapter, not a reimplementation. Every query still comes from
:mod:`coreyard.yms.inventory` and every table and column name still comes from
``schema.json``; all this class does is present them through :class:`~coreyard.source.Source`
so the rest of CoreYard can stop importing the transport directly.

It is the default, so an installation that sets no ``COREYARD_SOURCE`` behaves exactly as
it did before the seam existed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from coreyard.models import Part
from coreyard.source import SourceTraits


class DatabaseSource:
    """Parts from the configured source database."""

    TRAITS = SourceTraits(
        kind="database",
        needs_schema_mapping=True,      # CoreYard ships no vendor schema
        needs_database_config=True,     # YMS_DB_HOST / YMS_DB_NAME name the server
        needs_smb=True,                 # SQL over the \sql\query named pipe
        carries_own_photos=False,       # photographs live on a separate SMB share
        supports_delta=True,            # when the mapping supplies a modified-at column
        supports_fitment=True,          # the interchange catalogue is in the same database
        supports_order_booking=True,    # still gated by YMS_WRITE_ORDERS and order_write
    )

    @classmethod
    def probe(cls, argument: str) -> tuple[str, str, tuple[str, ...]]:
        """Credentials only. Whether the server answers is `ping`'s question, not this one."""
        from coreyard.capabilities import MISSING, ON, _text

        keys = ("SMB_HOST", "SMB_USER", "SMB_PASSWORD")
        absent = [key for key in keys if not _text(key)]
        if absent:
            return MISSING, f"missing {', '.join(absent)}", keys
        return ON, f"source database at {_text('SMB_HOST')}", keys

    def parts(self, limit: Optional[int] = None,
              images_only: Optional[bool] = None) -> list[Part]:
        from coreyard.yms.inventory import fetch_parts
        return fetch_parts(limit=limit, images_only=images_only)

    def parts_by_r_number(self, r_numbers: list[str]) -> dict[str, Part]:
        from coreyard.yms.inventory import fetch_parts_by_r_number
        return fetch_parts_by_r_number(r_numbers)

    def listable_r_numbers(self, images_only: Optional[bool] = None) -> set[str]:
        from coreyard.yms.inventory import listable_r_numbers
        return listable_r_numbers(images_only=images_only)

    def server_now(self) -> datetime:
        from coreyard.yms.db import connect, server_now
        with connect() as connection:
            return server_now(connection)

    def ping(self) -> str:
        from coreyard.yms.db import ping
        return ping()
