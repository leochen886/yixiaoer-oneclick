import * as fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";

const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();

export const METRIC_LABELS = ["播放", "点赞", "评论", "转发", "收藏"];
const COMMENT_URL = "https://channels.weixin.qq.com/platform/interaction/comment";

export function parseMetricNumber(value) {
  const raw = String(value ?? "").replace(/[,，\s]/g, "");
  const m = /^(\d+(?:\.\d+)?)(万|亿)?$/.exec(raw);
  if (!m) return null;
  const multiplier = { 万: 1e4, 亿: 1e8 }[m[2]] || 1;
  return { value: Number(m[1]) * multiplier, approximate: !!m[2] };
}

export function metricDelta(before, after) {
  const a = parseMetricNumber(before);
  const b = parseMetricNumber(after);
  if (!a || !b) return "—";
  const delta = Math.round(b.value - a.value);
  return (a.approximate || b.approximate ? "约" : "") + (delta >= 0 ? "+" : "") + delta;
}

export function computeEngagement(metrics) {
  const list = Array.isArray(metrics) ? metrics : [];
  const nums = METRIC_LABELS.map((_, i) => {
    const item = list[i];
    const parsed = parseMetricNumber(item && typeof item === "object" ? item.num : item);
    return parsed ? Math.round(parsed.value) : null;
  });
  const [plays, likes, comments, shares, saves] = nums;
  const parts = [likes, comments, shares, saves].filter((v) => v != null);
  const interactions = parts.length ? parts.reduce((acc, v) => acc + v, 0) : null;
  const rate = (n) => (plays != null && plays > 0 && n != null ? Number((n / plays).toFixed(4)) : null);
  return {
    plays,
    likes,
    comments,
    shares,
    saves,
    interactions,
    interactionRate: rate(interactions),
    likeRate: rate(likes),
    commentRate: rate(comments),
    shareRate: rate(shares),
    saveRate: rate(saves),
  };
}

export function computeAgeAndStage(capturedAt, collectedAt = new Date()) {
  const start = capturedAt ? Date.parse(capturedAt) : NaN;
  if (!Number.isFinite(start)) return { ageHours: null, stage: "未知" };
  const ageHours = Number(Math.max(0, (collectedAt.getTime() - start) / 3600000).toFixed(2));
  let stage = "<24h";
  if (ageHours >= 168) stage = "7天+";
  else if (ageHours >= 72) stage = "3天";
  else if (ageHours >= 24) stage = "1天";
  return { ageHours, stage };
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

export function advanceList() {
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

export async function collectAllMatchingPosts(page, targets, { timeout = 90000, maxPages = 20, exclude = new Set() } = {}) {
  const targetList = Array.isArray(targets) ? targets : [];
  const matched = new Map();
  const errors = new Map();
  if (!targetList.length) return { matched, errors };

  const resolved = new Set();
  const deadline = Date.now() + timeout;
  let signature;
  let movedAt = 0;
  let mode = "waiting";
  let previousRows = new Set();
  let pagesVisited = 0;

  while (Date.now() < deadline) {
    const rows = await page.evaluate(readPostRows);
    const keys = rows.map((r) => JSON.stringify([r.id, clean(r.title), r.time]));
    const key = JSON.stringify(keys);
    if (rows.length && key !== signature) {
      for (const { key: tKey, target } of targetList) {
        if (errors.has(tKey) || resolved.has(tKey)) continue;
        const matches = rows.filter((r) => !exclude.has(postKey(r)) && matchesPost(r, target));
        if (matches.length > 1) {
          errors.set(tKey, "多条作品符合留档信息，请核对作品标识");
          matched.delete(tKey);
          continue;
        }
        if (matches.length === 1) {
          const hit = matches[0];
          const hitKey = keys[rows.indexOf(hit)];
          if (target.postId && hit.id === target.postId) {
            matched.set(tKey, hit);
            resolved.add(tKey);
          } else if (matched.has(tKey)) {
            if (!(mode === "scroll" && previousRows.has(hitKey))) {
              errors.set(tKey, "跨页发现多个匹配作品，请核对作品标识");
              matched.delete(tKey);
            } else {
              matched.set(tKey, hit);
            }
          } else {
            matched.set(tKey, hit);
          }
        }
      }
      signature = key;
      previousRows = new Set(keys);
      pagesVisited += 1;
      if (resolved.size + errors.size === targetList.length || pagesVisited >= maxPages) {
        return { matched, errors };
      }
      mode = await page.evaluate(advanceList);
      movedAt = Date.now();
      if (mode === "end") return { matched, errors };
    } else if (mode === "scroll" && Date.now() - movedAt >= 6000) {
      return { matched, errors };
    }
    await page.waitForTimeout(500);
  }
  return { matched, errors };
}

export async function openCommentsPage(page) {
  await page.goto(COMMENT_URL, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
  await page
    .waitForFunction(
      () => {
        const wujie = document.querySelector("wujie-app");
        const sr = wujie && wujie.shadowRoot;
        const t = sr ? sr.textContent || "" : "";
        return t.includes("全部视频");
      },
      undefined,
      { timeout: 30000 },
    )
    .catch(() => {});
  await page.waitForTimeout(1500);
}

export function locateCommentItem({ id, title, time }) {
  const root = document.querySelector("wujie-app")?.shadowRoot;
  if (!root) return { status: "no_shadow", count: 0 };
  const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const items = Array.from(root.querySelectorAll(".comment-feed-wrap"));
  const tClean = normalize(title);
  const timeClean = normalize(time);
  let matches = items.filter((el) => {
    const rowId = el.getAttribute("data-object-id") || el.getAttribute("data-feed-id");
    if (rowId && id) return rowId === id;
    const text = normalize(el.textContent);
    return !!tClean && text.includes(tClean) && !!timeClean && text.includes(timeClean);
  });
  if (!matches.length && tClean && timeClean) {
    const prefix = tClean.slice(0, 16);
    matches = items.filter((el) => {
      const text = normalize(el.textContent);
      return text.includes(prefix) && text.includes(timeClean);
    });
  }
  if (matches.length > 1) return { status: "ambiguous", count: items.length };
  if (matches.length === 1) {
    const el = matches[0];
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect();
    return { status: "found", x: r.x + r.width / 2, y: r.y + r.height / 2, count: items.length };
  }
  const last = items[items.length - 1];
  if (last) last.scrollIntoView({ block: "end" });
  return { status: "not_found", count: items.length };
}

export function readCommentRows() {
  const wujie = document.querySelector("wujie-app");
  const sr = wujie && wujie.shadowRoot;
  if (!sr) return { __noShadow: true, list: [] };
  const authors = [...sr.querySelectorAll(".comment-user-name")].map((e) => (e.textContent || "").trim());
  const times = [...sr.querySelectorAll(".comment-time")].map((e) => (e.textContent || "").trim());
  const texts = [...sr.querySelectorAll(".comment-row__main")].map((e) => (e.textContent || "").replace(/\s+/g, " ").trim());
  const list = [];
  const n = Math.min(authors.length, times.length, texts.length, 20);
  for (let i = 0; i < n; i++) {
    if (texts[i]) list.push({ author: authors[i] || "", time: times[i] || "", text: texts[i] });
  }
  return { __noShadow: false, list };
}

export async function selectAndReadComments(page, post, { maxScrolls = 15 } = {}) {
  let lastCount = -1;
  let stagnant = 0;
  let target = null;

  for (let step = 0; step <= maxScrolls; step++) {
    const probe = await page.evaluate(locateCommentItem, {
      id: post.id || "",
      title: post.title || "",
      time: post.time || "",
    });
    if (!probe || probe.status === "no_shadow" || probe.status === "ambiguous") return null;
    if (probe.status === "found") {
      await page.waitForTimeout(300);
      const settled = await page.evaluate(locateCommentItem, {
        id: post.id || "",
        title: post.title || "",
        time: post.time || "",
      });
      target = settled && settled.status === "found" ? settled : probe;
      break;
    }
    if (probe.count > lastCount) {
      lastCount = probe.count;
      stagnant = 0;
    } else {
      stagnant += 1;
      if (stagnant >= 2) return null;
    }
    if (step === maxScrolls) return null;
    await page.waitForTimeout(1200);
  }

  if (!target) return null;
  await page.cdp("Input.dispatchMouseEvent", {
    type: "mousePressed",
    x: target.x,
    y: target.y,
    button: "left",
    clickCount: 1,
  });
  await page.cdp("Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x: target.x,
    y: target.y,
    button: "left",
    clickCount: 1,
  });
  await page.waitForTimeout(3500);

  const res = await page.evaluate(readCommentRows);
  return res && !res.__noShadow ? res.list : null;
}

export function renderRunReport({ input, post, comments, extraMetrics = null, collectedAt = new Date() }) {
  const display = (n) => (n == null || n === "" ? "—" : String(n));
  const fmtRate = (r) => (r == null ? "—" : (r * 100).toFixed(2) + "%");
  const atPublish = input.metricsAtPublish || [];
  const current = METRIC_LABELS.map((label, i) => ({ label, value: display(post.metrics?.[i]?.num) }));
  const engagement = computeEngagement(post.metrics);

  const lines = [];
  lines.push("# 视频号复盘报告");
  lines.push("");
  lines.push("- 作品：`" + (input.publishTitle || (post.title || "").slice(0, 40)) + "`");
  if (input.hookTitle) {
    lines.push("- 顶部黄字：`" + input.hookTitle + "`");
  }
  lines.push("- 发布日期：" + (post.time || input.publishDate || "未知"));
  lines.push("- 复盘时间：" + collectedAt.toLocaleString("zh-CN", { hour12: false }));
  lines.push("- 平台：视频号");
  lines.push("");
  lines.push("## 数据");
  lines.push("");
  lines.push("| 指标 | 发布时 | 复盘时 | 变化 |");
  lines.push("|---|---|---|---|");
  for (let i = 0; i < current.length; i++) {
    const pub = display(atPublish[i]?.num);
    const cur = current[i].value;
    const d = metricDelta(pub, cur);
    lines.push("| " + current[i].label + " | " + pub + " | " + cur + " | " + d + " |");
  }
  lines.push("");
  lines.push("## 互动表现");
  lines.push("");
  lines.push("| 指标 | 数值 |");
  lines.push("|---|---|");
  lines.push("| 总互动（赞+评+转+藏） | " + display(engagement.interactions) + " |");
  lines.push("| 总互动率 | " + fmtRate(engagement.interactionRate) + " |");
  lines.push("| 点赞率 | " + fmtRate(engagement.likeRate) + " |");
  lines.push("| 评论率 | " + fmtRate(engagement.commentRate) + " |");
  lines.push("| 转发率 | " + fmtRate(engagement.shareRate) + " |");
  lines.push("| 收藏率 | " + fmtRate(engagement.saveRate) + " |");
  lines.push("");

  if (extraMetrics && (extraMetrics.completionRate != null || extraMetrics.avgWatchDuration != null)) {
    lines.push("## 深度数据");
    lines.push("");
    lines.push("| 指标 | 数值 |");
    lines.push("|---|---|");
    if (extraMetrics.completionRate != null) {
      lines.push("| 完播率 | " + extraMetrics.completionRate + " |");
    }
    if (extraMetrics.avgWatchDuration != null) {
      lines.push("| 平均播放时长 | " + extraMetrics.avgWatchDuration + " |");
    }
    lines.push("");
  }

  if (input.transcript) {
    lines.push("## 口播文案");
    lines.push("");
    lines.push(input.transcript);
    lines.push("");
  }

  if (comments !== null && comments !== undefined) {
    lines.push("## 评论（Top " + comments.length + "）");
    lines.push("");
    if (comments.length === 0) {
      lines.push("（暂无评论）");
    } else {
      comments.forEach((c, i) => {
        lines.push((i + 1) + ". **" + (c.author || "匿名") + "** · " + (c.time || "") + "：" + c.text);
      });
    }
    lines.push("");
  } else {
    lines.push("## 评论", "", "本次未能核实该作品的评论。", "");
  }

  if (input.publishDesc) {
    lines.push("## 原文案");
    lines.push("");
    lines.push(input.publishDesc);
    lines.push("");
  }

  return lines.join("\n");
}

export async function appendRetroSnapshot(runDir, snapshot) {
  const file = path.join(runDir, "retro-snapshots.json");
  let store = { version: 1, latest: null, snapshots: [] };
  try {
    const existing = JSON.parse(await fs.readFile(file, "utf8"));
    if (existing && Array.isArray(existing.snapshots)) {
      store = { version: 1, latest: existing.latest || null, snapshots: existing.snapshots };
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  store.snapshots.push(snapshot);
  store.latest = snapshot;
  const temp = file + "." + randomUUID() + ".tmp";
  await fs.writeFile(temp, JSON.stringify(store, null, 2), "utf8");
  await fs.rename(temp, file);
  return store;
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
