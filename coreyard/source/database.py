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


class DatabaseSource:
    """Parts from the configured source database."""

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
