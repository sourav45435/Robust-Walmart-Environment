"""
walmart_scraper.py
Production-Ready Walmart Scraper for Microsoft Azure
Location-Specific • Multi-Location • Azure Ready
Commit: d4bb9f4c4c2c3c5bafb569edf4e255a844b37bac (HEAD -> main)
"""

import asyncio
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

from playwright.async_api import async_playwright, Page, BrowserContext

from config import StoreConfig, ScraperSettings, DEFAULT_STORES, get_settings
from deals_parser import parse_deals
from validation import (
    sanitize_and_order_record,
    is_approximate_weight,
    detect_store_brand,
    format_price,
    validate_dataset,
    REQUIRED_FIELD_ORDER
)
from checkpoint_manager import CheckpointManager

# Machine-Readable NDJSON & Structured Logger
class NDJSONFileHandler(logging.FileHandler):
    def emit(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage()
        }
        if hasattr(record, "extra_fields") and isinstance(record.extra_fields, dict):
            log_entry.update(record.extra_fields)
        with open(self.baseFilename, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")

logger = logging.getLogger("WalmartScraper")
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
logger.addHandler(stream_handler)
ndjson_handler = NDJSONFileHandler("walmart_scraper_execution.ndjson", mode="a", encoding="utf-8")
logger.addHandler(ndjson_handler)

class WalmartProductionScraper:
    """
    Production-grade Walmart Scraper engineered for Microsoft Azure Container App Jobs / VMs.
    Features:
    - Direct live collection path (DOM + Next.js __NEXT_DATA__ / Apollo GraphQL responses)
    - Store affinity & in-store pickup cookie locking with strict fulfillment verification
    - Source-bound locality verification per item (binding item.storeId == target store and intent == PICKUP)
    - Genuine retailer UPC extraction from item data fields (never parsed from URL digits)
    - Exact 22-field ordering with strict "" for gupc/unit_price
    - Retains unavailable store-assortment products indefinitely even when price or UPC is missing
    - Distinct carried observation tracking vs fresh evidence
    - Separate Volume Deals vs Digital Deals (no invented prices for BOGO, excludes cash back)
    - Checkpoint journaling with hard interruption recovery (SIGINT/crash safe)
    - Full category/path/page coverage publication protection gate (blocking thin or failed routes)
    """

    def __init__(self, store: StoreConfig, settings: Optional[ScraperSettings] = None, resume: bool = True):
        self.store = store
        self.settings = settings or get_settings()
        self.crawl_date = datetime.now().strftime("%Y-%m-%d")
        self.checkpoint_mgr = CheckpointManager(
            store_number=self.store.store_number,
            crawl_date=self.crawl_date,
            staging_dir=".staging",
            output_dir=self.settings.output_directory
        )
        self.resume = resume
        self.checkpoint_state = self.checkpoint_mgr.load_checkpoint() if resume else {}
        self.scraped_records: List[Dict[str, str]] = self.checkpoint_mgr.load_staging_records() if resume else []
        self.seen_signatures = set(self.checkpoint_state.get("seen_record_signatures", []))
        self.category_yields: Dict[str, int] = {}
        self.canary_results: Dict[str, bool] = {}
        self.failed_routes: List[str] = []
        self.store_fulfillment_verified: bool = False
        self.observed_availability_transitions: List[Dict[str, Any]] = []
        self.intercepted_items: Dict[str, Dict[str, Any]] = {}

    def _get_proxy_dict(self) -> Optional[Dict[str, str]]:
        if self.settings.proxy_server:
            proxy = {"server": self.settings.proxy_server}
            if self.settings.proxy_username:
                proxy["username"] = self.settings.proxy_username
                proxy["password"] = self.settings.proxy_password
            return proxy
        return None

    async def initialize_store_context(self, context: BrowserContext, page: Page) -> bool:
        """
        Sets store location cookies and verifies local in-store/pickup fulfillment context.
        Locks pricing and inventory to Store #self.store.store_number.
        Requires positive verification from both cookie persistence and page fulfillment response.
        Fails if response is blocked, invalid, or mismatched.
        """
        logger.info(f"[STORE LOCK] Injecting store affinity cookies for Store #{self.store.store_number} (ZIP: {self.store.zipcode})...")

        cookies = [
            {"name": "walmart.store.id", "value": self.store.store_number, "domain": ".walmart.com", "path": "/"},
            {"name": "walmart.postalCode", "value": self.store.zipcode, "domain": ".walmart.com", "path": "/"},
            {"name": "walmart.fulfillment.type", "value": "PICKUP", "domain": ".walmart.com", "path": "/"},
            {"name": "walmart.store.pickup", "value": "true", "domain": ".walmart.com", "path": "/"},
            {"name": "hasLocation", "value": "true", "domain": ".walmart.com", "path": "/"}
        ]
        await context.add_cookies(cookies)

        # Set up response listener to verify price-bearing payloads and bind item locality
        price_bearing_response_verified = False
        async def on_response_received(resp):
            nonlocal price_bearing_response_verified
            try:
                target_endpoints = ["/orchestra/home/graphql", "/graphql/browse", "/graphql/search", "_next/data"]
                if any(ep in resp.url for ep in target_endpoints) and resp.status == 200:
                    text_data = await resp.text()
                    try:
                        j_data = json.loads(text_data)
                        search_res = j_data.get("data", {}).get("searchResult", {}) or j_data.get("data", {}).get("browse", {})
                        stacks = search_res.get("itemStacks", []) or [search_res.get("itemStack", {})]

                        # Rule 1: Keep unknown context unknown! Never fill missing storeId or fulfillmentMode with target defaults
                        observed_store_id = search_res.get("storeId")
                        if observed_store_id is None:
                            observed_store_id = j_data.get("data", {}).get("storeId")

                        observed_fulfillment_mode = search_res.get("fulfillmentMode")
                        if observed_fulfillment_mode is None:
                            observed_fulfillment_mode = j_data.get("data", {}).get("fulfillmentMode")

                        # Observed store and fulfillment verification: require both store ID match and PICKUP fulfillment mode
                        if observed_store_id and str(observed_store_id) == str(self.store.store_number):
                            if observed_fulfillment_mode and "PICKUP" in str(observed_fulfillment_mode).upper():
                                price_bearing_response_verified = True

                        for stk in stacks:
                            for itm in stk.get("items", []):
                                itm_id = str(itm.get("usItemId") or itm.get("id") or "").strip()
                                if itm_id:
                                    # Never default pickupAvailable or inStoreStock to True if absent
                                    f_info = itm.get("fulfillment")
                                    pickup_avail = None
                                    instore_stock = None
                                    if isinstance(f_info, dict):
                                        pickup_avail = f_info.get("pickupAvailable")
                                        instore_stock = f_info.get("inStoreStock")

                                    p_info = itm.get("priceInfo") or {}
                                    line_p = p_info.get("linePrice") or p_info.get("currentPrice", {}).get("priceString") or itm.get("price") or ""

                                    self.intercepted_items[itm_id] = {
                                        "store_id": str(observed_store_id) if observed_store_id is not None else None,
                                        "fulfillment_mode": str(observed_fulfillment_mode) if observed_fulfillment_mode is not None else None,
                                        "pickup_available": pickup_avail,
                                        "in_store_stock": instore_stock,
                                        "observed_sale_price": str(line_p) if line_p else "",
                                    }

                        # Rule 3: Record the actual captured request rather than configured dummy fields
                        captured_req = {
                            "url": resp.request.url if hasattr(resp, "request") else resp.url,
                            "method": resp.request.method if hasattr(resp, "request") else "GET",
                            "headers": {k: v for k, v in resp.request.headers.items() if k.lower() != "cookie"} if hasattr(resp, "request") and hasattr(resp.request, "headers") else {},
                            "post_data": resp.request.post_data if hasattr(resp, "request") and hasattr(resp.request, "post_data") else None
                        }
                        self.checkpoint_mgr.append_raw_evidence(
                            endpoint=resp.url,
                            request_data=captured_req,
                            response_payload=j_data
                        )
                    except Exception as e:
                        logger.debug(f"[RESPONSE PARSE] Error processing response payload: {e}")
            except Exception:
                pass

        page.on("response", on_response_received)

        try:
            response = await page.goto("https://www.walmart.com", timeout=self.settings.page_load_timeout_ms, wait_until="domcontentloaded")
            if not response or response.status >= 400:
                logger.error(f"[STORE VERIFICATION FAILED] HTTP status {response.status if response else 'None'} received during store lock probe.")
                self.store_fulfillment_verified = False
                return False

            await asyncio.sleep(1.5)

            # Verification 1: Cookie persistence
            current_cookies = await context.cookies()
            cookie_map = {c["name"]: c["value"] for c in current_cookies}
            has_store = cookie_map.get("walmart.store.id") == self.store.store_number
            has_pickup = cookie_map.get("walmart.fulfillment.type") == "PICKUP" or cookie_map.get("walmart.store.pickup") == "true"

            if not has_store or not has_pickup:
                logger.error(f"[STORE VERIFICATION FAILED] Store cookies unverified. Store match: {has_store}, Pickup match: {has_pickup}")
                self.store_fulfillment_verified = False
                return False

            # Verification 2: Observed source-bound response verification
            # Substrings in HTML or cookies alone do NOT prove store/pickup context.
            # Must observe affirmative confirmation of target store and PICKUP fulfillment mode in price-bearing network payload.
            if not price_bearing_response_verified:
                logger.error(f"[STORE VERIFICATION FAILED] Target Store #{self.store.store_number} and PICKUP fulfillment mode were not observed in retailer network response.")
                self.store_fulfillment_verified = False
                return False

            self.store_fulfillment_verified = True
            logger.info(f"[FULFILLMENT CONFIRMED] Store #{self.store.store_number} ({self.store.address}) and PICKUP mode affirmatively observed in retailer network response.")
            return True
        except Exception as e:
            logger.error(f"[STORE VERIFICATION FAILED] Exception during store context verification: {e}")
            self.store_fulfillment_verified = False
            return False

    async def scrape_category(
        self,
        page: Page,
        cat_index: int,
        category: str,
        sub_category: str,
        url: str,
        canary_upc: str = ""
    ) -> int:
        """
        Harvests category items using live HTTP/DOM parsing.
        Retains unavailable store-assortment items even if price/UPC is missing.
        Extracts UPC strictly from genuine retailer UPC fields (never URL digits).
        Does not terminate pagination merely on empty deduplicated batches.
        Flushes progress to CheckpointManager continuously.
        """
        route_name = f"{category} > {sub_category}"
        logger.info(f"[CATEGORY HARVEST] [{route_name}] starting from {url}")
        category_count = 0
        current_page = 1
        # Full pagination without artificial page caps: uses configured max or runs until natural catalog EOF
        max_pages = self.settings.max_pages_per_category  # None = unconstrained pagination to EOF
        consecutive_empty_pages = 0

        # Resume logic: check if this category was partially completed
        if self.checkpoint_state.get("category_index") == cat_index:
            current_page = max(1, self.checkpoint_state.get("current_page", 1))
            logger.info(f"[CHECKPOINT RESUME] Resuming category '{sub_category}' from page {current_page}")

        while (max_pages is None) or (current_page <= max_pages):
            paged_url = f"{url}?page={current_page}&affinityOverride=store" if current_page > 1 else f"{url}?affinityOverride=store"

            success = False
            page_response = None
            for attempt in range(1, self.settings.max_retries_per_page + 1):
                try:
                    logger.info(f"Loading page {current_page} for {sub_category} (attempt {attempt})...")
                    page_response = await page.goto(paged_url, timeout=self.settings.page_load_timeout_ms, wait_until="domcontentloaded")
                    await asyncio.sleep(self.settings.delay_between_requests_sec)

                    if page_response and page_response.status in [403, 429]:
                        logger.warning(f"Rate limited (HTTP {page_response.status}). Backoff {5 * attempt}s...")
                        await asyncio.sleep(5 * attempt)
                        continue

                    # Trigger lazy loaded items via smooth scroll triggers
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3);")
                    await asyncio.sleep(0.6)
                    await page.evaluate("window.scrollTo(0, (document.body.scrollHeight / 3) * 2);")
                    await asyncio.sleep(0.6)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    await asyncio.sleep(0.8)

                    success = True
                    break
                except Exception as ex:
                    logger.warning(f"Error loading {paged_url}: {ex}. Retrying...")
                    await asyncio.sleep(2 * attempt)

            if not success:
                logger.error(f"Failed to load page {current_page} after max retries. Moving to next category.")
                break

            # Verify canary product in DOM
            if canary_upc:
                content = await page.content()
                if canary_upc in content:
                    self.canary_results[f"{category}_{sub_category}"] = True
                    logger.info(f"[CANARY VERIFIED] Canary barcode {canary_upc} observed in {sub_category}")

            # Direct extraction from DOM elements
            product_elements = await page.query_selector_all('[data-item-id], [data-testid="item-stack"], div[data-testid="list-view"], div[data-testid="grid-view"]')
            logger.info(f"Page {current_page} found {len(product_elements)} product containers")
            page_batch: List[Dict[str, str]] = []

            for el in product_elements:
                try:
                    # 1. Exclude Sponsored Placements
                    is_sponsored = await el.query_selector('[data-testid="sponsored-badge"], span:has-text("Sponsored")')
                    if is_sponsored:
                        continue

                    # 2. Exclude 3P Marketplace items (must be sold/shipped by Walmart or local in-store)
                    raw_text = (await el.inner_text()) if el else ""
                    if "sold by" in raw_text.lower() and "walmart" not in raw_text.lower():
                        continue

                    # 3. Fulfillment status: Do NOT drop store-assortment items merely because not available for pickup!
                    # Items belonging to store assortment are retained even when currently unavailable for pickup or out of stock.
                    is_pickup_unavailable = "not available for pickup" in raw_text.lower() or "shipping only" in raw_text.lower()

                    # 4. Extract Item ID, Title & URL
                    raw_item_id = await el.get_attribute('data-item-id') or ""
                    link_elem = await el.query_selector('a[href*="/ip/"]')
                    href = await link_elem.get_attribute('href') if link_elem else ""
                    full_url = f"https://www.walmart.com{href}" if href.startswith('/') else href

                    if not raw_item_id and href:
                        import re
                        m_id = re.search(r'/ip/[^/]+/(\d+)', href)
                        if m_id:
                            raw_item_id = m_id.group(1)

                    title_elem = await el.query_selector('[data-automation-id="product-title"], span.w_iUH7, a[link-identifier]')
                    title = (await title_elem.inner_text()).strip() if title_elem else ""
                    if not title:
                        continue

                    # Item-Level Locality & Validated Source Offer Verification:
                    # Require returned fulfillment mode to be PICKUP and bind actual displayed price to validated source offer.
                    # Missing/wrong context must prevent qualification as a verified local price, while eligible unavailable products remain retained.
                    bound_item = self.intercepted_items.get(raw_item_id)
                    is_verified_local_pickup = False
                    is_eligible_store_assortment = True

                    if bound_item is None:
                        # Missing intercepted item:
                        # Cannot establish observed local store, fulfillment mode, or source offer.
                        # Missing context prevents qualification as a verified local price.
                        is_verified_local_pickup = False
                        logger.debug(f"[UNMATCHED ITEM] No intercepted network payload for item {raw_item_id} ('{title}'). Cannot qualify as verified local price.")
                    else:
                        observed_store = bound_item.get("store_id")
                        observed_fulfillment = bound_item.get("fulfillment_mode")
                        pickup_available = bound_item.get("pickup_available")

                        # Require observed store ID to match target store
                        if not observed_store or str(observed_store) != str(self.store.store_number):
                            logger.warning(f"[WRONG STORE CONTEXT] Item {raw_item_id} observed store {observed_store} != {self.store.store_number}. Disqualifying local price.")
                            is_verified_local_pickup = False
                            is_eligible_store_assortment = False
                        # Require returned fulfillment mode to be PICKUP
                        elif not observed_fulfillment or "PICKUP" not in str(observed_fulfillment).upper():
                            logger.warning(f"[WRONG FULFILLMENT CONTEXT] Item {raw_item_id} fulfillment {observed_fulfillment} is not PICKUP. Disqualifying local price.")
                            is_verified_local_pickup = False
                        elif pickup_available is False:
                            # Part of store catalog but pickup currently unavailable/out-of-stock
                            is_verified_local_pickup = False
                        else:
                            is_verified_local_pickup = True

                    # DOM badge verification as secondary guard against non-local fulfillment
                    badge_elem = await el.query_selector('[data-automation-id="fulfillment-badge"], [data-testid="fulfillment-badge"]')
                    badge_str = (await badge_elem.inner_text()).lower() if badge_elem else ""

                    if "shipping only" in badge_str or "delivery from central warehouse" in badge_str:
                        logger.debug(f"[EXCLUDE NON-LOCAL] Item {raw_item_id} has non-local delivery badge. Excluded from local store catalog.")
                        is_verified_local_pickup = False
                        is_eligible_store_assortment = False

                    # Exclude non-local assortment items completely
                    if not is_eligible_store_assortment:
                        continue

                    # 5. Extract Weight & filter approximate wording
                    weight = ""
                    weight_elem = await el.query_selector('[data-automation-id="product-weight"], span:has-text("oz"), span:has-text("lb"), span:has-text("ct")')
                    if weight_elem:
                        weight = (await weight_elem.inner_text()).strip()

                    if not weight:
                        import re
                        m = re.search(r'\b\d+(\.\d+)?\s*(fl\s*oz|oz|lb|g|kg|ct|pack|count|gal)\b', title, re.IGNORECASE)
                        if m:
                            weight = m.group(0)

                    # Strict rule: Exclude approximate weights
                    if is_approximate_weight(weight) or is_approximate_weight(title):
                        continue

                    # 6. Availability Check: Detect Out-of-Stock / Unavailable Store Assortment
                    is_out_of_stock = (
                        is_pickup_unavailable or
                        "out of stock" in raw_text.lower() or
                        "temporarily out of stock" in raw_text.lower() or
                        bool(await el.query_selector('[data-testid="out-of-stock-badge"], span:has-text("Out of stock")'))
                    )

                    # 7. Extract Prices and Bind to Validated Source Offer
                    price_elem = await el.query_selector('[data-automation-id="product-price"], span.w_iUH7')
                    price_text = (await price_elem.inner_text()) if price_elem else ""
                    raw_sale_price = format_price(price_text)

                    was_price_elem = await el.query_selector('[data-automation-id="was-price"], span.strike')
                    was_price_text = (await was_price_elem.inner_text()) if was_price_elem else ""
                    raw_reg_price = format_price(was_price_text) if was_price_text else raw_sale_price

                    # Price Context & Source Offer Binding:
                    # Missing/wrong context must prevent qualification as a verified local price!
                    # While eligible unavailable products remain retained with empty string prices.
                    if is_verified_local_pickup and not is_out_of_stock:
                        observed_offer_price = bound_item.get("observed_sale_price") if bound_item else ""
                        if observed_offer_price:
                            formatted_offer = format_price(str(observed_offer_price))
                            if formatted_offer and raw_sale_price and formatted_offer != raw_sale_price:
                                logger.info(f"[PRICE BINDING] Bound displayed price {raw_sale_price} to validated source offer {formatted_offer} for item {raw_item_id}.")
                                sale_price = formatted_offer
                            else:
                                sale_price = raw_sale_price or formatted_offer
                        else:
                            sale_price = raw_sale_price
                        regular_price = raw_reg_price if raw_reg_price else sale_price
                    else:
                        # Missing/wrong context or out of stock:
                        # Retain eligible store-assortment product with empty price string per specification
                        sale_price = ""
                        regular_price = ""
                        logger.info(f"[ASSORTMENT RETENTION] Retained item '{title}' with empty prices (unverified context or unavailable assortment).")

                    # 8. Directly Scraped PPM
                    ppm_elem = await el.query_selector('[data-automation-id="product-unit-price"], span:has-text("/oz"), span:has-text("/fl oz"), span:has-text("/lb"), span:has-text("/ct")')
                    ppm_text = ""
                    if ppm_elem and not is_out_of_stock:
                        ppm_raw = (await ppm_elem.inner_text()).strip()
                        if "/" in ppm_raw and "$" in ppm_raw:
                            ppm_text = ppm_raw

                    # 9. Volume vs Digital Deals Separation
                    badge_elem = await el.query_selector('[data-testid="badge"], [data-automation-id="promo-badge"], span.bg-light-blue')
                    badge_text = (await badge_elem.inner_text()).strip() if badge_elem else ""
                    deals_dict = parse_deals(badge_text, promo_price_text=sale_price)

                    # 10. Image URL
                    img_elem = await el.query_selector('img[data-testid="productTileImage"], img[src*="walmartimages.com"]')
                    image_url = (await img_elem.get_attribute('src')) if img_elem else ""

                    # 11. UPC extraction: ONLY from genuine retailer UPC fields, NEVER from URL numbers
                    upc = ""
                    # Check DOM data attributes specifically assigned to barcode/UPC/GTIN
                    dom_upc = (
                        await el.get_attribute('data-upc') or
                        await el.get_attribute('data-gtin') or
                        await el.get_attribute('data-product-upc') or
                        await el.get_attribute('data-item-upc')
                    )
                    if dom_upc and re.match(r'^\d{11,14}$', dom_upc.strip()):
                        upc = dom_upc.strip()

                    # Fallback to Next.js or JSON-LD embedded catalog gtin/upc if present on tile
                    if not upc:
                        json_el = await el.query_selector('script[type="application/ld+json"]')
                        if json_el:
                            try:
                                j_txt = await json_el.inner_text()
                                j_obj = json.loads(j_txt)
                                gtin = j_obj.get("gtin12") or j_obj.get("gtin13") or j_obj.get("gtin")
                                if gtin and re.match(r'^\d{11,14}$', str(gtin).strip()):
                                    upc = str(gtin).strip()
                            except Exception:
                                pass

                    # If not available in genuine retailer UPC fields, leave strictly as empty string ""
                    # Do NOT regex parse itemId from product URL!

                    # 12. Store brand detection
                    store_brand = detect_store_brand(title)

                    # Build record with exact 22 required fields
                    record = {
                        "zipcode": self.store.zipcode,
                        "store_name": self.store.name,
                        "store_location": self.store.address,
                        "store_logo": self.store.logo_url,
                        "store_brand": store_brand,
                        "category": category,
                        "sub_category": sub_category,
                        "product_title": title,
                        "weight": weight,
                        "regular_price": regular_price,
                        "sale_price": sale_price,
                        "volume_deals_description": deals_dict["volume_deals_description"],
                        "volume_deals_price": deals_dict["volume_deals_price"],
                        "digital_deals_description": deals_dict["digital_deals_description"],
                        "digital_deals_price": deals_dict["digital_deals_price"],
                        "image_url": image_url,
                        "url": full_url,
                        "upc": upc,
                        "gupc": "", # Strictly empty string
                        "ppm": ppm_text,
                        "unit_price": "", # Strictly empty string
                        "crawl_date": self.crawl_date
                    }

                    ordered = sanitize_and_order_record(record, self.crawl_date)

                    sig = f"{ordered['upc'] or ordered['url']}_{ordered['zipcode']}"
                    if sig not in self.seen_signatures:
                        self.seen_signatures.add(sig)
                        page_batch.append(ordered)
                        category_count += 1

                except Exception:
                    continue

            # Flush batch to staging & update checkpoint
            if page_batch:
                self.scraped_records.extend(page_batch)
                self.checkpoint_mgr.append_staging_records(page_batch)
                logger.info(f"Page {current_page} yielded {len(page_batch)} new unique records (category total: {category_count})")
            else:
                # Rule: A page with zero newly deduplicated items is NOT the end of pagination!
                # Cross-category duplicates occur naturally on multi-category product listings.
                logger.info(f"Page {current_page} yielded 0 newly deduplicated items (containers on page: {len(product_elements)}). Continuing category pagination.")

            self.checkpoint_mgr.save_checkpoint(
                category_index=cat_index,
                current_page=current_page + 1,
                completed_categories=list(self.category_yields.keys()),
                flushed_count=len(self.scraped_records),
                seen_signatures=list(self.seen_signatures),
                status="IN_PROGRESS"
            )

            # Check for next page:
            # Rule: Only terminate when next button is absent/disabled OR page returns 0 product elements (genuine category EOF)
            next_btn = await page.query_selector('[aria-label="Next Page"], a[aria-label="next page"], [data-automation-id="pagination-next-page"]')
            is_disabled = False
            if next_btn:
                attr_dis = await next_btn.get_attribute("disabled")
                aria_dis = await next_btn.get_attribute("aria-disabled")
                is_disabled = (attr_dis is not None) or (aria_dis == "true")

            if len(product_elements) == 0 or not next_btn or is_disabled:
                logger.info(f"[CATEGORY COMPLETE] Pagination naturally completed at page {current_page} for '{sub_category}' (containers: {len(product_elements)}, next_btn_active: {bool(next_btn and not is_disabled)}). Category yield: {category_count} unique items.")
                break

            current_page += 1

        self.category_yields[f"{category} > {sub_category}"] = category_count
        return category_count

    async def run(self) -> Dict[str, Any]:
        """
        Executes full multi-category scrape pipeline with staging protection.
        Interruption-safe and publication-gated.
        Enforces complete category routes; failed/blocked routes remain incomplete.
        """
        start_time = time.time()
        logger.info(f"Starting scraper run for Store #{self.store.store_number} ({self.store.address}) on {self.crawl_date}")

        async with async_playwright() as p:
            proxy = self._get_proxy_dict()
            browser = await p.chromium.launch(
                headless=self.settings.headless,
                proxy=proxy,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars"
                ]
            )

            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )

            page = await context.new_page()

            # Initialize store context with strict verification
            context_ok = await self.initialize_store_context(context, page)
            if not context_ok:
                logger.error("[ABORT RUN] Could not verify store context and fulfillment locks. Halting scraper to prevent unverified publication.")
                await browser.close()
                return {
                    "success": False,
                    "reason": "Store context and fulfillment verification failed.",
                    "total_records": len(self.scraped_records),
                    "publication_gated": True
                }

            # Load taxonomy CSV
            csv_path = self.settings.categories_csv_path
            if not os.path.exists(csv_path):
                csv_path = os.path.join(os.path.dirname(__file__), "Walmart_Category_URLs.csv")

            categories_to_scrape = []
            if os.path.exists(csv_path):
                with open(csv_path, mode="r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        categories_to_scrape.append(row)

            logger.info(f"Loaded {len(categories_to_scrape)} category/subcategory endpoints.")

            start_cat_idx = self.checkpoint_state.get("category_index", 0)
            required_category_names = [f"{c.get('category')} > {c.get('sub_category')}" for c in categories_to_scrape]

            for idx in range(start_cat_idx, len(categories_to_scrape)):
                item = categories_to_scrape[idx]
                cat = item.get("category", "")
                sub_cat = item.get("sub_category", "")
                url = item.get("walmart_url", "")
                canary = item.get("canary_upc", "")
                route_key = f"{cat} > {sub_cat}"
                if url:
                    try:
                        await self.scrape_category(page, idx, cat, sub_cat, url, canary_upc=canary)
                    except Exception as route_err:
                        logger.error(f"[ROUTE FAILURE] Route '{route_key}' encountered unrecovered failure: {route_err}")
                        self.failed_routes.append(route_key)

            await browser.close()

        runtime_sec = int(time.time() - start_time)
        hours, remainder = divmod(runtime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        runtime_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m {seconds}s"

        # Incomplete-Run Publication Protection Gate Evaluation
        # Requires: record threshold, 0 duplicates, full category completion, 0 failed routes, verified store context
        is_promoted = self.checkpoint_mgr.promote_staging_to_production(
            min_threshold=self.settings.min_expected_records_per_run,
            required_categories=required_category_names,
            failed_routes=self.failed_routes,
            store_fulfillment_verified=self.store_fulfillment_verified
        )

        return {
            "success": is_promoted,
            "total_records": len(self.scraped_records),
            "runtime": runtime_str,
            "store": f"Walmart #{self.store.store_number}",
            "location": self.store.address,
            "category_yields": self.category_yields,
            "failed_routes": self.failed_routes,
            "output_file": self.checkpoint_mgr.production_target_file,
            "publication_gated": not is_promoted
        }

if __name__ == "__main__":
    store = DEFAULT_STORES[0]
    scraper = WalmartProductionScraper(store)
    result = asyncio.run(scraper.run())
    print("Scraper Execution Completed:", json.dumps(result, indent=2))
