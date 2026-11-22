// 视频号复盘采集（ego 版）—— 数据半环第 2 块
// 由 scripts\review-sph.ps1 调用，不要手动运行。
// 输入：复盘运行目录的 JSON，经模块 URL 传入。
//   { runDir, publishDesc, publishTitle, publishDate, metricsAtPublish }
// 输出：<runDir>/report.md；最后一行打印 RESULT=OK / RESULT=FAIL

import * as fs from "node:fs/promises";
import { findPost, metricDelta } from "./sph-posts.mjs";
import { randomUUID } from "node:crypto";
const INPUT_PATH = new URL(import.meta.url).searchParams.get("input");
const LIST_URL = "https://channels.weixin.qq.com/platform/post/list";
const COMMENT_URL = "https://channels.weixin.qq.com/platform/interaction/comment";

// 5 个指标按列表固定顺序（图标已校准）：播放/点赞/评论/转发/收藏
const METRIC_LABELS = ["播放", "点赞", "评论", "转发", "收藏"];

async function readInput() {
  const raw = await fs.readFile(INPUT_PATH, "utf8");
  return JSON.parse(raw);
}

// 进评论管理页，点目标作品，抓 Top20 评论（作者/时间/文本）。失败返回 null。
async function collectComments(page, post) {
  await page.goto(COMMENT_URL, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
  await page
    .waitForFunction(
      () => {
        const wujie = document.querySelector("wujie-app");
        const sr = wujie && wujie.shadowRoot;
        const t = sr ? sr.textContent || "" : "";
        return t.includes("全部视频");
      },
      undefined,
      { timeout: 30000 },
    )
    .catch(() => {});
  await page.waitForTimeout(2000);

  // 点左侧目标作品（.comment-feed-wrap 行）
  const rect = await page.evaluate(
    ({ id, title, time }) => {
      const root = document.querySelector("wujie-app")?.shadowRoot;
      if (!root) return null;
      const clean = value => String(value || "").replace(/\s+/g, " ").trim();
      const matches = Array.from(root.querySelectorAll(".comment-feed-wrap")).filter(el => {
        const rowId = el.getAttribute("data-object-id") || el.getAttribute("data-feed-id");
        if (rowId && id) return rowId === id;
        const text = clean(el.textContent);
        return !!clean(title) && text.includes(clean(title)) && !!time && text.includes(clean(time));
      });
      if (matches.length !== 1) return null;
      const el = matches[0];
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    }, post,
  );

  if (!rect) return null;
  await page.cdp("Input.dispatchMouseEvent", { type: "mousePressed", x: rect.x, y: rect.y, button: "left", clickCount: 1 });
  await page.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x: rect.x, y: rect.y, button: "left", clickCount: 1 });
  await page.waitForTimeout(3500);

  const res = await page.evaluate(() => {
    const wujie = document.querySelector("wujie-app");
    const sr = wujie && wujie.shadowRoot;
    if (!sr) return { __noShadow: true, list: [] };
    const authors = [...sr.querySelectorAll(".comment-user-name")].map((e) => (e.textContent || "").trim());
    const times = [...sr.querySelectorAll(".comment-time")].map((e) => (e.textContent || "").trim());
    const texts = [...sr.querySelectorAll(".comment-row__main")].map((e) => (e.textContent || "").replace(/\s+/g, " ").trim());
    const list = [];
    const n = Math.min(authors.length, times.length, texts.length, 20);
    for (let i = 0; i < n; i++) {
      if (texts[i]) list.push({ author: authors[i] || "", time: times[i] || "", text: texts[i] });
    }
    return { __noShadow: false, list };
  });

  return res && !res.__noShadow ? res.list : null;
}

async function main() {
  const input = await readInput();
  const task = await taskSpace("sph-review-" + Date.now());
  const page = task.page("p1");
  try {
    // 1) 作品列表 → 5 个计数
    await page.goto(LIST_URL, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(2000);
    const post = await findPost(page, input);
    if (!post) throw new Error("未找到与发布记录一致的作品");
    const display = n => n == null || n === "" ? "—" : String(n);
    const current = METRIC_LABELS.map((label, i) => ({ label, value: display(post.metrics[i]?.num) }));

    // 2) 评论（尽力而为）
    let comments = null;
    try {
      comments = await collectComments(page, post);
    } catch (e) {
      console.log("COMMENTS_SKIP " + (e && e.message ? e.message : String(e)));
    }

    // 3) 组装 report
    const atPublish = input.metricsAtPublish || [];
    const now = new Date();

    const lines = [];
    lines.push("# 视频号复盘报告");
    lines.push("");
    lines.push("- 作品：`" + (input.publishTitle || post.title.slice(0, 40)) + "`");
    lines.push("- 发布日期：" + (post.time || input.publishDate || "未知"));
    lines.push("- 复盘时间：" + now.toLocaleString("zh-CN", { hour12: false }));
    lines.push("- 平台：视频号");
    lines.push("");
    lines.push("## 数据");
    lines.push("");
    lines.push("| 指标 | 发布时 | 复盘时 | 变化 |");
    lines.push("|---|---|---|---|");
    for (let i = 0; i < current.length; i++) {
      const pub = display(atPublish[i]?.num);
      const cur = current[i].value;
      const d = metricDelta(pub, cur);
      lines.push("| " + current[i].label + " | " + pub + " | " + cur + " | " + d + " |");
    }
    lines.push("");

    if (input.transcript) {
      lines.push("## 口播文案");
      lines.push("");
      lines.push(input.transcript);
      lines.push("");
    }

    if (comments !== null) {
      lines.push("## 评论（Top " + comments.length + "）");
      lines.push("");
      if (comments.length === 0) {
        lines.push("（暂无评论）");
      } else {
        comments.forEach((c, i) => {
          lines.push((i + 1) + ". **" + (c.author || "匿名") + "** · " + (c.time || "") + "：" + c.text);
        });
      }
      lines.push("");
    }

    if (comments === null) lines.push("## 评论", "", "本次未能核实该作品的评论。", "");

    if (input.publishDesc) {
      lines.push("## 原文案");
      lines.push("");
      lines.push(input.publishDesc);
      lines.push("");
    }

    const report = lines.join("\n");
    const outPath = input.runDir + "/report.md";
    await fs.mkdir(input.runDir, { recursive: true });
    const temporary = outPath + "." + randomUUID() + ".tmp";
    await fs.writeFile(temporary, report, "utf8");
    await fs.rename(temporary, outPath);

    console.log("REPORT_WRITTEN " + outPath);
    console.log("RESULT=OK");
  } catch (e) {
    console.log("RESULT=FAIL " + (e && e.message ? e.message : String(e)));
  } finally {
    try { await task.finish({ keep: [] }); } catch (e) {}
  }
}

await main();
