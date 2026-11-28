// 视频号助手登录门禁与窗口拉起共用模块
import { verifyAssistant } from "./sph-posts.mjs";

export const ASSISTANT_HOME = "https://channels.weixin.qq.com/platform";

export async function showWindow(task, page) {
  try {
    let targetId = page?.targetId;
    if (!targetId && task?.tabs) {
      const tabs = await task.tabs();
      const active = tabs.find((t) => t.active) || tabs[0];
      targetId = active?.targetId;
    }
    if (targetId) {
      const win = await task.cdp("Browser.getWindowForTarget", { targetId });
      if (win?.windowId && win.bounds?.windowState === "minimized") {
        await task.cdp("Browser.setWindowBounds", { windowId: win.windowId, bounds: { windowState: "normal" } });
      }
    }
  } catch (e) {
    console.log("EGO_WINDOW " + (e?.message || String(e)));
  }
  await page?.cdp?.("Page.bringToFront")?.catch((e) => console.log("EGO_FOREGROUND " + (e?.message || String(e))));
}

export async function ensureLogin(task, page, { waitForScanMs = 300000 } = {}) {
  await page.goto(ASSISTANT_HOME, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(3000);
  let url = await page.url();

  if (url.includes("/login") || url.includes("oauth-callback")) {
    console.log("[登录检测] 未登录，需要扫码：请在弹出的浏览器窗口里用微信扫码登录（最多等 5 分钟）");
    await showWindow(task, page);
    const deadline = Date.now() + waitForScanMs;
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
      throw new Error("登录超时，未完成扫码");
    }
    console.log("[登录检测] 扫码成功，已登录");
  }

  await verifyAssistant(page);
  return url;
}
