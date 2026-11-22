import assert from "node:assert/strict";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { declareOriginal, publishFromForm, shortTitle } from "./sph-publish-form.mjs";

const create = "https://channels.weixin.qq.com/platform/post/create";
const list = "https://channels.weixin.qq.com/platform/post/list";

// 模拟 ego 的页面回执。真实视频号只验证到预演，提交分支在本地运行。
function form({ readiness = { ready: true }, before = {}, after = {}, clickError } = {}) {
  return {
    clicks: 0,
    receipts: 0,
    async evaluate(fn, args) {
      if (args?.selector) return readiness;
      assert.equal(fn.name, "readReceipt");
      this.receipts++;
      return { url: create, clicked: false, notice: false, ...(this.clicks ? after : before) };
    },
    async click(selector) {
      assert.match(selector, /\.form-btns button.*:text-is\("发表"\)$/);
      this.clicks++;
      if (clickError) throw new Error(clickError);
    },
  };
}

const unchecked = { checked: false, target: ".declare-original-checkbox label", dialog: false, agreed: false, agreementTarget: null };
const checked = { ...unchecked, checked: true };
const inDialog = { ...unchecked, dialog: true, agreementTarget: ".declare-original-dialog .weui-desktop-dialog .original-proto-wrapper label" };

function originalPage(states) {
  return {
    clicks: [],
    async evaluate() {
      return states.length > 1 ? states.shift() : states[0];
    },
    async waitForTimeout() {},
    async waitForSelector() { throw new Error("dialog has not appeared yet"); },
    async click(selector) { this.clicks.push(selector); },
  };
}

test("协议弹窗可以延迟出现，确认后等待表单回写，只勾选和确认各一次", async () => {
  const page = originalPage([unchecked, unchecked, inDialog, inDialog, { ...inDialog, agreed: true }, unchecked, checked]);
  await declareOriginal(page);
  assert.deepEqual(page.clicks.slice(0, 2), [unchecked.target, inDialog.agreementTarget]);
  assert.equal(page.clicks.length, 3);
  assert.match(page.clicks[2], /:text-is\("声明原创"\)$/);
});

test("无需协议弹窗的账号只点一次，已经确认的账号直接通过", async () => {
  const page = originalPage([unchecked, unchecked, checked]);
  await declareOriginal(page);
  assert.deepEqual(page.clicks, [unchecked.target]);
  const existing = originalPage([checked]);
  await declareOriginal(existing);
  assert.deepEqual(existing.clicks, []);
});

test("始终没有选中标记时给出原因并停在编辑页", async () => {
  const page = originalPage([unchecked]);
  await assert.rejects(declareOriginal(page, 0), /勾选后未看到协议弹窗或选中标记/);
  assert.deepEqual(page.clicks, [unchecked.target]);
});

test("弹窗关闭但声明未生效时不能报告成功", async () => {
  const agreed = { ...inDialog, agreed: true };
  const page = originalPage([agreed, agreed, unchecked]);
  await assert.rejects(declareOriginal(page, 0), /未看到声明已勾选/);
  assert.equal(page.clicks.length, 1);
  assert.match(page.clicks[0], /:text-is\("声明原创"\)$/);
});

test("从可见声明项读取 aria-checked，忽略隐藏表单中未选中的副本", async () => {
  const checkbox = visible => ({
    getBoundingClientRect: () => ({ width: visible ? 50 : 0 }),
    matches: selector => selector === '[role="checkbox"]',
    getAttribute: name => name === "aria-checked" ? String(visible) : null,
    querySelector: () => null,
  });
  const fields = [checkbox(false), checkbox(true)];
  const root = { querySelectorAll: selector => selector === ".declare-original-checkbox" ? fields : [] };
  const document = { querySelectorAll: selector => selector === "wujie-app" ? [{ shadowRoot: root }] : [] };
  const page = originalPage([]);
  page.evaluate = (fn, arg) => runInNewContext("(" + fn.toString() + ")(arg)", {
    document, arg, getComputedStyle: () => ({ visibility: "visible" }),
  });
  await declareOriginal(page, 0);
  assert.deepEqual(page.clicks, []);
});

test("短标题中英文逗号变成空格，保留支持的问号和书名号", () => {
  assert.equal(shortTitle(" 今天出门，记得带伞 "), "今天出门 记得带伞");
  assert.equal(shortTitle("《AI》,  怎么用？"), "《AI》 怎么用？");
  assert.equal(shortTitle(""), "");
});

for (const reason of ["短标题含特殊字符", "按钮仍为灰色", "找不到按钮"]) {
  test(reason + "时不点击也不核实发布", async () => {
    const page = form({ readiness: { reason } });
    assert.equal(await publishFromForm(page, false, 0), "RESULT=NOT_CLICKED " + reason);
    assert.equal(page.clicks, 0);
    assert.equal(page.receipts, 0);
  });
}

test("预演在可点击状态直接结束", async () => {
  const page = form();
  assert.equal(await publishFromForm(page, true, 0), "RESULT=NO_PUBLISH");
  assert.equal(page.clicks, 0);
  assert.equal(page.receipts, 0);
});

for (const [label, options, expected] of [
  ["按钮没收到点击", { after: { clicked: false } }, "NOT_CLICKED"],
  ["确实点击后仍在编辑页", { after: { clicked: true } }, "UNCONFIRMED"],
  ["点击后出现新的成功回执", { after: { clicked: true, notice: true } }, "OK"],
  ["旧的成功回执不算", { before: { notice: true }, after: { clicked: true, notice: true } }, "UNCONFIRMED"],
  ["点击后进入作品列表", { after: { clicked: true, url: list } }, "OK"],
  ["失去鼠标回执但捕捉到真实点击", { clickError: "通道断开", after: { clicked: true } }, "UNCONFIRMED"],
  ["浏览器故障并丢失页面", { clickError: "通道断开", after: { clicked: undefined } }, "CLICK_UNKNOWN"],
  ["没有点击就跳转不算成功", { after: { clicked: false, url: list } }, "NOT_CLICKED"],
]) {
  test(label + "，最多点一次", async () => {
    const page = form(options);
    const result = await publishFromForm(page, false, 0);
    assert.equal(result.split(" ")[0], "RESULT=" + expected);
    assert.equal(page.clicks, 1);
  });
}
