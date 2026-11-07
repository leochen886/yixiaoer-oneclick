// 视频号复盘采集（ego 版）—— 数据半环第 2 块
// 由 scripts\review-sph.ps1 调用，不要手动运行。
// 输入：本脚本同目录 collect-sph-post-input.json（由 review-sph.ps1 写入，见 INPUT_PATH）
//   { runDir, publishDesc, publishTitle, publishDate, metricsAtPublish }
// 输出：<runDir>/report.md；最后一行打印 RESULT=OK / RESULT=FAIL

const fs = await import("node:fs/promises");
const path = await import("node:path");
const { fileURLToPath } = await import("node:url");

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INPUT_PATH = path.join(__dirname, "collect-sph-post-input.json");
const LIST_URL = "https://channels.weixin.qq.com/platform/post/list";
const COMMENT_URL = "https://channels.weixin.qq.com/platform/interaction/comment";

// 5 个指标按列表固定顺序（图标已校准）：播放/点赞/评论/转发/收藏
const METRIC_LABELS = ["播放", "点赞", "评论", "转发", "收藏"];

async function readInput() {
  const raw = await fs.readFile(INPUT_PATH, "utf8");
  return JSON.parse(raw);
}

async function shadowText(page) {
  return page.evaluate(() => {
    const wujie = document.querySelector("wujie-app");
    const sr = wujie && wujie.shadowRoot;
    return sr ? sr.textContent || "" : "";
  });
}

async function findPost(page, descPrefix, titlePrefix) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const extract = await page.evaluate(
      ({ descPrefix, titlePrefix }) => {
        const wujie = document.querySelector("wujie-app");
        const sr = wujie && wujie.shadowRoot;
        if (!sr) return { __noShadow: true, matched: null };
        const items = [...sr.querySelectorAll(".post-feed-item")];
        const rows = items.map((el) => {
          const titleEl = el.querySelector(".post-title");
          const timeEl = el.querySelector(".time-label");
          const dataItems = [...el.querySelectorAll(".post-data .data-item")];
          const metrics = dataItems.map((d) => {
            const num = (d.textContent || "").replace(/\s+/g, "");
            const icon = d.querySelector("[class*=icon]");
            const use = d.querySelector("use");
            return {
              num,
              iconCls: icon ? icon.getAttribute("class") || "" : "",
              useHref: use ? use.getAttribute("xlink:href") || use.getAttribute("href") || "" : "",
            };
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
        return { __noShadow: false, matched, rowCount: rows.length };
      },
      { descPrefix, titlePrefix },
    );
    if (extract && !extract.__noShadow && extract.matched) return extract;
    await page.waitForTimeout(3000);
  }
  return null;
}

function parseMetrics(metrics) {
  return metrics.map((m, i) => ({
    label: METRIC_LABELS[i] || ("指标" + (i + 1)),
    value: m.num === "" ? "0" : m.num,
    icon: m.useHref || m.iconCls,
  }));
}

// 进评论管理页，点目标作品，抓 Top20 评论（作者/时间/文本）。失败返回 null。
async function collectComments(page, descPrefix, titlePrefix) {
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
  await page.waitForTimeout(2000);

  // 点左侧目标作品（.comment-feed-wrap 行）
  const rect = await page.evaluate(
    ({ descPrefix, titlePrefix }) => {
      const wujie = document.querySelector("wujie-app");
      const sr = wujie && wujie.shadowRoot;
      if (!sr) return null;
      const els = [...sr.querySelectorAll(".comment-feed-wrap")];
      for (const e of els) {
        const t = (e.textContent || "").replace(/\s+/g, " ").trim();
        if ((descPrefix && t.includes(descPrefix)) || (titlePrefix && t.includes(titlePrefix))) {
          e.scrollIntoView({ block: "center" });
          const r = e.getBoundingClientRect();
          return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
        }
      }
      return null;
    },
    { descPrefix, titlePrefix },
  );

  if (!rect) return null;
  await page.cdp("Input.dispatchMouseEvent", { type: "mousePressed", x: rect.x, y: rect.y, button: "left", clickCount: 1 });
  await page.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x: rect.x, y: rect.y, button: "left", clickCount: 1 });
  await page.waitForTimeout(3500);

  const res = await page.evaluate(() => {
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
  });

  return res && !res.__noShadow ? res.list : null;
}

async function main() {
  const input = await readInput();
  const task = await taskSpace("sph-review-" + Date.now());
  const page = task.page("p1");
  const descPrefix = (input.publishDesc || "").replace(/\s+/g, " ").trim().slice(0, 14);
  const titlePrefix = (input.publishTitle || "").trim().slice(0, 14);

  try {
    // 1) 作品列表 → 5 个计数
    await page.goto(LIST_URL, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(2000);
    const found = await findPost(page, descPrefix, titlePrefix);
    if (!found || !found.matched) {
      console.log("RESULT=FAIL 未在作品列表前 20 条找到匹配作品（descPrefix=" + descPrefix + "）");
      return;
    }
    const post = found.matched;
    const current = parseMetrics(post.metrics);

    // 2) 评论（尽力而为）
    let comments = null;
    try {
      comments = await collectComments(page, descPrefix, titlePrefix);
    } catch (e) {
      console.log("COMMENTS_SKIP " + (e && e.message ? e.message : String(e)));
    }

    // 3) 组装 report
    const atPublish = (input.metricsAtPublish || []).map((m, i) => ({
      label: METRIC_LABELS[i] || ("指标" + (i + 1)),
      value: m && m.num ? m.num : "0",
    }));
    const now = new Date();
    const delta = (a, b) => {
      const na = parseInt(String(a).replace(/[^\d]/g, "") || "0", 10);
      const nb = parseInt(String(b).replace(/[^\d]/g, "") || "0", 10);
      return isNaN(na) || isNaN(nb) ? "-" : nb - na;
    };

    const lines = [];
    lines.push("# 视频号复盘报告");
    lines.push("");
    lines.push("- 作品：`" + (input.publishTitle || post.title.slice(0, 40)) + "`");
    lines.push("- 发布日期：" + (post.time || input.publishDate || "未知"));
    lines.push("- 复盘时间：" + now.toLocaleString("zh-CN", { hour12: false }));
    lines.push("- 平台：视频号");
    lines.push("");
    lines.push("## 数据");
    lines.push("");
    lines.push("| 指标 | 发布时 | 复盘时 | 变化 |");
    lines.push("|---|---|---|---|");
    for (let i = 0; i < current.length; i++) {
      const pub = atPublish[i] ? atPublish[i].value : "0";
      const cur = current[i].value;
      const d = delta(pub, cur);
      lines.push("| " + current[i].label + " | " + pub + " | " + cur + " | " + (d >= 0 ? "+" : "") + d + " |");
    }
    lines.push("");

    if (input.transcript) {
      lines.push("## 口播文案");
      lines.push("");
      lines.push(input.transcript);
      lines.push("");
    }

    if (comments !== null) {
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
    }

    if (input.publishDesc) {
      lines.push("## 原文案");
      lines.push("");
      lines.push(input.publishDesc);
      lines.push("");
    }

    const report = lines.join("\n");
    const outPath = input.runDir + "/report.md";
    await fs.mkdir(input.runDir, { recursive: true });
    await fs.writeFile(outPath, report, "utf8");

    console.log("REPORT_WRITTEN " + outPath);
    console.log("RESULT=OK");
  } catch (e) {
    console.log("RESULT=FAIL " + (e && e.message ? e.message : String(e)));
  } finally {
    try { await task.finish({ keep: [] }); } catch (e) {}
  }
}

await main();
