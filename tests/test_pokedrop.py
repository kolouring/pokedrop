"""Offline test suite.

Exercises payload parsing, the database, and the event diff engine against
fixtures shaped like real RedSky responses. No network, no Discord token.

Run:  python tests/test_pokedrop.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pokedrop.db import Database
from pokedrop.monitor import EventKind, Monitor
from pokedrop.target_api import (
    Product,
    RedSkyClient,
    StoreStock,
    _parse_street_date,
    looks_like_pokemon,
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, note: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {note}")


# ----------------------------------------------------------------- fixtures

SEARCH_PAYLOAD = {
    "data": {"search": {"products": [
        {
            "tcin": "94300069",
            "item": {
                "dpci": "087-06-4321",
                "product_description": {"title": "Pokemon Trading Card Game: Scarlet & Violet Elite Trainer Box"},
                "enrichment": {
                    "buy_url": "https://www.target.com/p/pokemon-etb/-/A-94300069",
                    "images": {"primary_image_url": "https://target.scene7.com/is/image/Target/etb"},
                },
                "mmbv_content": {"street_date": "2026-09-26"},
            },
            "price": {"current_retail": 49.99},
        },
        {
            "tcin": "88888888",
            "item": {
                "product_description": {"title": "Magic: The Gathering Bundle"},
                "enrichment": {"buy_url": "https://www.target.com/p/mtg/-/A-88888888"},
            },
            "price": {"current_retail": 39.99},
        },
        # Bundle shape: the real SKU is nested one level down.
        {
            "items": [{
                "tcin": "77777777",
                "item": {
                    "dpci": "087-06-1111",
                    "product_description": {"title": "Pokémon TCG Booster Bundle"},
                },
                "price": {"current_retail": 26.99},
            }]
        },
    ]}}
}

SUMMARY_OOS = {
    "data": {"product_summaries": [{
        "tcin": "94300069",
        "item": {
            "dpci": "087-06-4321",
            "product_description": {"title": "Pokemon TCG Elite Trainer Box"},
        },
        "price": {"current_retail": 49.99},
        "fulfillment": {
            "shipping_options": {"availability_status": "OUT_OF_STOCK"},
            "store_options": [
                {"location_id": "1357", "location_name": "Minneapolis",
                 "order_pickup": {"availability_status": "OUT_OF_STOCK"}},
            ],
        },
    }]}
}

SUMMARY_IN_STOCK = {
    "data": {"product_summaries": [{
        "tcin": "94300069",
        "item": {
            "dpci": "087-06-4321",
            "product_description": {"title": "Pokemon TCG Elite Trainer Box"},
        },
        "price": {"current_retail": 49.99},
        "fulfillment": {
            "shipping_options": {"availability_status": "IN_STOCK"},
            "store_options": [
                {"location_id": "1357", "location_name": "Minneapolis",
                 "order_pickup": {"availability_status": "IN_STOCK"},
                 "location_available_to_promise_quantity": 12},
            ],
        },
    }]}
}

SUMMARY_PREORDER = {
    "data": {"product_summaries": [{
        "tcin": "94300069",
        "item": {"product_description": {"title": "Pokemon TCG Elite Trainer Box"}},
        "fulfillment": {
            "shipping_options": {"availability_status": "PRE_ORDER_SELLABLE"},
            "store_options": [],
        },
    }]}
}

PDP_PAYLOAD = {
    "data": {"product": {
        "tcin": "94300069",
        "item": {
            "dpci": "087-06-4321",
            "product_description": {"title": "Pokemon TCG Elite Trainer Box"},
            "mmbv_content": {"street_date": "2026-09-26"},
            "enrichment": {"buy_url": "https://www.target.com/p/x/-/A-94300069"},
        },
        "price": {"current_retail": 49.99},
        "fulfillment": {"shipping_options": {"availability_status": "OUT_OF_STOCK"}},
    }}
}


class StubClient(RedSkyClient):
    """RedSkyClient with the HTTP layer replaced by canned payloads."""

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self.summary_payload = SUMMARY_OOS
        self.search_payload = SEARCH_PAYLOAD
        self.pdp_payload = PDP_PAYLOAD
        self.calls: list[str] = []

    async def _get(self, url, params, attempts=3):
        name = url.rsplit("/", 1)[-1]
        self.calls.append(name)
        if name == "plp_search_v2":
            # Second page empty so paging terminates.
            if int(params.get("offset", 0)) > 0:
                return {"data": {"search": {"products": []}}}
            return self.search_payload
        if name == "product_summary_with_fulfillment_v1":
            return self.summary_payload
        if name == "pdp_client_v1":
            return self.pdp_payload
        return {}


class Cfg:
    redsky_key = "test"
    zip_code = "55403"
    state = "MN"
    store_id = "1357"
    extra_store_ids: list = []
    categories: list = []
    keywords = ["pokemon trading cards"]
    title_filter = "pokemon|pokémon"
    status_interval = 90
    discovery_interval = 900
    street_date_interval = 3600
    street_date_warn_hours = 48
    request_timeout = 5.0
    max_concurrent_requests = 2
    db_path = ""
    log_level = "CRITICAL"


# -------------------------------------------------------------------- tests

def test_street_date_parsing() -> None:
    print("\nstreet date parsing")
    check("ISO date", _parse_street_date("2026-09-26") == date(2026, 9, 26))
    check("ISO datetime", _parse_street_date("2026-09-26T00:00:00Z") == date(2026, 9, 26))
    check("US slashes", _parse_street_date("09/26/2026") == date(2026, 9, 26))
    check("embedded", _parse_street_date("street 2026-09-26 ok") == date(2026, 9, 26))
    check("empty is None", _parse_street_date("") is None)
    check("garbage is None", _parse_street_date("soon") is None)
    check("non-string is None", _parse_street_date(12345) is None)


def test_title_filter() -> None:
    print("\ntitle filter")
    check("matches pokemon", looks_like_pokemon("Pokemon TCG Box", "pokemon|pokémon"))
    check("matches accented", looks_like_pokemon("Pokémon Bundle", "pokemon|pokémon"))
    check("rejects mtg", not looks_like_pokemon("Magic: The Gathering", "pokemon|pokémon"))
    check("handles empty", not looks_like_pokemon("", "pokemon|pokémon"))


async def test_parsing() -> None:
    print("\npayload parsing")
    client = StubClient(Cfg())
    try:
        products = await client.search(keyword="pokemon trading cards")
        by_tcin = {p.tcin: p for p in products}

        check("search returns all 3 rows", len(products) == 3, f"got {len(products)}")
        check("nested bundle SKU parsed", "77777777" in by_tcin)

        etb = by_tcin.get("94300069")
        check("title parsed", etb is not None and "Elite Trainer Box" in etb.title)
        check("dpci parsed", etb is not None and etb.dpci == "087-06-4321")
        check("price parsed", etb is not None and etb.price == 49.99)
        check("street date parsed", etb is not None and etb.street_date == date(2026, 9, 26))
        check("url parsed", etb is not None and etb.link.endswith("A-94300069"))

        mtg = by_tcin.get("88888888")
        check("missing dpci is empty not None", mtg is not None and mtg.dpci == "")
        check("missing street date is None", mtg is not None and mtg.street_date is None)

        summaries = await client.summaries(["94300069"])
        s = summaries["94300069"]
        check("ship status parsed", s.ship_status == "OUT_OF_STOCK")
        check("store parsed", len(s.stores) == 1 and s.stores[0].store_name == "Minneapolis")
        check("not shippable when OOS", not s.shippable)

        client.summary_payload = SUMMARY_IN_STOCK
        s2 = (await client.summaries(["94300069"]))["94300069"]
        check("shippable when in stock", s2.shippable)
        check("quantity parsed", s2.stores[0].quantity == 12)
        check("in-store count", s2.in_store_count == 1)

        client.summary_payload = SUMMARY_PREORDER
        s3 = (await client.summaries(["94300069"]))["94300069"]
        check("preorder detected", s3.preorder)
        check("preorder counts as purchasable", s3.shippable)

        detail = await client.detail("94300069")
        check("pdp dpci", detail is not None and detail.dpci == "087-06-4321")
        check("pdp street date", detail is not None and detail.street_date == date(2026, 9, 26))

        missing = await client.detail("00000000")
        client.pdp_payload = {}
        check("missing product returns None", (await client.detail("00000000")) is None)

        check("malformed payload survives", client._product_from_search({}) is None)
    finally:
        await client.aclose()


async def test_state_key() -> None:
    print("\nstate key")
    a = Product(tcin="1", ship_status="OUT_OF_STOCK", stores=[])
    b = Product(tcin="1", ship_status="IN_STOCK", stores=[])
    check("ship change alters key", a.state_key() != b.state_key())

    c = Product(tcin="1", ship_status="OUT_OF_STOCK",
                stores=[StoreStock("1357", "Mpls", "IN_STOCK")])
    check("store change alters key", a.state_key() != c.state_key())

    d = Product(tcin="1", ship_status="OUT_OF_STOCK",
                stores=[StoreStock("1357", "Mpls", "OUT_OF_STOCK")])
    check("unavailable store does not alter key", a.state_key() == d.state_key())

    e = Product(tcin="1", ship_status="OUT_OF_STOCK", stores=[
        StoreStock("2", "B", "IN_STOCK"), StoreStock("1", "A", "IN_STOCK")])
    f = Product(tcin="1", ship_status="OUT_OF_STOCK", stores=[
        StoreStock("1", "A", "IN_STOCK"), StoreStock("2", "B", "IN_STOCK")])
    check("store order does not matter", e.state_key() == f.state_key())


async def _fresh_monitor(tmpdir: str):
    cfg = Cfg()
    cfg.db_path = os.path.join(tmpdir, "test.db")
    db = Database(cfg.db_path)
    await db.connect()
    client = StubClient(cfg)
    events = []

    async def on_event(e):
        events.append(e)

    return db, client, Monitor(db, client, on_event, cfg), events


async def test_discovery() -> None:
    print("\ndiscovery + new listing alerts")
    with tempfile.TemporaryDirectory() as tmp:
        db, client, monitor, events = await _fresh_monitor(tmp)
        try:
            await monitor.discovery_pass()
            check("first run seeds silently", len(events) == 0, f"got {len(events)} events")
            check("only pokemon SKUs stored", await db.count_products() == 2)
            check("mtg filtered out", await db.get_product("88888888") is None)

            row = await db.get_product("94300069")
            check("dpci persisted", row is not None and row["dpci"] == "087-06-4321")
            check("street date persisted", row is not None and row["street_date"] == "2026-09-26")

            # A brand-new SKU appears in the next sweep.
            client.search_payload = {
                "data": {"search": {"products": SEARCH_PAYLOAD["data"]["search"]["products"] + [{
                    "tcin": "99999999",
                    "item": {
                        "dpci": "087-06-9999",
                        "product_description": {"title": "Pokemon TCG Mega Surprise Box"},
                    },
                    "price": {"current_retail": 59.99},
                }]}}
            }
            await monitor.discovery_pass()
            new_events = [e for e in events if e.kind == EventKind.NEW_LISTING]
            check("new SKU alerts once", len(new_events) == 1, f"got {len(new_events)}")
            check("alert carries the new tcin",
                  bool(new_events) and new_events[0].product.tcin == "99999999")
            check("catalog grew to 3", await db.count_products() == 3)

            events.clear()
            await monitor.discovery_pass()
            check("no duplicate alert on re-sweep", len(events) == 0, f"got {len(events)}")
        finally:
            await client.aclose()
            await db.close()


async def test_status_transitions() -> None:
    print("\nstock transitions")
    with tempfile.TemporaryDirectory() as tmp:
        db, client, monitor, events = await _fresh_monitor(tmp)
        try:
            await monitor.discovery_pass()   # seed catalog
            events.clear()

            client.summary_payload = SUMMARY_OOS
            await monitor.status_pass()
            check("first observation is a baseline", len(events) == 0, f"got {len(events)}")

            await monitor.status_pass()
            check("unchanged state is silent", len(events) == 0, f"got {len(events)}")

            client.summary_payload = SUMMARY_IN_STOCK
            await monitor.status_pass()
            kinds = [e.kind for e in events]
            check("restock fires", EventKind.RESTOCK in kinds, str(kinds))
            check("in-store fires", EventKind.IN_STORE in kinds, str(kinds))
            check("restock is urgent",
                  all(e.urgent for e in events if e.kind == EventKind.RESTOCK))

            events.clear()
            await monitor.status_pass()
            check("no repeat while still in stock", len(events) == 0, f"got {len(events)}")

            client.summary_payload = SUMMARY_OOS
            await monitor.status_pass()
            check("sold out fires", [e.kind for e in events] == [EventKind.SOLD_OUT],
                  str([e.kind for e in events]))

            events.clear()
            client.summary_payload = SUMMARY_PREORDER
            await monitor.status_pass()
            check("preorder fires", EventKind.PREORDER_OPEN in [e.kind for e in events],
                  str([e.kind for e in events]))
            check("preorder is not reported as restock",
                  EventKind.RESTOCK not in [e.kind for e in events])

            # Summary payloads carry no street_date; it must survive the poll.
            row = await db.get_product("94300069")
            check("street date not clobbered by status poll",
                  row is not None and row["street_date"] == "2026-09-26")
        finally:
            await client.aclose()
            await db.close()


async def test_street_date_warning() -> None:
    print("\nstreet date warnings")
    with tempfile.TemporaryDirectory() as tmp:
        db, client, monitor, events = await _fresh_monitor(tmp)
        try:
            soon = date.today() + timedelta(days=1)
            far = date.today() + timedelta(days=30)
            await db.upsert_product(
                Product(tcin="111", title="Pokemon Soon Box", street_date=soon),
                announced=True)
            await db.upsert_product(
                Product(tcin="222", title="Pokemon Later Box", street_date=far),
                announced=True)

            await monitor.street_date_pass()
            check("warns on imminent street date", len(events) == 1, f"got {len(events)}")
            check("warns about the right SKU",
                  bool(events) and events[0].product.tcin == "111")
            check("detail says tomorrow",
                  bool(events) and "tomorrow" in events[0].detail)

            events.clear()
            await monitor.street_date_pass()
            check("does not warn twice", len(events) == 0, f"got {len(events)}")
        finally:
            await client.aclose()
            await db.close()


async def test_subscriptions() -> None:
    print("\nsubscriptions")
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "s.db"))
        await db.connect()
        try:
            ok = await db.add_subscription("tcin", "94300069", guild_id=1,
                                           channel_id=2, user_id=3)
            check("subscription added", ok)
            dup = await db.add_subscription("tcin", "94300069", guild_id=1,
                                            channel_id=2, user_id=3)
            check("duplicate rejected", not dup)

            await db.add_subscription("keyword", "elite trainer", guild_id=1,
                                      channel_id=2, user_id=4)

            p = Product(tcin="94300069", title="Pokemon Elite Trainer Box ...")
            matches = await db.matching_subscriptions(p)
            check("tcin and keyword both match", len(matches) == 2, f"got {len(matches)}")

            other = Product(tcin="55555555", title="Pokemon Booster Bundle")
            check("no false match", len(await db.matching_subscriptions(other)) == 0)

            by_dpci = Product(tcin="66666666", title="Something", dpci="087-06-4321")
            await db.add_subscription("keyword", "087-06-4321", guild_id=1,
                                      channel_id=2, user_id=5)
            check("dpci keyword matches", len(await db.matching_subscriptions(by_dpci)) == 1)

            check("list scoped to user",
                  len(await db.list_subscriptions(guild_id=1, user_id=3)) == 1)
            check("removal works",
                  await db.remove_subscription("tcin", "94300069", guild_id=1, user_id=3))
            check("removing twice is False",
                  not await db.remove_subscription("tcin", "94300069", guild_id=1, user_id=3))
        finally:
            await db.close()


async def test_db_merge_semantics() -> None:
    print("\ndatabase merge semantics")
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "m.db"))
        await db.connect()
        try:
            full = Product(tcin="1", title="Pokemon Box", dpci="087-06-0001",
                           price=49.99, street_date=date(2026, 9, 26),
                           image="http://img", url="http://buy")
            check("insert reports new", await db.upsert_product(full))
            check("second upsert reports not new", not await db.upsert_product(full))

            sparse = Product(tcin="1")   # everything blank, as a summary can be
            await db.upsert_product(sparse)
            row = await db.get_product("1")
            check("title preserved", row["title"] == "Pokemon Box")
            check("dpci preserved", row["dpci"] == "087-06-0001")
            check("price preserved", row["price"] == 49.99)
            check("street date preserved", row["street_date"] == "2026-09-26")
            check("image preserved", row["image"] == "http://img")

            better = Product(tcin="1", dpci="087-06-9999")
            await db.upsert_product(better)
            check("non-empty value does overwrite",
                  (await db.get_product("1"))["dpci"] == "087-06-9999")
        finally:
            await db.close()


def test_embeds() -> None:
    print("\nembed rendering")
    from pokedrop import formatting
    from pokedrop.monitor import Event

    p = Product(tcin="94300069", title="Pokemon TCG Elite Trainer Box",
                dpci="087-06-4321", price=49.99, street_date=date(2026, 9, 26),
                ship_status="IN_STOCK",
                stores=[StoreStock("1357", "Minneapolis", "IN_STOCK", 12)])

    for kind in EventKind:
        embed = formatting.event_embed(Event(kind, p, "detail text"))
        check(f"embed builds for {kind.value}", embed.title is not None)
        check(f"embed within Discord limits for {kind.value}", len(embed) <= 6000)

    bare = Product(tcin="1")
    check("embed survives an empty product",
          formatting.status_embed(bare).title is not None)
    check("rows_embed handles empty list",
          formatting.rows_embed([], title="t", empty="none").description == "none")


def test_redirect_node() -> None:
    """Live check showed keyword 'pokemon' returns no products and a redirect
    to category 4yka5. Both redirect forms must be understood."""
    print("\nsearch redirect parsing")
    from pokedrop.target_api import redirect_node

    deeplink = {"data": {"search": {"search_response": {"metadata": {
        "redirect_deeplink": "target://landing/custom?pageType=c&nodeId=4yka5",
        "keyword": "pokemon",
    }}}}}
    check("node from deeplink", redirect_node(deeplink) == "4yka5")

    url_only = {"data": {"search": {"search_response": {"metadata": {
        "redirect_url": "https://www.target.com/c/pokemon/-/N-4yka5",
    }}}}}
    check("node from redirect_url", redirect_node(url_only) == "4yka5")

    check("no redirect returns None", redirect_node({"data": {}}) is None)
    check("empty payload returns None", redirect_node({}) is None)


async def test_redirect_following() -> None:
    print("\nsearch redirect following")

    class RedirectingClient(StubClient):
        async def _get(self, url, params, attempts=3):
            name = url.rsplit("/", 1)[-1]
            self.calls.append(f"{name}:{params.get('keyword') or params.get('category')}")
            if name != "plp_search_v2":
                return await super()._get(url, params, attempts)
            if params.get("keyword"):
                return {"data": {"search": {"products": [], "search_response": {
                    "metadata": {
                        "redirect_deeplink": "target://landing/custom?pageType=c&nodeId=4yka5"
                    }}}}}
            if int(params.get("offset", 0)) > 0:
                return {"data": {"search": {"products": []}}}
            return SEARCH_PAYLOAD

    client = RedirectingClient(Cfg())
    try:
        products = await client.search(keyword="pokemon")
        check("redirect yields the category's products", len(products) == 3,
              f"got {len(products)}")
        check("category browse was actually issued",
              any(c.endswith(":4yka5") for c in client.calls), str(client.calls))
        check("did not loop forever", len(client.calls) < 8, str(len(client.calls)))
    finally:
        await client.aclose()


async def test_jsonstore() -> None:
    print("\njson store")
    from pokedrop.jsonstore import JsonStore

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        store = JsonStore(path)
        await store.connect()
        check("cold store is empty", await store.count_products() == 0)
        check("cold store is not dirty", not store.dirty)

        full = Product(tcin="1", title="Pokemon Box", dpci="087-06-0001",
                       price=49.99, street_date=date(2026, 9, 26),
                       image="http://img", url="http://buy")
        check("insert reports new", await store.upsert_product(full))
        check("insert marks dirty", store.dirty)

        await store.upsert_product(Product(tcin="1"))  # sparse
        row = await store.get_product("1")
        check("title preserved", row["title"] == "Pokemon Box")
        check("dpci preserved", row["dpci"] == "087-06-0001")
        check("price preserved", row["price"] == 49.99)
        check("street date preserved", row["street_date"] == "2026-09-26")

        await store.save()
        check("file written", os.path.exists(path))

        # Round-trip.
        reloaded = JsonStore(path)
        await reloaded.connect()
        check("survives reload", await reloaded.count_products() == 1)
        check("fields survive reload",
              (await reloaded.get_product("1"))["dpci"] == "087-06-0001")
        check("reload starts clean", not reloaded.dirty)

        # An unchanged upsert must not mark the file dirty, or the workflow
        # would commit on every single run.
        await reloaded.upsert_product(Product(tcin="1", title="Pokemon Box",
                                              dpci="087-06-0001", price=49.99,
                                              street_date=date(2026, 9, 26),
                                              image="http://img", url="http://buy"))
        check("no-op upsert stays clean", not reloaded.dirty)

        # A real change must mark it dirty.
        await reloaded.upsert_product(Product(tcin="1", dpci="087-06-9999"))
        check("real change marks dirty", reloaded.dirty)

        # State dirty tracking.
        s = JsonStore(os.path.join(tmp, "s2.json"))
        await s.connect()
        p = Product(tcin="9", ship_status="OUT_OF_STOCK")
        await s.save_state(p)
        check("first state marks dirty", s.dirty)
        s.dirty = False
        await s.save_state(p)
        check("identical state stays clean", not s.dirty)
        await s.save_state(Product(tcin="9", ship_status="IN_STOCK"))
        check("changed state marks dirty", s.dirty)

        # Corrupt files must not wedge the bot permanently.
        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{not json")
        broken = JsonStore(bad)
        await broken.connect()
        check("corrupt file recovers empty", await broken.count_products() == 0)


async def test_monitor_on_jsonstore() -> None:
    """The Actions path runs the same Monitor against JsonStore, so the whole
    event pipeline has to work with no SQLite at all."""
    print("\nmonitor against json store")
    from pokedrop.jsonstore import JsonStore

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Cfg()
        store = JsonStore(os.path.join(tmp, "s.json"))
        await store.connect()
        client = StubClient(cfg)
        events = []

        async def on_event(e):
            events.append(e)

        monitor = Monitor(store, client, on_event, cfg)
        try:
            await monitor.discovery_pass()
            check("seeds silently on json store", len(events) == 0, f"got {len(events)}")
            check("json store holds the catalog", await store.count_products() == 2)

            client.summary_payload = SUMMARY_OOS
            await monitor.status_pass()
            events.clear()
            client.summary_payload = SUMMARY_IN_STOCK
            await monitor.status_pass()
            check("restock fires on json store",
                  EventKind.RESTOCK in [e.kind for e in events],
                  str([e.kind for e in events]))

            await store.save()
            reloaded = JsonStore(store.path)
            await reloaded.connect()
            check("state survives a run boundary",
                  (await reloaded.get_state("94300069"))["ship_status"] == "IN_STOCK")

            # Simulating the next Actions run: no repeat alert from cold state.
            client2 = StubClient(cfg)
            client2.summary_payload = SUMMARY_IN_STOCK
            events2 = []

            async def on_event2(e):
                events2.append(e)

            monitor2 = Monitor(reloaded, client2, on_event2, cfg)
            await monitor2.status_pass()
            check("no duplicate alert across runs", len(events2) == 0,
                  f"got {[e.kind for e in events2]}")
            await client2.aclose()
        finally:
            await client.aclose()


def test_oneshot_priority() -> None:
    print("\none-shot event priority")
    from pokedrop.oneshot import PRIORITY

    check("every event kind is prioritised",
          all(k in PRIORITY for k in EventKind), "missing a kind")
    check("restock outranks new listing",
          PRIORITY[EventKind.RESTOCK] < PRIORITY[EventKind.NEW_LISTING])
    check("sold out ranks last",
          PRIORITY[EventKind.SOLD_OUT] == max(PRIORITY.values()))


def test_embed_dicts() -> None:
    """The webhook path serialises embeds to raw dicts rather than letting
    discord.py send them, so that conversion has to hold up."""
    print("\nwebhook embed serialisation")
    from pokedrop import formatting
    from pokedrop.monitor import Event

    p = Product(tcin="94300069", title="Pokemon TCG Elite Trainer Box",
                dpci="087-06-4321", price=49.99, street_date=date(2026, 9, 26),
                ship_status="IN_STOCK",
                stores=[StoreStock("1357", "Minneapolis", "IN_STOCK", 12)])

    for kind in EventKind:
        d = formatting.event_embed(Event(kind, p, "detail")).to_dict()
        check(f"{kind.value} serialises to a dict", isinstance(d, dict))
        check(f"{kind.value} has a title", bool(d.get("title")))
        check(f"{kind.value} has fields", len(d.get("fields", [])) >= 5)
        check(f"{kind.value} colour is an int", isinstance(d.get("color"), int))


# --------------------------------------------------------------------- main

async def main() -> int:
    test_street_date_parsing()
    test_title_filter()
    await test_parsing()
    await test_state_key()
    await test_discovery()
    await test_status_transitions()
    await test_street_date_warning()
    await test_subscriptions()
    await test_db_merge_semantics()
    test_embeds()
    test_redirect_node()
    await test_redirect_following()
    await test_jsonstore()
    await test_monitor_on_jsonstore()
    test_oneshot_priority()
    test_embed_dicts()

    print(f"\n{'=' * 52}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
