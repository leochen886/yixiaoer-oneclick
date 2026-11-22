import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { findPost, matchesPost, metricDelta, verifyAssistant } from "./sph-posts.mjs";
import { submission } from "./submission.mjs";

test("指标区分缺失、零和带单位的近似值", () => {
  assert.equal(metricDelta("10000", "1.2万"), "约+2000");
  assert.equal(metricDelta("9", ""), "—");
  assert.equal(metricDelta("9", "0"), "-9");
  assert.equal(metricDelta(null, "10"), "—");
});
test("按完整文本、日期及作品标识核对", () => {
  const target = { publishTitle: "系列节目", publishDesc: "系列节目 第二集", publishDate: "今天" };
  assert.equal(matchesPost({ title: "系列节目 第一集", time: "今天" }, target), false);
  assert.equal(matchesPost({ title: "系列节目 第二集 #科技", time: "昨天" }, target), false);
  assert.equal(matchesPost({ title: "系列节目 第二集 #科技", time: "今天" }, target), true);
  assert.equal(matchesPost({ id: "other", title: target.publishDesc, time: "今天" }, { ...target, postId: "mine" }), false);
  assert.equal(matchesPost({ title: "正片", time: "2026年11月22日 08:00" },
    { publishTitle: "正片", capturedAt: "2026-11-22T00:00:54Z" }), true);
});

function paged(pages) {
  let index = 0;
  return {
    async evaluate(fn) {
      if (fn.name === "readPostRows") return pages[index];
      assert.equal(fn.name, "advanceList");
      if (index + 1 === pages.length) return "end";
      index++;
      return "page";
    },
    async waitForTimeout() {},
  };
}
test("查找第二页，拒绝跨页歧义", async () => {
  const row = { title: "正片", time: "周一" };
  const target = { publishTitle: row.title, publishDate: row.time };
  assert.deepEqual(await findPost(paged([[{ title: "预告" }], [row]]), target), row);
  await assert.rejects(findPost(paged([[row], [row, { title: "其他" }]]), target), /多个匹配/);
});
test("空白页面无法验证登录", async () => {
  await assert.rejects(verifyAssistant({async waitForFunction(fn) {
    globalThis.location = new URL("about:blank");
    try { if (!fn()) throw Error("未登录"); } finally { delete globalThis.location; }
  }}), /未登录/);
});
test("并发提交仅一次，未决和成功可跨进程识别，未点击可重试", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "sph-review-"));
  try {
    const dirs = ["a","b","c"].map(n => path.join(root,n));
    for (const d of dirs) await mkdir(d);
    const key = "a".repeat(64);
    const exported = "c".repeat(64);
    const attempts = dirs.slice(0,2).map(d => submission(d, key, exported));
    const results = await Promise.all(attempts.map(a => a.reserve()));
    assert.equal(results.filter(r => r === null).length, 1);
    assert.match(results.find(r => r !== null), /UNCONFIRMED/);
    await attempts[results.indexOf(null)].finish("RESULT=UNCONFIRMED");
    assert.match(await submission(dirs[2], key).reserve(), /UNCONFIRMED/);
    assert.match(await submission(dirs[2], exported).existing(), /UNCONFIRMED/);
    assert.equal(await submission(dirs[2], "d".repeat(64)).existing(), null);
    const alternate = "b".repeat(64);
    const clean = submission(dirs[2], alternate);
    assert.equal(await clean.reserve(), null);
    await clean.finish("RESULT=NOT_CLICKED");
    const retry = submission(dirs[2], alternate);
    assert.equal(await retry.reserve(), null);
    await retry.finish("RESULT=OK");
    assert.match(await submission(dirs[0], alternate).reserve(), /ALREADY_PUBLISHED/);
  } finally { await rm(root, { recursive: true, force: true }); }
});
