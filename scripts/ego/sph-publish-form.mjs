const TITLE_INPUT = 'input[placeholder="填写短标题有机会获得更多流量"]';
const FORM_BUTTONS = ".post-edit-wrap .form-btns button";
const ENABLED = ':not([disabled]):not([aria-disabled="true"]):not([class*="disabled"])';
const ORIGINAL_DIALOG = ".declare-original-dialog .weui-desktop-dialog";
const LIST_URL = "https://channels.weixin.qq.com/platform/post/list";

export function shortTitle(title) {
  return Array.from((title || "").replace(/[,，\s]+/gu, " ").trim()).slice(0, 16).join("").trimEnd();
}

export async function fillShortTitle(page, inputTitle) {
  const title = shortTitle(inputTitle);
  if (title) {
    await page.waitForSelector(TITLE_INPUT, { state: "visible", timeout: 900000 });
    await page.fill(TITLE_INPUT, title);
  }
  console.log("FORM_TITLE=" + title);
  return title;
}

export async function declareOriginal(page) {
  const checkbox = ".declare-original-checkbox input[type=checkbox]";
  const state = () => page.evaluate(({ checkbox, dialog }) => {
    const root = document.querySelector("wujie-app")?.shadowRoot;
    return {
      checked: root?.querySelector(checkbox)?.checked === true,
      dialog: !!root?.querySelector(dialog)?.getBoundingClientRect().width,
      agreed: root?.querySelector(".original-proto-wrapper input")?.checked === true,
    };
  }, { checkbox, dialog: ORIGINAL_DIALOG });
  await page.waitForSelector(".declare-original-checkbox label", { state: "visible", timeout: 15000 });
  let current = await state();
  if (!current.checked && !current.dialog) {
    await page.click(".declare-original-checkbox label", { label: "勾选原创声明" });
    // 已签过协议的账号可能直接勾上，不弹出协议。
    await page.waitForSelector(ORIGINAL_DIALOG, { state: "visible", timeout: 4000 }).catch(() => {});
    current = await state();
  }
  if (current.dialog) {
    if (!current.agreed) await page.click(".original-proto-wrapper label", { label: "勾选原创协议" });
    await page.click(ORIGINAL_DIALOG + " button" + ENABLED + ':text-is("声明原创")', {
      timeout: 10000, label: "确认原创声明",
    });
    await page.waitForSelector(ORIGINAL_DIALOG, { state: "hidden", timeout: 10000 });
  }
  current = await state();
  if (!current.checked || current.dialog) throw new Error("原创声明尚未完成");
  console.log("DECLARE_CONFIRMED");
}

async function inspectForm(page, watchClick = false) {
  return page.evaluate(({ selector, watchClick }) => {
    const root = document.querySelector("wujie-app")?.shadowRoot;
    const visible = el => !!el?.getBoundingClientRect().width && getComputedStyle(el).visibility !== "hidden";
    const titleError = root?.querySelector(".post-short-title-wrap .error-title");
    if (visible(titleError)) return { reason: "短标题：" + titleError.innerText.trim(), invalid: true };
    if (visible(root?.querySelector(".declare-original-dialog .weui-desktop-dialog"))) {
      return { reason: "原创权益弹窗仍然打开" };
    }
    const buttons = [...(root?.querySelectorAll(selector) || [])].filter(b => visible(b) && b.innerText.trim() === "发表");
    if (!buttons.length) return { reason: "表单操作区未出现「发表」按钮" };
    if (buttons.length !== 1) return { reason: "表单操作区有多个可见的「发表」按钮" };
    const [button] = buttons;
    if (button.disabled || button.getAttribute("aria-disabled") === "true" || button.className.includes("disabled")) {
      return { reason: "「发表」按钮仍为灰色，页面未显示短标题校验错误" };
    }
    if (watchClick) {
      // CDP 派发鼠标动作不等于按钮收到了 click。用捕获阶段记录实际命中的控件。
      window.__sphPublishClicked = false;
      root.addEventListener("click", function observe(event) {
        if (!event.isTrusted || !event.composedPath().some(el => el.matches?.(selector) && el.textContent.trim() === "发表")) return;
        window.__sphPublishClicked = true;
        root.removeEventListener("click", observe, true);
      }, true);
    }
    return { ready: true };
  }, { selector: FORM_BUTTONS, watchClick });
}

function readReceipt() {
  const roots = [document, document.querySelector("wujie-app")?.shadowRoot].filter(Boolean);
  const notice = roots.some(root =>
    [...root.querySelectorAll('.weui-desktop-toast,.ant-message-notice,[role="status"],[role="alert"]')].some(el =>
      el.getBoundingClientRect().width && getComputedStyle(el).visibility !== "hidden" &&
      /^(已发表|发表成功|发布成功)[！!。]?$/.test(el.innerText?.trim())
    )
  );
  return { clicked: window.__sphPublishClicked, notice, url: location.href };
}

export async function publishFromForm(page, noPublish, timeout = 60000, beforeSubmit) {
  const deadline = Date.now() + timeout;
  while (true) {
    const state = await inspectForm(page, !noPublish);
    if (state.ready) break;
    if (state.invalid || Date.now() >= deadline) return "RESULT=NOT_CLICKED " + state.reason;
    await page.waitForTimeout(500);
  }
  console.log("PUBLISH_BUTTON_READY");
  if (noPublish) return "RESULT=NO_PUBLISH";

  const before = await page.evaluate(readReceipt);
  if (beforeSubmit) {
    const prior = await beforeSubmit();
    if (prior) return prior;
  }
  let dispatchError = "";
  try {
    await page.click(FORM_BUTTONS + ENABLED + ':text-is("发表")', { timeout: 8000, label: "点击发表" });
  } catch (error) {
    dispatchError = String(error.message || error).split("\n")[0];
  }
  // 一次运行最多点击一次。即使浏览器在点击时跳转或断开，也不能补点。
  let clicked = false;
  let url = "";
  const verifyUntil = Date.now() + timeout;
  do {
    try {
      const state = await page.evaluate(readReceipt);
      url = state.url;
      const list = url.split(/[?#]/)[0].replace(/\/$/, "") === LIST_URL;
      if (!clicked && state.clicked === false) {
        return "RESULT=NOT_CLICKED 发表按钮未收到点击" + (dispatchError ? "：" + dispatchError : "");
      }
      // 完整导航会替换 window。此时必须同时取得 ego 的点击回执和平台列表跳转。
      if (!clicked && (state.clicked === true || (list && !dispatchError && state.clicked == null))) {
        clicked = true;
        console.log("PUBLISH_CLICKED");
      }
      if (clicked && (list || (state.notice && !before.notice))) return "RESULT=OK";
    } catch { /* 路由切换时旧页面可能已卸载，等新页面给出结果。 */ }
    if (Date.now() >= verifyUntil) break;
    try { await page.waitForTimeout(500); } catch { break; }
  } while (true);
  if (clicked) return "RESULT=UNCONFIRMED url=" + url;
  return "RESULT=CLICK_UNKNOWN 浏览器未返回点击记录" + (dispatchError ? "：" + dispatchError : "");
}
