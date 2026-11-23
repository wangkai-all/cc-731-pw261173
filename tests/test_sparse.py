"""End-to-end tests for the sparse matrix repository.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest

from app.sparse import (
    KIND_MISSING,
    KIND_VALUE,
    KIND_ZERO,
    Conflict,
    LayoutNotReady,
    Repository,
    UnknownLabel,
    VerificationFailed,
    parse_triples_text,
    read_container,
)


def triples(*rows):
    return [
        {"row": r, "col": c, "value": v, "source_version": ver, "source": src}
        for r, c, v, ver, src in rows
    ]


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sparse-test-")
        self.repo = Repository(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def build(self, layout="csr", budget=256, **kwargs):
        vid = kwargs.pop("vid", None)
        if vid is None:
            view = self.repo.ingest(self.records, **kwargs)
            vid = view["version_id"]
        res = self.repo.start_conversion(vid, layout, budget_bytes=budget)
        state = self.repo.wait_for_layout(vid, layout)
        return vid, res, state


class ImportTests(RepoTestCase):
    def test_duplicate_sum_collapses_to_explicit_zero(self):
        self.records = triples(
            ("A", "x", 2, 1, "s"), ("A", "x", -2, 1, "s"),
            ("B", "y", 5, 1, "s"), ("B", "y", 3, 1, "s"),
        )
        view = self.repo.ingest(self.records, policy="sum")
        triples_out = {(t["row"], t["col"]): t for t in
                       self.repo.logical_triples(view["version_id"])}
        self.assertEqual(triples_out[("A", "x")]["kind"], KIND_ZERO)
        self.assertEqual(triples_out[("B", "y")]["kind"], KIND_VALUE)
        self.assertEqual(triples_out[("B", "y")]["value"], 8.0)
        self.assertEqual(view["import"]["duplicates"], 2)
        self.assertEqual(view["counts"], {"value": 1, "zero": 1, "missing": 0})

    def test_duplicate_latest_uses_source_version(self):
        self.records = triples(
            ("A", "x", 1, 1, "s1"), ("A", "x", 9, 5, "s2"), ("A", "x", 4, 3, "s3"))
        view = self.repo.ingest(self.records, policy="latest")
        out = self.repo.logical_triples(view["version_id"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["value"], 9.0)
        self.assertEqual(out[0]["source"], "s2")

    def test_two_policies_make_two_versions(self):
        records = triples(("A", "x", 1, 1, "s1"), ("A", "x", 9, 5, "s2"))
        v1 = self.repo.ingest(records, policy="sum", label="sum")
        v2 = self.repo.ingest(records, policy="latest", label="latest")
        self.assertNotEqual(v1["version_id"], v2["version_id"])
        self.assertEqual(self.repo.logical_triples(v1["version_id"])[0]["value"], 10.0)
        self.assertEqual(self.repo.logical_triples(v2["version_id"])[0]["value"], 9.0)

    def test_zero_missing_and_absent_are_distinct(self):
        self.records = [
            {"row": "A", "col": "x", "value": 0},
            {"row": "A", "col": "y", "value": None},
            {"row": "A", "col": "z", "value": "NA"},
            {"row": "B", "col": "x", "value": 1},
        ]
        view = self.repo.ingest(self.records)
        self.assertEqual(view["counts"], {"value": 1, "zero": 1, "missing": 2})
        vid, _, state = self.build(vid=view["version_id"])
        self.assertEqual(state["state"], "ready")
        zero = self.repo.point(vid, "csr", "A", "x")
        missing = self.repo.point(vid, "csr", "A", "y")
        self.assertTrue(zero["present"])
        self.assertEqual(zero["kind"], KIND_ZERO)
        self.assertIsNone(zero["value"])
        self.assertTrue(missing["present"])
        self.assertEqual(missing["kind"], KIND_MISSING)
        # an untracked label is an error, not "absent"
        with self.assertRaises(UnknownLabel):
            self.repo.point(vid, "csr", "A", "no-such-col")
        # absent coordinate between two tracked labels
        absent = self.repo.point(vid, "csr", "B", "y")
        self.assertFalse(absent["present"])
        self.assertIsNone(absent["kind"])
        # arrays keep the zero and missing slots
        arrays = self.repo.arrays_preview(vid, "csr")
        self.assertEqual(arrays["nnz"], 4)
        self.assertIn(KIND_ZERO, arrays["kinds"])
        self.assertIn(KIND_MISSING, arrays["kinds"])

    def test_shape_from_labels_not_from_positions(self):
        view = self.repo.ingest([
            {"row": "r1", "col": "c1", "value": 1},
            {"row": "r1", "col": "c2", "value": 2},
            {"row": "r2", "col": "c2", "value": 3},
        ])
        self.repo.add_label("row", "empty-row")
        self.repo.add_label("col", "empty-col")
        view = self.repo.version_view(view["version_id"])
        self.assertEqual(view["shape"], [3, 3])
        self.assertIn("empty-row", view["empty_rows"])
        self.assertIn("empty-col", view["empty_cols"])
        vid, _, state = self.build(vid=view["version_id"])
        self.assertEqual(state["shape"], [3, 3])
        arrays = self.repo.arrays_preview(vid, "csr")
        self.assertEqual(arrays["indptr_len"], 4)

    def test_labels_numbered_independently(self):
        self.repo.ingest([{"row": "A", "col": "B", "value": 1}])
        row_has_b = any(l["name"] == "B" for l in self.repo.labels("row"))
        col_has_a = any(l["name"] == "A" for l in self.repo.labels("col"))
        self.assertFalse(row_has_b)
        self.assertFalse(col_has_a)
        self.repo.add_label("col", "A")
        self.repo.add_label("row", "B")
        rows = {l["name"]: l["id"] for l in self.repo.labels("row")}
        cols = {l["name"]: l["id"] for l in self.repo.labels("col")}
        self.assertNotEqual(rows["A"], cols["A"])
        self.assertNotEqual(rows["B"], cols["B"])

    def test_reorder_does_not_change_business_meaning(self):
        view = self.repo.ingest([
            {"row": "A", "col": "x", "value": 1},
            {"row": "B", "col": "x", "value": 2},
        ])
        vid, _, _ = self.build(vid=view["version_id"])
        before = self.repo.logical_triples(vid)
        labels = self.repo.labels("row")
        self.repo.reorder_labels("row", [l["id"] for l in reversed(labels)])
        after = self.repo.logical_triples(vid)
        self.assertEqual(
            {(t["row"], t["col"], t["value"]) for t in before},
            {(t["row"], t["col"], t["value"]) for t in after},
        )
        # published layout stays readable and unchanged
        self.assertEqual(self.repo.query(vid, "csr")["total_matches"], 2)
        self.assertEqual(self.repo.point(vid, "csr", "A", "x")["value"], 1.0)

    def test_rename_keeps_ids_and_layouts(self):
        view = self.repo.ingest([{"row": "A", "col": "x", "value": 1}])
        vid, _, _ = self.build(vid=view["version_id"])
        lab = self.repo.labels("row")[0]
        self.repo.rename_label("row", lab["id"], "A-renamed")
        self.assertEqual(self.repo.labels("row")[0]["id"], lab["id"])
        self.assertEqual(self.repo.point(vid, "csr", "A-renamed", "x")["value"], 1.0)

    def test_parse_text_tsv_and_headers(self):
        records = parse_triples_text("row\tcol\tvalue\nA\tx\t1.5\nB\ty\t\n")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["kind"], KIND_VALUE)
        self.assertEqual(records[1]["kind"], KIND_MISSING)


class LayoutTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.records = triples(
            ("A", "x", 1, 1, "s"), ("A", "y", 0, 1, "s"), ("B", "y", None, 1, "s"),
            ("B", "z", 4, 1, "s"), ("C", "x", 7, 1, "s"),
        )

    def test_csr_and_csc_arrays_are_real_and_consistent(self):
        vid = self.repo.ingest(self.records)["version_id"]
        self.repo.start_conversion(vid, "csr", budget_bytes=64)
        csr = self.repo.wait_for_layout(vid, "csr")
        self.repo.start_conversion(vid, "csc", budget_bytes=64)
        csc = self.repo.wait_for_layout(vid, "csc")
        self.assertEqual(csr["state"], "ready")
        self.assertEqual(csc["state"], "ready")
        a_csr = self.repo.arrays_preview(vid, "csr")
        a_csc = self.repo.arrays_preview(vid, "csc")
        self.assertEqual(a_csr["indptr_len"], 4)   # 3 rows + 1
        self.assertEqual(a_csc["indptr_len"], 4)   # 3 cols + 1
        self.assertEqual(a_csr["nnz"], 5)
        self.assertEqual(a_csc["nnz"], 5)
        self.assertEqual(a_csr["indptr"], [0, 2, 4, 5])
        self.assertEqual(a_csc["indptr"], [0, 2, 4, 5])
        self.assertEqual(a_csr["data"][1], 0.0)
        self.assertTrue(a_csr["data"][2] != a_csr["data"][2])  # NaN for missing
        self.assertTrue(self.repo.compare(vid, "csr")["all_match"])
        self.assertTrue(self.repo.compare(vid, "csc")["all_match"])
        for name, keys in (("csr", ("A", "B")), ("csc", ("x", "y"))):
            arrays = self.repo.arrays_preview(vid, name)
            self.assertEqual(len(arrays["indices"]), len(arrays["kinds"]))
            self.assertEqual(len(arrays["data"]), len(arrays["kinds"]))

    def test_empty_rows_and_cols_survive_compression(self):
        vid = self.repo.ingest([{"row": "A", "col": "x", "value": 1}])["version_id"]
        self.repo.add_label("row", "ghost-row")
        self.repo.add_label("col", "ghost-col")
        self.repo.start_conversion(vid, "csr", budget_bytes=256)
        self.repo.wait_for_layout(vid, "csr")
        arrays = self.repo.arrays_preview(vid, "csr")
        # indptr records the empty row as a repeated offset
        self.assertEqual(arrays["indptr"], [0, 1, 1])
        self.assertEqual(arrays["shape"], [2, 2])

    def test_unpublished_layout_cannot_be_read(self):
        records = [{"row": f"r{i // 4}", "col": f"c{i % 4}", "value": i + 1}
                   for i in range(80)]
        vid = self.repo.ingest(records)["version_id"]
        with self.assertRaises(LayoutNotReady):
            self.repo.query(vid, "csr")
        self.repo.start_conversion(vid, "csr", budget_bytes=64, pace_ms=25)
        with self.assertRaises(LayoutNotReady):
            self.repo.query(vid, "csr")
        paused = self.repo.pause_conversion(vid, "csr")
        self.assertTrue(paused["paused"])
        with self.assertRaises(LayoutNotReady):
            self.repo.query(vid, "csr")
        self.repo.resume_conversion(vid, "csr")
        self.assertEqual(self.repo.wait_for_layout(vid, "csr")["state"], "ready")
        self.assertEqual(self.repo.query(vid, "csr")["total_matches"], 80)

    def test_interrupted_conversion_resumes_from_checkpoint(self):
        records = [{"row": f"r{i // 3}", "col": f"c{i % 3}", "value": i + 1}
                   for i in range(60)]
        vid = self.repo.ingest(records)["version_id"]
        self.repo.start_conversion(vid, "csr", budget_bytes=64, pace_ms=30)
        time.sleep(0.12)
        paused = self.repo.pause_conversion(vid, "csr")
        self.assertTrue(paused["paused"])
        progress = paused["layout"]["progress"]
        self.assertGreater(progress["next_index"], 0)
        self.assertLess(progress["next_index"], 60)
        checkpoint = progress["next_index"]
        missing = self.repo.resume_conversion(vid, "csr")
        self.assertTrue(missing["resumed"])
        state = self.repo.wait_for_layout(vid, "csr")
        self.assertEqual(state["state"], "ready")
        self.assertGreater(state["progress"]["next_index"], checkpoint)
        self.assertTrue(self.repo.compare(vid, "csr")["all_match"])

    def test_restart_mid_conversion_is_recovered(self):
        records = [{"row": f"r{i // 4}", "col": f"c{i % 4}", "value": i + 1}
                   for i in range(80)]
        vid = self.repo.ingest(records)["version_id"]
        self.repo.start_conversion(vid, "csc", budget_bytes=64, pace_ms=40)
        time.sleep(0.15)
        # Leave the on-disk state mid-conversion, exactly like a killed process.
        self.repo.close(crash=True)
        state_next = self.repo.state["layouts"][vid]["csc"]["progress"]["next_index"]
        self.assertGreater(state_next, 0)
        fresh = Repository(self.root)
        view = fresh.state["layouts"][vid]["csc"]
        self.assertEqual(view["state"], "paused")
        self.assertEqual(view["pause_reason"], "进程重启")
        self.assertTrue(view["progress"]["interrupted"])
        fresh.resume_conversion(vid, "csc")
        ready = fresh.wait_for_layout(vid, "csc")
        self.assertEqual(ready["state"], "ready")
        self.assertTrue(fresh.compare(vid, "csc")["all_match"])

    def test_verification_rejects_broken_arrays(self):
        vid = self.repo.ingest(self.records)["version_id"]
        self.repo.start_conversion(vid, "csr", budget_bytes=512)
        self.repo.wait_for_layout(vid, "csr")
        files_dir = self.repo.state["layouts"][vid]["csr"]["dir"]
        # Corrupt one stored value, then re-verify.
        import struct as _struct
        path = os.path.join(files_dir, "data.bin")
        with open(path, "r+b") as fh:
            fh.seek(0)
            fh.write(_struct.pack("<d", 999.0))
        self.repo._array_cache.clear()
        report = self.repo.verify_layout(vid, "csr")
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["values_match_logical"])
        with self.assertRaises(LayoutNotReady):
            self.repo.query(vid, "csr")
        again = self.repo.verify_layout(vid, "csr")
        self.assertFalse(again["ok"])

    def test_corrupt_indptr_is_rejected(self):
        vid = self.repo.ingest(self.records)["version_id"]
        self.repo.start_conversion(vid, "csr", budget_bytes=512)
        self.repo.wait_for_layout(vid, "csr")
        files_dir = self.repo.state["layouts"][vid]["csr"]["dir"]
        import struct as _struct
        path = os.path.join(files_dir, "indptr.bin")
        with open(path, "r+b") as fh:
            fh.seek(8)
            fh.write(_struct.pack("<q", 99))  # offset beyond nnz
        self.repo._array_cache.clear()
        report = self.repo.verify_layout(vid, "csr")
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["indptr_monotonic"])
        self.assertTrue(any("indptr" in e for e in report["errors"]))

    def test_query_filters_and_slices(self):
        vid = self.repo.ingest(self.records)["version_id"]
        self.repo.start_conversion(vid, "csr", budget_bytes=512)
        self.repo.wait_for_layout(vid, "csr")
        self.repo.start_conversion(vid, "csc", budget_bytes=512)
        self.repo.wait_for_layout(vid, "csc")
        all_rows = self.repo.query(vid, "csr")
        self.assertEqual(all_rows["total_matches"], 5)
        only_a = self.repo.query(vid, "csr", row_names=["A"])
        self.assertEqual({m["row"] for m in only_a["matches"]}, {"A"})
        self.assertEqual(only_a["total_matches"], 2)
        missing_only = self.repo.query(vid, "csr", kinds={KIND_MISSING})
        self.assertEqual(missing_only["total_matches"], 1)
        zero_only = self.repo.query(vid, "csr", kinds={KIND_ZERO})
        self.assertEqual(zero_only["total_matches"], 1)
        # reorder then slice by positions
        order = [l["id"] for l in reversed(self.repo.labels("row"))]
        self.repo.reorder_labels("row", order)
        first_pos = self.repo.query(vid, "csr", row_positions=(0, 0))
        self.assertEqual({m["row"] for m in first_pos["matches"]}, {"C"})
        csc = self.repo.query(vid, "csc", col_names=["y"])
        self.assertEqual({m["col"] for m in csc["matches"]}, {"y"})

    def test_no_leak_of_dropped_zero_in_arrays(self):
        vid = self.repo.ingest([
            {"row": "A", "col": "x", "value": 0},
            {"row": "A", "col": "y", "value": 5},
        ])["version_id"]
        self.repo.start_conversion(vid, "csr", budget_bytes=256)
        self.repo.wait_for_layout(vid, "csr")
        arrays = self.repo.arrays_preview(vid, "csr")
        self.assertEqual(arrays["nnz"], 2)  # the zero is kept, not compressed away
        self.assertEqual(arrays["kinds"], [KIND_ZERO, KIND_VALUE])
        self.assertEqual(arrays["indices"], [0, 1])


class EditTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.view = self.repo.ingest(triples(
            ("A", "x", 1, 1, "s"), ("B", "y", 2, 1, "s")))
        self.vid = self.view["version_id"]
        self.repo.start_conversion(self.vid, "csr", budget_bytes=256)
        self.repo.wait_for_layout(self.vid, "csr")

    def test_correction_invalidates_layout_until_rebuilt(self):
        res = self.repo.correct_element(self.vid, "A", "x", kind="value", value=42)
        self.assertIn("csr", res["invalidated_layouts"])
        with self.assertRaises(LayoutNotReady):
            self.repo.point(self.vid, "csr", "A", "x")
        self.repo.start_conversion(self.vid, "csr", budget_bytes=256)
        self.repo.wait_for_layout(self.vid, "csr")
        self.assertEqual(self.repo.point(self.vid, "csr", "A", "x")["value"], 42.0)
        self.assertTrue(self.repo.compare(self.vid, "csr")["all_match"])

    def test_correction_can_insert_zero_or_missing(self):
        self.repo.correct_element(self.vid, "A", "y", kind="zero")
        self.repo.correct_element(self.vid, "B", "x", kind="missing")
        self.repo.start_conversion(self.vid, "csr", budget_bytes=256)
        self.repo.wait_for_layout(self.vid, "csr")
        self.assertEqual(self.repo.point(self.vid, "csr", "A", "y")["kind"], KIND_ZERO)
        self.assertEqual(self.repo.point(self.vid, "csr", "B", "x")["kind"], KIND_MISSING)
        self.assertTrue(self.repo.compare(self.vid, "csr")["all_match"])

    def test_correcting_new_label_changes_shape_and_invalidates(self):
        res = self.repo.correct_element(self.vid, "A", "new-col", kind="value", value=3)
        self.assertTrue(res["shape_changed"])
        self.repo.start_conversion(self.vid, "csr", budget_bytes=256)
        self.repo.wait_for_layout(self.vid, "csr")
        self.assertEqual(self.repo.arrays_preview(self.vid, "csr")["shape"], [2, 3])

    def test_delete_source_keeps_values(self):
        sources = self.repo.summary()["sources"]
        res = self.repo.delete_source(sources[0]["id"])
        self.assertEqual(res["orphaned_elements"], 2)
        self.assertTrue(self.repo.compare(self.vid, "csr")["all_match"])
        self.assertTrue(self.repo.query(self.vid, "csr")["total_matches"] == 2)
        self.assertFalse(self.repo.summary()["sources"][0]["deleted"] is False)


class ExportTests(RepoTestCase):
    def test_container_is_decodable_and_matches(self):
        view = self.repo.ingest(triples(
            ("A", "x", 1, 1, "s"), ("A", "y", 0, 1, "s"), ("B", "y", None, 1, "s")))
        vid = view["version_id"]
        self.repo.start_conversion(vid, "csr", budget_bytes=256)
        self.repo.wait_for_layout(vid, "csr")
        entry = self.repo.export(vid, "csr")
        path = os.path.join(self.repo.export_dir, entry["name"])
        with open(path, "rb") as fh:
            blob = fh.read()
        decoded = read_container(blob)
        self.assertEqual(decoded["header"]["nnz"], 3)
        self.assertEqual(decoded["indptr"], [0, 2, 3])
        self.assertEqual(decoded["kinds"], [KIND_VALUE, KIND_ZERO, KIND_MISSING])
        self.assertEqual(decoded["data"][2] != decoded["data"][2], True)
        self.assertEqual(decoded["header"]["shape"], [2, 2])
        self.assertEqual([l["name"] for l in decoded["header"]["row_labels"]], ["A", "B"])

    def test_unpublished_layout_cannot_be_exported(self):
        vid = self.repo.ingest(triples(("A", "x", 1, 1, "s")))["version_id"]
        with self.assertRaises(LayoutNotReady):
            self.repo.export(vid, "csr")

    def test_independent_verifier_accepts_and_detects_tampering(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "verify_delivery",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "scripts", "verify_delivery.py"))
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)

        vid = self.repo.ingest(triples(
            ("A", "x", 1.5, 1, "s"), ("A", "y", 0, 1, "s"),
            ("B", "y", None, 1, "s")))["version_id"]
        for layout in ("csr", "csc"):
            self.repo.start_conversion(vid, layout, budget_bytes=256)
            self.repo.wait_for_layout(vid, layout)
            entry = self.repo.export(vid, layout)
            path = os.path.join(self.repo.export_dir, entry["name"])
            report = verifier.check(path)
            self.assertTrue(report["ok"], report["errors"])
            self.assertEqual((report["value"], report["zero"], report["missing"]), (1, 1, 1))

        # Flip one byte of the payload: the checksum must catch it.
        with open(path, "rb") as fh:
            blob = bytearray(fh.read())
        blob[-80] ^= 0xFF
        with open(path, "wb") as fh:
            fh.write(blob)
        with self.assertRaises(SystemExit):
            verifier.load(path)


class PolicyConsistencyTests(RepoTestCase):
    def test_sum_and_latest_share_shape_and_labels(self):
        records = triples(
            ("A", "x", 1, 1, "s"), ("A", "x", 2, 2, "s"), ("B", "y", None, 1, "s"))
        a = self.repo.ingest(records, policy="sum")
        b = self.repo.ingest(records, policy="latest")
        self.assertEqual(a["shape"], b["shape"])
        self.assertEqual([l["name"] for l in self.repo.labels("row")], ["A", "B"])
        for view in (a, b):
            self.repo.start_conversion(view["version_id"], "csr", budget_bytes=256)
            self.assertTrue(self.repo.wait_for_layout(view["version_id"], "csr")["state"] == "ready")
            self.assertTrue(self.repo.compare(view["version_id"], "csr")["all_match"])


if __name__ == "__main__":
    unittest.main()
