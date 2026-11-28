// 视频号一键发布（ego 版）—— 浏览器自动化脚本
// 由 scripts\oneclick-sph-ego.ps1 调用，不要手动运行。
// 输入：本次发布目录的 ego-input.json，通过模块 URL 的 input 参数传入。
//   { videoPath, title, desc, tags, noPublish }
// 输出：RESULT=... 供桌面入口区分表单失败、点击、平台回执与预演。
import { ensureLogin, showWindow } from "./ensure-login.mjs";
import { declareOriginal, fillShortTitle, publishFromForm } from "./sph-publish-form.mjs";
import { computeAgeAndStage, computeEngagement, findPost, matchesPost, postKey, readPostRows } from "./sph-posts.mjs";
import { submission } from "./submission.mjs";

const fs = await import("node:fs/promises");
const path = await import("node:path");
const { fileURLToPath } = await import("node:url");
const { randomUUID } = await import("node:crypto");

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INPUT_PATH = new URL(import.meta.url).searchParams.get("input") || path.join(__dirname, "publish-sph-ego-input.json");
const LIST_URL = "https://channels.weixin.qq.com/platform/post/list";
const CREATE_URL = "https://channels.weixin.qq.com/platform/post/create";

async function readInput() {
  const raw = await fs.readFile(INPUT_PATH, "utf8");
  return JSON.parse(raw);
}

async function shadowText(page) {
  return page.evaluate(() => {
    const wujie = document.querySelector("wujie-app");
    const sr = wujie && wujie.shadowRoot;
    return sr ? sr.textContent || "" : "";
  });
}

async function waitForShadowText(page, text, present, timeout) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const t = await shadowText(page);
    const found = present ? t.includes(text) : !t.includes(text);
    if (found) return true;
    await page.waitForTimeout(3000);
  }
  throw new Error("timeout waiting for shadow text: " + text);
}

// 顺手把首屏可见的其他已发布视频最新数据同步写入对应目录的 live-metrics.json（零额外翻页成本）
async function syncVisibleRuns(page, currentRunDir) {
  try {
    const rows = await page.evaluate(readPostRows);
    if (!rows || !rows.length) return;
    const runsRoot = path.dirname(currentRunDir);
    const currentName = path.basename(currentRunDir);
    const entries = await fs.readdir(runsRoot, { withFileTypes: true });
    let synced = 0;
    const now = new Date();

    for (const entry of entries) {
      if (!entry.isDirectory() || entry.name === currentName || entry.name === ".submissions") continue;
      const dir = path.join(runsRoot, entry.name);
      const retroFile = path.join(dir, "retro.json");
      let retro;
      try {
        retro = JSON.parse(await fs.readFile(retroFile, "utf8"));
      } catch {
        continue;
      }
      const hits = rows.filter((r) => matchesPost(r, retro));
      if (hits.length !== 1) continue;
      const hit = hits[0];
      const engagement = computeEngagement(hit.metrics);
      const { ageHours, stage } = computeAgeAndStage(retro.capturedAt, now);
      const liveData = {
        capturedAt: now.toISOString(),
        stage,
        ageHours,
        post: { id: hit.id || null, title: hit.title || "", time: hit.time || "" },
        rawMetrics: hit.metrics,
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
      };
      const liveFile = path.join(dir, "live-metrics.json");
      const tmpLive = liveFile + "." + randomUUID() + ".tmp";
      await fs.writeFile(tmpLive, JSON.stringify(liveData, null, 2), "utf8");
      await fs.rename(tmpLive, liveFile);

      if (hit.id && !retro.postId) {
        retro.postId = hit.id;
        const tmpRetro = retroFile + "." + randomUUID() + ".tmp";
        await fs.writeFile(tmpRetro, JSON.stringify(retro, null, 2), "utf8");
        await fs.rename(tmpRetro, retroFile);
      }
      synced += 1;
    }
    if (synced > 0) console.log("LIVE_SYNC_COUNT=" + synced);
  } catch (e) {
    console.log("LIVE_SYNC_SKIP " + (e && e.message ? e.message : String(e)));
  }
}

// 发布后核对本次新增的作品及其时间，并保存复盘基线。
// 把标识 + 日期 + 当时计数写进 <runDir>/retro.json。任何失败都不影响发布结果。
async function captureRetro(page, input, before) {
  const capturedAt = new Date().toISOString();
  const retro = {
    capturedAt, publishTitle: input.title, publishDesc: input.desc, publishTags: input.tags,
    postId: null, matchMethod: "unmatched", matchedTitle: null, publishDate: null, metricsAtPublish: null,
  };
  const file = path.join(input.runDir, "retro.json");
  // Preserve the successful publication even if list navigation or collection times out.
  await fs.writeFile(file, JSON.stringify(retro, null, 2), "utf8");
  if (!before) return;
  await page.goto(LIST_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
  const chosen = await findPost(page, retro, { latest: true, timeout: 90000, exclude: before });
  if (chosen) {
    Object.assign(retro, { postId: chosen.id || null, matchMethod: chosen.id ? "post-id" : "text-and-time",
      matchedTitle: chosen.title, publishDate: chosen.time, metricsAtPublish: chosen.metrics });
    await fs.writeFile(file + ".tmp", JSON.stringify(retro, null, 2), "utf8");
    await fs.rename(file + ".tmp", file);
    console.log("RETRO_CAPTURED method=" + retro.matchMethod);
  }
  await syncVisibleRuns(page, input.runDir);
}

async function main() {
  const input = await readInput();
  const attempt = input.noPublish ? null : submission(input.runDir, input.publishKey, input.videoHash);
  if (attempt) {
    const prior = await attempt.existing();
    if (prior) { console.log(prior); return; }
  }
  // 每次用新空间名（时间戳）：ego 的新空间默认落在同一 profile（Profile 4），
  // 登录 cookie 在 profile 层跨空间共享，所以登录态能保留；同时避免固定空间名
  // 变成 user-owned 后触发 NAME_IN_USE 报错。
  const SPACE = "视频号一键发布-" + Date.now();
  const task = await taskSpace(SPACE);
  const page = task.page("p1");
  let keepResult = false;
  let previousPosts = null;
  try {
    console.log("EGO_SPACE=" + task.spaceId + " " + SPACE);
    await showWindow(task, page);
    // 1) 打开视频号助手，判断登录态
    try {
      await ensureLogin(task, page);
    } catch (loginErr) {
      console.log("RESULT=FAIL " + (loginErr && loginErr.message ? loginErr.message : String(loginErr)).split("\n")[0]);
      return;
    }

    if (attempt) {
      try {
        await page.goto(LIST_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
        await page.waitForFunction(() => {
          const root = document.querySelector("wujie-app")?.shadowRoot;
          return !!root?.querySelector(".post-feed-item") || /暂无作品|暂无动态/.test(root?.textContent || "");
        }, undefined, { timeout: 8000 });
        previousPosts = new Set((await page.evaluate(readPostRows)).map(postKey));
      } catch (error) { console.log("RETRO_BASELINE_SKIP " + error.message); }
    }

    // 2) 进入发表页
    await page.goto(CREATE_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(4000);

    // 3) 上传视频（shadow DOM 里的 file input）
    console.log("UPLOAD_START");
    await page.setInputFiles("input[type=file]", [input.videoPath]);

    // 4) 等上传开始（出现“取消上传”），再等上传完成（消失）
    try {
      await waitForShadowText(page, "取消上传", true, 60000);
      console.log("UPLOADING");
    } catch (e) {
      // 极小文件可能瞬间完成；不致命，继续往下走
      console.log("UPLOAD_START_NOT_OBSERVED");
    }
    try {
      await waitForShadowText(page, "取消上传", false, 900000); // 最多 15 分钟
      console.log("UPLOAD_DONE");
    } catch (e) {
      console.log("RESULT=FAIL 视频上传超时或失败");
      return;
    }
    // 5) 短标题（上限 16 字，兜底截断）
    // “取消上传”消失只代表字节传完，不代表表单已渲染；大视频转码/出封面更慢，
    // 固定 sleep 猜时长会在这步超时。改成按真实条件等待：短标题输入框可见后再填。
    input.title = await fillShortTitle(page, input.title);

    // 6) 视频描述 + 标签（contenteditable）
    const desc = (input.desc || "").trim();
    const tags = (input.tags || []).map((t) => "#" + String(t).replace(/^#/, "")).join(" ");
    const fullDesc = [desc, tags].filter(Boolean).join(" ").trim();
    if (fullDesc) {
      await page.waitForSelector(".input-editor[contenteditable]", { state: "visible", timeout: 900000 });
      await page.focus(".input-editor[contenteditable]");
      await page.keyboard.insertText(fullDesc);
      console.log("DESC_FILLED");
    }

    // 7) 核实原创声明，再检查最终表单是否可发表。
    if (input.declareOriginal !== false) {
      await declareOriginal(page);
    } else {
      console.log("SKIP_DECLARE");
    }

    const result = await publishFromForm(page, input.noPublish, 60000, attempt?.reserve);
    if (attempt) await attempt.finish(result);
    keepResult = result === "RESULT=OK" || result === "RESULT=NO_PUBLISH";
    if (result === "RESULT=OK") {
      // 12) 数据半环：抓取刚发布作品的标识（非致命，失败不影响发布结果）
      try {
        await captureRetro(page, input, previousPosts);
      } catch (e) {
        console.log("RETRO_CAPTURE_SKIP " + (e && e.message ? e.message : String(e)));
      }
    }
    console.log(result);
  } catch (e) {
    console.log((attempt?.attempted ? "RESULT=UNCONFIRMED " : "RESULT=NOT_CLICKED ") + String(e.message || e).split("\n")[0]);
  } finally {
    try {
      if (keepResult) await task.finish({ keep: [page.label] });
      else await task.handOff();
    } catch (e) { console.log("EGO_SPACE " + String(e.message || e).split("\n")[0]); }
  }
}

await main();
