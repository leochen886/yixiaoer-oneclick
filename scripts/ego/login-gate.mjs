// 视频号登录门禁：CMD 一打开就先检测登录态，未登录则弹码等扫码
// 输出 RESULT=LOGIN_OK（已登录 / 扫码成功）或 RESULT=FAIL（超时）
import { ensureLogin } from "./ensure-login.mjs";

async function main() {
  const SPACE = "sph-login-" + Date.now();
  const task = await taskSpace(SPACE);
  const page = task.page("p1");
  try {
    await ensureLogin(task, page);
    console.log("RESULT=LOGIN_OK");
  } catch (error) {
    const msg = error && error.message ? error.message : String(error);
    if (msg.includes("登录超时")) {
      console.log("RESULT=FAIL " + msg);
    } else {
      console.log("RESULT=FAIL 无法核实登录状态：" + msg);
    }
  } finally {
    try { await task.finish({ keep: [] }); } catch (e) {}
  }
}

await main();
