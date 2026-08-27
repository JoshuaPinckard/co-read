from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from instruments.arms.java.gate import (
    collect_surefire,
    focal_scope,
    load_sites,
    map_site,
    map_test_side,
)


class MappingTests(unittest.TestCase):
    def test_test_side_distinguishes_surefire_classes_from_helpers(self) -> None:
        mapped = map_test_side(
            [
                "src/test/java/org/example/AlphaTest.java",
                "src/test/java/org/example/TestBeta.java",
                "src/test/java/org/example/GammaTests.java",
                "src/test/java/org/example/LegacyTestCase.java",
                "src/test/java/org/example/Fixture.java",
                "src/test/resources/example.txt",
            ]
        )
        self.assertEqual(
            [item["selector"] for item in mapped["focal_classes"]],
            ["AlphaTest", "GammaTests", "LegacyTestCase", "TestBeta"],
        )
        self.assertEqual(
            mapped["non_runnable_java_helpers"],
            ["src/test/java/org/example/Fixture.java"],
        )
        self.assertEqual(mapped["non_java_test_artifacts"], ["src/test/resources/example.txt"])

    def test_site_mapping_retains_absent_classes_and_detects_ambiguity(self) -> None:
        site = {
            "parents": ["a", "b"],
            "diffs": {
                "parent1": {
                    "test_files": ["src/test/java/a/SameTest.java", "src/test/java/a/PresentTest.java"]
                },
                "parent2": {"test_files": ["src/test/java/b/SameTest.java"]},
            },
        }
        mapped = map_site(site, {"src/test/java/a/PresentTest.java"})
        self.assertEqual(len(mapped["missing_at_base"]), 2)
        self.assertEqual(
            mapped["ambiguous_selectors"],
            {"SameTest": ["a.SameTest", "b.SameTest"]},
        )

    def test_frozen_census_is_nineteen(self) -> None:
        sites = load_sites()
        self.assertEqual(len(sites), 19)
        self.assertEqual(len({site["merge"] for site in sites}), 19)


class SurefireNormalizationTests(unittest.TestCase):
    def test_normalization_ignores_order_and_time_but_keeps_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / "target" / "surefire-reports"
            reports.mkdir(parents=True)
            (reports / "TEST-b.xml").write_text(
                """<?xml version="1.0"?>
<testsuite tests="2" time="99.5">
  <testcase classname="p.OuterTest$Nested" name="z" time="9.0"><skipped message=" guard "/></testcase>
  <testcase classname="p.ParamTest" name="case[1]" time="8.0" />
</testsuite>
""",
                encoding="utf-8",
            )
            (reports / "TEST-a.xml").write_text(
                """<?xml version="1.0"?>
<testsuite tests="2" time="0.1">
  <testcase classname="p.ParamTest" name="case[0]" time="0.01"><failure type="AssertionError">ignored body</failure></testcase>
  <testcase classname="p.ErrorTest" name="boom" time="0.02"><error message=" stable message "/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            normalized = collect_surefire(root)
            self.assertEqual(normalized["test_count"], 4)
            self.assertEqual(
                normalized["outcome_counts"],
                {"error": 1, "failure": 1, "pass": 1, "skipped": 1},
            )
            self.assertEqual(normalized["tests"][0]["classname"], "p.ErrorTest")
            self.assertNotIn("time", normalized["tests"][0])
            scope = focal_scope(
                ["p.OuterTest", "p.ParamTest", "p.MissingTest"], normalized["tests"]
            )
            self.assertEqual(scope["unobserved_focal_classes"], ["p.MissingTest"])
            self.assertEqual(scope["focal_classes_without_pass"], ["p.OuterTest"])
            self.assertEqual(scope["unexpected_test_classes"], ["p.ErrorTest"])

    def test_rerun_evidence_is_not_normalized_as_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / "target" / "surefire-reports"
            reports.mkdir(parents=True)
            (reports / "TEST-rerun.xml").write_text(
                """<testsuite><testcase classname="p.FlakyTest" name="x">
<flakyFailure type="AssertionError">first try</flakyFailure>
</testcase></testsuite>""",
                encoding="utf-8",
            )
            normalized = collect_surefire(root)
            self.assertEqual(normalized["outcome_counts"], {"flaky_failure": 1})
            self.assertEqual(
                focal_scope(["p.FlakyTest"], normalized["tests"])[
                    "focal_classes_without_pass"
                ],
                ["p.FlakyTest"],
            )


if __name__ == "__main__":
    unittest.main()
