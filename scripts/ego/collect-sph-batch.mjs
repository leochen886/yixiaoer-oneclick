// 视频号批量复盘采集（ego 版）
// 由 scripts\review-all-sph.ps1 调用，不要手动运行。
// 输入：{ targets: [{ runDir, publishDesc, publishTitle, publishDate, capturedAt, postId, hookTitle, transcript, metricsAtPublish }], outputPath, includeComments, probeStats }
// 输出：逐条更新 <runDir>/retro-snapshots.json 与 <runDir>/report.md，并写回 outputPath；最后一行打印 RESULT=OK collected=x/y

import * as fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { ensureLogin } from "./ensure-login.mjs";
import {
  appendRetroSnapshot,
  collectAllMatchingPosts,
  computeAgeAndStage,
  computeEngagement,
  openCommentsPage,
  parseMetricNumber,
  renderRunReport,
  selectAndReadComments,
} from "./sph-posts.mjs";

const INPUT_PATH = new URL(import.meta.url).searchParams.get("input");
const LIST_URL = "https://channels.weixin.qq.com/platform/post/list";
const STAT_URL = "https://channels.weixin.qq.com/platform/statistic/post";

const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();

async function readInput() {
  const raw = await fs.readFile(INPUT_PATH, "utf8");
  return JSON.parse(raw);
}

async function writeJsonAtomic(filePath, data) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const temp = filePath + "." + randomUUID() + ".tmp";
  await fs.writeFile(temp, JSON.stringify(data, null, 2), "utf8");
  await fs.rename(temp, filePath);
}

async function probePostStatistics(page, matchedMap, targets) {
  const extraByRunDir = new Map();
  if (!matchedMap.size) return extraByRunDir;
  try {
    await page.goto(STAT_URL, { waitUntil: "domcontentloaded", timeout: 12000 });
    await page
      .waitForFunction(
        () => {
          const roots = [document, ...Array.from(document.querySelectorAll("wujie-app"), (a) => a.shadowRoot).filter(Boolean)];
          return roots.some((r) => /完播|平均播放|播放时长/.test(r.textContent || ""));
        },
        undefined,
        { timeout: 5000 },
      )
      .catch(() => {});
    await page.waitForTimeout(1000);

    const rows = await page.evaluate(() => {
      const roots = [document, ...Array.from(document.querySelectorAll("wujie-app"), (a) => a.shadowRoot).filter(Boolean)];
      const candidates = [];
      const seen = new Set();
      for (const root of roots) {
        const els = root.querySelectorAll('tr, [class*="row"], [class*="item"]');
        for (const el of els) {
          const text = (el.textContent || "").replace(/\s+/g, " ").trim();
          if (text.length < 6 || text.length > 500 || seen.has(text)) continue;
          if (!/%|\d+\s*[秒分]|\d+:\d{2}/.test(text)) continue;
          seen.add(text);
          const pctMatch = text.match(/(\d+(?:\.\d+)?\s*%)/g);
          const durMatch = text.match(/(\d+\s*分\s*\d+(?:\.\d+)?\s*秒|\d+(?:\.\d+)?\s*秒|\d+:\d{2})/);
          candidates.push({
            text,
            completionRate: pctMatch ? pctMatch[0].replace(/\s+/g, "") : null,
            avgWatchDuration: durMatch ? durMatch[1].replace(/\s+/g, "") : null,
          });
        }
      }
      return candidates;
    });

    if (!rows.length) {
      console.log("STATS_PROBE_NONE");
      return extraByRunDir;
    }

    let foundCount = 0;
    for (const target of targets) {
      const post = matchedMap.get(target.runDir);
      if (!post) continue;
      const snippet = clean(post.title || target.publishDesc || target.publishTitle).slice(0, 16);
      if (!snippet) continue;
      const hits = rows.filter((r) => r.text.includes(snippet));
      if (hits.length === 1 && (hits[0].completionRate || hits[0].avgWatchDuration)) {
        extraByRunDir.set(target.runDir, {
          completionRate: hits[0].completionRate,
          avgWatchDuration: hits[0].avgWatchDuration,
        });
        foundCount += 1;
      }
    }
    console.log("STATS_PROBE_FOUND count=" + foundCount);
  } catch (e) {
    console.log("STATS_PROBE_SKIP " + (e && e.message ? e.message : String(e)).split("\n")[0]);
  }
  return extraByRunDir;
}

async function main() {
  const input = await readInput();
  const targets = Array.isArray(input.targets) ? input.targets : [];
  const task = await taskSpace("sph-review-batch-" + Date.now());
  const page = task.page("p1");

  try {
    await ensureLogin(task, page);

    await page.goto(LIST_URL, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
    await page
      .waitForFunction(
        () => {
          const root = document.querySelector("wujie-app")?.shadowRoot;
          return !!root?.querySelector(".post-feed-item") || /暂无作品|暂无动态/.test(root?.textContent || "");
        },
        undefined,
        { timeout: 15000 },
      )
      .catch(() => {});

    const { matched, errors } = await collectAllMatchingPosts(
      page,
      targets.map((t) => ({ key: t.runDir, target: t })),
      { timeout: 90000, maxPages: 20 },
    );

    const extraByRunDir =
      input.probeStats !== false && matched.size > 0
        ? await probePostStatistics(page, matched, targets)
        : new Map();

    const commentsByRunDir = new Map();
    const needsComments = [];
    for (const target of targets) {
      const post = matched.get(target.runDir);
      if (!post) continue;
      const commentCount = parseMetricNumber(post.metrics?.[2]?.num)?.value ?? null;
      if (input.includeComments === false || commentCount === 0) {
        commentsByRunDir.set(target.runDir, commentCount === 0 ? [] : null);
      } else {
        needsComments.push({ target, post });
      }
    }

    if (needsComments.length > 0) {
      try {
        await openCommentsPage(page);
        for (const { target, post } of needsComments) {
          try {
            const list = await selectAndReadComments(page, post);
            commentsByRunDir.set(target.runDir, list);
          } catch (e) {
            console.log("COMMENTS_SKIP " + path.basename(target.runDir) + " " + (e?.message || String(e)));
            commentsByRunDir.set(target.runDir, null);
          }
        }
      } catch (e) {
        console.log("COMMENTS_PAGE_SKIP " + (e?.message || String(e)));
      }
    }

    const results = [];
    let okCount = 0;
    for (const target of targets) {
      if (errors.has(target.runDir)) {
        results.push({ runDir: target.runDir, status: "error", error: errors.get(target.runDir) });
        continue;
      }
      const post = matched.get(target.runDir);
      if (!post) {
        results.push({ runDir: target.runDir, status: "not_found" });
        continue;
      }

      const now = new Date();
      const comments = commentsByRunDir.has(target.runDir) ? commentsByRunDir.get(target.runDir) : null;
      const extraMetrics = extraByRunDir.get(target.runDir) || null;
      const engagement = computeEngagement(post.metrics);
      const { ageHours, stage } = computeAgeAndStage(target.capturedAt, now);

      await appendRetroSnapshot(target.runDir, {
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
        extraMetrics,
        commentsCount: Array.isArray(comments) ? comments.length : null,
        comments,
        post: { id: post.id || null, title: post.title || "", time: post.time || "" },
      });

      if (post.id && !target.postId) {
        try {
          const retroFile = path.join(target.runDir, "retro.json");
          const retro = JSON.parse(await fs.readFile(retroFile, "utf8"));
          if (!retro.postId) {
            retro.postId = post.id;
            await writeJsonAtomic(retroFile, retro);
          }
        } catch (e) {}
      }

      const report = renderRunReport({ input: target, post, comments, extraMetrics, collectedAt: now });
      const reportPath = path.join(target.runDir, "report.md");
      const tmpReport = reportPath + "." + randomUUID() + ".tmp";
      await fs.writeFile(tmpReport, report, "utf8");
      await fs.rename(tmpReport, reportPath);

      okCount += 1;
      results.push({
        runDir: target.runDir,
        status: "ok",
        post: { id: post.id || null, title: post.title || "", time: post.time || "" },
        engagement,
        extraMetrics,
        commentsCount: Array.isArray(comments) ? comments.length : null,
      });
    }

    if (input.outputPath) {
      await writeJsonAtomic(input.outputPath, {
        collectedAt: new Date().toISOString(),
        total: targets.length,
        collected: okCount,
        results,
      });
    }

    console.log(`RESULT=OK collected=${okCount}/${targets.length}`);
  } catch (e) {
    console.log("RESULT=FAIL " + (e && e.message ? e.message : String(e)).split("\n")[0]);
  } finally {
    try { await task.finish({ keep: [] }); } catch (e) {}
  }
}

await main();
