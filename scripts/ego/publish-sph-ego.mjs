// 视频号一键发布（ego 版）—— 浏览器自动化脚本
// 由 scripts\oneclick-sph-ego.ps1 调用，不要手动运行。
// 输入：本次发布目录的 ego-input.json，通过模块 URL 的 input 参数传入。
//   { videoPath, title, desc, tags, noPublish }
// 输出：RESULT=... 供桌面入口区分表单失败、点击、平台回执与预演。
import { declareOriginal, fillShortTitle, publishFromForm } from "./sph-publish-form.mjs";

const fs = await import("node:fs/promises");
const path = await import("node:path");
const { fileURLToPath } = await import("node:url");

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INPUT_PATH = new URL(import.meta.url).searchParams.get("input") || path.join(__dirname, "publish-sph-ego-input.json");
const ASSISTANT_HOME = "https://channels.weixin.qq.com/platform";
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

async function showWindow(task, page) {
  try {
    const win = await task.cdp("Browser.getWindowForTarget", { targetId: page.targetId });
    if (win.bounds?.windowState === "minimized") {
      await task.cdp("Browser.setWindowBounds", { windowId: win.windowId, bounds: { windowState: "normal" } });
    }
  } catch (e) {
    console.log("EGO_WINDOW " + e.message);
  }
  await page.cdp("Page.bringToFront").catch(e => console.log("EGO_FOREGROUND " + e.message));
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

// 数据半环：发布成功后，跳作品列表，按描述/标题前缀匹配到刚发的那条，
// 把标识 + 日期 + 当时计数写进 <runDir>/retro.json。任何失败都不影响发布结果。
async function captureRetro(page, input) {
  const LIST_URL = "https://channels.weixin.qq.com/platform/post/list";
  const descPrefix = (input.desc || "").replace(/\s+/g, " ").trim().slice(0, 14);
  const titlePrefix = (input.title || "").trim().slice(0, 14);

  await page.goto(LIST_URL, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(2000);

  const deadline = Date.now() + 90000; // 最多等 90 秒，等新作品出现在列表
  let extract = null;
  let titleMatch = null; // 标题匹配上（日期/计数可能还没渲染）
  let fullMatch = null;  // 标题 + 日期 + 计数都齐了
  while (Date.now() < deadline) {
    await page.waitForTimeout(3000);
    extract = await page.evaluate(
      ({ descPrefix, titlePrefix }) => {
        const wujie = document.querySelector("wujie-app");
        const sr = wujie && wujie.shadowRoot;
        if (!sr) return { __noShadow: true, matched: null, top: null };
        const items = [...sr.querySelectorAll(".post-feed-item")];
        const rows = items.map((el) => {
          const titleEl = el.querySelector(".post-title");
          const timeEl = el.querySelector(".time-label");
          const dataItems = [...el.querySelectorAll(".post-data .data-item")];
          const metrics = dataItems.map((d) => {
            const num = (d.textContent || "").replace(/\s+/g, "");
            const icon = d.querySelector("[class*=icon]");
            return { num, iconCls: icon ? icon.getAttribute("class") || "" : "" };
          });
          return {
            title: titleEl ? (titleEl.textContent || "").replace(/\s+/g, " ").trim() : "",
            time: timeEl ? (timeEl.textContent || "").replace(/\s+/g, " ").trim() : "",
            metrics,
          };
        });
        let matched = null;
        for (const r of rows) {
          if ((descPrefix && r.title.includes(descPrefix)) || (titlePrefix && r.title.includes(titlePrefix))) {
            matched = r;
            break;
          }
        }
        return { rows, matched, top: rows[0] || null };
      },
      { descPrefix, titlePrefix },
    );
    if (extract && extract.matched) {
      if (!titleMatch) titleMatch = extract.matched;
      // 标题先渲染，日期和计数晚一步；等它们也出来再收，否则记录不全
      if (extract.matched.time && extract.matched.metrics && extract.matched.metrics.length > 0) {
        fullMatch = extract.matched;
        break;
      }
    }
  }

  const chosen = fullMatch || titleMatch || (extract && extract.top) || null;
  const retro = {
    capturedAt: new Date().toISOString(),
    publishTitle: input.title || "",
    publishDesc: input.desc || "",
    publishTags: input.tags || [],
    matchMethod: titleMatch || fullMatch ? "text-prefix" : "top-row",
    matchedTitle: chosen ? chosen.title : null,
    publishDate: chosen ? chosen.time : null,
    metricsAtPublish: chosen ? chosen.metrics : null,
    note: extract && extract.__noShadow ? "shadow-root not found" : "",
  };

  if (!input.runDir) {
    console.log("RETRO_NO_RUNDIR");
    return;
  }
  await fs.mkdir(input.runDir, { recursive: true });
  await fs.writeFile(input.runDir + "/retro.json", JSON.stringify(retro, null, 2), "utf8");
  console.log(
    "RETRO_CAPTURED method=" + retro.matchMethod + " title=" + (retro.matchedTitle || "").slice(0, 30),
  );
}

async function main() {
  const input = await readInput();
  // 每次用新空间名（时间戳）：ego 的新空间默认落在同一 profile（Profile 4），
  // 登录 cookie 在 profile 层跨空间共享，所以登录态能保留；同时避免固定空间名
  // 变成 user-owned 后触发 NAME_IN_USE 报错。
  const SPACE = "视频号一键发布-" + Date.now();
  const task = await taskSpace(SPACE);
  const page = task.page("p1");
  let keepResult = false;
  try {
    console.log("EGO_SPACE=" + task.spaceId + " " + SPACE);
    await showWindow(task, page);
    // 1) 打开视频号助手，判断登录态
    await page.goto(ASSISTANT_HOME, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(3000);
    let url = await page.url();

    // 未登录 -> 需要扫码
    if (url.includes("/login") || url.includes("oauth-callback")) {
      console.log("[登录检测] 未登录，需要扫码：请在弹出的浏览器窗口里用微信扫码登录（最多等 5 分钟）");
      await showWindow(task, page);
      const deadline = Date.now() + 300000; // 最多等 5 分钟
      let loggedIn = false;
      while (Date.now() < deadline) {
        await page.waitForTimeout(5000);
        url = await page.url();
        if (url.includes("/platform") && !url.includes("/login") && !url.includes("oauth-callback")) {
          loggedIn = true;
          break;
        }
      }
      if (!loggedIn) {
        console.log("RESULT=FAIL 登录超时，未完成扫码");
        return;
      }
      console.log("[登录检测] 扫码成功，已登录");
    } else {
      console.log("[登录检测] 已登录，无需扫码，继续发布");
    }

    // 2) 进入发表页
    await page.goto(CREATE_URL, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
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

    const result = await publishFromForm(page, input.noPublish);
    keepResult = result === "RESULT=OK" || result === "RESULT=NO_PUBLISH";
    if (result === "RESULT=OK") {
      // 12) 数据半环：抓取刚发布作品的标识（非致命，失败不影响发布结果）
      try {
        await captureRetro(page, input);
      } catch (e) {
        console.log("RETRO_CAPTURE_SKIP " + (e && e.message ? e.message : String(e)));
      }
    }
    console.log(result);
  } catch (e) {
    console.log("RESULT=NOT_CLICKED " + String(e.message || e).split("\n")[0]);
  } finally {
    try {
      if (keepResult) await task.finish({ keep: [page.label] });
      else await task.handOff();
    } catch (e) { console.log("EGO_SPACE " + String(e.message || e).split("\n")[0]); }
  }
}

await main();
