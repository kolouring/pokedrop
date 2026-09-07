"""Thin async client over Target's public RedSky aggregation API.

RedSky is the same JSON API target.com's own front end calls from the browser.
Nothing here is authenticated and nothing here places an order -- this reads
public product and availability data only.

Response shapes drift without notice, so every extraction goes through
defensive `_dig` lookups rather than direct indexing.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence

import httpx

from .config import config

log = logging.getLogger(__name__)

BASE = "https://redsky.target.com/redsky_aggregations/v1/web"
SEARCH_URL = f"{BASE}/plp_search_v2"
SUMMARY_URL = f"{BASE}/product_summary_with_fulfillment_v1"
PDP_URL = f"{BASE}/pdp_client_v1"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# RedSky rejects oversized tcin batches; keep well under the observed ceiling.
SUMMARY_BATCH_SIZE = 20

# Availability values RedSky returns that mean "you can give us money now".
PURCHASABLE = {"IN_STOCK", "PRE_ORDER_SELLABLE", "AVAILABLE"}


def new_visitor_id() -> str:
    return "".join(random.choices(string.hexdigits.upper()[:16], k=32))


def _dig(obj: Any, *path: str | int, default: Any = None) -> Any:
    """Walk nested dicts/lists, returning `default` the moment anything misses."""
    cur = obj
    for key in path:
        if cur is None:
            return default
        try:
            if isinstance(key, int):
                cur = cur[key]
            else:
                cur = cur.get(key)
        except (KeyError, IndexError, TypeError, AttributeError):
            return default
    return cur if cur is not None else default


def _parse_street_date(raw: Any) -> date | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


@dataclass
class StoreStock:
    store_id: str
    store_name: str
    status: str
    quantity: float | None = None

    @property
    def purchasable(self) -> bool:
        return self.status in PURCHASABLE


@dataclass
class Product:
    """Everything the monitor cares about for one SKU."""

    tcin: str
    title: str = ""
    dpci: str = ""            # the in-store SKU number, e.g. 087-06-1234
    url: str = ""
    image: str = ""
    price: float | None = None
    street_date: date | None = None
    ship_status: str = "UNKNOWN"
    stores: list[StoreStock] = field(default_factory=list)

    @property
    def shippable(self) -> bool:
        return self.ship_status in PURCHASABLE

    @property
    def preorder(self) -> bool:
        return self.ship_status.startswith("PRE_ORDER")

    @property
    def in_store_count(self) -> int:
        return sum(1 for s in self.stores if s.purchasable)

    @property
    def link(self) -> str:
        return self.url or f"https://www.target.com/p/-/A-{self.tcin}"

    def state_key(self) -> str:
        """Compact signature of purchasability; a change here is an alert."""
        stores = ",".join(
            sorted(s.store_id for s in self.stores if s.purchasable)
        )
        return f"{self.ship_status}|{stores}"


class RedSkyClient:
    def __init__(self, cfg=config) -> None:
        self.cfg = cfg
        self.visitor_id = new_visitor_id()
        self._sem = asyncio.Semaphore(cfg.max_concurrent_requests)
        self._client = httpx.AsyncClient(
            timeout=cfg.request_timeout,
            headers={
                "user-agent": USER_AGENT,
                "accept": "application/json",
                "accept-language": "en-US,en;q=0.9",
                "origin": "https://www.target.com",
                "referer": "https://www.target.com/",
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "RedSkyClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ HTTP

    async def _get(self, url: str, params: dict[str, Any], attempts: int = 3) -> dict:
        delay = 2.0
        for attempt in range(1, attempts + 1):
            async with self._sem:
                try:
                    resp = await self._client.get(url, params=params)
                except httpx.HTTPError as exc:
                    log.warning("RedSky request failed (%s/%s): %s", attempt, attempts, exc)
                    resp = None

            if resp is not None:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        log.warning("RedSky returned non-JSON body")
                elif resp.status_code in (403, 404, 410):
                    # 403 usually means the key rotated or the IP is throttled.
                    log.warning(
                        "RedSky %s for %s -- key may be stale or IP rate-limited",
                        resp.status_code,
                        url.rsplit("/", 1)[-1],
                    )
                    if resp.status_code == 403:
                        self.visitor_id = new_visitor_id()
                elif resp.status_code == 429:
                    log.warning("RedSky rate limited; backing off")
                else:
                    log.warning("RedSky HTTP %s", resp.status_code)

            if attempt < attempts:
                await asyncio.sleep(delay + random.uniform(0, 1.5))
                delay *= 2
        return {}

    def _base_params(self) -> dict[str, Any]:
        return {
            "key": self.cfg.redsky_key,
            "channel": "WEB",
            "visitor_id": self.visitor_id,
            "store_id": self.cfg.store_id,
            "pricing_store_id": self.cfg.store_id,
            "zip": self.cfg.zip_code,
            "state": self.cfg.state,
        }

    # -------------------------------------------------------------- discovery

    async def search(
        self,
        *,
        keyword: str | None = None,
        category: str | None = None,
        limit: int = 96,
    ) -> list[Product]:
        """Sweep search/browse results, paging until `limit` or exhaustion.

        New TCINs showing up here are the earliest public signal that Target
        has set up a SKU -- often days or weeks before it is orderable.
        """
        found: dict[str, Product] = {}
        page_size = 24
        offset = 0

        while len(found) < limit:
            params = self._base_params() | {
                "count": page_size,
                "offset": offset,
                "platform": "desktop",
                "useragent": USER_AGENT,
                "scheduled_delivery_store_id": self.cfg.store_id,
                "store_ids": self.cfg.store_id,
                # Off, so we still see items that are not currently buyable.
                "default_purchasable_filter": "false",
                "include_sponsored": "false",
                "include_dmc_dmr": "true",
            }
            if keyword:
                params["keyword"] = keyword
                params["page"] = f"/s/{keyword.replace(' ', '+')}"
            elif category:
                params["category"] = category
                params["page"] = f"/c/{category}"
            else:
                raise ValueError("search() needs a keyword or a category")

            data = await self._get(SEARCH_URL, params)
            products = _dig(data, "data", "search", "products", default=[]) or []

            if not products:
                # RedSky answers a broad keyword with an empty product list plus
                # a redirect to a category node -- "pokemon" does exactly this.
                # Follow it once rather than reporting nothing found.
                node = redirect_node(data)
                if node and keyword and node != category:
                    log.info("keyword %r redirects to category %s; following", keyword, node)
                    return await self.search(category=node, limit=limit)
                break

            for raw in products:
                p = self._product_from_search(raw)
                if p and p.tcin not in found:
                    found[p.tcin] = p

            if len(products) < page_size:
                break
            offset += page_size
            await asyncio.sleep(random.uniform(0.4, 1.2))

        return list(found.values())

    def _product_from_search(self, raw: dict) -> Product | None:
        # Bundles nest the real SKU one level down.
        if not raw.get("tcin") and raw.get("items"):
            raw = raw["items"][0]
        tcin = raw.get("tcin")
        if not tcin:
            return None
        item = raw.get("item", {})
        return Product(
            tcin=str(tcin),
            title=_dig(item, "product_description", "title", default="") or "",
            dpci=item.get("dpci", "") or "",
            url=_dig(item, "enrichment", "buy_url", default="") or "",
            image=_dig(item, "enrichment", "images", "primary_image_url", default="") or "",
            price=_dig(raw, "price", "current_retail"),
            street_date=_parse_street_date(
                _dig(item, "mmbv_content", "street_date")
                or _dig(item, "eligibility_rules", "street_date", "value")
            ),
        )

    # ------------------------------------------------------------ availability

    async def summaries(self, tcins: Sequence[str]) -> dict[str, Product]:
        """Batch availability poll -- the workhorse of the status loop."""
        out: dict[str, Product] = {}
        batches = [
            list(tcins[i : i + SUMMARY_BATCH_SIZE])
            for i in range(0, len(tcins), SUMMARY_BATCH_SIZE)
        ]
        for batch in batches:
            params = self._base_params() | {
                "tcins": ",".join(batch),
                "required_store_id": self.cfg.store_id,
                "scheduled_delivery_store_id": self.cfg.store_id,
                "page": f"/p/A-{batch[0]}",
            }
            data = await self._get(SUMMARY_URL, params)
            for raw in _dig(data, "data", "product_summaries", default=[]) or []:
                p = self._product_from_summary(raw)
                if p:
                    out[p.tcin] = p
            if len(batches) > 1:
                await asyncio.sleep(random.uniform(0.5, 1.5))
        return out

    def _product_from_summary(self, raw: dict) -> Product | None:
        tcin = raw.get("tcin")
        if not tcin:
            return None
        item = raw.get("item", {})
        fulfillment = raw.get("fulfillment", {})

        ship = (
            _dig(fulfillment, "shipping_options", "availability_status")
            or _dig(fulfillment, "scheduled_delivery", "availability_status")
            or "UNKNOWN"
        )

        stores: list[StoreStock] = []
        for so in _dig(fulfillment, "store_options", default=[]) or []:
            status = (
                _dig(so, "order_pickup", "availability_status")
                or _dig(so, "in_store_only", "availability_status")
                or _dig(so, "ship_to_store", "availability_status")
                or "UNKNOWN"
            )
            stores.append(
                StoreStock(
                    store_id=str(so.get("location_id", "")),
                    store_name=so.get("location_name", "") or "",
                    status=status,
                    quantity=so.get("location_available_to_promise_quantity"),
                )
            )

        return Product(
            tcin=str(tcin),
            title=_dig(item, "product_description", "title", default="") or "",
            dpci=item.get("dpci", "") or "",
            url=_dig(item, "enrichment", "buy_url", default="") or "",
            image=_dig(item, "enrichment", "images", "primary_image_url", default="") or "",
            price=_dig(raw, "price", "current_retail"),
            street_date=_parse_street_date(_dig(item, "mmbv_content", "street_date")),
            ship_status=ship,
            stores=stores,
        )

    # ------------------------------------------------------------------- PDP

    async def detail(self, tcin: str) -> Product | None:
        """Full product page data -- the reliable source for DPCI + street date."""
        params = self._base_params() | {
            "tcin": tcin,
            "has_pricing_store_id": "true",
            "has_financing_options": "false",
            "include_obsolete": "true",
            "page": f"/p/A-{tcin}",
        }
        data = await self._get(PDP_URL, params)
        product = _dig(data, "data", "product")
        if not product:
            return None
        item = product.get("item", {})
        street = (
            _dig(item, "mmbv_content", "street_date")
            or _dig(item, "eligibility_rules", "street_date", "value")
            or _dig(product, "item", "product_vendors", 0, "street_date")
        )
        return Product(
            tcin=str(product.get("tcin", tcin)),
            title=_dig(item, "product_description", "title", default="") or "",
            dpci=item.get("dpci", "") or "",
            url=_dig(item, "enrichment", "buy_url", default="") or "",
            image=_dig(item, "enrichment", "images", "primary_image_url", default="") or "",
            price=_dig(product, "price", "current_retail"),
            street_date=_parse_street_date(street),
            ship_status=_dig(
                product, "fulfillment", "shipping_options", "availability_status",
                default="UNKNOWN",
            ),
        )


def redirect_node(payload: dict) -> str | None:
    """Pull the category node out of a search redirect, if there is one.

    A broad keyword like "pokemon" returns no products and instead:
        redirect_deeplink: target://landing/custom?pageType=c&nodeId=4yka5
        redirect_url:      https://www.target.com/c/pokemon/-/N-4yka5
    Either form gives us the node to browse instead.
    """
    meta = _dig(payload, "data", "search", "search_response", "metadata", default={}) or {}

    deeplink = meta.get("redirect_deeplink") or ""
    m = re.search(r"nodeId=([A-Za-z0-9]+)", deeplink)
    if m:
        return m.group(1)

    url = meta.get("redirect_url") or ""
    m = re.search(r"/N-([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)

    return None


def looks_like_pokemon(title: str, pattern: str | None = None) -> bool:
    return bool(re.search(pattern or config.title_filter, title or "", re.IGNORECASE))
