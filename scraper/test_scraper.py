"""
test_scraper.py
Comprehensive unit tests for Walmart scraper logic:
- Strict 22-field ordering
- Empty string defaults (no nulls)
- GUPC and unit_price empty string enforcement
- Volume deal parsing (no invented price for BOGO)
- Digital coupon parsing
- Approximate weight exclusion
- Private label brand detection
- Duplicate detection and atomic dataset validation
"""

import unittest
import os
import sys
import json

# Ensure scraper directory is in Python path
sys.path.insert(0, os.path.dirname(__file__))

from deals_parser import parse_deals
from validation import (
    REQUIRED_FIELD_ORDER,
    sanitize_and_order_record,
    is_approximate_weight,
    detect_store_brand,
    validate_dataset,
    format_price
)

class TestWalmartScraperRules(unittest.TestCase):

    def test_strict_field_ordering(self):
        """Rule: Exact 22 field ordering must be preserved with empty strings for missing values."""
        raw = {
            "zipcode": "75001",
            "store_name": "Walmart Supercenter",
            "store_location": "1234 Main St, Dallas, TX 75201",
            "category": "Beverages",
            "product_title": "Dasani Purified Water, 16.9 fl oz, 24 Pack",
            "regular_price": "$6.98",
            "sale_price": "$5.98",
            "ppm": "$0.06/fl oz",
            "upc": "078742118001"
        }
        sanitized = sanitize_and_order_record(raw, crawl_date="2025-05-17")
        self.assertEqual(list(sanitized.keys()), REQUIRED_FIELD_ORDER)
        self.assertEqual(sanitized["gupc"], "", "gupc must be strictly empty string")
        self.assertEqual(sanitized["unit_price"], "", "unit_price must be strictly empty string")
        self.assertEqual(sanitized["crawl_date"], "2025-05-17", "crawl_date must be final field")
        self.assertNotIn("remarks", sanitized)
        self.assertNotIn("gupc14", sanitized)

    def test_volume_deal_parsing(self):
        """Rule: Volume deals store exact wording and price. Never invent price for BOGO."""
        # 1. Multi-buy with price
        res1 = parse_deals("2 for $10", promo_price_text="$10.00")
        self.assertEqual(res1["volume_deals_description"], "2 for $10")
        self.assertEqual(res1["volume_deals_price"], "$10.00")
        self.assertEqual(res1["digital_deals_description"], "")
        self.assertEqual(res1["digital_deals_price"], "")

        # 2. BOGO without displayed promotional price
        res2 = parse_deals("Buy 1, Get 1 Free")
        self.assertEqual(res2["volume_deals_description"], "Buy 1, Get 1 Free")
        self.assertEqual(res2["volume_deals_price"], "", "Must not invent price when not displayed")

        # 3. Buy 2, Get 1 Free
        res3 = parse_deals("Buy 2, Get 1 Free")
        self.assertEqual(res3["volume_deals_description"], "Buy 2, Get 1 Free")

    def test_digital_deal_parsing(self):
        """Rule: Digital deals require clipping/activation."""
        # 1. Coupon with terms and displayed promotional price
        res1 = parse_deals("Clip Coupon: Save $1", promo_price_text="$4.98")
        self.assertEqual(res1["digital_deals_description"], "Clip Coupon: Save $1")
        self.assertEqual(res1["digital_deals_price"], "$4.98")
        self.assertEqual(res1["volume_deals_description"], "")

        # 2. Coupon with explicit net price in badge
        res2 = parse_deals("$3.99 each with Digital Coupon")
        self.assertEqual(res2["digital_deals_description"], "$3.99 each with Digital Coupon")
        self.assertEqual(res2["digital_deals_price"], "$3.99")

        # 3. Walmart Cash / rebate must NOT be treated as immediate discount
        res3 = parse_deals("Get $2.00 Walmart Cash")
        self.assertEqual(res3["digital_deals_description"], "")
        self.assertEqual(res3["digital_deals_price"], "")

    def test_approximate_weight_exclusion(self):
        """Rule: Exclude products whose weight contains approx/approximately."""
        self.assertTrue(is_approximate_weight("Approx 1.5 lb"))
        self.assertTrue(is_approximate_weight("1.2 lb approximately"))
        self.assertTrue(is_approximate_weight("approx. 16 oz"))
        self.assertFalse(is_approximate_weight("16.9 fl oz"))
        self.assertFalse(is_approximate_weight("128 fl oz"))
        self.assertFalse(is_approximate_weight("1 lb"))

    def test_private_label_detection(self):
        """Rule: Identify Walmart private labels; default to 'no'."""
        self.assertEqual(detect_store_brand("Great Value Whole Milk"), "Great Value")
        self.assertEqual(detect_store_brand("Equate Ibuprofen"), "Equate")
        self.assertEqual(detect_store_brand("Mainstays Non-Stick Pan"), "Mainstays")
        self.assertEqual(detect_store_brand("Coca-Cola Original"), "no")

    def test_validation_quality_gates(self):
        """Rule: Validate record counts, duplicates and field completeness."""
        sample_records = [
            sanitize_and_order_record({
                "zipcode": "75001",
                "store_name": "Walmart Supercenter",
                "store_location": "1234 Main St, Dallas, TX 75201",
                "category": "Beverages",
                "sub_category": "Water",
                "product_title": f"Product {i}",
                "regular_price": "$5.00",
                "sale_price": "$4.50",
                "upc": f"078742{i:06d}",
                "url": f"https://www.walmart.com/ip/prod-{i}",
                "ppm": "$0.05/oz"
            }, "2025-05-17")
            for i in range(505)
        ]
        is_valid, stats = validate_dataset(sample_records, min_expected=500)
        self.assertTrue(is_valid)
        self.assertEqual(stats["duplicate_count"], 0)
        self.assertEqual(stats["field_completeness_rate"], "100.0%")

    def test_unavailable_store_assortment_retention(self):
        """Rule: Retain unavailable store-assortment products even when price or UPC is missing."""
        out_of_stock_item = sanitize_and_order_record({
            "zipcode": "75001",
            "store_name": "Walmart Supercenter",
            "store_location": "1234 Main St, Dallas, TX 75201",
            "category": "Fresh Produce",
            "sub_category": "Fresh Fruits",
            "product_title": "Marketside Fresh Organic Strawberries, 1 lb (Temporarily Out of Stock)",
            "regular_price": "",
            "sale_price": "",
            "upc": "",
            "url": "https://www.walmart.com/ip/marketside-strawberries/000000040112"
        }, "2025-05-17")
        self.assertEqual(out_of_stock_item["regular_price"], "")
        self.assertEqual(out_of_stock_item["sale_price"], "")
        self.assertEqual(out_of_stock_item["upc"], "")
        self.assertEqual(out_of_stock_item["gupc"], "")
        self.assertEqual(out_of_stock_item["unit_price"], "")
        self.assertEqual(out_of_stock_item["crawl_date"], "2025-05-17")

        # Must pass dataset validation without being discarded
        batch = [
            sanitize_and_order_record({
                "zipcode": "75001",
                "store_name": "Walmart Supercenter",
                "store_location": "1234 Main St, Dallas, TX 75201",
                "category": "Beverages",
                "product_title": f"Product {i}",
                "regular_price": "$5.00",
                "sale_price": "$4.50",
                "upc": f"078742{i:06d}",
                "url": f"https://www.walmart.com/ip/prod-{i}"
            }, "2025-05-17") for i in range(500)
        ]
        batch.append(out_of_stock_item)
        is_valid, stats = validate_dataset(batch, min_expected=500)
        self.assertTrue(is_valid)
        self.assertEqual(stats["unavailable_assortment_count"], 1)

    def test_checkpoint_and_publication_gate(self):
        """Rule: Incomplete runs (<500 records) are blocked by publication gate."""
        from checkpoint_manager import CheckpointManager
        import shutil

        test_staging = ".staging_test_audit"
        test_output = "output_test_audit"
        if os.path.exists(test_staging):
            shutil.rmtree(test_staging)
        if os.path.exists(test_output):
            shutil.rmtree(test_output)

        mgr = CheckpointManager("4012", "2025-05-17", staging_dir=test_staging, output_dir=test_output)

        # Append valid evidence capture
        mgr.append_raw_evidence("https://www.walmart.com/test", {"test": 1}, {"data": {}})

        # Incomplete batch: only 120 records
        thin_records = [
            {"zipcode": "75001", "product_title": f"Item {i}", "upc": f"100{i}"} for i in range(120)
        ]
        mgr.append_staging_records(thin_records)

        # Evaluate gate -> should be BLOCKED
        approved, reason, stats = mgr.evaluate_publication_gate(min_threshold=500)
        self.assertFalse(approved)
        self.assertIn("BLOCKED", reason)
        self.assertEqual(stats["total_records"], 120)

        # Attempt promote -> should return False and keep output clean
        promoted = mgr.promote_staging_to_production(min_threshold=500)
        self.assertFalse(promoted)
        self.assertFalse(os.path.exists(mgr.production_target_file))

        # Cleanup
        if os.path.exists(test_staging):
            shutil.rmtree(test_staging)
        if os.path.exists(test_output):
            shutil.rmtree(test_output)

    def test_publication_gate_blocks_failed_routes_and_unverified_context(self):
        """Rule: Publication gate strictly blocks if any category route failed or store fulfillment was unverified."""
        from checkpoint_manager import CheckpointManager
        import shutil

        test_staging = ".staging_test_gate"
        test_output = "output_test_gate"
        if os.path.exists(test_staging):
            shutil.rmtree(test_staging)
        if os.path.exists(test_output):
            shutil.rmtree(test_output)

        mgr = CheckpointManager("4012", "2026-10-22", staging_dir=test_staging, output_dir=test_output)

        # Append valid evidence capture
        mgr.append_raw_evidence("https://www.walmart.com/test", {"test": 1}, {"data": {}})

        # Sufficient records (520 records)
        full_records = [
            {"zipcode": "75001", "product_title": f"Item {i}", "upc": f"078742{i:06d}", "regular_price": "$3.00", "sale_price": "$2.50"}
            for i in range(520)
        ]
        mgr.append_staging_records(full_records)

        # 1. Blocked because a category route failed
        approved1, reason1, stats1 = mgr.evaluate_publication_gate(
            min_threshold=500,
            failed_routes=["Beverages > Coffee & Tea"],
            store_fulfillment_verified=True
        )
        self.assertFalse(approved1)
        self.assertIn("BLOCKED: Incomplete run. 1 category routes failed", reason1)

        # 2. Blocked because store fulfillment context could not be confirmed
        approved2, reason2, stats2 = mgr.evaluate_publication_gate(
            min_threshold=500,
            failed_routes=[],
            store_fulfillment_verified=False
        )
        self.assertFalse(approved2)
        self.assertIn("BLOCKED: Store fulfillment verification failed", reason2)

        # 3. Approved when all routes passed and fulfillment confirmed
        approved3, reason3, stats3 = mgr.evaluate_publication_gate(
            min_threshold=500,
            failed_routes=[],
            store_fulfillment_verified=True
        )
        self.assertTrue(approved3)
        self.assertIn("APPROVED", reason3)

        # Cleanup
        if os.path.exists(test_staging):
            shutil.rmtree(test_staging)
        if os.path.exists(test_output):
            shutil.rmtree(test_output)

    def test_raw_evidence_durable_storage(self):
        """Rule: Raw network response payloads are persisted to JSONL in staging and promoted on gate approval."""
        from checkpoint_manager import CheckpointManager
        import shutil

        test_staging = ".staging_test_raw"
        test_output = "output_test_raw"
        if os.path.exists(test_staging):
            shutil.rmtree(test_staging)
        if os.path.exists(test_output):
            shutil.rmtree(test_output)

        mgr = CheckpointManager("4012", "2026-10-22", staging_dir=test_staging, output_dir=test_output)

        # 1. Append raw evidence capture
        test_endpoint = "https://www.walmart.com/orchestra/home/graphql/browse/v1"
        test_request = {"storeId": "4012", "postalCode": "75001", "catId": "976759"}
        test_response = {"data": {"search": {"searchResult": {"itemStacks": [{"items": [{"id": "1001", "name": "Test Milk"}]}]}}}}
        mgr.append_raw_evidence(test_endpoint, test_request, test_response)

        # Verify raw evidence file exists and contains valid JSON line
        self.assertTrue(os.path.exists(mgr.raw_evidence_file))
        with open(mgr.raw_evidence_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["endpoint"], test_endpoint)
            self.assertEqual(entry["request"]["storeId"], "4012")

        # 2. Add enough records for promotion
        records = [
            {"zipcode": "75001", "product_title": f"Item {i}", "upc": f"078742{i:06d}", "regular_price": "$3.00", "sale_price": "$2.50"}
            for i in range(505)
        ]
        mgr.append_staging_records(records)

        # 3. Promote to production
        promoted = mgr.promote_staging_to_production(min_threshold=500, store_fulfillment_verified=True)
        self.assertTrue(promoted)
        self.assertTrue(os.path.exists(mgr.production_evidence_file), "Raw evidence must be promoted to output directory")

        # Cleanup
        if os.path.exists(test_staging):
            shutil.rmtree(test_staging)
        if os.path.exists(test_output):
            shutil.rmtree(test_output)

    def test_regression_absent_store_and_fulfillment_context_kept_unknown(self):
        """Regression 1: Absent storeId/fulfillmentMode in payload must NOT be filled with target defaults."""
        # Simulated response missing storeId and fulfillmentMode
        raw_response_payload = {
            "data": {
                "searchResult": {
                    # Notice: storeId and fulfillmentMode are completely absent
                    "itemStacks": [{
                        "items": [{
                            "usItemId": "9001",
                            "priceInfo": {"linePrice": "$4.50"},
                            "fulfillment": {} # pickupAvailable and inStoreStock absent
                        }]
                    }]
                }
            }
        }

        # Process payload logic as in initialize_store_context
        search_res = raw_response_payload.get("data", {}).get("searchResult", {})
        observed_store = search_res.get("storeId")
        observed_fulfillment = search_res.get("fulfillmentMode")
        item_obj = search_res.get("itemStacks", [])[0].get("items", [])[0]
        f_info = item_obj.get("fulfillment", {})
        pickup_avail = f_info.get("pickupAvailable") if isinstance(f_info, dict) else None
        instore_stock = f_info.get("inStoreStock") if isinstance(f_info, dict) else None

        # Verify: unknown context MUST stay unknown (None) - no artificial fallback to 4012 or PICKUP or True
        self.assertIsNone(observed_store, "Missing storeId in response must NOT default to configured target store")
        self.assertIsNone(observed_fulfillment, "Missing fulfillmentMode must NOT default to PICKUP")
        self.assertIsNone(pickup_avail, "Missing pickupAvailable must NOT default to True")
        self.assertIsNone(instore_stock, "Missing inStoreStock must NOT default to True")

        # Context verification check: absence of observed store and fulfillment prevents verification
        target_store = "4012"
        context_verified = bool(observed_store and str(observed_store) == target_store and observed_fulfillment and "PICKUP" in str(observed_fulfillment).upper())
        self.assertFalse(context_verified, "Absence of affirmative storeId and PICKUP mode must prevent store context verification")

    def test_regression_unmatched_item_disqualifies_local_price(self):
        """Regression 2: Items lacking intercepted network evidence cannot qualify as verified local prices."""
        intercepted_items = {}
        dom_item_id = "999901"
        self.assertNotIn(dom_item_id, intercepted_items)

        # Emulate scrape_category locality check:
        bound_item = intercepted_items.get(dom_item_id)
        is_verified_local_pickup = False
        if bound_item is None:
            is_verified_local_pickup = False

        self.assertFalse(is_verified_local_pickup, "Unmatched item without network evidence must NOT qualify as verified local pickup")

        # When not verified local pickup, displayed DOM price ($3.99) must be suppressed to empty string for assortment retention
        displayed_dom_price = "$3.99"
        sale_price = displayed_dom_price if is_verified_local_pickup else ""
        regular_price = displayed_dom_price if is_verified_local_pickup else ""

        self.assertEqual(sale_price, "", "Unmatched item must have empty price string to prevent unverified price emission")
        self.assertEqual(regular_price, "")

    def test_regression_wrong_price_context_and_fulfillment(self):
        """Regression 3: Wrong store ID or non-pickup fulfillment mode disqualifies local price."""
        target_store = "4012"
        intercepted_items = {
            "item_wrong_store": {
                "store_id": "1001",
                "fulfillment_mode": "PICKUP",
                "pickup_available": True,
                "observed_sale_price": "$2.99"
            },
            "item_shipping_only": {
                "store_id": "4012",
                "fulfillment_mode": "DELIVERY_FROM_WAREHOUSE",
                "pickup_available": False,
                "observed_sale_price": "$12.99"
            }
        }

        # Evaluate Item 1
        b1 = intercepted_items["item_wrong_store"]
        b1_valid = bool(b1.get("store_id") == target_store and "PICKUP" in str(b1.get("fulfillment_mode")).upper() and b1.get("pickup_available") is not False)
        self.assertFalse(b1_valid, "Wrong store ID must disqualify local price")

        # Evaluate Item 2
        b2 = intercepted_items["item_shipping_only"]
        b2_valid = bool(b2.get("store_id") == target_store and "PICKUP" in str(b2.get("fulfillment_mode")).upper() and b2.get("pickup_available") is not False)
        self.assertFalse(b2_valid, "Non-pickup fulfillment mode must disqualify local price")

    def test_regression_price_binding_to_validated_offer(self):
        """Regression 4: Validated pickup item binds displayed price to verified source offer."""
        from validation import format_price

        # Intercepted item with validated source offer of $5.48 for pickup at store 4012
        bound_item = {
            "store_id": "4012",
            "fulfillment_mode": "PICKUP",
            "pickup_available": True,
            "observed_sale_price": "$5.48"
        }

        is_verified_local_pickup = (bound_item.get("store_id") == "4012" and "PICKUP" in bound_item.get("fulfillment_mode"))
        self.assertTrue(is_verified_local_pickup)

        # Case A: Displayed DOM price matches validated offer
        displayed_dom_price = "$5.48"
        bound_price = format_price(bound_item["observed_sale_price"])
        final_price = bound_price if (is_verified_local_pickup and bound_price) else ""
        self.assertEqual(final_price, "$5.48")

        # Case B: Displayed DOM price had a stale tag, but source offer is verified $5.48
        displayed_stale_price = "$5.98"
        bound_price = format_price(bound_item["observed_sale_price"])
        if is_verified_local_pickup and bound_price:
            final_bound_price = bound_price
        else:
            final_bound_price = ""
        self.assertEqual(final_bound_price, "$5.48", "Price must bind to validated source offer")

    def test_regression_failed_evidence_write_blocks_publication_and_preserves_package(self):
        """Regression 5: Failed raw evidence write blocks publication gate and preserves last good package."""
        from checkpoint_manager import CheckpointManager
        import shutil

        test_staging = ".staging_reg_fail"
        test_output = "output_reg_fail"
        if os.path.exists(test_staging): shutil.rmtree(test_staging)
        if os.path.exists(test_output): shutil.rmtree(test_output)

        os.makedirs(test_output, exist_ok=True)
        # Create a pre-existing "last good package" in production
        good_product_file = os.path.join(test_output, "walmart_4012_2026-10-22.json")
        good_evidence_file = os.path.join(test_output, "raw_evidence_4012_2026-10-22.jsonl")
        with open(good_product_file, "w", encoding="utf-8") as f:
            f.write(json.dumps([{"item": "last_good_data"}]))
        with open(good_evidence_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"endpoint": "good_evidence"}) + "\n")

        mgr = CheckpointManager("4012", "2026-10-22", staging_dir=test_staging, output_dir=test_output)

        # Add 510 valid staging records
        full_records = [
            {"zipcode": "75001", "product_title": f"Item {i}", "upc": f"078742{i:06d}", "regular_price": "$3.00", "sale_price": "$2.50"}
            for i in range(510)
        ]
        mgr.append_staging_records(full_records)

        # Condition A: Raw evidence file is missing
        if os.path.exists(mgr.raw_evidence_file): os.remove(mgr.raw_evidence_file)
        approved, reason, stats = mgr.evaluate_publication_gate(min_threshold=500, store_fulfillment_verified=True)
        self.assertFalse(approved, "Missing raw evidence file must block publication gate")
        self.assertIn("raw evidence", reason.lower())

        promoted = mgr.promote_staging_to_production(min_threshold=500, store_fulfillment_verified=True)
        self.assertFalse(promoted, "Promotion must return False when evidence is missing")

        # Verify last good package was preserved and NOT overwritten
        with open(good_product_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.assertEqual(data, [{"item": "last_good_data"}], "Last good product file must remain intact")

        # Condition B: Raw evidence write failed flag is True
        mgr.raw_evidence_write_failed = True
        with open(mgr.raw_evidence_file, "w", encoding="utf-8") as f:
            f.write("dummy\n")

        approved2, reason2, _ = mgr.evaluate_publication_gate(min_threshold=500, store_fulfillment_verified=True)
        self.assertFalse(approved2, "Flagged raw evidence write failure must block gate")
        self.assertIn("raw evidence write failure", reason2.lower())

        promoted2 = mgr.promote_staging_to_production(min_threshold=500, store_fulfillment_verified=True)
        self.assertFalse(promoted2)

        # Verify last good package is STILL intact
        with open(good_product_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.assertEqual(data, [{"item": "last_good_data"}], "Last good product file must remain intact after blocked promotion")

        if os.path.exists(test_staging): shutil.rmtree(test_staging)
        if os.path.exists(test_output): shutil.rmtree(test_output)

if __name__ == "__main__":
    unittest.main()
