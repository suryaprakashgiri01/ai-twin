"""Paging, sorting, search and filters, with the same query parameters on every list route.

`page`, `pageSize`, `sortColumn`, `sortOrder`, `q` and `filter` (`field:value` pairs, comma
separated). Each route declares a `ListSpec`: the sort columns, filter fields (and their allowed
values) and the fields `q` searches, all by their camelCase API names. Anything outside it is
`422 INVALID_INPUT`, never silently ignored.

Paging runs here, over the newest `LIST_SCAN_MAX` rows the store returns once the filters it can
use have been pushed down to it. One owner's vault stays far below that.

No `from __future__ import annotations` here: the dependency's `Query(...)` descriptions read
`spec` from the closure, which FastAPI cannot resolve from a string annotation.
"""

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import Query
from pydantic.alias_generators import to_snake

from app.api.envelope import ApiModel
from app.exceptions import AppError, ErrorCode

LIST_SCAN_MAX = 1000
PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 200

SortOrder = Literal["asc", "desc"]


class Page[T](ApiModel):
    list_of_items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int
    sort_column: str
    sort_order: SortOrder


@dataclass(frozen=True)
class ListSpec:
    sortable: tuple[str, ...]
    default_sort: str
    # Filter field -> allowed values; None allows any value.
    filters: Mapping[str, Collection[str] | None] = field(default_factory=dict)
    search: tuple[str, ...] = ()
    default_order: SortOrder = "desc"


@dataclass(frozen=True)
class PageQuery:
    page: int = 1
    page_size: int = PAGE_SIZE_DEFAULT
    sort_column: str = ""
    sort_order: SortOrder = "desc"
    q: str | None = None
    filters: Mapping[str, str] = field(default_factory=dict)


def _invalid(message: str) -> AppError:
    return AppError(ErrorCode.INVALID_INPUT, message)


def _filter_pair(raw: str, spec: ListSpec) -> tuple[str, str]:
    name, sep, value = (part.strip() for part in raw.partition(":"))
    if not sep or not name or not value:
        raise _invalid(f"filter takes field:value pairs, got '{raw}'")
    if name not in spec.filters:
        allowed = ", ".join(spec.filters) or "none"
        raise _invalid(f"Unknown filter field '{name}'; filterable: {allowed}")
    values = spec.filters[name]
    if values is not None and value not in values:
        raise _invalid(f"filter {name} must be one of: {', '.join(sorted(values))}")
    return name, value


def parse_filter(raw: str | None, spec: ListSpec) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        name, value = _filter_pair(part, spec)
        if name in found:
            raise _invalid(f"filter names {name} twice")
        found[name] = value
    return found


def page_query(spec: ListSpec) -> Callable[..., PageQuery]:
    """A FastAPI dependency reading the list parameters, checked against `spec`."""
    filterable = ", ".join(spec.filters) or "none"

    def dependency(
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[
            int, Query(alias="pageSize", ge=1, le=PAGE_SIZE_MAX)
        ] = PAGE_SIZE_DEFAULT,
        sort_column: Annotated[
            str | None,
            Query(alias="sortColumn", description=f"one of {', '.join(spec.sortable)}"),
        ] = None,
        sort_order: Annotated[SortOrder | None, Query(alias="sortOrder")] = None,
        q: Annotated[
            str | None, Query(max_length=200, description=f"searches {', '.join(spec.search)}")
        ] = None,
        filter_: Annotated[
            str | None,
            Query(alias="filter", max_length=500, description=f"field:value,…; {filterable}"),
        ] = None,
    ) -> PageQuery:
        column = sort_column or spec.default_sort
        if column not in spec.sortable:
            raise _invalid(f"sortColumn must be one of: {', '.join(spec.sortable)}")
        return PageQuery(
            page=page,
            page_size=page_size,
            sort_column=column,
            sort_order=sort_order or spec.default_order,
            q=(q or "").strip().casefold() or None,
            filters=parse_filter(filter_, spec),
        )

    return dependency


def _value(item: Any, name: str) -> Any:
    return getattr(item, to_snake(name), None)


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return " ".join(_text(v) for v in value)
    return "" if value is None else str(value)


def _matches(item: Any, query: PageQuery, search: Sequence[str]) -> bool:
    for name, wanted in query.filters.items():
        if _text(_value(item, name)).casefold() != wanted.casefold():
            return False
    if query.q is None:
        return True
    return any(query.q in _text(_value(item, name)).casefold() for name in search)


def _sort_key(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else value


def _sorted[T](rows: list[T], query: PageQuery) -> list[T]:
    present = [r for r in rows if _value(r, query.sort_column) is not None]
    missing = [r for r in rows if _value(r, query.sort_column) is None]
    present.sort(
        key=lambda r: _sort_key(_value(r, query.sort_column)), reverse=query.sort_order == "desc"
    )
    return present + missing


def paginate[T](items: Sequence[T], query: PageQuery, spec: ListSpec) -> Page[T]:
    """Filter, search, sort (missing values last) and slice one page of `items`."""
    rows = _sorted([i for i in items if _matches(i, query, spec.search)], query)
    start = (query.page - 1) * query.page_size
    return Page[T](
        list_of_items=rows[start : start + query.page_size],
        page=query.page,
        page_size=query.page_size,
        total=len(rows),
        total_pages=math.ceil(len(rows) / query.page_size),
        sort_column=query.sort_column,
        sort_order=query.sort_order,
    )
