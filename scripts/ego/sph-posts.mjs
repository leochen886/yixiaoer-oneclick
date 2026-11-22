const clean = value => String(value || "").replace(/\s+/g, " ").trim();

export function metricDelta(before, after) {
  function parse(value) {
    const m = /^(\d+(?:\.\d+)?)(万|亿)?$/.exec(String(value ?? "").replace(/[,，\s]/g, ""));
    return m && { value: Number(m[1]) * ({ 万: 1e4, 亿: 1e8 }[m[2]] || 1), approximate: !!m[2] };
  }
  const a = parse(before), b = parse(after);
  if (!a || !b) return "—";
  const delta = Math.round(b.value - a.value);
  return (a.approximate || b.approximate ? "约" : "") + (delta >= 0 ? "+" : "") + delta;
}

export function matchesPost(row, target) {
  if (target.postId && row.id) return row.id === target.postId;
  if (target.postId && !target.publishDate) return false;
  const title = clean(row.title);
  const desc = clean(target.publishDesc);
  const matchesText = (desc && (title === desc || title.startsWith(desc + " #"))) ||
    (title === clean(target.publishTitle) && !!title);
  if (!matchesText) return false;
  if (target.publishDate) return clean(row.time) === clean(target.publishDate);
  if (target.capturedAt) {
    const m = /^(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})/.exec(row.time);
    if (!m) return false;
    const [, year, month, day, hour, minute] = m.map(Number);
    const published = Date.UTC(year, month - 1, day, hour - 8, minute);
    const age = Date.parse(target.capturedAt) - published;
    return age >= -60000 && age < 180000;
  }
  return true;
}

export function postKey(row) {
  return row.id ? "id:" + row.id : JSON.stringify([clean(row.title), row.time]);
}

export function readPostRows() {
  const root = document.querySelector("wujie-app")?.shadowRoot;
  return Array.from(root?.querySelectorAll(".post-feed-item") || [], row => ({
    id: row.getAttribute("data-object-id") || row.getAttribute("data-feed-id") || "",
    title: row.querySelector(".post-title")?.textContent || "",
    time: (row.querySelector(".time-label")?.textContent || "").trim(),
    metrics: Array.from(row.querySelectorAll(".post-data .data-item"), cell => ({
      num: (cell.textContent || "").replace(/\s+/g, ""),
      iconCls: cell.querySelector("[class*=icon]")?.getAttribute("class") || "",
      useHref: cell.querySelector("use")?.getAttribute("href") || "",
    })),
  }));
}

function advanceList() {
  const root = document.querySelector("wujie-app")?.shadowRoot;
  if (!root) return "waiting";
  const next = root.querySelector('[aria-label="下一页"],[title="下一页"],.weui-desktop-pagination__next,.ant-pagination-next');
  if (next) {
    if (next.matches('[disabled],[aria-disabled="true"],[class*="disabled"]') || next.querySelector("button:disabled")) return "end";
    (next.querySelector("button,a") || next).click();
    return "page";
  }
  const last = Array.from(root.querySelectorAll(".post-feed-item")).pop();
  if (!last) return "waiting";
  last.scrollIntoView({ block: "end" });
  return "scroll";
}

export async function findPost(page, target, { latest = false, timeout = 60000, exclude = new Set() } = {}) {
  const deadline = Date.now() + timeout;
  let signature, movedAt = 0, mode = "waiting";
  let candidate = null;
  let previousRows = new Set();
  while (Date.now() < deadline) {
    const rows = await page.evaluate(readPostRows);
    const keys = rows.map(r => JSON.stringify([r.id, clean(r.title), r.time]));
    const key = JSON.stringify(keys);
    if (rows.length && key !== signature) {
      const matches = rows.filter(r => !exclude.has(postKey(r)) && matchesPost(r, target));
      if (matches.length > 1) throw new Error("多条作品符合留档信息，请核对作品标识");
      if (matches.length) {
        const hit = matches[0];
        const hitKey = keys[rows.indexOf(hit)];
        if (latest && !hit.metrics.some(m => m.num !== "")) {
          signature = undefined;
          await page.waitForTimeout(500);
          continue;
        }
        if ((target.postId && hit.id === target.postId) || latest) return hit;
        if (candidate && !(mode === "scroll" && previousRows.has(hitKey))) {
          throw new Error("跨页发现多个匹配作品，请核对作品标识");
        }
        candidate = hit;
      }
      signature = key;
      previousRows = new Set(keys);
      if (!latest) {
        mode = await page.evaluate(advanceList);
        movedAt = Date.now();
        if (mode === "end") return candidate;
      }
    } else if (!latest && mode === "scroll" && Date.now() - movedAt >= 6000) {
      return candidate;
    }
    await page.waitForTimeout(500);
  }
  if (mode === "page") throw new Error("等待作品列表翻页超时，请重试复盘");
  return candidate;
}

export async function verifyAssistant(page) {
  await page.waitForFunction(() => {
    const url = new URL(location.href);
    if (url.origin !== "https://channels.weixin.qq.com" || !/^\/platform(?:\/|$)/.test(url.pathname) ||
        /login|oauth-callback/.test(url.href)) return false;
    const shadow = document.querySelector("wujie-app")?.shadowRoot;
    return /作品管理|内容管理|发表动态|发表视频|数据中心|发布视频|首页.*动态管理/.test(
      (document.body?.innerText || "") + (shadow?.textContent || "")) ||
      !!shadow?.querySelector(".post-feed-item,.post-edit-wrap");
  }, undefined, { timeout: 20000 });
}
