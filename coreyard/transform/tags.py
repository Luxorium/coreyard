"""Tag ownership: keep CoreYard's tags current without deleting anybody else's.

Shopify's ``productSet`` has set semantics — the tag list it is given becomes the whole tag
list. CoreYard generates tags from yard data, so sending only those on every update quietly
deletes any tag another system put there. That matters because storefront systems really do
own tags: a theme that has to warn "this part is pickup only" before checkout reads it from
a tag, and losing it turns a warning into a dead checkout the shopper discovers at the end.

The rule is ownership, not a list of exceptions:

* **CoreYard owns what it generates.** Anything the renderer produces is authoritative, so a
  stale generated tag is replaced rather than accumulated — which is what lets tag repair
  actually repair something.
* **Namespaced tags belong to whoever set them.** CoreYard never emits a ``prefix:value``
  tag, so a tag containing ``:`` is by definition another system's, and it is carried
  through every update untouched. That is what makes ``ship:*``, ``chan:*`` or ``promo:*``
  survive without CoreYard knowing any of them exist.
* **Anything else can be named explicitly** through ``STORE_PRESERVED_TAG_PREFIXES`` or the
  profile's ``preserved_tag_prefixes``, for systems that write plain tags.

Preserved tags keep their original spelling and relative order, and are appended after the
generated ones, so the result is deterministic — the same inputs always produce the same
list, which matters because it is compared against the live product to decide whether a
write is needed at all.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

NAMESPACE_SEPARATOR = ":"


def is_external(tag: str, prefixes: Sequence[str] = (), namespaced: bool = True) -> bool:
    """Whether ``tag`` looks like it belongs to a system other than CoreYard."""
    text = (tag or "").strip()
    if not text:
        return False
    if namespaced and NAMESPACE_SEPARATOR in text:
        return True
    lowered = text.lower()
    return any(lowered.startswith(p.lower()) for p in prefixes if p)


def merge(
    generated: Iterable[str],
    existing: Optional[Iterable[str]] = None,
    prefixes: Sequence[str] = (),
    namespaced: bool = True,
) -> list[str]:
    """CoreYard's tags plus the external tags already on the product.

    ``existing`` is the live product's current tag list — ``None`` for a product that does
    not exist yet, where there is nothing to preserve. Comparison is case-insensitive so a
    tag is never duplicated in a different case, and generated spelling wins.
    """
    out: list[str] = []
    seen: set[str] = set()
    for tag in generated:
        text = (tag or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    for tag in existing or ():
        text = (tag or "").strip()
        if not text or text.lower() in seen:
            continue
        if is_external(text, prefixes, namespaced):
            seen.add(text.lower())
            out.append(text)
    return out


def dropped(
    generated: Iterable[str],
    existing: Iterable[str],
    prefixes: Sequence[str] = (),
    namespaced: bool = True,
) -> list[str]:
    """Existing tags a merge would remove — the CoreYard-owned ones that no longer apply.

    Repair reports this so an operator can see what a rewrite is about to delete before it
    happens, rather than discovering it in the storefront's filters afterwards.
    """
    keep = {t.lower() for t in merge(generated, existing, prefixes, namespaced)}
    return [t for t in existing if (t or "").strip() and t.strip().lower() not in keep]
