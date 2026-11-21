import io
import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from koubo import is_exported_video
from koubo_plan import align_reviewed_words, caption_events, make_events, make_plan, read_words, wrap_caption
from koubo_proofread import ReviewTruncated, add_missing_cues, align_hearing, compatible, project_reference, review_captions, transcribe_deepseek


def words(text, spans):
    return read_words([{"text": text, "timestamp": spans}])


class CutPlanTests(unittest.TestCase):
    def test_publish_recognizes_export_and_stops_on_mismatched_subtitles(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "example_cut.mp4"
            srt = video.with_suffix(".srt")
            srt.write_bytes(b"subtitles")
            report = {"source": "example.mp4", "keep": [[0, 2]], "output_duration": 1.5,
                      "proofreading": {"srt_sha256": hashlib.sha256(srt.read_bytes()).hexdigest()}}
            video.with_name("剪辑记录.json").write_text(json.dumps(report), encoding="utf-8")
            self.assertFalse(is_exported_video(video.with_name("example.mp4")))
            with patch("koubo.probe", return_value=(0, 1.5, 25)):
                self.assertTrue(is_exported_video(video))
            srt.write_bytes(b"wrong subtitles")
            with self.assertRaisesRegex(ValueError, "SRT"):
                is_exported_video(video)

    def test_pause_requires_both_asr_gap_and_continuous_quiet(self):
        tokens = words("甲乙", [[0, 200], [1800, 2000]])
        keep, dropped, _ = make_plan(tokens, [(0.3, 0.8), (1.0, 1.6)], 2, 25)
        self.assertTrue(any(a <= 22 < b for a, b in keep))  # untranscribed sound at 0.88 s
        self.assertEqual(len(keep), 3)
        events = make_events(tokens, dropped, keep, 25)
        self.assertAlmostEqual(events[-1]["end"], sum(b-a for a, b in keep) / 25)

    def test_ambiguous_words_stay_in_audio_and_subtitles(self):
        text = "然后其实就是这个那个"
        tokens = words(text, [[200 + i*200, 300 + i*200] for i in range(len(text))])
        keep, dropped, _ = make_plan(tokens, [(0, 2.5)], 2.5, 25)
        self.assertFalse(dropped)
        self.assertEqual("".join(e["text"] for e in make_events(tokens, dropped, keep, 25)), text)
        for w in tokens:
            self.assertTrue(any(a/25 <= w.start and w.end <= b/25 for a, b in keep))

    def test_audio_and_text_remove_the_same_filler_or_both_keep_it(self):
        tokens = words("甲，嗯，乙", [[0, 200], [500, 700], [1000, 1300]])
        for quiet, expected in (([(0.2, 0.5), (0.7, 1)], "甲乙"), ([(0.2, 0.5)], "甲嗯乙")):
            keep, dropped, _ = make_plan(tokens, quiet, 1.3, 25)
            self.assertEqual("".join(e["text"] for e in make_events(tokens, dropped, keep, 25)), expected)
            self.assertEqual(any(a/25 <= 0.6 < b/25 for a, b in keep), "嗯" in expected)

    def test_stutter_keeps_final_pronoun_with_its_sentence(self):
        tokens = words("我我我不", [[0, 200], [300, 450], [700, 850], [850, 1000]])
        keep, dropped, _ = make_plan(tokens, [(0.45, 0.7)], 1, 25)
        self.assertEqual(dropped, {0, 1})
        self.assertEqual(make_events(tokens, dropped, keep, 25)[0]["text"], "我不")

    def test_repeat_needs_a_matching_continuation(self):
        spans = [[0, 100], [100, 200], [200, 300], [720, 820], [820, 920],
                 [920, 1020], [1020, 1120], [1120, 1300]]
        for text, expected in (("我说过，我说过这事", {0, 1, 2}), ("我说过，我没说这事", set())):
            tokens = words(text, spans)
            self.assertEqual(make_plan(tokens, [(0.3, 0.72)], 1.3, 25)[1], expected)

    def test_mixed_text_and_invalid_alignment(self):
        self.assertEqual([w.text for w in words("这是 AI。", [[0, 100], [100, 200], [200, 400]])],
                         ["这", "是", "AI"])
        with self.assertRaises(ValueError):
            words("对齐失败", [[0, 100]])

    def test_pause_punctuation_and_width_cannot_split_a_word(self):
        tokens = words("而，且说实话", [[0, 100], [1500, 1600], [1600, 1700],
                                  [1700, 1800], [1800, 1950]])
        events = caption_events(tokens, limit=1)
        self.assertEqual(events[0], {"text": "而且", "start": 0, "end": 1.6})
        self.assertEqual("".join(e["text"] for e in events), "而且说实话")

    def test_asr_period_inside_a_nickname_does_not_change_the_screen(self):
        text = "就是这个口罩。哥，说实话。"
        spans = [[100*i, 100*i+90] for i in range(10)]
        spans[7:] = [[a+1800, b+1800] for a, b in spans[7:]]
        events = caption_events(words(text, spans))
        self.assertEqual([e["text"] for e in events], ["就是这个口罩哥", "说实话"])
        self.assertEqual((events[0]["start"], events[0]["end"]), (0, 0.69))
        for nickname in ("口罩哥", "眼镜姐"):
            lines = wrap_caption("我刚才看见这个" + nickname + "正在和大家打招呼", limit=10)
            self.assertIn("\n", lines)
            self.assertIn(nickname, lines)

    def test_pause_between_sense_groups_starts_a_new_single_line(self):
        clauses = ["今天下班以后，", "我准备和朋友一起去图书馆看书。", "你呢？"]
        chars = [c for c in "".join(clauses) if c not in "，。？"]
        spans = []
        for i, _ in enumerate(chars):
            t = 100*i + (550 if i >= 6 else 0)
            spans.append([t, t+100])
        events = caption_events(words("".join(clauses), spans))
        self.assertEqual([e["text"] for e in events], ["今天下班以后", "我准备和朋友一起去图书馆看书", "你呢"])
        self.assertEqual(events[1]["start"], 1.15)

    def test_two_short_sentences_that_fit_one_line_follow_the_speaker(self):
        tokens = words("今天就这样了，回头再聊吧。",
                       [[i*180, (i+1)*180] for i in range(6)]
                       + [[1400+i*180, 1580+i*180] for i in range(5)])
        cues = caption_events(tokens, limit=16, wrap=True)
        self.assertEqual([e["text"] for e in cues], ["今天就这样了", "回头再聊吧"])
        self.assertEqual(cues[1]["start"], 1.4)

    def test_frozen_replacement_stays_on_its_acoustic_anchors(self):
        tokens = words("他说 on SOOP 可以。", [[0, 100], [100, 200], [200, 450],
                                              [450, 700], [700, 850], [850, 1000]])
        fixed = align_reviewed_words(tokens, [{"start": 0, "end": 1, "text": "他说安苏匹可以"}])
        self.assertEqual([(w.start, w.end) for w in fixed if w.text == "安苏匹"], [(0.2, 0.7)])
        cues = caption_events(fixed, limit=3, wrap=True)
        self.assertEqual("".join(e["text"].replace("\n", "") for e in cues), "他说安苏匹可以")
        self.assertTrue(any("安苏匹" in e["text"] for e in cues))

    def test_wrap_keeps_corrected_name_and_short_latin_phrase_intact(self):
        original = "前天有的有哥们儿就问我on SOOP克跟不跟我坚决不跟啊"
        corrected = original.replace("on SOOP", "安苏匹")
        self.assertIn("on SOOP", wrap_caption(original))
        self.assertIn("安苏匹克", wrap_caption(corrected, source_text=original))

    def test_false_stop_after_an_unfinished_predicate_stays_in_the_sentence(self):
        spans = [[i*100, (i+1)*100] for i in range(9)]
        spans[5:] = [[a+1600, b+1600] for a, b in spans[5:]]
        events = caption_events(words("主要原因是。时间不够。", spans))
        self.assertEqual([e["text"] for e in events], ["主要原因是时间不够"])

    def test_replacements_require_relevant_acoustic_support(self):
        self.assertTrue(compatible("正坛", "政坛", "政坛的位置", ""))
        self.assertFalse(compatible("大漂乱滚", "大票乱滚", "股票。大高乱国。", "大漂亮果"))
        self.assertFalse(compatible("三成", "七成", "七成", ""))
        self.assertFalse(compatible("不建议", "建议", "不建议", ""))
        self.assertFalse(compatible("不建议", "建议", "不建议", "我不建议你去"))

    def test_context_can_resolve_homophones_absent_from_both_transcripts(self):
        self.assertTrue(compatible("这门成序课", "这门程序课", "这门成续课", ""))
        self.assertFalse(compatible("这门成序课", "这门数学课", "这门成续课", ""))
        self.assertFalse(compatible("他在备课", "他在散步", "", ""))
        self.assertTrue(compatible("大家都来修可", "大家都来修课", "", "大家都来修可"))

    @patch("koubo_proofread.api_key", return_value="test")
    @patch("koubo_proofread.requests.post")
    def test_audio_asr_splits_on_silence_and_preserves_all_samples(self, post, _key):
        post.return_value.status_code = 200
        post.return_value.json.side_effect = [{"request_id": "a", "text": "早上"}, {"request_id": "b", "text": "好"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.wav"
            with wave.open(str(path), "wb") as out:
                out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                out.writeframes(bytes(121 * 32000))
            receipt = transcribe_deepseek(path, [(114, 116)])
        self.assertEqual(receipt["text"], "早上好")
        self.assertEqual([c["end"] for c in receipt["chunks"]], [115, 121])
        for request, seconds in zip(post.call_args_list, (115, 6)):
            with wave.open(io.BytesIO(request.kwargs["files"]["file"][1])) as part:
                self.assertEqual(part.getnframes(), seconds * 16000)


    @patch("koubo_proofread.api_key", return_value="test")
    @patch("koubo_proofread.request_review")
    def test_final_cues_are_checked_once_and_keep_their_times(self, review, _key):
        events = [{"start": 1, "end": 2, "text": "正坛"}, {"start": 3, "end": 4, "text": "而且"}]
        review.return_value = ([[1, "政坛", "改"], [2, "而且", "原"]], {"prompt_tokens": 30}, "run-id")
        fixed, report = review_captions(events, [], {"text": "政坛，而且"})
        self.assertEqual(fixed[0], {**events[0], "text": "政坛"})
        self.assertEqual(report["checked_cues"], 2)
        self.assertEqual(report["request_ids"], ["run-id"])
        review.return_value = ([[2, "而且", "原"]], {}, "bad-id")
        with self.assertRaisesRegex(ValueError, "编号"):
            review_captions(events, [])

    def test_reference_can_fill_a_retained_gap_without_restoring_cut_speech(self):
        source = [{"start": 2.1, "end": 3.9, "text": "完全删掉"},
                  {"start": 4.5, "end": 5.2, "text": "任务完成了"}]
        kept = project_reference(source, [(0, 2), (4, 6)])
        events = [{"start": 0.2, "end": 1.9, "text": "前句"}]
        fixed, added = add_missing_cues(events, kept, "前句。任务完成了。")
        self.assertEqual(len(added), 1)
        self.assertEqual(fixed[-1]["text"], "任务完成了")
        self.assertAlmostEqual(fixed[-1]["start"], 2.5)
        self.assertEqual(add_missing_cues(events, kept, "")[0], events)

    @patch("koubo_proofread.api_key", return_value="test")
    @patch("koubo_proofread.request_review")
    def test_truncated_batch_retries_parts_in_order_and_counts_usage(self, review, _key):
        events = [{"start": 0, "end": 1, "text": "甲"}, {"start": 1, "end": 2, "text": "乙"}]
        review.side_effect = [
            ReviewTruncated({"id": "partial", "usage": {"completion_tokens": 900}}),
            ([[1, "甲", "原"]], {"completion_tokens": 10}, "first"),
            ([[2, "乙", "原"]], {"completion_tokens": 12}, "second"),
        ]
        fixed, report = review_captions(events, [])
        self.assertEqual(fixed, events)
        self.assertEqual(report["request_ids"], ["partial", "first", "second"])
        self.assertEqual((report["completion_tokens"], report["checked_cues"]), (922, 2))

    def test_audio_reference_preserves_whole_words_and_right_hand_anchor(self):
        rows = [{"text": s} for s in ("封号sure", "第一真狠", "这真的大漂乱滚", "真的是让我长见识")]
        spans, _ = align_hearing(rows, "封号，秀儿，真狠。这这大漂亮果，真的是让我长见识。")
        self.assertEqual(spans, ["封号秀儿", "真狠", "这这大漂亮果", "真的是让我长见识"])

    def test_missing_line_uses_only_the_unoccupied_part_of_the_interval(self):
        events = [{"start": 0, "end": 1, "text": "前句"}, {"start": 2.9, "end": 4, "text": "后句"}]
        ref = [{"start": 1.2, "end": 3.1, "text": "来哥这我完成了"}]
        _, added = add_missing_cues(events, ref, "前句。来哥，任务完成了。后句。")
        self.assertEqual(added, [{"start": 1.2, "end": 2.9, "text": "来哥任务完成了"}])


if __name__ == "__main__":
    unittest.main()
