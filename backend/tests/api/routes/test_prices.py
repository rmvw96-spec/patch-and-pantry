"""Price book, catalog, aliases, and item settings APIs (P1-7), against real Postgres.

Items are owned by the test user's household (scope "household"), so the tests do not depend on
the shared national catalog, and everything is deleted with the users when the session ends.
"""

import uuid
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    CatalogItem,
    CatalogPortion,
    Household,
    HouseholdAlias,
    PriceObservation,
)
from app.tools.catalog import find_catalog_item
from app.tools.normalize import GRAMS_PER_LB

API = settings.API_V1_STR
PRICES = f"{API}/prices/"


def _household(db: Session, client: TestClient, headers: dict[str, str]) -> Household:
    household_id = client.get(f"{API}/household/", headers=headers).json()["id"]
    household = db.get(Household, uuid.UUID(household_id))
    assert household is not None
    return household


def _item(db: Session, household: Household, name: str, **fields: Any) -> CatalogItem:
    values: dict[str, Any] = {
        "slug": f"test-{uuid.uuid4().hex[:10]}",
        "name": name,
        "kind": "produce",
        "category": "vegetable",
        "scope": "household",
        "household_id": household.id,
        "preparation_yield_factor": 0.9,
        "cup_equivalent_lb": 0.3527,
        "conversion_source": "test",
    }
    item = CatalogItem(**(values | fields))
    db.add(item)
    db.commit()
    return item


@pytest.fixture
def mine(
    db: Session, client: TestClient, normal_user_token_headers: dict[str, str]
) -> dict[str, Any]:
    """The normal user's household, a store, and items: onion, kale, a tomato with sizes."""
    household = _household(db, client, normal_user_token_headers)
    vendor = client.post(
        f"{API}/vendors/",
        headers=normal_user_token_headers,
        json={"name": "Corner store"},
    ).json()
    tomato = _item(
        db,
        household,
        f"Test tomato {uuid.uuid4().hex[:4]}",
        preparation_yield_factor=0.91,
        cup_equivalent_lb=0.3748,
    )
    for size, grams in (("medium", 135.2), ("large", 200.0)):
        db.add(
            CatalogPortion(
                catalog_item_id=tomato.id,
                unit="each",
                size=size,
                grams=grams,
                description=f"{size} tomato",
                source="owner measurement",
                measured_on=date(2026, 11, 1),
            )
        )
    db.commit()
    return {
        "household": household,
        "vendor_id": vendor["id"],
        "onion": _item(db, household, f"Test onion {uuid.uuid4().hex[:4]}"),
        "kale": _item(db, household, f"Test kale {uuid.uuid4().hex[:4]}", preparation_yield_factor=1.05, cup_equivalent_lb=0.2866),
        "eggplant": _item(db, household, f"Test eggplant {uuid.uuid4().hex[:4]}", preparation_yield_factor=None, cup_equivalent_lb=None),
        "tomato": tomato,
    }  # fmt: skip


def _entry(item: CatalogItem, **fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "raw_text": f"{item.name} 3 lb 2.99",
        "catalog_item_id": str(item.id),
        "quantity": 3,
        "unit": "lb",
        "price_cents": 299,
    }
    return values | fields


def _save(
    client: TestClient,
    headers: dict[str, str],
    mine: dict[str, Any],
    *entries: dict[str, Any],
) -> Any:
    body = {
        "vendor_id": mine["vendor_id"],
        "observed_on": "2026-11-11",
        "entries": list(entries),
    }
    return client.post(PRICES, headers=headers, json=body)


# ---------- confirming drafts ----------


def test_confirmed_drafts_are_saved_with_their_cost_per_cup(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    r = _save(
        client, normal_user_token_headers, mine, _entry(mine["onion"], unit="Pounds")
    )

    assert r.status_code == 201, r.text
    (price,) = r.json()
    assert price["unit"] == "lb"  # spelled one way when saved
    assert price["vendor_name"] == "Corner store"
    assert price["raw_text"].endswith("3 lb 2.99")
    assert price["cost_per_cup_cents"] / 100 == pytest.approx(0.3906, abs=1e-4)
    assert price["note"] is None


def test_a_weight_answer_is_converted_by_the_tool(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    mine: dict[str, Any],
) -> None:
    """ "About half a pound" becomes 226.8 g in code, never in the browser or the model."""
    entry = _entry(
        mine["kale"],
        raw_text="kale bunch 2.50",
        quantity=1,
        unit="bunch",
        price_cents=250,
        piece_weight={"amount": 0.5, "unit": "lb", "source": "estimated"},
    )

    (price,) = _save(client, normal_user_token_headers, mine, entry).json()

    assert price["unit_grams"] == pytest.approx(GRAMS_PER_LB / 2)
    assert price["weight_source"] == "estimated"
    assert price["cost_per_cup_cents"] is not None


def test_a_size_answer_uses_that_pieces_weight(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    entry = _entry(
        mine["tomato"],
        raw_text="tomatoes 3 for $5",
        unit="each",
        price_cents=500,
        size="large",
    )
    (price,) = _save(client, normal_user_token_headers, mine, entry).json()
    assert price["size"] == "large"
    assert price["unit_grams"] is None  # the weight comes from the item's large piece
    assert price["cost_per_cup_cents"] is not None


def test_an_item_without_a_conversion_is_saved_by_price_only(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    (price,) = _save(
        client, normal_user_token_headers, mine, _entry(mine["eggplant"], quantity=1)
    ).json()
    assert price["cost_per_cup_cents"] is None
    assert "no cup conversion" in price["note"]


@pytest.mark.parametrize(
    ("fields", "detail"),
    [
        ({"unit": "sack"}, "Unknown unit 'sack'"),
        (
            {"unit": "bunch", "quantity": 1},
            "How much does one bunch",
        ),  # no piece weight at all
    ],
)
def test_unsavable_entries_are_refused(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    mine: dict[str, Any],
    fields: dict[str, Any],
    detail: str,
) -> None:
    item = mine["tomato"] if fields["unit"] == "each" else mine["onion"]
    r = _save(client, normal_user_token_headers, mine, _entry(item, **fields))
    assert r.status_code == 422
    assert detail in r.json()["detail"]


def test_saving_is_all_or_none(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    before = len(client.get(PRICES, headers=normal_user_token_headers).json())
    r = _save(
        client,
        normal_user_token_headers,
        mine,
        _entry(mine["onion"]),
        _entry(mine["onion"], unit="sack"),
    )
    assert r.status_code == 422
    assert len(client.get(PRICES, headers=normal_user_token_headers).json()) == before


def test_a_confirmed_fuzzy_match_becomes_a_household_alias(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    mine: dict[str, Any],
    db: Session,
) -> None:
    onion = mine["onion"]
    _save(
        client,
        normal_user_token_headers,
        mine,
        _entry(onion, remember_text="Yellow Onoins"),
    )  # spellchecker:disable-line

    db.expire_all()
    match = find_catalog_item(
        db, "yellow onoins", mine["household"]
    )  # spellchecker:disable-line
    assert match.kind == "household" and match.item is not None
    assert match.item.item_id == onion.id


def test_only_your_own_stores_and_visible_items(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    mine: dict[str, Any],
) -> None:
    theirs = client.post(
        f"{API}/vendors/", headers=superuser_token_headers, json={"name": "Theirs"}
    ).json()
    body = {
        "vendor_id": theirs["id"],
        "observed_on": "2026-11-11",
        "entries": [_entry(mine["onion"])],
    }
    r = client.post(PRICES, headers=normal_user_token_headers, json=body)
    assert r.json()["detail"] == "Choose one of your own stores."
    # The normal user's private item is not visible to the superuser's household.
    body = {
        "vendor_id": theirs["id"],
        "observed_on": "2026-11-11",
        "entries": [_entry(mine["onion"])],
    }
    r = client.post(PRICES, headers=superuser_token_headers, json=body)
    assert r.status_code == 422 and r.json()["detail"] == "Unknown produce item."


@pytest.mark.parametrize(
    "fields",
    [
        {"price_cents": -1},
        {"price_cents": 1_000_001},
        {"quantity": 0},
        {"size": "jumbo"},
        {"name": "x"},
    ],
)
def test_entry_values_are_checked(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    mine: dict[str, Any],
    fields: dict[str, Any],
) -> None:
    assert (
        _save(
            client, normal_user_token_headers, mine, _entry(mine["onion"], **fields)
        ).status_code
        == 422
    )


# ---------- the price book ----------


def _book(client: TestClient, headers: dict[str, str], item: CatalogItem) -> list[Any]:
    return [
        p
        for p in client.get(PRICES, headers=headers).json()
        if p["catalog_item_id"] == str(item.id)
    ]


def test_saving_a_line_again_updates_it(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    """Owner, 2026-11-12: one price per line as typed, store, and day; re-saving adds no copy."""
    headers, onion = normal_user_token_headers, mine["onion"]
    (first,) = _save(
        client, headers, mine, _entry(onion, raw_text="Onions 2 lb $1")
    ).json()
    assert first["replaced"] is False

    # The same line, read better and spaced differently: the same price, corrected.
    again = _save(
        client, headers, mine,
        _entry(onion, raw_text="  Onions   2 lb $1 ", quantity=2, price_cents=100),
    )  # fmt: skip

    assert again.status_code == 201, again.text
    (second,) = again.json()
    assert (second["id"], second["replaced"]) == (first["id"], True)
    assert (second["quantity"], second["price_cents"]) == (2, 100)
    assert [p["id"] for p in _book(client, headers, onion)] == [first["id"]]


def test_different_lines_days_and_stores_stay_separate(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    headers, onion = normal_user_token_headers, mine["onion"]
    bag, loose = (
        _entry(onion, raw_text="onions 3 lb bag 2.99"),
        _entry(onion, raw_text="onions 1.29/lb", quantity=1, price_cents=129),
    )
    saved = _save(client, headers, mine, bag, loose).json()
    assert [p["replaced"] for p in saved] == [False, False]
    other_day = {
        "vendor_id": mine["vendor_id"],
        "observed_on": "2026-11-12",
        "entries": [bag],
    }
    store = client.post(
        f"{API}/vendors/", headers=headers, json={"name": "Market"}
    ).json()
    other_store = {**other_day, "observed_on": "2026-11-11", "vendor_id": store["id"]}
    for body in (other_day, other_store):
        r = client.post(PRICES, headers=headers, json=body)
        assert r.json()[0]["replaced"] is False

    assert len(_book(client, headers, onion)) == 4


def test_a_line_listed_twice_in_one_save_is_one_price(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    headers, onion = normal_user_token_headers, mine["onion"]
    r = _save(client, headers, mine, _entry(onion), _entry(onion, price_cents=349))
    assert r.status_code == 201, r.text
    (price,) = _book(client, headers, onion)
    assert price["price_cents"] == 349  # the later entry wins


def test_an_edit_cannot_make_a_second_copy_of_a_line(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    headers, onion = normal_user_token_headers, mine["onion"]
    (monday,) = _save(client, headers, mine, _entry(onion)).json()
    body = {
        "vendor_id": mine["vendor_id"],
        "observed_on": "2026-11-12",
        "entries": [_entry(onion)],
    }
    (tuesday,) = client.post(PRICES, headers=headers, json=body).json()
    update = {
        "vendor_id": mine["vendor_id"],
        "observed_on": "2026-11-11",
        "quantity": 3,
        "unit": "lb",
        "price_cents": 299,
    }

    r = client.put(f"{PRICES}{tuesday['id']}", headers=headers, json=update)

    assert r.status_code == 422
    assert "already have this line" in r.json()["detail"]
    assert {p["id"] for p in _book(client, headers, onion)} == {
        monday["id"],
        tuesday["id"],
    }


def test_the_price_book_lists_newest_first(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    onion = mine["onion"]
    for day in ("2026-11-01", "2026-11-09"):
        body = {
            "vendor_id": mine["vendor_id"],
            "observed_on": day,
            "entries": [_entry(onion, raw_text=f"onions {day}")],
        }
        client.post(PRICES, headers=normal_user_token_headers, json=body)

    book = [
        p
        for p in client.get(PRICES, headers=normal_user_token_headers).json()
        if p["catalog_item_id"] == str(onion.id)
    ]

    assert [p["raw_text"] for p in book][:2] == [
        "onions 2026-11-09",
        "onions 2026-11-01",
    ]


def test_a_price_can_be_edited_and_deleted(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    (price,) = _save(
        client, normal_user_token_headers, mine, _entry(mine["onion"])
    ).json()
    update = {
        "vendor_id": mine["vendor_id"],
        "observed_on": "2026-11-12",
        "quantity": 5,
        "unit": "lb",
        "price_cents": 399,
    }

    edited = client.put(
        f"{PRICES}{price['id']}", headers=normal_user_token_headers, json=update
    )

    assert edited.status_code == 200, edited.text
    assert (edited.json()["quantity"], edited.json()["price_cents"]) == (5, 399)
    assert edited.json()["raw_text"] == price["raw_text"]  # the line as typed stays
    assert edited.json()["cost_per_cup_cents"] < price["cost_per_cup_cents"]
    deleted = client.delete(f"{PRICES}{price['id']}", headers=normal_user_token_headers)
    assert deleted.status_code == 200
    ids = [
        p["id"] for p in client.get(PRICES, headers=normal_user_token_headers).json()
    ]
    assert price["id"] not in ids


def test_several_prices_can_be_deleted_at_once(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    headers, onion = normal_user_token_headers, mine["onion"]
    saved = _save(
        client, headers, mine,
        *(_entry(onion, raw_text=f"onions {n} lb 2.99") for n in range(3)),
    ).json()  # fmt: skip
    gone, kept = [p["id"] for p in saved[:2]], saved[2]["id"]

    r = client.post(f"{PRICES}delete", headers=headers, json={"ids": gone})

    assert r.status_code == 200, r.text
    assert r.json()["message"] == "Deleted 2 prices"
    assert [p["id"] for p in _book(client, headers, onion)] == [kept]


def test_deleting_several_is_all_or_none(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    mine: dict[str, Any],
) -> None:
    """One price that is not yours (or does not exist) and nothing is deleted."""
    headers, onion = normal_user_token_headers, mine["onion"]
    (mine_price,) = _save(client, headers, mine, _entry(onion)).json()
    unknown = str(uuid.uuid4())
    r = client.post(
        f"{PRICES}delete", headers=headers, json={"ids": [mine_price["id"], unknown]}
    )
    assert r.status_code == 404
    # Someone else cannot delete it, even listed alone.
    r = client.post(
        f"{PRICES}delete",
        headers=superuser_token_headers,
        json={"ids": [mine_price["id"]]},
    )
    assert r.status_code == 404
    assert [p["id"] for p in _book(client, headers, onion)] == [mine_price["id"]]
    assert (
        client.post(f"{PRICES}delete", headers=headers, json={"ids": []}).status_code
        == 422
    )


def test_editing_a_bunch_keeps_its_weight(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    entry = _entry(
        mine["kale"],
        quantity=1,
        unit="bunch",
        price_cents=250,
        piece_weight={"amount": 280, "unit": "g", "source": "weighed"},
    )
    (price,) = _save(client, normal_user_token_headers, mine, entry).json()
    update = {
        "vendor_id": mine["vendor_id"],
        "observed_on": "2026-11-11",
        "quantity": 1,
        "unit": "bunch",
        "price_cents": 199,
    }

    edited = client.put(
        f"{PRICES}{price['id']}", headers=normal_user_token_headers, json=update
    ).json()

    assert (edited["unit_grams"], edited["weight_source"]) == (280, "weighed")
    assert edited["price_cents"] == 199


def test_others_cannot_edit_or_delete_your_prices(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    mine: dict[str, Any],
) -> None:
    (price,) = _save(
        client, normal_user_token_headers, mine, _entry(mine["onion"])
    ).json()
    update = {
        "vendor_id": mine["vendor_id"],
        "observed_on": "2026-11-11",
        "quantity": 1,
        "unit": "lb",
        "price_cents": 1,
    }

    assert (
        client.put(
            f"{PRICES}{price['id']}", headers=superuser_token_headers, json=update
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"{PRICES}{price['id']}", headers=superuser_token_headers
        ).status_code
        == 404
    )
    theirs = [
        p["id"] for p in client.get(PRICES, headers=superuser_token_headers).json()
    ]
    assert price["id"] not in theirs
    assert (
        client.delete(
            f"{PRICES}{uuid.uuid4()}", headers=normal_user_token_headers
        ).status_code
        == 404
    )


def test_prices_require_authentication(client: TestClient) -> None:
    assert client.get(PRICES).status_code == 401
    assert client.post(PRICES, json={}).status_code == 401


# ---------- catalog, aliases, and item settings ----------


def test_the_catalog_lists_visible_produce_by_name(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    mine: dict[str, Any],
) -> None:
    names = [
        i["name"]
        for i in client.get(f"{API}/catalog/", headers=normal_user_token_headers).json()
    ]
    assert mine["onion"].name in names
    assert names == sorted(names, key=lambda n: (n.casefold(), n))
    theirs = [
        i["name"]
        for i in client.get(f"{API}/catalog/", headers=superuser_token_headers).json()
    ]
    assert mine["onion"].name not in theirs


def test_choosing_an_item_saves_an_alias(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    mine: dict[str, Any],
    db: Session,
) -> None:
    r = client.post(
        f"{API}/household/aliases",
        headers=normal_user_token_headers,
        json={"text": "Greens!", "catalog_item_id": str(mine["kale"].id)},
    )
    assert r.status_code == 201, r.text
    assert r.json()["text"] == "green"  # folded
    db.expire_all()
    stored = db.exec(
        select(HouseholdAlias).where(
            HouseholdAlias.household_id == mine["household"].id,
            HouseholdAlias.text == "green",
        )
    ).one()
    assert stored.catalog_item_id == mine["kale"].id
    bad = client.post(
        f"{API}/household/aliases",
        headers=normal_user_token_headers,
        json={"text": "123", "catalog_item_id": str(mine["kale"].id)},
    )
    assert bad.status_code == 422
    unknown = client.post(
        f"{API}/household/aliases",
        headers=normal_user_token_headers,
        json={"text": "x", "catalog_item_id": str(uuid.uuid4())},
    )
    assert unknown.status_code == 422


def test_item_settings_can_be_set_and_read(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    url = f"{API}/household/item-settings"
    kale = str(mine["kale"].id)

    on = client.put(
        f"{url}/{kale}", headers=normal_user_token_headers, json={"freeze_extras": True}
    )
    off = client.put(
        f"{url}/{kale}",
        headers=normal_user_token_headers,
        json={"freeze_extras": False},
    )

    assert on.json()["freeze_extras"] is True and off.json()["freeze_extras"] is False
    settings_ = [
        s
        for s in client.get(url, headers=normal_user_token_headers).json()
        if s["catalog_item_id"] == kale
    ]
    assert settings_ == [
        {"catalog_item_id": kale, "freeze_extras": False}
    ]  # updated, not duplicated
    assert (
        client.put(
            f"{url}/{uuid.uuid4()}",
            headers=normal_user_token_headers,
            json={"freeze_extras": True},
        ).status_code
        == 422
    )


# ---------- items compared per piece, and nuts in the shell (spec P1-11) ----------


def test_a_piece_item_is_saved_and_listed_per_piece(
    db: Session,
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    mine: dict[str, Any],
) -> None:
    lemon = _item(
        db,
        mine["household"],
        f"Test lemon {uuid.uuid4().hex[:4]}",
        compared_by="each",
        preparation_yield_factor=None,
        cup_equivalent_lb=None,
    )
    r = _save(
        client,
        normal_user_token_headers,
        mine,
        _entry(lemon, raw_text=f"{lemon.name} 3 for $1", quantity=3, unit="each", price_cents=100),
    )  # fmt: skip
    assert r.status_code == 201, r.text
    (price,) = r.json()
    assert price["measure"] == lemon.name.lower()
    assert price["cost_per_piece_cents"] == pytest.approx(33.33, abs=0.01)
    assert price["cost_per_cup_cents"] is None and price["note"] is None
    catalog = client.get(f"{API}/catalog/", headers=normal_user_token_headers).json()
    measures = {i["name"]: i["measure"] for i in catalog}
    assert measures[lemon.name] == lemon.name.lower()
    assert measures[mine["onion"].name] == "cup"


def test_nuts_in_the_shell_are_saved_and_edited_as_such(
    db: Session,
    client: TestClient,
    normal_user_token_headers: dict[str, str],
    mine: dict[str, Any],
) -> None:
    walnut = _item(
        db,
        mine["household"],
        f"Test walnut {uuid.uuid4().hex[:4]}",
        preparation_yield_factor=1.0,
        cup_equivalent_lb=0.2579,
        shell_refuse_pct=55,
    )
    entry = _entry(
        walnut, raw_text="walnuts with shell $8/lb", quantity=1, price_cents=800
    )
    r = _save(client, normal_user_token_headers, mine, entry | {"in_shell": True})
    assert r.status_code == 201, r.text
    (price,) = r.json()
    assert price["in_shell"] is True
    assert price["cost_per_cup_cents"] == pytest.approx(459, abs=1)
    observation = db.get(PriceObservation, uuid.UUID(price["id"]))
    assert observation is not None
    db.refresh(observation)
    assert observation.grams_edible == pytest.approx(453.59237 * 0.45)

    edit = {
        "vendor_id": mine["vendor_id"],
        "observed_on": "2026-11-11",
        "quantity": 1,
        "unit": "lb",
        "price_cents": 800,
        "in_shell": False,
    }
    r = client.put(
        f"{PRICES}{price['id']}", headers=normal_user_token_headers, json=edit
    )
    assert r.status_code == 200, r.text
    assert r.json()["in_shell"] is False
    assert r.json()["cost_per_cup_cents"] == pytest.approx(
        800 / (453.59237 / 117), abs=1
    )


def test_in_shell_is_ignored_for_an_item_without_a_shell(
    client: TestClient, normal_user_token_headers: dict[str, str], mine: dict[str, Any]
) -> None:
    r = _save(
        client,
        normal_user_token_headers,
        mine,
        _entry(mine["onion"]) | {"in_shell": True},
    )
    assert r.status_code == 201, r.text
    assert r.json()[0]["in_shell"] is False
    assert r.json()[0]["cost_per_cup_cents"] / 100 == pytest.approx(0.3906, abs=1e-4)
