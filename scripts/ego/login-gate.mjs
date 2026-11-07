// 视频号登录门禁：CMD 一打开就先检测登录态，未登录则弹码等扫码
// 输出 RESULT=LOGIN_OK（已登录 / 扫码成功）或 RESULT=FAIL（超时）
const ASSISTANT_HOME = "https://channels.weixin.qq.com/platform";

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

async function main() {
  const SPACE = "sph-login-" + Date.now();
  const task = await taskSpace(SPACE);
  const page = task.page("p1");
  try {
    await page.goto(ASSISTANT_HOME, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(3000);
    let url = await page.url();

    if (url.includes("/login") || url.includes("oauth-callback")) {
      console.log("[登录检测] 未登录，需要扫码：请在弹出的浏览器窗口里用微信扫码登录（最多等 5 分钟）");
      await restoreWindow(task);
      const deadline = Date.now() + 300000;
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
      console.log("[登录检测] 已登录，无需扫码");
    }
    console.log("RESULT=LOGIN_OK");
  } finally {
    try { await task.finish({ keep: [] }); } catch (e) {}
  }
}

await main();
