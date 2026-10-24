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
        mgr = CheckpointManager("4012", "2025-05-17", staging_dir=test_staging, output_dir=test_output)

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
        mgr = CheckpointManager("4012", "2026-10-22", staging_dir=test_staging, output_dir=test_output)

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

if __name__ == "__main__":
    unittest.main()
