"""Household setup API (Phase 1, P1-2): budget and anonymous members, one household per user."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app import crud
from app.api.routes.household import _create_household
from app.core.config import settings
from app.models import Household, HouseholdMember, UserCreate
from tests.utils.user import create_random_user, user_authentication_headers
from tests.utils.utils import random_email, random_lower_string

URL = f"{settings.API_V1_STR}/household/"


def _new_user_headers(client: TestClient, db: Session) -> dict[str, str]:
    """A fresh user, so each test starts without a household."""
    email, password = random_email(), random_lower_string()
    crud.create_user(session=db, user_create=UserCreate(email=email, password=password))
    return user_authentication_headers(client=client, email=email, password=password)


def _member_count(db: Session, household_id: str) -> int:
    db.expire_all()
    return db.exec(
        select(func.count())
        .select_from(HouseholdMember)
        .where(HouseholdMember.household_id == uuid.UUID(household_id))
    ).one()


def test_first_read_creates_the_household_with_defaults(
    client: TestClient, db: Session
) -> None:
    headers = _new_user_headers(client, db)

    r = client.get(URL, headers=headers)

    assert r.status_code == 200
    body = r.json()
    assert body["region_code"] == "north-al"
    assert body["user_setting"] == "store_only"
    assert body["produce_budget_cents_per_week"] is None
    assert [m["calorie_level"] for m in body["members"]] == [2000]


def test_reading_twice_returns_the_same_household(
    client: TestClient, db: Session
) -> None:
    headers = _new_user_headers(client, db)

    first = client.get(URL, headers=headers).json()
    second = client.get(URL, headers=headers).json()

    assert first["id"] == second["id"]
    assert first["members"] == second["members"]


def test_update_sets_the_budget_and_members_in_order(
    client: TestClient, db: Session
) -> None:
    headers = _new_user_headers(client, db)
    payload = {"produce_budget_cents_per_week": 4500, "members": [2400, 1600, 2000]}

    r = client.put(URL, headers=headers, json=payload)

    assert r.status_code == 200
    assert r.json()["produce_budget_cents_per_week"] == 4500
    assert [m["calorie_level"] for m in r.json()["members"]] == [2400, 1600, 2000]
    assert client.get(URL, headers=headers).json() == r.json()


def test_update_before_any_read_creates_the_household(
    client: TestClient, db: Session
) -> None:
    headers = _new_user_headers(client, db)

    r = client.put(URL, headers=headers, json={"members": [1800, 1800]})

    assert r.status_code == 200
    assert r.json()["region_code"] == "north-al"
    assert [m["calorie_level"] for m in r.json()["members"]] == [1800, 1800]


def test_members_can_be_added_and_removed(client: TestClient, db: Session) -> None:
    headers = _new_user_headers(client, db)
    household_id = client.get(URL, headers=headers).json()["id"]

    added = client.put(URL, headers=headers, json={"members": [2000, 2000, 1400]})
    assert len(added.json()["members"]) == 3
    assert _member_count(db, household_id) == 3

    removed = client.put(URL, headers=headers, json={"members": [1400]})
    assert [m["calorie_level"] for m in removed.json()["members"]] == [1400]
    # The removed rows are gone from the database, not just hidden.
    assert _member_count(db, household_id) == 1


def test_update_clears_the_budget_when_it_is_omitted(
    client: TestClient, db: Session
) -> None:
    headers = _new_user_headers(client, db)
    client.put(
        URL,
        headers=headers,
        json={"produce_budget_cents_per_week": 3000, "members": [2000]},
    )

    r = client.put(URL, headers=headers, json={"members": [2000]})

    assert r.json()["produce_budget_cents_per_week"] is None


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"members": [2100]}, "members"),  # not a Dietary Guidelines level
        ({"members": [800]}, "members"),
        ({"members": [3400]}, "members"),
        ({"members": []}, "members"),  # a household has at least one person
        ({"members": [2000] * 21}, "members"),
        ({"members": [2000], "produce_budget_cents_per_week": -1}, "produce_budget"),
        (
            {"members": [2000], "produce_budget_cents_per_week": 1_000_001},
            "produce_budget",
        ),
        ({}, "members"),
    ],
)
def test_update_rejects_invalid_values(
    client: TestClient, db: Session, payload: dict[str, Any], field: str
) -> None:
    headers = _new_user_headers(client, db)
    before = client.get(URL, headers=headers).json()

    r = client.put(URL, headers=headers, json=payload)

    assert r.status_code == 422
    assert any(field in ".".join(map(str, e["loc"])) for e in r.json()["detail"])
    assert client.get(URL, headers=headers).json() == before


@pytest.mark.parametrize(
    "extra",
    [
        {"name": "Riley"},
        {"members_detail": [{"calorie_level": 2000, "age": 30}]},
        {"weight_lb": 180},
    ],
)
def test_update_rejects_personal_fields(
    client: TestClient, db: Session, extra: dict[str, Any]
) -> None:
    """Members are a calorie level and nothing else; other fields are refused, not dropped."""
    headers = _new_user_headers(client, db)

    r = client.put(URL, headers=headers, json={"members": [2000]} | extra)

    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "extra_forbidden"


def test_members_accept_only_a_calorie_level(client: TestClient, db: Session) -> None:
    headers = _new_user_headers(client, db)

    r = client.put(
        URL, headers=headers, json={"members": [{"calorie_level": 2000, "name": "A"}]}
    )

    assert r.status_code == 422


def test_users_see_and_change_only_their_own_household(
    client: TestClient, db: Session
) -> None:
    alice = _new_user_headers(client, db)
    bob = _new_user_headers(client, db)
    bob_before = client.get(URL, headers=bob).json()

    alice_household = client.put(URL, headers=alice, json={"members": [1200, 1200]})

    assert alice_household.json()["id"] != bob_before["id"]
    assert client.get(URL, headers=bob).json() == bob_before


def test_household_requires_sign_in(client: TestClient) -> None:
    assert client.get(URL).status_code == 401
    assert client.put(URL, json={"members": [2000]}).status_code == 401


def test_concurrent_first_access_returns_the_existing_household(db: Session) -> None:
    """If another request created the household first, creating again returns that one.

    Simulates the race directly: the household already exists when _create_household inserts,
    so the unique owner_id rejects the insert and the existing row is returned.
    """
    user = create_random_user(db)
    existing = _create_household(db, user.id)

    again = _create_household(db, user.id)

    assert again.id == existing.id
    assert (
        db.exec(
            select(func.count())
            .select_from(Household)
            .where(Household.owner_id == user.id)
        ).one()
        == 1
    )
