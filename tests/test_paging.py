"""Paging, sorting, search and filters shared by every list route."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.api.envelope import ApiModel
from app.api.paging import ListSpec, PageQuery, page_query, paginate, parse_filter
from app.exceptions import AppError, ErrorCode


class Row(ApiModel):
    name: str
    work_mode: str | None = None
    created_at: datetime | None = None


SPEC = ListSpec(
    sortable=("createdAt", "name"),
    default_sort="createdAt",
    filters={"workMode": ("remote", "onsite"), "name": None},
    search=("name",),
)


def at(day: int) -> datetime:
    return datetime(2026, 10, day, tzinfo=UTC)


ROWS = [
    Row(name="Acme", work_mode="remote", created_at=at(1)),
    Row(name="globex", work_mode="onsite", created_at=at(3)),
    Row(name="Initech", work_mode="remote", created_at=None),
    Row(name="Hooli", work_mode="remote", created_at=at(2)),
]


def query(**kwargs: object) -> PageQuery:
    dependency = page_query(SPEC)
    defaults: dict[str, object] = {
        "page": 1,
        "page_size": 20,
        "sort_column": None,
        "sort_order": None,
        "q": None,
        "filter_": None,
    }
    built: PageQuery = dependency(**{**defaults, **kwargs})
    return built


def names(q: PageQuery) -> list[str]:
    return [r.name for r in paginate(ROWS, q, SPEC).list_of_items]


def test_defaults_sort_newest_first_with_missing_values_last() -> None:
    q = query()
    assert (q.sort_column, q.sort_order) == ("createdAt", "desc")
    assert names(q) == ["globex", "Hooli", "Acme", "Initech"]


def test_sort_by_name_is_case_insensitive() -> None:
    assert names(query(sort_column="name", sort_order="asc")) == [
        "Acme",
        "globex",
        "Hooli",
        "Initech",
    ]


def test_page_slices_and_counts() -> None:
    page = paginate(ROWS, query(page=2, page_size=3), SPEC)
    assert [r.name for r in page.list_of_items] == ["Initech"]
    assert (page.total, page.total_pages, page.page, page.page_size) == (4, 2, 2, 3)
    dumped = page.model_dump(by_alias=True)
    assert {"listOfItems", "pageSize", "totalPages", "sortColumn", "sortOrder"} <= set(dumped)


def test_filter_and_search_combine() -> None:
    assert names(query(filter_="workMode:remote", q="  HO ")) == ["Hooli"]
    assert names(query(filter_="workMode:onsite,name:GLOBEX")) == ["globex"]


def test_empty_list_has_zero_pages() -> None:
    page = paginate(list[Row](), query(), SPEC)
    assert (page.total, page.total_pages, page.list_of_items) == (0, 0, [])


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"sort_column": "workMode"}, "sortColumn must be one of"),
        ({"filter_": "tier:public"}, "Unknown filter field 'tier'"),
        ({"filter_": "workMode:hybrid"}, "workMode must be one of"),
        ({"filter_": "workMode"}, "field:value"),
        ({"filter_": "name:a,name:b"}, "twice"),
    ],
)
def test_bad_sort_or_filter_is_invalid_input(kwargs: dict[str, object], fragment: str) -> None:
    with pytest.raises(AppError) as caught:
        query(**kwargs)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert fragment in str(caught.value)


def test_blank_filter_parts_are_ignored() -> None:
    assert parse_filter(" , workMode:remote ,", SPEC) == {"workMode": "remote"}
