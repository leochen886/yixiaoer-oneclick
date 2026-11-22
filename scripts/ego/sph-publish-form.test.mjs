import assert from "node:assert/strict";
import test from "node:test";
import { publishFromForm, shortTitle } from "./sph-publish-form.mjs";

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
