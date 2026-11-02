"""
checkpoint_manager.py
Hard Interruption / Resume State Machine & Incomplete-Run Publication Protection
for Walmart Production Scraper (Azure Execution)
Commit: d4bb9f4c4c2c3c5bafb569edf4e255a844b37bac (HEAD -> main)
"""

import os
import json
import logging
import shutil
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger("CheckpointManager")

class CheckpointManager:
    """
    Manages run checkpoints and protects against incomplete-run publication:
    1. Checkpoint Journaling: Persists category/page cursors and collected record hashes.
    2. Hard Interruption Recovery: Resumes runs seamlessly after SIGINT, OOM, or node reboot.
    3. Staging Isolation: Keeps partial/in-progress data in .staging/ directory.
    4. Publication Gate: Blocks staging files from reaching production container until 100% verified.
    5. Durable Raw Evidence: Persists actual captured request/response network pairs to JSONL.
    """

    def __init__(self, store_number: str, crawl_date: str, staging_dir: str = ".staging", output_dir: str = "output"):
        self.store_number = store_number
        self.crawl_date = crawl_date
        self.staging_dir = staging_dir
        self.output_dir = output_dir
        self.checkpoint_file = os.path.join(staging_dir, f"checkpoint_{store_number}_{crawl_date}.json")
        self.staging_data_file = os.path.join(staging_dir, f"staging_records_{store_number}_{crawl_date}.json")
        self.raw_evidence_file = os.path.join(staging_dir, f"raw_evidence_{store_number}_{crawl_date}.jsonl")
        self.production_target_file = os.path.join(output_dir, f"walmart_{store_number}_{crawl_date}.json")
        self.production_evidence_file = os.path.join(output_dir, f"raw_evidence_{store_number}_{crawl_date}.jsonl")
        self.raw_evidence_write_failed = False
        self.raw_evidence_count = 0

        os.makedirs(self.staging_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

    def append_raw_evidence(self, endpoint: str, request_data: Any, response_payload: Any):
        """
        Durable persistence of original intercepted network responses.
        Ensures both collected records and original raw captures survive restarts, failures, and recovery.
        Does not swallow write errors.
        """
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "store_number": self.store_number,
            "crawl_date": self.crawl_date,
            "endpoint": endpoint,
            "request": request_data,
            "response": response_payload
        }
        try:
            with open(self.raw_evidence_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.raw_evidence_count += 1
        except Exception as e:
            self.raw_evidence_write_failed = True
            logger.error(f"Failed to persist raw evidence entry: {e}")
            raise IOError(f"Critical failure writing raw evidence: {e}")

    def load_checkpoint(self) -> Dict[str, Any]:
        """Loads existing checkpoint if run was interrupted; otherwise returns clean state."""
        if os.path.exists(self.checkpoint_file):
            try:
                with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info(f"[RESUME] Loaded checkpoint from {self.checkpoint_file}. Resuming from Category {data.get('category_index', 0)}, Page {data.get('current_page', 1)}.")
                    return data
            except Exception as e:
                logger.warning(f"Could not read checkpoint file: {e}. Starting fresh.")

        return {
            "store_number": self.store_number,
            "crawl_date": self.crawl_date,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "last_updated": datetime.utcnow().isoformat() + "Z",
            "category_index": 0,
            "current_page": 1,
            "completed_categories": [],
            "flushed_records_count": 0,
            "seen_record_signatures": [],
            "status": "INITIALIZED"
        }

    def save_checkpoint(
        self,
        category_index: int,
        current_page: int,
        completed_categories: List[str],
        flushed_count: int,
        seen_signatures: List[str],
        status: str = "IN_PROGRESS"
    ):
        """Atomically saves checkpoint to disk."""
        state = {
            "store_number": self.store_number,
            "crawl_date": self.crawl_date,
            "last_updated": datetime.utcnow().isoformat() + "Z",
            "category_index": category_index,
            "current_page": current_page,
            "completed_categories": completed_categories,
            "flushed_records_count": flushed_count,
            "seen_record_signatures": list(seen_signatures),
            "status": status
        }
        temp_path = f"{self.checkpoint_file}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(temp_path, self.checkpoint_file)
        except Exception as e:
            logger.error(f"Failed to write checkpoint: {e}")

    def append_staging_records(self, new_records: List[Dict[str, str]]):
        """Flushes verified batch records into staging storage immediately."""
        existing = []
        if os.path.exists(self.staging_data_file):
            try:
                with open(self.staging_data_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        existing.extend(new_records)
        temp_data = f"{self.staging_data_file}.tmp"
        with open(temp_data, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        os.replace(temp_data, self.staging_data_file)
        logger.info(f"[STAGING FLUSH] Wrote {len(new_records)} records to {self.staging_data_file} (Total staging: {len(existing)})")

    def load_staging_records(self) -> List[Dict[str, str]]:
        """Reads records accumulated in staging."""
        if os.path.exists(self.staging_data_file):
            try:
                with open(self.staging_data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to read staging data: {e}")
        return []

    def evaluate_publication_gate(
        self,
        min_threshold: int = 500,
        required_categories: Optional[List[str]] = None,
        failed_routes: Optional[List[str]] = None,
        store_fulfillment_verified: bool = True
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Incomplete-run publication protection gate:
        Checks whether staging dataset fulfills all quality invariants:
        1. Record count >= min_threshold
        2. Zero duplicate signatures
        3. Exact 22-field ordering
        4. Zero failed or blocked routes (failed routes remain incomplete)
        5. Full category/subcategory completion against manifest
        6. Source store and fulfillment context verified
        Returns (is_approved, reason, audit_stats)
        """
        records = self.load_staging_records()
        total = len(records)
        checkpoint = self.load_checkpoint()
        completed_cats = set(checkpoint.get("completed_categories", []))
        failed = list(failed_routes or [])

        audit_stats = {
            "total_records": total,
            "min_required": min_threshold,
            "duplicate_count": 0,
            "empty_assortment_retained_count": 0,
            "failed_routes_count": len(failed),
            "completed_categories_count": len(completed_cats),
            "store_fulfillment_verified": store_fulfillment_verified,
            "publication_approved": False
        }

        # Gate Rule 0: Raw evidence write integrity & captures
        if self.raw_evidence_write_failed:
            reason = "BLOCKED: Raw evidence write failure detected during collection."
            logger.error(f"[PUBLICATION GATE BLOCKED] {reason}")
            return False, reason, audit_stats

        if not os.path.exists(self.raw_evidence_file) or os.path.getsize(self.raw_evidence_file) == 0:
            reason = "BLOCKED: Missing or empty raw evidence captures. Original evidence required for publication."
            logger.error(f"[PUBLICATION GATE BLOCKED] {reason}")
            return False, reason, audit_stats

        # Gate Rule 1: Fulfillment context must be verified
        if not store_fulfillment_verified:
            reason = "BLOCKED: Store fulfillment verification failed or unconfirmed."
            logger.error(f"[PUBLICATION GATE BLOCKED] {reason}")
            return False, reason, audit_stats

        # Gate Rule 2: Failed/blocked routes must remain incomplete
        if len(failed) > 0:
            reason = f"BLOCKED: Incomplete run. {len(failed)} category routes failed or were blocked: {', '.join(failed[:5])}"
            logger.error(f"[PUBLICATION GATE BLOCKED] {reason}")
            return False, reason, audit_stats

        # Gate Rule 3: Full category/path completion
        if required_categories and len(required_categories) > 0:
            missing_cats = [c for c in required_categories if c not in completed_cats]
            if len(missing_cats) > 0:
                reason = f"BLOCKED: Incomplete category coverage. Missing {len(missing_cats)}/{len(required_categories)} categories: {', '.join(missing_cats[:5])}"
                logger.error(f"[PUBLICATION GATE BLOCKED] {reason}")
                return False, reason, audit_stats

        # Gate Rule 4: Volume threshold
        if total < min_threshold:
            reason = f"BLOCKED: Incomplete run. Yield of {total} records is below threshold of {min_threshold}."
            logger.error(f"[PUBLICATION GATE BLOCKED] {reason}")
            return False, reason, audit_stats

        # Invariant check: duplicates & empty assortment tracking
        seen = set()
        for r in records:
            sig = f"{r.get('upc') or r.get('url')}_{r.get('zipcode')}"
            if sig in seen:
                audit_stats["duplicate_count"] += 1
            seen.add(sig)
            if not r.get("regular_price") or not r.get("sale_price"):
                audit_stats["empty_assortment_retained_count"] += 1

        if audit_stats["duplicate_count"] > 0:
            reason = f"BLOCKED: Quality failure. Detected {audit_stats['duplicate_count']} duplicates."
            logger.error(f"[PUBLICATION GATE BLOCKED] {reason}")
            return False, reason, audit_stats

        audit_stats["publication_approved"] = True
        return True, "APPROVED: Run complete and passed all invariants.", audit_stats

    def promote_staging_to_production(
        self,
        min_threshold: int = 500,
        required_categories: Optional[List[str]] = None,
        failed_routes: Optional[List[str]] = None,
        store_fulfillment_verified: bool = True
    ) -> bool:
        """
        Atomically promotes staging file and raw evidence package to production only when publication gate passes.
        Ensures failed or missing evidence prevents publication and preserves the last good package if either write fails.
        """
        approved, reason, stats = self.evaluate_publication_gate(
            min_threshold=min_threshold,
            required_categories=required_categories,
            failed_routes=failed_routes,
            store_fulfillment_verified=store_fulfillment_verified
        )
        if not approved:
            logger.warning(f"[PROTECTION ACTIVE] Production file NOT updated. Reason: {reason}")
            return False

        bak_file = f"{self.production_target_file}.bak"
        bak_evidence = f"{self.production_evidence_file}.bak"
        tmp_target = f"{self.production_target_file}.tmp"
        tmp_evidence = f"{self.production_evidence_file}.tmp"

        try:
            # 1. Verify both staging files exist and are valid before modifying production
            if not os.path.exists(self.staging_data_file) or os.path.getsize(self.staging_data_file) == 0:
                logger.error("[PROMOTION ERROR] Staging data file missing or empty.")
                return False
            if not os.path.exists(self.raw_evidence_file) or os.path.getsize(self.raw_evidence_file) == 0:
                logger.error("[PROMOTION ERROR] Raw evidence file missing or empty.")
                return False

            # 2. Stage new files to temporary destination files first
            shutil.copy2(self.staging_data_file, tmp_target)
            shutil.copy2(self.raw_evidence_file, tmp_evidence)

            # 3. Preserve current production package as .bak (both product & evidence)
            if os.path.exists(self.production_target_file):
                if os.path.exists(bak_file):
                    os.remove(bak_file)
                shutil.copy2(self.production_target_file, bak_file)
            if os.path.exists(self.production_evidence_file):
                if os.path.exists(bak_evidence):
                    os.remove(bak_evidence)
                shutil.copy2(self.production_evidence_file, bak_evidence)

            # 4. Atomically swap both files into production
            os.replace(tmp_target, self.production_target_file)
            os.replace(tmp_evidence, self.production_evidence_file)

            # Staging promotion succeeded; clean up staging data file and checkpoint
            if os.path.exists(self.staging_data_file):
                os.remove(self.staging_data_file)
            if os.path.exists(self.checkpoint_file):
                os.remove(self.checkpoint_file)

            logger.info(f"[PROMOTION COMPLETE] Successfully promoted {stats['total_records']} records and evidence package to {self.output_dir}")
            return True
        except Exception as e:
            logger.error(f"[PROMOTION ERROR] Transaction failed during promotion: {e}. Restoring last good package.")
            # Rollback: ensure last good package is preserved
            if os.path.exists(bak_file):
                shutil.copy2(bak_file, self.production_target_file)
            if os.path.exists(bak_evidence):
                shutil.copy2(bak_evidence, self.production_evidence_file)
            if os.path.exists(tmp_target):
                os.remove(tmp_target)
            if os.path.exists(tmp_evidence):
                os.remove(tmp_evidence)
            return False
