// 视频号一键发布（ego 版）—— 浏览器自动化脚本
// 由 scripts\oneclick-sph-ego.ps1 调用，不要手动运行。
// 输入：本脚本同目录 publish-sph-ego-input.json（由 oneclick-sph-ego.ps1 写入，见 INPUT_PATH）
//   { videoPath, title, desc, tags, noPublish }
// 输出：最后一行固定打印 RESULT=OK / RESULT=FAIL / RESULT=NO_PUBLISH / RESULT=NEED_LOGIN

const fs = await import("node:fs/promises");
const path = await import("node:path");
const { fileURLToPath } = await import("node:url");

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INPUT_PATH = path.join(__dirname, "publish-sph-ego-input.json");
const ASSISTANT_HOME = "https://channels.weixin.qq.com/platform";
const CREATE_URL = "https://channels.weixin.qq.com/platform/post/create";

async function readInput() {
  const raw = await fs.readFile(INPUT_PATH, "utf8");
  return JSON.parse(raw);
}

async function cdpClick(page, x, y) {
  await page.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await page.cdp("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  await page.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
}

// 在 wujie shadow DOM 里定位可见元素，滚进视口后用 CDP 真实点击。
// kind: "button"(按文字精确匹配可见按钮) | "checkbox"(按容器开头文字匹配 .ant-checkbox)
async function clickInShadow(page, kind, text) {
  const findRect = (scroll) =>
    page.evaluate(
      ({ kind, text, scroll }) => {
        const wujie = document.querySelector("wujie-app");
        const sr = wujie && wujie.shadowRoot;
        if (!sr) return null;
        const vis = (b) => !!(b.offsetWidth || b.offsetHeight || b.getClientRects().length);
        let el = null;
        if (kind === "button") {
          for (const b of sr.querySelectorAll("button")) {
            if ((b.innerText || b.textContent || "").trim() === text && vis(b) && !b.disabled && !b.className.includes("disabled")) {
              el = b;
              break;
            }
          }
        } else if (kind === "checkbox") {
          for (const e of sr.querySelectorAll("*")) {
            if (e.textContent.trim().startsWith(text) && e.querySelector(".ant-checkbox")) {
              el = e.querySelector(".ant-checkbox");
              break;
            }
          }
        }
        if (!el) return null;
        if (scroll) el.scrollIntoView({ block: "center", inline: "center" });
        const r = el.getBoundingClientRect();
        return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
      },
      { kind, text, scroll },
    );

  const r1 = await findRect(true);
  if (!r1) return false;
  await page.waitForTimeout(700);
  const r2 = await findRect(false);
  if (!r2) return false;
  await cdpClick(page, r2.x, r2.y);
  return true;
}

async function shadowText(page) {
  return page.evaluate(() => {
    const wujie = document.querySelector("wujie-app");
    const sr = wujie && wujie.shadowRoot;
    return sr ? sr.textContent || "" : "";
  });
}

// 恢复 ego-lite 窗口（最小化时无法扫码/截图）
async function restoreWindow(task) {
  try {
    const tabs = await task.tabs();
    const active = tabs.find((t) => t.active) || tabs[0];
    if (active) {
      const win = await task.cdp("Browser.getWindowForTarget", { targetId: active.targetId });
      if (win && win.windowId) {
        await task.cdp("Browser.setWindowBounds", { windowId: win.windowId, bounds: { windowState: "normal" } });
      }
    }
  } catch (e) {
    /* 非关键 */
  }
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
    if (extract && extract.matched) break;
  }

  const chosen = extract && (extract.matched || extract.top);
  const retro = {
    capturedAt: new Date().toISOString(),
    publishTitle: input.title || "",
    publishDesc: input.desc || "",
    publishTags: input.tags || [],
    matchMethod: extract && extract.matched ? "text-prefix" : "top-row",
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
  const SPACE = "sph-ego-" + Date.now();
  const task = await taskSpace(SPACE);
  const page = task.page("p1");

  try {
    // 1) 打开视频号助手，判断登录态
    await page.goto(ASSISTANT_HOME, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(3000);
    let url = await page.url();

    // 未登录 -> 需要扫码
    if (url.includes("/login") || url.includes("oauth-callback")) {
      console.log("[登录检测] 未登录，需要扫码：请在弹出的浏览器窗口里用微信扫码登录（最多等 5 分钟）");
      await restoreWindow(task);
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
    await page.waitForTimeout(2000);

    // 5) 短标题（上限 16 字，兜底截断）
    const title = (input.title || "").trim().slice(0, 16);
    if (title) {
      await page.fill('input[placeholder="填写短标题有机会获得更多流量"]', title);
      console.log("TITLE_FILLED");
    }

    // 6) 视频描述 + 标签（contenteditable）
    const desc = (input.desc || "").trim();
    const tags = (input.tags || []).map((t) => "#" + String(t).replace(/^#/, "")).join(" ");
    const fullDesc = [desc, tags].filter(Boolean).join(" ").trim();
    if (fullDesc) {
      await page.focus(".input-editor[contenteditable]");
      await page.keyboard.insertText(fullDesc);
      console.log("DESC_FILLED");
    }

    // 7) 声明原创（按配置决定）：勾选表单复选框
    if (input.declareOriginal !== false) {
      const declared = await page.evaluate(() => {
        const wujie = document.querySelector("wujie-app");
        const sr = wujie && wujie.shadowRoot;
        if (!sr) return null;
        for (const el of sr.querySelectorAll("*")) {
          if (el.textContent.includes("声明后，作品将展示原创标记") && el.querySelector(".ant-checkbox-input")) {
            return el.querySelector(".ant-checkbox-input").checked;
          }
        }
        return null;
      });
      if (declared === false) {
        const ok = await clickInShadow(page, "checkbox", "声明后，作品将展示原创标记");
        console.log(ok ? "DECLARE_CHECKED" : "DECLARE_CHECK_FAILED");
        await page.waitForTimeout(1500);
      } else if (declared === true) {
        console.log("DECLARE_ALREADY_CHECKED");
      }

      // 8) 处理“原创权益”弹窗
      const t1 = await shadowText(page);
      if (t1.includes("原创权益")) {
        await clickInShadow(page, "checkbox", "我已阅读并同意");
        await page.waitForTimeout(1200);
        await clickInShadow(page, "button", "声明原创");
        await page.waitForTimeout(2000);
        console.log("DECLARE_CONFIRMED");
      } else {
        console.log("NO_DECLARE_DIALOG");
      }
    } else {
      console.log("SKIP_DECLARE");
    }

    // 9) 测试模式：填好即止，不发表
    if (input.noPublish) {
      console.log("RESULT=NO_PUBLISH");
      return; // 不 finish，留着浏览器给用户检查
    }

    // 10) 发表
    const clicked = await clickInShadow(page, "button", "发表");
    console.log(clicked ? "PUBLISH_CLICKED" : "PUBLISH_BTN_NOT_FOUND");

    // 11) 核实
    await page.waitForTimeout(5000);
    url = await page.url();
    let published = false;
    if (url.includes("/post/list")) {
      published = true;
    } else {
      const t2 = await shadowText(page);
      if (t2.includes("已发表")) {
        published = true;
      }
    }

    if (published) {
      // 12) 数据半环：抓取刚发布作品的标识（非致命，失败不影响发布结果）
      try {
        await captureRetro(page, input);
      } catch (e) {
        console.log("RETRO_CAPTURE_SKIP " + (e && e.message ? e.message : String(e)));
      }
      console.log("RESULT=OK");
    } else {
      console.log("RESULT=UNCONFIRMED url=" + url);
    }
  } catch (e) {
    console.log("RESULT=FAIL " + (e && e.message ? e.message : String(e)));
  } finally {
    // 登录态靠 profile 层 cookie 保留（跨空间共享），不依赖空间本身，
    // 所以正常 finish 关闭本次空间即可，登录态仍然在。
    if (!input.noPublish) {
      try { await task.finish({ keep: [] }); } catch (e) {}
    }
  }
}

await main();
