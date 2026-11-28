// 视频号单条复盘采集（ego 版）
// 由 scripts\review-sph.ps1 调用，不要手动运行。
// 输入：复盘运行目录的 JSON，经模块 URL 传入。
//   { runDir, publishDesc, publishTitle, publishDate, capturedAt, postId, hookTitle, transcript, metricsAtPublish }
// 输出：<runDir>/report.md 与 <runDir>/retro-snapshots.json；最后一行打印 RESULT=OK / RESULT=FAIL

import * as fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { ensureLogin } from "./ensure-login.mjs";
import {
  appendRetroSnapshot,
  computeAgeAndStage,
  computeEngagement,
  findPost,
  openCommentsPage,
  parseMetricNumber,
  renderRunReport,
  selectAndReadComments,
} from "./sph-posts.mjs";

const INPUT_PATH = new URL(import.meta.url).searchParams.get("input");
const LIST_URL = "https://channels.weixin.qq.com/platform/post/list";

async function readInput() {
  const raw = await fs.readFile(INPUT_PATH, "utf8");
  return JSON.parse(raw);
}

async function main() {
  const input = await readInput();
  const task = await taskSpace("sph-review-" + Date.now());
  const page = task.page("p1");
  try {
    await ensureLogin(task, page);

    // 1) 作品列表 → 5 个计数
    await page.goto(LIST_URL, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(2000);
    const post = await findPost(page, input);
    if (!post) throw new Error("未找到与发布记录一致的作品");

    // 2) 评论（有评论或未知评论数时尽力而为）
    const commentCount = parseMetricNumber(post.metrics?.[2]?.num)?.value ?? null;
    let comments = commentCount === 0 ? [] : null;
    if (commentCount !== 0) {
      try {
        await openCommentsPage(page);
        comments = await selectAndReadComments(page, post);
      } catch (e) {
        console.log("COMMENTS_SKIP " + (e && e.message ? e.message : String(e)));
      }
    }

    // 3) 写入历史快照与 report.md
    const now = new Date();
    const engagement = computeEngagement(post.metrics);
    const { ageHours, stage } = computeAgeAndStage(input.capturedAt, now);
    await appendRetroSnapshot(input.runDir, {
      collectedAt: now.toISOString(),
      stage,
      ageHours,
      rawMetrics: post.metrics,
      parsedMetrics: {
        plays: engagement.plays,
        likes: engagement.likes,
        comments: engagement.comments,
        shares: engagement.shares,
        saves: engagement.saves,
      },
      engagement: {
        interactions: engagement.interactions,
        interactionRate: engagement.interactionRate,
        likeRate: engagement.likeRate,
        commentRate: engagement.commentRate,
        shareRate: engagement.shareRate,
        saveRate: engagement.saveRate,
      },
      extraMetrics: null,
      commentsCount: Array.isArray(comments) ? comments.length : null,
      comments,
      post: { id: post.id || null, title: post.title || "", time: post.time || "" },
    });

    if (post.id && !input.postId) {
      try {
        const retroFile = path.join(input.runDir, "retro.json");
        const retro = JSON.parse(await fs.readFile(retroFile, "utf8"));
        if (!retro.postId) {
          retro.postId = post.id;
          const tmpRetro = retroFile + "." + randomUUID() + ".tmp";
          await fs.writeFile(tmpRetro, JSON.stringify(retro, null, 2), "utf8");
          await fs.rename(tmpRetro, retroFile);
        }
      } catch (e) {}
    }

    const report = renderRunReport({ input, post, comments, collectedAt: now });
    const outPath = path.join(input.runDir, "report.md");
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
