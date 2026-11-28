import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {
  appendRetroSnapshot,
  collectAllMatchingPosts,
  computeEngagement,
  findPost,
  matchesPost,
  metricDelta,
  parseMetricNumber,
  renderRunReport,
  selectAndReadComments,
  verifyAssistant,
} from "./sph-posts.mjs";
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

test("parseMetricNumber 与 computeEngagement 正确计算大数与互动率", () => {
  assert.deepEqual(parseMetricNumber("1.2万"), { value: 12000, approximate: true });
  assert.deepEqual(parseMetricNumber("1亿"), { value: 100000000, approximate: true });
  assert.equal(parseMetricNumber("—"), null);

  const eng = computeEngagement([{ num: "1000" }, { num: "30" }, { num: "10" }, { num: "5" }, { num: "5" }]);
  assert.equal(eng.plays, 1000);
  assert.equal(eng.interactions, 50);
  assert.equal(eng.interactionRate, 0.05);

  const zero = computeEngagement([{ num: "0" }, { num: "0" }, { num: "0" }, { num: "0" }, { num: "0" }]);
  assert.equal(zero.interactionRate, null);
});

test("collectAllMatchingPosts 跨页收集并隔离单条歧义错误", async () => {
  const rowA = { id: "a", title: "作品A", time: "周一", metrics: [{ num: "100" }] };
  const rowB = { id: "b", title: "作品B", time: "周二", metrics: [{ num: "200" }] };
  const rowDup = { id: "", title: "重名作品", time: "周三", metrics: [{ num: "10" }] };
  const page = paged([[rowA, rowDup], [rowB, rowDup]]);
  const targets = [
    { key: "kA", target: { postId: "a", publishTitle: "作品A" } },
    { key: "kB", target: { postId: "b", publishTitle: "作品B" } },
    { key: "kDup", target: { publishTitle: "重名作品", publishDate: "周三" } },
  ];
  const { matched, errors } = await collectAllMatchingPosts(page, targets, { maxPages: 5 });
  assert.equal(matched.get("kA").id, "a");
  assert.equal(matched.get("kB").id, "b");
  assert.equal(matched.has("kDup"), false);
  assert.match(errors.get("kDup"), /多个匹配/);
});

test("selectAndReadComments 自动滚动加载旧视频并读取评论", async () => {
  let step = 0;
  let clicked = null;
  const fakePage = {
    async evaluate(fn) {
      if (fn.name === "locateCommentItem") {
        step += 1;
        if (step <= 2) return { status: "not_found", count: step * 5 };
        return { status: "found", x: 120, y: 240, count: 15 };
      }
      assert.equal(fn.name, "readCommentRows");
      return { __noShadow: false, list: [{ author: "老张", time: "昨天", text: "讲得真透彻" }] };
    },
    async waitForTimeout() {},
    async cdp(method, params) {
      if (method === "Input.dispatchMouseEvent" && params.type === "mousePressed") {
        clicked = [params.x, params.y];
      }
    },
  };
  const comments = await selectAndReadComments(fakePage, { id: "old1", title: "旧视频", time: "三天前" });
  assert.deepEqual(clicked, [120, 240]);
  assert.equal(comments.length, 1);
  assert.equal(comments[0].text, "讲得真透彻");
});

test("appendRetroSnapshot 与 renderRunReport 写入快照与完整报告", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "sph-snap-"));
  try {
    await appendRetroSnapshot(dir, { stage: "1天", parsedMetrics: { plays: 500 } });
    const store = await appendRetroSnapshot(dir, { stage: "3天", parsedMetrics: { plays: 1200 } });
    assert.equal(store.snapshots.length, 2);
    assert.equal(store.latest.stage, "3天");
    const disk = JSON.parse(await readFile(path.join(dir, "retro-snapshots.json"), "utf8"));
    assert.equal(disk.latest.parsedMetrics.plays, 1200);

    const md = renderRunReport({
      input: { publishTitle: "短标题", hookTitle: "顶部黄字", transcript: "口播全文", publishDesc: "原文案" },
      post: { title: "短标题", time: "2026年11月20日", metrics: [{ num: "1000" }, { num: "20" }, { num: "5" }, { num: "3" }, { num: "2" }] },
      comments: [{ author: "观众", time: "今天", text: "赞" }],
      extraMetrics: { completionRate: "41.5%" },
    });
    assert.match(md, /顶部黄字：`顶部黄字`/);
    assert.match(md, /总互动率 \| 3\.00%/);
    assert.match(md, /完播率 \| 41\.5%/);
    assert.match(md, /讲|赞/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
