import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch
import requests

from koubo import compact_word_clock
from koubo_deepseek import ask_flash, compact_silences, index_inputs, pause_trims, select_cuts
from koubo_plan import Word


def decisions(corrections=(), repeats=(), pauses=()):
    return {"corrections": list(corrections), "repeats": list(repeats), "pauses": list(pauses)}


def response(request_id, reason, content):
    data = {"id": request_id, "model": "deepseek-flash",
            "usage": {"completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 3}},
            "choices": [{"finish_reason": reason, "message": {"content": content}}]}
    return Mock(**{"json.return_value": data})


class PauseTests(unittest.TestCase):
    @patch("faster_whisper.vad.get_speech_timestamps")
    @patch("koubo_deepseek.subprocess.check_output")
    def test_compact_quiet_inside_a_padded_word_preserves_voice_and_word_text(self, decode, vad):
        import numpy as np
        audio = np.zeros(3*16000, dtype="<i2")
        audio[16000:17600] = 1500  # Audible transient not classified as speech.
        decode.return_value = audio.tobytes()
        vad.return_value = [{"start":0, "end":4000}, {"start":40000, "end":48000}]
        word = Word("cloud", .10, 2.9, True, False)
        # One pre-existing edit inside the pause must not be subtracted twice.
        keep = [(0,40),(45,90)]
        cuts = compact_silences(Path("example.mp4"), 3, 30, [word], keep)
        self.assertTrue(cuts)
        self.assertEqual(compact_silences(Path("example.mp4"), 3, 30, [word], keep, min_pause=5), [])
        self.assertTrue(all(a/30 >= .25 and b/30 <= 2.5 for a,b in cuts))
        self.assertTrue(all(b/30 <= 1 or a/30 >= 1.1 for a,b in cuts))
        self.assertTrue(all(b<=40 or a>=45 for a,b in cuts))
        mapped, = compact_word_clock([Word("cloud",.1,2.9-5/30,True,False)],cuts,keep,30)
        self.assertEqual(mapped.text, word.text)
        self.assertAlmostEqual(mapped.start, .1)
        self.assertAlmostEqual(mapped.end, 2.9-(5+sum(b-a for a,b in cuts))/30)
        vad.return_value = []
        self.assertEqual(compact_silences(Path("example.mp4"), 3, 30, [word], keep), [])

    def test_unmarked_long_gap_is_shortened_without_cutting_the_sound_between_quiet_runs(self):
        anchors = [Word("甲",0,.3,True,False), Word("乙",1.9,2.1,True,False)]
        meta = {"fps":"30","duration":2.1,"quiet":[(.3,1.),(1.20,1.9)]}
        _, gaps, _ = index_inputs({"chunks":[]}, anchors, meta)
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp)/"source.wav"
            with wave.open(str(wav),"wb") as f:
                f.setparams((1,2,16000,0,"NONE",""))
                f.writeframes(bytes(33600*2))
            for proposed in ([], [[1,1.5,"句末长停顿"]]):
                cuts, audit = select_cuts(anchors,[0,1],gaps,decisions(pauses=proposed),meta,wav)
                self.assertEqual(len(cuts),2)
                self.assertLessEqual(cuts[0]["source_end"],1.)
                self.assertGreaterEqual(cuts[1]["source_start"],1.2)
                self.assertEqual(audit[0]["source"],"Flash" if proposed else "本地静音")
                self.assertAlmostEqual(audit[0]["remaining"],.50,delta=.06)

    def test_pause_budget_excludes_frames_already_deleted_with_a_repeat(self):
        gap = {"a":.5,"b":2.5,"quiet":[(.5,2.5)]}
        cuts, remaining = pause_trims(gap,.5,[(0,30),(70,90)],30,leading=False,trailing=False)
        self.assertAlmostEqual(remaining,2-40/30)
        self.assertTrue(cuts)
        self.assertTrue(all(b<=30 or a>=70 for a,b in cuts))
        self.assertAlmostEqual(remaining-sum(b-a for a,b in cuts)/30,.5)


class FlashLengthTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.content = {"主稿全文": [[1, "现在开始。"], [2, "现在开始读书。"]]}
        self.limits = {"corrections": 2, "repeats": 4, "pauses": 1}
        self.post = self.enter_patch("koubo_deepseek.requests.post")
        self.enter_patch("koubo_deepseek.api_key", return_value="test")

    def enter_patch(self, target, **kwargs):
        context = patch(target, **kwargs)
        self.addCleanup(context.stop)
        return context.start()

    def run_flash(self):
        return ask_flash(self.content, self.folder, self.limits)

    def test_scoped_response_keeps_setup_question_and_evidence_together(self):
        hook = dict(hook_context="每天早上都在赶车", hook_title="早起能省多少时间", hook_evidence="省十分钟")
        self.post.side_effect = [response("first", "length", ""), response("second", "length", ""),
                                 response("corr", "stop", json.dumps({**decisions(), **hook})),
                                 response("rep", "stop", json.dumps(decisions())),
                                 response("pause", "stop", json.dumps(decisions()))]
        self.assertEqual(self.run_flash()["proposal"], {**decisions(), **hook})

    def test_previous_heading_cache_requires_the_same_transcript(self):
        self.post.return_value = response("ok", "stop", json.dumps(decisions()))
        result = self.run_flash()
        input_file = self.folder / "flash-input.json"
        body = json.loads(input_file.read_text(encoding="utf-8"))
        base, _, _ = body["messages"][0]["content"].partition("\n另返回三个字符串")
        body["messages"][0]["content"] = base + "\n同时返回字符串 hook_title：根据保留下来的全文，写一句钩子。"
        old_input = json.dumps(body, ensure_ascii=False)
        input_file.write_text(old_input, encoding="utf-8")
        result["input_sha256"] = hashlib.sha256(old_input.encode()).hexdigest()
        (self.folder / "flash.json").write_text(json.dumps(result), encoding="utf-8")
        self.assertEqual(self.run_flash(), result)
        self.content["新原话"] = "内容已经改变"
        with self.assertRaisesRegex(ValueError, "与本次盲测输入不同"):
            self.run_flash()
        self.post.assert_called_once()

    def test_length_discards_partial_even_if_parseable_and_retries_original_text(self):
        complete = decisions(pauses=[[1, .55, "拖长"]])
        partial = decisions(corrections=[[1, "开始", "结束", "残缺的回复"]])
        self.post.side_effect = [response("one", "length", json.dumps(partial)),
                                 response("two", "stop", json.dumps(complete))]
        result = self.run_flash()
        self.assertEqual(result["proposal"], complete)
        self.assertEqual(result["request_ids"], ["one", "two"])
        self.assertEqual(result["usage"]["completion_tokens"], 20)
        self.assertEqual(result["usage"]["completion_tokens_details"]["reasoning_tokens"], 6)
        first, retry = [call.kwargs["json"] for call in self.post.call_args_list]
        self.assertEqual(first["messages"], retry["messages"])
        self.assertEqual(first["thinking"]["type"], "enabled")
        self.assertEqual(retry["thinking"]["type"], "disabled")
        self.assertNotIn("reasoning_effort", retry)
        self.assertEqual(self.run_flash(), result)
        self.assertEqual(self.post.call_count, 2)

    def test_repeated_length_splits_only_the_truncated_range_and_merges_global_ids(self):
        a, b = [1, "甲", "乙", "同音"], [2, "丙", "丁", "误识"]
        repeat = [1, 2, 3, 4, "之前", "之后", "重说"]
        pause = [1, .5, "过长"]
        self.post.side_effect = [
            response("all", "length", "{"), response("brief", "length", "{"),
            response("corr", "length", "{"), response("left", "stop", json.dumps(decisions([a, b]))),
            response("right", "stop", json.dumps(decisions([a, b]))),
            response("rep", "stop", json.dumps(decisions(repeats=[repeat]))),
            response("gap", "stop", json.dumps(decisions(pauses=[pause])))]
        result = self.run_flash()
        self.assertEqual(result["proposal"], decisions([a,b], [repeat], [pause]))
        self.assertEqual([a["scope"] for a in result["attempts"]],
                         ["全文", "全文", "corrections 1–2", "corrections 1–1", "corrections 2–2",
                          "repeats 1–4", "pauses 1–1"])
        for call in self.post.call_args_list:
            self.assertEqual(json.loads(call.kwargs["json"]["messages"][1]["content"]), self.content)

    def test_persistent_length_on_one_id_stops_without_caching_an_incomplete_plan(self):
        self.limits = {"corrections": 0, "repeats": 0, "pauses": 1}
        self.post.side_effect = [response(str(i), "length", "") for i in range(3)]
        with self.assertRaisesRegex(ValueError, "pauses 1–1"):
            self.run_flash()
        self.assertFalse((self.folder / "flash.json").exists())
        self.assertEqual(self.post.call_count, 3)

    def test_other_finish_reasons_stop_immediately(self):
        self.post.return_value = response("other", "content_filter", "")
        with self.assertRaisesRegex(ValueError, "content_filter"):
            self.run_flash()
        self.post.assert_called_once()

    @patch("koubo_deepseek.time.sleep")
    def test_flash_retries_transient_network_errors_and_stops_on_auth_error(self, sleep):
        ok = response("ok", "stop", json.dumps(decisions(pauses=[[1, .4, "停顿"]])))
        self.post.side_effect = [requests.exceptions.ConnectionError("drop"), ok]
        result = self.run_flash()
        self.assertEqual(result["request_id"], "ok")
        self.assertEqual(self.post.call_count, 2)
        sleep.assert_called_once_with(1)

        (self.folder / "flash.json").unlink()
        bad_resp = Mock(status_code=401)
        http_err = requests.exceptions.HTTPError("unauthorized", response=bad_resp)
        bad_resp.raise_for_status.side_effect = http_err
        self.post.side_effect = [bad_resp]
        with self.assertRaises(requests.exceptions.HTTPError):
            self.run_flash()
        self.assertEqual(self.post.call_count, 3)
