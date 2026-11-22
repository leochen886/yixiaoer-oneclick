import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from koubo_deepseek import ask_flash


def decisions(corrections=(), repeats=(), pauses=()):
    return {"corrections": list(corrections), "repeats": list(repeats), "pauses": list(pauses)}


def response(request_id, reason, content):
    data = {"id": request_id, "model": "deepseek-flash",
            "usage": {"completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 3}},
            "choices": [{"finish_reason": reason, "message": {"content": content}}]}
    return Mock(**{"json.return_value": data})


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
