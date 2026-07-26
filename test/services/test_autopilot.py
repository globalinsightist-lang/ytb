import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from app.services import autopilot as ap


class TestWordBudget(unittest.TestCase):
    def test_budget_tracks_target_duration(self):
        cfg = {"target_seconds_min": 30, "target_seconds_max": 40, "words_per_second": 2.6}
        lo, hi = ap.word_budget(cfg)
        self.assertEqual((lo, hi), (78, 104))

    def test_budget_stays_ordered_when_min_equals_max(self):
        cfg = {"target_seconds_min": 30, "target_seconds_max": 30, "words_per_second": 2.6}
        lo, hi = ap.word_budget(cfg)
        self.assertGreater(hi, lo)

    def test_zero_words_per_second_falls_back(self):
        # A bad config value must not divide by zero mid-run.
        cfg = {"target_seconds_min": 30, "target_seconds_max": 40, "words_per_second": 0}
        lo, hi = ap.word_budget(cfg)
        self.assertGreater(lo, 0)
        self.assertGreater(hi, lo)


class TestTrimToBudget(unittest.TestCase):
    def test_under_budget_is_untouched(self):
        script = "One two three. Four five six."
        self.assertEqual(ap.trim_to_budget(script, 50), script)

    def test_trims_on_sentence_boundary(self):
        script = "Alpha beta gamma. Delta epsilon zeta. Eta theta iota."
        out = ap.trim_to_budget(script, 6)
        self.assertEqual(out, "Alpha beta gamma. Delta epsilon zeta.")
        # Never cuts mid-sentence — the payoff is the point of the script.
        self.assertTrue(out.endswith("."))

    def test_single_overlong_sentence_is_kept_whole(self):
        script = "word " * 200
        out = ap.trim_to_budget(script, 10)
        self.assertTrue(out.startswith("word"))
        self.assertGreater(len(out.split()), 0)


class TestExtractHook(unittest.TestCase):
    def test_returns_first_sentence(self):
        self.assertEqual(
            ap.extract_hook("Your coffee costs 400% more. And here is why."),
            "Your coffee costs 400% more.",
        )

    def test_empty_script(self):
        self.assertEqual(ap.extract_hook(""), "")


class TestPickVoice(unittest.TestCase):
    def test_rotates_through_pool(self):
        cfg = {"voice_pool": ["a", "b", "c"], "voice_name": "fallback"}
        self.assertEqual([ap.pick_voice(cfg, i) for i in range(4)], ["a", "b", "c", "a"])

    def test_falls_back_when_pool_empty(self):
        cfg = {"voice_pool": [], "voice_name": "fallback"}
        self.assertEqual(ap.pick_voice(cfg, 3), "fallback")


class TestSanitizeTitle(unittest.TestCase):
    def test_rejects_banned_opener(self):
        self.assertEqual(ap.sanitize_title("Unlocking the Mystery", "Topic"), "Topic")

    def test_rejects_secrets(self):
        self.assertEqual(ap.sanitize_title("The Secret of Pizza", "Topic"), "Topic")

    def test_strips_emoji_and_trailing_bang(self):
        self.assertEqual(ap.sanitize_title("The Rise of Football 🔥!!!", "Topic"), "The Rise of Football")

    def test_keeps_a_good_title(self):
        title = "Why coffee costs 400% more than in 2019"
        self.assertEqual(ap.sanitize_title(title, "Topic"), title)

    def test_caps_at_youtube_limit(self):
        self.assertLessEqual(len(ap.sanitize_title("x" * 250, "Topic")), 100)

    def test_blank_title_falls_back(self):
        self.assertEqual(ap.sanitize_title("", "Topic"), "Topic")


class TestJudgeShuffleMapping(unittest.TestCase):
    """The judge must not inherit the prompt's positional bias."""

    def test_winner_index_maps_back_to_caller_ordering(self):
        candidates = ["AAA", "BBB", "CCC", "DDD"]

        def fake_response(prompt):
            # Always pick whichever candidate is shown in slot 0 — a perfectly
            # position-biased judge. The mapping must still resolve to the
            # script that actually occupied that slot.
            shown_first = prompt.split("### Candidate 0\n")[1].split("\n")[0]
            fake_response.picked = shown_first
            return json.dumps({"winner_index": 0, "scores": [], "lessons": []})

        with patch.object(ap, "_generate_response", side_effect=fake_response):
            for _ in range(20):
                verdict = ap.judge_candidates("topic", candidates, {})
                self.assertEqual(candidates[verdict["winner_index"]], fake_response.picked)

    def test_scores_are_remapped_to_caller_indices(self):
        candidates = ["AAA", "BBB"]

        def fake_response(prompt):
            return json.dumps(
                {
                    "winner_index": 0,
                    "scores": [{"index": 0, "total": 30}, {"index": 1, "total": 20}],
                    "lessons": [],
                }
            )

        with patch.object(ap, "_generate_response", side_effect=fake_response):
            verdict = ap.judge_candidates("topic", candidates, {})
        self.assertEqual(sorted(s["index"] for s in verdict["scores"]), [0, 1])

    def test_single_candidate_skips_judging(self):
        self.assertEqual(
            ap.judge_candidates("topic", ["only"], {}),
            {"winner_index": 0, "scores": [], "lessons": []},
        )

    def test_judge_failure_does_not_raise(self):
        with patch.object(ap, "_generate_response", side_effect=RuntimeError("boom")):
            verdict = ap.judge_candidates("topic", ["a", "b"], {})
        self.assertEqual(verdict["winner_index"], 0)


class TestEvidenceBlock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.perf_patch = patch.object(
            ap, "PERFORMANCE_FILE", str(Path(self.tmp.name) / "performance.jsonl")
        )
        self.perf_patch.start()

    def tearDown(self):
        self.perf_patch.stop()
        self.tmp.cleanup()

    def _write(self, rows):
        with open(ap.PERFORMANCE_FILE, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_returns_empty_without_enough_data(self):
        self._write([{"video_id": "a", "views": 100, "avg_view_pct": 50, "hook": "h"}])
        self.assertEqual(ap.build_evidence_block({"evidence_sample_size": 5}), "")

    def test_low_view_videos_are_excluded_as_noise(self):
        rows = [
            {"video_id": str(i), "views": 3, "avg_view_pct": 50, "hook": f"h{i}"}
            for i in range(20)
        ]
        self._write(rows)
        self.assertEqual(
            ap.build_evidence_block({"evidence_sample_size": 5, "evidence_min_views": 25}),
            "",
        )

    def test_ranks_by_retention_not_views(self):
        rows = [
            # Huge view count but nobody stayed — must land in the losers.
            {"video_id": "viral_dud", "views": 100000, "avg_view_pct": 5, "hook": "DUD"},
        ] + [
            {"video_id": str(i), "views": 100, "avg_view_pct": 40 + i, "hook": f"h{i}"}
            for i in range(10)
        ]
        self._write(rows)
        block = ap.build_evidence_block(
            {"evidence_sample_size": 5, "evidence_min_views": 25}
        )
        self.assertIn("DUD", block)
        winners, losers = block.split("These lost viewers fastest")
        self.assertNotIn("DUD", winners)
        self.assertIn("DUD", losers)


class TestComposeSystemPrompt(unittest.TestCase):
    def test_length_requirement_is_always_present(self):
        cfg = {"target_seconds_min": 30, "target_seconds_max": 40, "words_per_second": 2.85}
        with patch.object(ap, "build_evidence_block", return_value=""):
            prompt = ap.compose_system_prompt([], cfg)
        self.assertIn("30-40 seconds", prompt)
        # 2.85 words/sec measured across the voice pool -> 85-114 words.
        self.assertIn("85 and 114 words", prompt)

    def test_human_voice_rules_included_by_default(self):
        with patch.object(ap, "build_evidence_block", return_value=""):
            prompt = ap.compose_system_prompt([], {})
        self.assertIn("delve", prompt)

    def test_human_voice_rules_can_be_disabled(self):
        with patch.object(ap, "build_evidence_block", return_value=""):
            prompt = ap.compose_system_prompt([], {"enforce_human_voice": False})
        self.assertNotIn("delve", prompt)

    def test_evidence_supersedes_notes(self):
        with patch.object(ap, "build_evidence_block", return_value="\nEVIDENCE HERE"):
            prompt = ap.compose_system_prompt(["a stale lesson"], {})
        self.assertIn("EVIDENCE HERE", prompt)
        self.assertNotIn("a stale lesson", prompt)

    def test_notes_used_as_cold_start_fallback(self):
        with patch.object(ap, "build_evidence_block", return_value=""):
            prompt = ap.compose_system_prompt(["a stale lesson"], {})
        self.assertIn("a stale lesson", prompt)

    def test_respects_llm_prompt_cap(self):
        with patch.object(ap, "build_evidence_block", return_value="x" * 20000):
            prompt = ap.compose_system_prompt([], {})
        self.assertLessEqual(len(prompt), 7900)


if __name__ == "__main__":
    unittest.main()
