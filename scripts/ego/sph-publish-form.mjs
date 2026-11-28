const TITLE_INPUT = 'input[placeholder="填写短标题有机会获得更多流量"]';
const FORM_BUTTONS = ".post-edit-wrap .form-btns button";
const ENABLED = ':not([disabled]):not([aria-disabled="true"]):not([class*="disabled"])';
const ORIGINAL_DIALOG = ".declare-original-dialog .weui-desktop-dialog";
const LIST_URL = "https://channels.weixin.qq.com/platform/post/list";

export function shortTitle(title) {
  return Array.from((title || "").replace(/[,，\s]+/gu, " ").trim()).slice(0, 16).join("").trimEnd();
}

// Runs in the page. A laid-out Wujie field may still be outside its scroll container.
function inspectShortTitle({ selector, reveal = false }) {
  const roots = [document, ...Array.from(document.querySelectorAll("wujie-app"), host => host.shadowRoot).filter(Boolean)];
  const inputs = roots.flatMap(root => Array.from(root.querySelectorAll(selector))).filter(input => {
    const rect = input.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && getComputedStyle(input).visibility === "visible";
  });
  if (inputs.length !== 1) throw new Error("无法唯一定位短标题输入框，找到 " + inputs.length + " 个");
  const [input] = inputs;
  if (reveal) input.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
  return input.value;
}

export async function fillShortTitle(page, inputTitle) {
  const title = shortTitle(inputTitle);
  if (title) {
    await page.waitForSelector(TITLE_INPUT, { state: "visible", timeout: 900000 });
    try {
      await page.fill(TITLE_INPUT, title);
    } catch (error) {
      if (!/not visible in the viewport/i.test(error.message)) throw error;
      const value = await page.evaluate(inspectShortTitle, { selector: TITLE_INPUT, reveal: true });
      if (value !== title) await page.fill(TITLE_INPUT, title, { timeout: 8000 });
    }
    if (await page.evaluate(inspectShortTitle, { selector: TITLE_INPUT }) !== title) {
      throw new Error("短标题回读与预期不符，请核对编辑页");
    }
  }
  console.log("FORM_TITLE=" + title);
  return title;
}

// This function is serialized into the page by ego-browser.
function readOriginalState(dialogSelector) {
  const roots = [document, ...Array.from(document.querySelectorAll("wujie-app"), app => app.shadowRoot).filter(Boolean)];
  const visible = el => el.getBoundingClientRect().width > 0 && getComputedStyle(el).visibility !== "hidden";
  function visibleOne(selector) {
    const matches = roots.flatMap(root => Array.from(root.querySelectorAll(selector))).filter(visible);
    if (matches.length > 1) throw new Error("找到多个可见的原创声明控件，请在编辑页核对");
    return matches[0];
  }
  function checkbox(scope, selector) {
    if (!scope) return { checked: false, target: null };
    const native = scope.querySelector('input[type="checkbox"]');
    const role = '[role="checkbox"]';
    const widget = scope.matches(role) ? scope : scope.querySelector(role);
    const aria = widget?.getAttribute("aria-checked");
    const marker = ".ant-checkbox-checked,.ant-checkbox-wrapper-checked";
    let checked = scope.matches(marker) || !!scope.querySelector(marker);
    if (aria != null) checked = aria === "true";
    if (native) checked = native.checked;
    let target = null;
    if (scope.querySelector("label")) target = selector + " label";
    else if (widget) target = widget === scope ? selector : selector + " " + role;
    else if (native) target = selector + ' input[type="checkbox"]';
    return { checked, target };
  }
  const fieldSelector = ".declare-original-checkbox";
  const field = checkbox(visibleOne(fieldSelector), fieldSelector);
  const dialog = visibleOne(dialogSelector);
  const agreement = checkbox(dialog?.querySelector(".original-proto-wrapper"), dialogSelector + " .original-proto-wrapper");
  return { checked: field.checked, target: field.target, dialog: !!dialog, agreed: agreement.checked, agreementTarget: agreement.target };
}

export async function declareOriginal(page, timeout = 15000) {
  async function waitForState(accept, reason) {
    const deadline = Date.now() + timeout;
    while (true) {
      const state = await page.evaluate(readOriginalState, ORIGINAL_DIALOG);
      if (accept(state)) return state;
      if (Date.now() >= deadline) throw new Error("原创声明尚未完成：" + reason);
      await page.waitForTimeout(250);
    }
  }
  const confirmed = state => state.checked && !state.dialog;
  let state = await waitForState(s => s.dialog || s.checked || s.target, "未找到可操作的声明控件");
  if (!state.checked && !state.dialog) {
    await page.click(state.target, { label: "勾选原创声明" });
    // Some accounts first show a provisional check and then open the agreement dialog.
    await page.waitForSelector(ORIGINAL_DIALOG, { state: "visible", timeout: Math.min(4000, timeout) }).catch(() => {});
    state = await waitForState(s => s.dialog || s.checked, "勾选后未看到协议弹窗或选中标记");
  }
  if (state.dialog) {
    state = await waitForState(s => confirmed(s) || (s.dialog && (s.agreed || s.agreementTarget)), "未找到原创协议勾选项");
    if (state.dialog) {
      if (!state.agreed) {
        await page.click(state.agreementTarget, { label: "勾选原创协议" });
        await waitForState(s => s.agreed && s.dialog, "协议勾选状态未确认");
      }
      await page.click(ORIGINAL_DIALOG + " button" + ENABLED + ':text-is("声明原创")', {
        timeout, label: "确认原创声明",
      });
    }
  }
  await waitForState(confirmed, "未看到声明已勾选且协议弹窗已关闭");
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
