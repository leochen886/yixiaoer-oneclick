"""视频号本地交互式复盘工作台（纯 Python 标准库 HTTP 服务 + 单页控制台）。"""

from __future__ import annotations

import argparse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
from typing import Any
from urllib.parse import unquote, urlparse
import uuid
import webbrowser

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import history_db
import retro_analyze


WORKBENCH_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视频号复盘工作台 · 咬金战AI</title>
<style>
  :root {
    --bg: #f4f6f9;
    --surface: #ffffff;
    --surface-2: #f8fafc;
    --ink: #0f172a;
    --muted: #64748b;
    --line: #e2e8f0;
    --green: #07c160;
    --green-dark: #05964b;
    --green-soft: #eafaf1;
    --blue: #2563eb;
    --blue-soft: #eff6ff;
    --amber: #d97706;
    --amber-soft: #fffbeb;
    --red: #dc2626;
    --red-soft: #fef2f2;
    --purple: #7c3aed;
    --purple-soft: #f5f3ff;
    --radius: 12px;
    --shadow: 0 2px 10px rgba(15, 23, 42, 0.05);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: "Microsoft YaHei", "PingFang SC", system-ui, -apple-system, sans-serif;
    font-size: 14px;
    line-height: 1.55;
  }
  button, input, select, textarea { font: inherit; }
  button {
    cursor: pointer;
    border: 1px solid var(--line);
    background: var(--surface);
    color: var(--ink);
    border-radius: 8px;
    padding: 7px 13px;
    transition: all 0.15s ease;
  }
  button:hover:not(:disabled) { border-color: #cbd5e1; background: #f8fafc; }
  button:disabled { opacity: 0.55; cursor: not-allowed; }
  button.primary {
    background: var(--green);
    color: #fff;
    border-color: var(--green);
    font-weight: 600;
  }
  button.primary:hover:not(:disabled) { background: var(--green-dark); border-color: var(--green-dark); }
  button.dark { background: var(--ink); color: #fff; border-color: var(--ink); }
  button.dark:hover:not(:disabled) { background: #1e293b; }
  button.sm { padding: 4px 10px; font-size: 12px; border-radius: 6px; }
  button.active { background: var(--ink); color: #fff; border-color: var(--ink); }

  .topbar {
    position: sticky; top: 0; z-index: 30;
    background: rgba(255, 255, 255, 0.94);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--line);
    padding: 12px 24px;
  }
  .topbar-inner {
    max-width: 1440px; margin: 0 auto;
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px;
  }
  .brand h1 { margin: 0; font-size: 19px; display: flex; align-items: center; gap: 8px; }
  .status-pill {
    font-size: 12px; color: var(--muted); background: var(--surface-2);
    border: 1px solid var(--line); border-radius: 999px; padding: 3px 10px; display: inline-block; margin-top: 3px;
  }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }

  .wrap { max-width: 1440px; margin: 0 auto; padding: 18px 24px 64px; }
  .banner {
    display: none; align-items: center; justify-content: space-between; gap: 12px;
    padding: 10px 14px; border-radius: 10px; margin-bottom: 14px; font-size: 13px;
    border: 1px solid #bfdbfe; background: var(--blue-soft); color: #1e3a8a;
  }
  .banner.show { display: flex; }
  .banner.err { border-color: #fecaca; background: var(--red-soft); color: #991b1b; }
  .banner.ok { border-color: #bbf7d0; background: var(--green-soft); color: #14532d; }

  .card {
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 16px 18px; margin-bottom: 16px; box-shadow: var(--shadow);
  }
  .card-head {
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px;
    margin-bottom: 12px;
  }
  .card-head h2 { margin: 0; font-size: 16px; }

  .filters {
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 12px 16px; margin-bottom: 16px; box-shadow: var(--shadow);
  }
  .seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .seg button { border: none; border-radius: 0; border-right: 1px solid var(--line); padding: 6px 11px; font-size: 12px; }
  .seg button:last-child { border-right: none; }
  .search-input {
    flex: 1; min-width: 210px; padding: 6px 11px; border: 1px solid var(--line); border-radius: 8px;
  }
  .chk { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); user-select: none; }

  .kpis {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px; margin-bottom: 16px;
  }
  .kpi {
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 13px 16px; box-shadow: var(--shadow);
  }
  .kpi .lbl { font-size: 12px; color: var(--muted); }
  .kpi .val { font-size: 24px; font-weight: 700; margin-top: 2px; letter-spacing: -0.02em; }
  .kpi .sub { font-size: 11px; color: var(--muted); margin-top: 2px; }

  .chart-box { position: relative; width: 100%; overflow-x: auto; }
  .chart-tip {
    position: fixed; pointer-events: none; z-index: 60; display: none;
    background: rgba(15, 23, 42, 0.94); color: #fff; padding: 8px 11px; border-radius: 8px;
    font-size: 12px; max-width: 280px; box-shadow: 0 8px 20px rgba(0,0,0,0.2);
  }

  .tabs { display: flex; flex-wrap: wrap; gap: 6px; }
  .exec-summary {
    background: var(--surface-2); border-left: 4px solid var(--green);
    padding: 10px 14px; border-radius: 6px; margin-bottom: 12px; font-size: 14px;
  }
  .rule-list { display: grid; gap: 8px; }
  .rule-item {
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; gap: 10px;
    padding: 10px 12px; border: 1px solid var(--line); border-radius: 9px; background: #fff;
  }
  .rule-item.approved { border-color: #bbf7d0; background: #f8fffb; }
  .rule-item.rejected { opacity: 0.55; background: #f8fafc; }
  .rule-item.rejected .rule-text { text-decoration: line-through; }
  .rule-main { flex: 1; min-width: 240px; }
  .rule-text { font-size: 13.5px; }
  .rule-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 5px; align-items: center; }
  .rule-actions { display: flex; gap: 5px; flex-shrink: 0; }
  .add-rule-row {
    display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--line);
  }
  .add-rule-row input { flex: 1; min-width: 220px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px; }
  .add-rule-row select { padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px; }

  .topics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; }
  .topic-card {
    border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; background: var(--surface-2);
    display: flex; flex-direction: column; justify-content: space-between; gap: 8px;
  }
  .topic-card.dismissed { opacity: 0.5; }
  .topic-card h3 { margin: 0; font-size: 14.5px; }
  .topic-card p { margin: 4px 0 0; font-size: 12.5px; color: var(--muted); }

  .badge {
    display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600;
    border: 1px solid transparent; white-space: nowrap;
  }
  .b-green { background: var(--green-soft); color: #0f5132; border-color: #bbf7d0; }
  .b-amber { background: var(--amber-soft); color: #92400e; border-color: #fde68a; }
  .b-red { background: var(--red-soft); color: #991b1b; border-color: #fecaca; }
  .b-blue { background: var(--blue-soft); color: #1e40af; border-color: #bfdbfe; }
  .b-purple { background: var(--purple-soft); color: #5b21b6; border-color: #ddd6fe; }
  .b-gray { background: #f1f5f9; color: #475569; border-color: #e2e8f0; }
  .b-ev { background: #fff; color: var(--blue); border-color: #bfdbfe; cursor: pointer; }
  .b-ev:hover { background: var(--blue-soft); }

  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
  th { color: var(--muted); font-weight: 600; white-space: nowrap; user-select: none; }
  th.sortable { cursor: pointer; }
  th.sortable:hover { color: var(--ink); }
  td.num, th.num { text-align: right; white-space: nowrap; }
  tbody tr.vrow { cursor: pointer; transition: background 0.1s; }
  tbody tr.vrow:hover { background: #f8fafc; }
  tbody tr.vrow.selected { background: var(--green-soft); }
  .subline { font-size: 11.5px; color: var(--muted); margin-top: 2px; }
  .badges-row { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }

  .drawer-backdrop {
    position: fixed; inset: 0; background: rgba(15, 23, 42, 0.35); z-index: 45; display: none;
  }
  .drawer-backdrop.open { display: block; }
  .drawer {
    position: fixed; top: 0; right: -640px; width: min(640px, 96vw); height: 100vh; z-index: 50;
    background: var(--surface); border-left: 1px solid var(--line); box-shadow: -8px 0 30px rgba(15, 23, 42, 0.14);
    transition: right 0.22s ease; display: flex; flex-direction: column;
  }
  .drawer.open { right: 0; }
  .drawer-head {
    padding: 16px 20px; border-bottom: 1px solid var(--line);
    display: flex; justify-content: space-between; align-items: flex-start; gap: 12px;
  }
  .drawer-head h2 { margin: 0; font-size: 17px; }
  .drawer-body { padding: 16px 20px 36px; overflow-y: auto; flex: 1; }
  .d-section { margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid var(--line); }
  .d-section:last-child { border-bottom: none; }
  .d-section h3 { margin: 0 0 8px; font-size: 14px; color: var(--ink); }
  .kv-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
  .kv { background: var(--surface-2); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
  .kv .k { font-size: 11px; color: var(--muted); }
  .kv .v { font-size: 15px; font-weight: 700; margin-top: 2px; }
  .hook-box {
    background: #0f172a; color: #f8fafc; border-radius: 10px; padding: 12px 14px; margin-bottom: 10px;
  }
  .hook-white { color: #e2e8f0; font-size: 13px; }
  .hook-yellow { color: #facc15; font-size: 16px; font-weight: 700; margin-top: 2px; }
  .transcript-box {
    background: var(--surface-2); border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 12px; white-space: pre-wrap; font-size: 13px; max-height: 240px; overflow-y: auto;
  }
  .opening-pill {
    background: #fef9c3; border: 1px solid #fde047; color: #713f12;
    padding: 6px 10px; border-radius: 8px; font-size: 12.5px; margin-bottom: 8px;
  }
  .form-row { margin-bottom: 10px; }
  .form-row label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  .form-row input, .form-row textarea, .form-row select {
    width: 100%; padding: 7px 10px; border: 1px solid var(--line); border-radius: 8px;
  }

  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); z-index: 70;
    display: none; align-items: center; justify-content: center; padding: 16px;
  }
  .modal-backdrop.open { display: flex; }
  .modal {
    background: var(--surface); border-radius: 14px; width: min(620px, 100%);
    max-height: 90vh; overflow-y: auto; padding: 20px 22px; box-shadow: 0 20px 50px rgba(15, 23, 42, 0.25);
  }
  .drop-zone {
    border: 2px dashed #cbd5e1; border-radius: 10px; padding: 18px; text-align: center;
    background: var(--surface-2); cursor: pointer; margin-top: 8px;
  }
  .drop-zone.drag { border-color: var(--green); background: var(--green-soft); }
  .toast {
    position: fixed; bottom: 20px; right: 20px; z-index: 90;
    background: var(--ink); color: #fff; padding: 10px 16px; border-radius: 10px;
    font-size: 13px; display: none; box-shadow: 0 8px 24px rgba(0,0,0,0.22);
  }
  .toast.show { display: block; }
  .empty { text-align: center; color: var(--muted); padding: 20px; }
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <h1>📊 视频号复盘工作台 <span class="badge b-green">history JSON 数据库</span></h1>
      <div class="status-pill" id="statusPill">正在加载数据库…</div>
    </div>
    <div class="actions">
      <button class="primary" onclick="openImportModal()">📥 导入官方数据表 (CSV)</button>
      <button onclick="syncLocalRuns()">🔄 同步本地发布</button>
      <button id="btnEgo" onclick="startJob('collect-ego')">💬 抓评论与收藏 (ego)</button>
      <button id="btnAnalyze" onclick="startJob('analyze')">🤖 AI 复盘诊断</button>
      <button onclick="copyContextBrief()">📋 复制上下文给 AI</button>
    </div>
  </div>
</div>

<div class="wrap">
  <div class="banner" id="jobBanner">
    <span id="jobText">正在执行任务…</span>
    <button class="sm" onclick="hideBanner()">收起</button>
  </div>

  <div class="filters">
    <div class="seg" id="segRange">
      <button data-val="7" onclick="setFilter('range', '7')">近 7 天</button>
      <button data-val="30" class="active" onclick="setFilter('range', '30')">近 30 天</button>
      <button data-val="all" onclick="setFilter('range', 'all')">全部</button>
    </div>
    <div class="seg" id="segStage">
      <button data-val="all" class="active" onclick="setFilter('stage', 'all')">全部阶段</button>
      <button data-val="mature" onclick="setFilter('stage', 'mature')">成熟 ≥24h</button>
      <button data-val="3d" onclick="setFilter('stage', '3d')">满 3 天</button>
      <button data-val="obs" onclick="setFilter('stage', 'obs')">观察 &lt;24h</button>
    </div>
    <div class="seg" id="segTranscript">
      <button data-val="all" class="active" onclick="setFilter('transcript', 'all')">全部文案</button>
      <button data-val="yes" onclick="setFilter('transcript', 'yes')">有口播</button>
      <button data-val="no" onclick="setFilter('transcript', 'no')">缺口播</button>
    </div>
    <label class="chk">
      <input type="checkbox" id="chkHideExcluded" checked onchange="renderAll()">
      隐藏测试/排除片 (<span id="excludedCount">0</span>)
    </label>
    <input class="search-input" id="searchBox" type="search" placeholder="搜索短标题 / 白黄字 / 口播 / 备注…" oninput="renderAll()">
  </div>

  <div class="kpis" id="kpiGrid"></div>

  <div class="card">
    <div class="card-head">
      <h2>📈 播放量 &amp; 完播率走势（点击柱子查看该视频详情）</h2>
      <div class="actions">
        <button class="sm" id="btnScale" onclick="toggleChartScale()">切换线性刻度</button>
      </div>
    </div>
    <div class="chart-box" id="chartBox"></div>
  </div>

  <div class="card">
    <div class="card-head">
      <h2>🧠 AI 策略记忆 &amp; 写文案红线（点击“采纳/划掉”直接生效于下次写文案与发片）</h2>
      <div class="tabs" id="pbTabs">
        <button class="sm active" data-tab="winning" onclick="setPbTab('winning')">✅ 有效套路</button>
        <button class="sm" data-tab="avoid" onclick="setPbTab('avoid')">🚫 踩坑与红线</button>
        <button class="sm" data-tab="writing" onclick="setPbTab('writing')">✍️ 白黄字 &amp; 文案建议</button>
        <button class="sm" data-tab="topics" onclick="setPbTab('topics')">🎬 下期选题库</button>
        <label class="chk" style="margin-left:8px;">
          <input type="checkbox" id="chkShowRejected" onchange="renderPlaybook()"> 显示已划掉
        </label>
      </div>
    </div>
    <div class="exec-summary" id="execSummary">尚未运行深度诊断，请导入 CSV 或点击右上角「AI 复盘诊断」。</div>
    <div id="playbookBody"></div>
    <div class="add-rule-row">
      <select id="newRuleCat">
        <option value="copywriting">文案红线 / 提醒</option>
        <option value="hook">白黄字技巧</option>
        <option value="winning">有效套路</option>
        <option value="avoid">避坑规则</option>
      </select>
      <input id="newRuleText" type="text" placeholder="手动补充一条给 AI 记住的规矩，例如：不要在开头第一句自嘲还没搞定…">
      <button class="dark sm" onclick="addUserRule()">+ 添加规则</button>
    </div>
  </div>

  <div class="card">
    <div class="card-head">
      <h2>📋 视频数据库明细（<span id="tableCount">0</span> 条）</h2>
      <span class="subline">点击任意行在右侧展开完整口播、白黄字、时间线、窗口对照与备注编辑</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="sortable" onclick="setSort('date')">发布日期 ▾</th>
            <th>短标题 / 白黄字 / 标签</th>
            <th>时长</th>
            <th>阶段</th>
            <th class="num sortable" onclick="setSort('plays')">播放量 ▾</th>
            <th class="num sortable" onclick="setSort('completion')">完播率 ▾</th>
            <th class="num sortable" onclick="setSort('watch')">均播 ▾</th>
            <th class="num sortable" onclick="setSort('follows')">涨粉 ▾</th>
            <th class="num">赞 / 推 / 评 / 转 / 藏</th>
            <th class="num sortable" onclick="setSort('rate')">互动率 ▾</th>
            <th>口播</th>
          </tr>
        </thead>
        <tbody id="videoTbody"></tbody>
      </table>
    </div>
  </div>
</div>

<div class="chart-tip" id="chartTip"></div>

<div class="drawer-backdrop" id="drawerBackdrop" onclick="closeDrawer()"></div>
<aside class="drawer" id="videoDrawer">
  <div class="drawer-head">
    <div>
      <h2 id="drawerTitle">视频详情</h2>
      <div class="subline" id="drawerSub"></div>
    </div>
    <div class="actions">
      <button class="sm" onclick="copySingleContext()">📋 复制给 AI</button>
      <button class="sm" onclick="closeDrawer()">✕ 关闭</button>
    </div>
  </div>
  <div class="drawer-body" id="drawerBody"></div>
</aside>

<div class="modal-backdrop" id="importModal">
  <div class="modal">
    <div class="card-head">
      <h2>📥 导入视频号官方数据表 (CSV)</h2>
      <button class="sm" onclick="closeImportModal()">✕</button>
    </div>
    <p class="subline" style="margin-top:0;">
      视频号助手 → 数据中心 → 视频数据 → 下载表格（<code>视频号动态数据明细.csv</code>）。<br>
      系统会自动按 <strong>视频ID + 描述</strong> 合并到已有口播文案中，绝不产生重复记录。
    </p>
    <div class="form-row">
      <label>这次导出选择的统计时间范围（非常关键，保护老视频历史总数据）</label>
      <select id="impWindow" onchange="toggleCustomDates()">
        <option value="auto">自动识别（按表格内日期跨度判断 7天 / 30天 / 自定义）</option>
        <option value="7d">近 7 天（7d：发片超过7天的老视频只记入 7天窗口，不覆盖总播放）</option>
        <option value="30d">近 30 天（30d：30天内视频更新全周期总数据）</option>
        <option value="custom">自定义日期范围（手动指定起止日期）</option>
      </select>
    </div>
    <div id="customDatesRow" style="display:none; grid-template-columns:1fr 1fr; gap:10px;" class="form-row">
      <div><label>开始日期</label><input type="date" id="impStart"></div>
      <div><label>结束日期</label><input type="date" id="impEnd"></div>
    </div>

    <div class="card" style="background:var(--surface-2); margin:12px 0; padding:12px;">
      <div style="display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap;">
        <div>
          <strong>方式一：一键导入下载目录最新表格</strong>
          <div class="subline" id="detectedCsvInfo">检测中…</div>
        </div>
        <button class="primary sm" id="btnImportDownloads" onclick="importFromDownloads()">一键导入</button>
      </div>
    </div>

    <div>
      <strong>方式二：拖拽或选择任意 CSV 文件导入</strong>
      <div class="drop-zone" id="dropZone" onclick="document.getElementById('csvFileInput').click()">
        把 CSV 文件拖到这里，或点击选择文件
        <input type="file" id="csvFileInput" accept=".csv,text/csv" style="display:none" onchange="handleFilePick(this.files[0])">
      </div>
    </div>

    <div style="margin-top:14px;">
      <strong style="font-size:12.5px;">最近导入记录</strong>
      <div id="importHistoryList" class="subline" style="margin-top:6px;">暂无记录</div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const STATE = {
  index: { videos: [], stats_summary: {} },
  playbook: { rules: [], next_topics: [], executive_summary: "" },
  imports: [],
  detectedCsv: null,
  filters: { range: "30", stage: "all", transcript: "all" },
  sort: { key: "date", dir: -1 },
  chartLog: true,
  pbTab: "winning",
  selectedId: null,
  currentVideo: null,
  activeJobId: null
};

const esc = (s) => String(s ?? "")
  .replace(/&/g, "&amp;")
  .replace(/</g, "&lt;")
  .replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;")
  .replace(/'/g, "&#39;");

function fmtInt(v) {
  return (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toLocaleString("zh-CN");
}
function fmtPct(v) {
  return (v === null || v === undefined || Number.isNaN(v)) ? "—" : (Number(v) * 100).toFixed(2) + "%";
}
function fmtSec(v) {
  return (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(1) + "s";
}

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.remove("show"), 3200);
}

async function api(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

async function loadOverview() {
  const data = await api("/api/overview");
  STATE.index = data.index || { videos: [], stats_summary: {} };
  STATE.playbook = data.playbook || { rules: [], next_topics: [] };
  STATE.imports = data.imports || [];
  STATE.detectedCsv = data.detected_downloads_csv || null;
  renderAll();
}

function setFilter(group, val) {
  STATE.filters[group] = val;
  const map = { range: "segRange", stage: "segStage", transcript: "segTranscript" };
  document.querySelectorAll("#" + map[group] + " button").forEach((b) => {
    b.classList.toggle("active", b.dataset.val === val);
  });
  renderAll();
}

function setSort(key) {
  if (STATE.sort.key === key) STATE.sort.dir *= -1;
  else { STATE.sort.key = key; STATE.sort.dir = -1; }
  renderTable();
}

function setPbTab(tab) {
  STATE.pbTab = tab;
  document.querySelectorAll("#pbTabs button[data-tab]").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });
  renderPlaybook();
}

function toggleChartScale() {
  STATE.chartLog = !STATE.chartLog;
  document.getElementById("btnScale").textContent = STATE.chartLog ? "切换线性刻度" : "切换对数刻度";
  renderChart(getFilteredVideos());
}

function getFilteredVideos() {
  const hideExcluded = document.getElementById("chkHideExcluded").checked;
  const q = document.getElementById("searchBox").value.trim().toLowerCase();
  const videos = STATE.index.videos || [];
  let maxDateMs = 0;
  for (const v of videos) {
    const t = Date.parse(v.published_at || v.publish_date || "");
    if (Number.isFinite(t) && t > maxDateMs) maxDateMs = t;
  }
  const refNow = Math.max(Date.now(), maxDateMs);
  return videos.filter((v) => {
    if (hideExcluded && v.status === "excluded") return false;
    if (STATE.filters.range !== "all") {
      const days = Number(STATE.filters.range);
      const t = Date.parse(v.published_at || v.publish_date || "");
      if (Number.isFinite(t) && (refNow - t) > days * 86400000) return false;
    }
    if (STATE.filters.stage === "mature" && !["1天", "3天", "7天+"].includes(v.stage)) return false;
    if (STATE.filters.stage === "3d" && !["3天", "7天+"].includes(v.stage)) return false;
    if (STATE.filters.stage === "obs" && v.stage !== "<24h") return false;
    if (STATE.filters.transcript === "yes" && !v.has_transcript) return false;
    if (STATE.filters.transcript === "no" && v.has_transcript) return false;
    if (q) {
      const hay = [
        v.publish_title, v.hook_context, v.hook_title, v.publish_desc_excerpt,
        v.opening_3s, v.user_notes, v.user_mark, (v.publish_tags || []).join(" "),
        (v.topic_tags || []).join(" ")
      ].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderStatusBar() {
  const all = STATE.index.videos || [];
  const excluded = all.filter((v) => v.status === "excluded").length;
  document.getElementById("excludedCount").textContent = excluded;
  const withTr = all.filter((v) => v.has_transcript).length;
  const lastImp = STATE.imports[0];
  const csvPart = lastImp
    ? `最近CSV：${lastImp.window_type} (${lastImp.window_start}~${lastImp.window_end})`
    : "尚未导入官方 CSV";
  document.getElementById("statusPill").textContent =
    `共 ${all.length} 条 · 有口播 ${withTr} 条 · ${csvPart}`;
}

function renderKpis(list) {
  const plays = list.map((v) => v.plays).filter((x) => typeof x === "number");
  const sortedPlays = [...plays].sort((a, b) => a - b);
  const median = sortedPlays.length
    ? (sortedPlays.length % 2
        ? sortedPlays[(sortedPlays.length - 1) / 2]
        : (sortedPlays[sortedPlays.length / 2 - 1] + sortedPlays[sortedPlays.length / 2]) / 2)
    : 0;
  const sumPlays = plays.reduce((a, b) => a + b, 0);
  const avgPlays = plays.length ? Math.round(sumPlays / plays.length) : 0;
  const comps = list.map((v) => v.completion_rate).filter((x) => typeof x === "number");
  const avgComp = comps.length ? comps.reduce((a, b) => a + b, 0) / comps.length : null;
  const watches = list.map((v) => v.avg_watch_sec).filter((x) => typeof x === "number");
  const avgWatch = watches.length ? watches.reduce((a, b) => a + b, 0) / watches.length : null;
  const follows = list.map((v) => v.follows).filter((x) => typeof x === "number");
  const sumFollows = follows.reduce((a, b) => a + b, 0);
  const matureN = list.filter((v) => ["1天", "3天", "7天+"].includes(v.stage)).length;
  const obsN = list.filter((v) => v.stage === "<24h").length;

  const cards = [
    { lbl: "筛选作品数", val: fmtInt(list.length), sub: `成熟 ${matureN} 条 · 观察 ${obsN} 条` },
    { lbl: "累计播放量", val: fmtInt(sumPlays), sub: `平均 ${fmtInt(avgPlays)} / 条` },
    { lbl: "中位播放量", val: fmtInt(Math.round(median)), sub: `最高 ${fmtInt(plays.length ? Math.max(...plays) : 0)}` },
    { lbl: "平均完播率", val: fmtPct(avgComp), sub: `覆盖 ${comps.length} 条有完播样本` },
    { lbl: "平均播放时长", val: fmtSec(avgWatch), sub: "官方数据中心真实均值" },
    { lbl: "累计涨粉 (关注)", val: fmtInt(sumFollows), sub: sumPlays > 0 ? `整体涨粉率 ${((sumFollows / sumPlays) * 100).toFixed(2)}%` : "—" }
  ];
  document.getElementById("kpiGrid").innerHTML = cards.map((c) => `
    <div class="kpi">
      <div class="lbl">${esc(c.lbl)}</div>
      <div class="val">${esc(c.val)}</div>
      <div class="sub">${esc(c.sub)}</div>
    </div>
  `).join("");
}

function renderChart(list) {
  const box = document.getElementById("chartBox");
  const items = [...list]
    .filter((v) => typeof v.plays === "number")
    .sort((a, b) => String(a.published_at || a.publish_date).localeCompare(String(b.published_at || b.publish_date)))
    .slice(-45);
  if (!items.length) {
    box.innerHTML = `<div class="empty">当前筛选下暂无播放数据</div>`;
    return;
  }
  const W = Math.max(760, items.length * 26 + 90);
  const H = 220;
  const padL = 52, padR = 46, padT = 18, padB = 34;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const maxPlays = Math.max(10, ...items.map((x) => x.plays || 0));
  const maxComp = Math.max(0.4, ...items.map((x) => x.completion_rate || 0));
  const yPlays = (p) => {
    if (!p || p <= 0) return padT + plotH;
    const ratio = STATE.chartLog
      ? Math.log10(p + 1) / Math.log10(maxPlays + 1)
      : p / maxPlays;
    return padT + plotH - ratio * plotH;
  };
  const yComp = (c) => padT + plotH - (c / maxComp) * plotH;
  const step = plotW / items.length;
  const barW = Math.max(8, Math.min(20, step * 0.62));

  let bars = "";
  let pts = [];
  let circles = "";
  let xlabels = "";
  items.forEach((v, idx) => {
    const cx = padL + idx * step + step / 2;
    const yTop = yPlays(v.plays);
    const h = padT + plotH - yTop;
    const isObs = v.stage === "<24h";
    const fill = v.id === STATE.selectedId ? "#0f172a" : (isObs ? "#94a3b8" : "#07c160");
    bars += `<rect x="${(cx - barW / 2).toFixed(1)}" y="${yTop.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(2, h).toFixed(1)}" rx="3" fill="${fill}" opacity="0.88" style="cursor:pointer" data-vid="${esc(v.id)}" onmousemove="showTip(event, '${esc(v.id)}')" onmouseleave="hideTip()" onclick="openVideo('${esc(v.id)}')"/>`;
    if (typeof v.completion_rate === "number") {
      const cy = yComp(v.completion_rate);
      pts.push(`${cx.toFixed(1)},${cy.toFixed(1)}`);
      circles += `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="3.2" fill="#f59e0b" stroke="#fff" stroke-width="1" style="cursor:pointer" onmousemove="showTip(event, '${esc(v.id)}')" onmouseleave="hideTip()" onclick="openVideo('${esc(v.id)}')"/>`;
    }
    if (idx % Math.max(1, Math.floor(items.length / 8)) === 0 || idx === items.length - 1) {
      xlabels += `<text x="${cx.toFixed(1)}" y="${H - 10}" text-anchor="middle" font-size="10" fill="#64748b">${esc((v.publish_date || "").slice(5))}</text>`;
    }
  });
  const polyline = pts.length > 1
    ? `<polyline fill="none" stroke="#f59e0b" stroke-width="2" points="${pts.join(" ")}"/>`
    : "";
  box.innerHTML = `
    <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
      <line x1="${padL}" y1="${padT}" x2="${padL}" y2="${padT + plotH}" stroke="#cbd5e1"/>
      <line x1="${padL}" y1="${padT + plotH}" x2="${W - padR}" y2="${padT + plotH}" stroke="#cbd5e1"/>
      <text x="6" y="${padT + 10}" font-size="10" fill="#64748b">${fmtInt(maxPlays)}</text>
      <text x="6" y="${padT + plotH}" font-size="10" fill="#64748b">0</text>
      <text x="${W - padR + 4}" y="${padT + 10}" font-size="10" fill="#d97706">${(maxComp * 100).toFixed(0)}%</text>
      <text x="${W - padR + 4}" y="${padT + plotH}" font-size="10" fill="#d97706">0%</text>
      ${bars}${polyline}${circles}${xlabels}
    </svg>
    <div class="subline">绿柱 = 成熟作品播放量 · 灰柱 = &lt;24h 观察期播放量 · 橙线 = 完播率（${STATE.chartLog ? "当前：对数刻度" : "当前：线性刻度"}）</div>
  `;
}

function showTip(ev, vid) {
  const v = (STATE.index.videos || []).find((x) => x.id === vid);
  if (!v) return;
  const tip = document.getElementById("chartTip");
  tip.innerHTML = `
    <div><strong>${esc(v.publish_title)}</strong></div>
    <div>${esc(v.publish_date || "")} · ${esc(v.stage)}</div>
    <div>播放：${fmtInt(v.plays)} ｜ 完播：${fmtPct(v.completion_rate)} ｜ 涨粉：${fmtInt(v.follows)}</div>
  `;
  tip.style.display = "block";
  tip.style.left = Math.min(window.innerWidth - 290, ev.clientX + 12) + "px";
  tip.style.top = Math.max(10, ev.clientY - 70) + "px";
}
function hideTip() { document.getElementById("chartTip").style.display = "none"; }

function renderPlaybook() {
  const pb = STATE.playbook || {};
  const srcMap = { claude: "Claude", deepseek: "DeepSeek", local: "本地统计", legacy: "历史迁移" };
  const upd = pb.updated_at ? String(pb.updated_at).slice(0, 16).replace("T", " ") : "未更新";
  document.getElementById("execSummary").innerHTML = pb.executive_summary
    ? `<strong>最新复盘总评</strong>（${esc(srcMap[pb.analysis_source] || pb.analysis_source || "")} · ${esc(upd)}）：${esc(pb.executive_summary)}`
    : "尚未生成 AI 深度复盘总评，点击右上角「🤖 AI 复盘诊断」即可生成并自动更新规则。";

  const body = document.getElementById("playbookBody");
  if (STATE.pbTab === "topics") {
    const topics = pb.next_topics || [];
    if (!topics.length) {
      body.innerHTML = `<div class="empty">暂无下期选题建议</div>`;
      return;
    }
    body.innerHTML = `<div class="topics-grid">${topics.map((t) => `
      <div class="topic-card ${t.status === "dismissed" ? "dismissed" : ""}">
        <div>
          <div style="display:flex; justify-content:space-between; gap:6px; align-items:center; margin-bottom:4px;">
            <span class="badge ${t.status === "filmed" ? "b-green" : (t.status === "dismissed" ? "b-gray" : "b-blue")}">
              ${t.status === "filmed" ? "已开拍" : (t.status === "dismissed" ? "已跳过" : "待拍选题")}
            </span>
          </div>
          <h3>${esc(t.title)}</h3>
          ${t.hook_context ? `<div class="subline">白字：${esc(t.hook_context)}</div>` : ""}
          ${t.hook_title ? `<div style="margin-top:4px;"><span class="badge b-amber">黄字：${esc(t.hook_title)}</span></div>` : ""}
          <p>${esc(t.angle || "")}</p>
        </div>
        <div class="actions">
          <button class="sm" onclick="setTopicStatus('${esc(t.id)}', 'filmed')">✓ 已拍</button>
          <button class="sm" onclick="setTopicStatus('${esc(t.id)}', 'todo')">待拍</button>
          <button class="sm" onclick="setTopicStatus('${esc(t.id)}', 'dismissed')">跳过</button>
        </div>
      </div>
    `).join("")}</div>`;
    return;
  }

  const showRejected = document.getElementById("chkShowRejected").checked;
  const cats = STATE.pbTab === "writing" ? ["hook", "copywriting"] : [STATE.pbTab];
  const rules = (pb.rules || []).filter((r) => cats.includes(r.category) && (showRejected || r.status !== "rejected"));
  rules.sort((a, b) => {
    const rank = { approved: 0, proposed: 1, rejected: 2 };
    return (rank[a.status] ?? 9) - (rank[b.status] ?? 9);
  });
  if (!rules.length) {
    body.innerHTML = `<div class="empty">该分类下暂无有效规则（可点下方手动添加，或运行 AI 复盘）</div>`;
    return;
  }
  const catLabel = { winning: "有效套路", avoid: "避坑红线", hook: "白黄字", copywriting: "写文案" };
  const vidMap = Object.fromEntries((STATE.index.videos || []).map((v) => [v.id, v]));
  body.innerHTML = `<div class="rule-list">${rules.map((r) => {
    const stBadge = r.status === "approved"
      ? `<span class="badge b-green">✓ 已采纳</span>`
      : (r.status === "rejected" ? `<span class="badge b-gray">✕ 已划掉</span>` : `<span class="badge b-amber">AI 建议</span>`);
    const evHtml = (r.evidence_ids || []).map((eid) => {
      const ev = vidMap[eid];
      const lbl = ev ? (ev.publish_title || eid).slice(0, 14) : eid;
      return `<span class="badge b-ev" onclick="openVideo('${esc(eid)}')">依据：${esc(lbl)}</span>`;
    }).join("");
    return `
      <div class="rule-item ${esc(r.status)}">
        <div class="rule-main">
          <div class="rule-text">${esc(r.text)}</div>
          <div class="rule-meta">
            ${stBadge}
            <span class="badge b-gray">${esc(catLabel[r.category] || r.category)}</span>
            ${evHtml}
          </div>
        </div>
        <div class="rule-actions">
          ${r.status !== "approved" ? `<button class="sm" onclick="setRuleStatus('${esc(r.id)}', 'approved')">✓ 采纳</button>` : ""}
          ${r.status !== "rejected" ? `<button class="sm" onclick="setRuleStatus('${esc(r.id)}', 'rejected')">✕ 划掉</button>` : `<button class="sm" onclick="setRuleStatus('${esc(r.id)}', 'proposed')">恢复</button>`}
        </div>
      </div>
    `;
  }).join("")}</div>`;
}

function renderTable() {
  const list = getFilteredVideos();
  document.getElementById("tableCount").textContent = list.length;
  const k = STATE.sort.key;
  const d = STATE.sort.dir;
  const valOf = (v) => {
    if (k === "date") return String(v.published_at || v.publish_date || "");
    if (k === "plays") return v.plays ?? -1;
    if (k === "completion") return v.completion_rate ?? -1;
    if (k === "watch") return v.avg_watch_sec ?? -1;
    if (k === "follows") return v.follows ?? -1;
    if (k === "rate") return v.interaction_rate ?? -1;
    return "";
  };
  list.sort((a, b) => {
    const va = valOf(a), vb = valOf(b);
    if (typeof va === "string") return va.localeCompare(vb) * d;
    return (va - vb) * d;
  });

  const maturePlays = (STATE.index.videos || [])
    .filter((x) => x.status !== "excluded" && ["1天", "3天", "7天+"].includes(x.stage) && typeof x.plays === "number")
    .map((x) => x.plays)
    .sort((a, b) => a - b);
  const matureMed = maturePlays.length ? maturePlays[Math.floor(maturePlays.length / 2)] : 500;
  const hitThreshold = Math.max(2000, matureMed * 3);

  const tbody = document.getElementById("videoTbody");
  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="empty">没有符合当前筛选的视频</td></tr>`;
    return;
  }
  tbody.innerHTML = list.map((v) => {
    const badges = [];
    if (v.status === "excluded") badges.push(`<span class="badge b-gray">已排除</span>`);
    if (v.user_mark === "标杆") badges.push(`<span class="badge b-green">⭐ 标杆</span>`);
    if (v.user_mark === "想二创") badges.push(`<span class="badge b-purple">♻ 想二创</span>`);
    if (v.user_mark === "踩坑") badges.push(`<span class="badge b-red">💥 踩坑</span>`);
    if ((v.plays || 0) >= hitThreshold && v.status !== "excluded") badges.push(`<span class="badge b-green">🔥 爆款</span>`);
    if ((v.completion_rate || 0) >= 0.25 && (v.plays || 0) >= 100) badges.push(`<span class="badge b-blue">🎯 高完播</span>`);
    if ((v.follows || 0) >= 5) badges.push(`<span class="badge b-purple">📈 涨粉强</span>`);
    if (String(v.metrics_scope || "").startsWith("window:")) badges.push(`<span class="badge b-amber">⏱ 仅${esc(v.metrics_scope.replace("window:", ""))}</span>`);
    if (!v.has_transcript) badges.push(`<span class="badge b-amber">缺口播</span>`);

    const hy = [v.hook_context, v.hook_title].filter(Boolean).join(" / ");
    const counts = `${fmtInt(v.likes)} / ${fmtInt(v.recommends)} / ${fmtInt(v.comments)} / ${fmtInt(v.shares)} / ${fmtInt(v.saves)}`;
    return `
      <tr class="vrow ${v.id === STATE.selectedId ? "selected" : ""}" onclick="openVideo('${esc(v.id)}')">
        <td>
          <div>${esc(v.publish_date || "—")}</div>
          <div class="subline">${esc((v.published_at || "").slice(11, 16) || "")}</div>
        </td>
        <td>
          <div><strong>${esc(v.publish_title)}</strong></div>
          ${hy ? `<div class="subline">首帧：${esc(hy)}</div>` : ""}
          ${v.user_notes ? `<div class="subline" style="color:#0f5132;">备注：${esc(v.user_notes)}</div>` : ""}
          ${badges.length ? `<div class="badges-row">${badges.join("")}</div>` : ""}
        </td>
        <td>${v.output_duration_sec ? esc(Math.round(v.output_duration_sec) + "s") : "—"}</td>
        <td><span class="badge ${v.stage === "<24h" ? "b-gray" : "b-blue"}">${esc(v.stage)}</span></td>
        <td class="num"><strong>${fmtInt(v.plays)}</strong></td>
        <td class="num">${fmtPct(v.completion_rate)}</td>
        <td class="num">${fmtSec(v.avg_watch_sec)}</td>
        <td class="num">${fmtInt(v.follows)}</td>
        <td class="num" style="font-size:12px;">${esc(counts)}</td>
        <td class="num">${fmtPct(v.interaction_rate)}</td>
        <td>${v.has_transcript ? `<span class="badge b-green">✓ ${v.transcript_len}字</span>` : `<span class="badge b-gray">无</span>`}</td>
      </tr>
    `;
  }).join("");
}

function renderAll() {
  renderStatusBar();
  const filtered = getFilteredVideos();
  renderKpis(filtered);
  renderChart(filtered);
  renderPlaybook();
  renderTable();
}

async function openVideo(vid) {
  STATE.selectedId = vid;
  renderTable();
  const data = await api("/api/video/" + encodeURIComponent(vid));
  STATE.currentVideo = data;
  renderDrawer(data);
  document.getElementById("drawerBackdrop").classList.add("open");
  document.getElementById("videoDrawer").classList.add("open");
}

function closeDrawer() {
  document.getElementById("drawerBackdrop").classList.remove("open");
  document.getElementById("videoDrawer").classList.remove("open");
}

function renderDrawer(v) {
  const c = v.creative || {};
  const m = v.latest_metrics || {};
  const e = v.engagement || {};
  const rev = v.review || {};
  document.getElementById("drawerTitle").textContent = c.publish_title || v.id;
  document.getElementById("drawerSub").textContent =
    `发布：${v.publish_date || "未知"} ｜ 口径：${v.metrics_scope || "pending"} (${v.metrics_source || ""}) ｜ ID：${v.platform_id || v.id}`;

  const winRows = ["7d", "30d", "custom"].map((wk) => {
    const w = (v.window_metrics || {})[wk];
    if (!w) return "";
    const wm = w.metrics || {};
    return `<tr>
      <td><strong>${esc(wk)}</strong></td>
      <td>${esc(w.window_start)} ~ ${esc(w.window_end)}</td>
      <td>${w.covers_full_lifetime ? `<span class="badge b-green">全生命周期</span>` : `<span class="badge b-amber">仅窗口增量</span>`}</td>
      <td class="num">${fmtInt(wm.plays)}</td>
      <td class="num">${fmtPct(wm.completion_rate)}</td>
      <td class="num">${fmtInt(wm.follows)}</td>
    </tr>`;
  }).filter(Boolean).join("");

  const snapRows = (v.snapshots || []).slice().reverse().slice(0, 12).map((s) => {
    const sm = s.metrics || {};
    return `<tr>
      <td>${esc(String(s.collected_at || "").slice(0, 16).replace("T", " "))}</td>
      <td>${esc(s.source)} (${esc(s.window_type)})</td>
      <td class="num">${fmtInt(sm.plays)}</td>
      <td class="num">${fmtPct(sm.completion_rate)}</td>
      <td class="num">${fmtInt(sm.likes)}/${fmtInt(sm.comments)}/${fmtInt(sm.shares)}/${fmtInt(sm.saves)}</td>
      <td class="num">${fmtInt(sm.follows)}</td>
    </tr>`;
  }).join("");

  const commentsHtml = (v.comments || []).length
    ? v.comments.map((cm) => `<div style="padding:6px 0; border-bottom:1px solid var(--line);">
        <div class="subline"><strong>${esc(cm.author || "观众")}</strong> · ${esc(cm.time || "")}</div>
        <div>${esc(cm.text || "")}</div>
      </div>`).join("")
    : `<div class="subline">暂无已抓取评论内容（点击顶部「💬 抓评论与收藏 (ego)」可补全）</div>`;

  document.getElementById("drawerBody").innerHTML = `
    <div class="d-section">
      ${(c.hook_context || c.hook_title) ? `
        <div class="hook-box">
          ${c.hook_context ? `<div class="hook-white">${esc(c.hook_context)}</div>` : ""}
          ${c.hook_title ? `<div class="hook-yellow">${esc(c.hook_title)}</div>` : ""}
        </div>` : ""}
      ${c.hook_summary ? `<div class="subline" style="margin-bottom:6px;"><strong>主旨：</strong>${esc(c.hook_summary)}</div>` : ""}
      <div><strong>视频简介：</strong>${esc(c.publish_desc || "（无）")}</div>
      <div class="badges-row" style="margin-top:6px;">
        ${(c.publish_tags || []).map((t) => `<span class="badge b-gray">#${esc(t)}</span>`).join("")}
      </div>
    </div>

    <div class="d-section">
      <h3>核心指标</h3>
      <div class="kv-grid">
        <div class="kv"><div class="k">播放量</div><div class="v">${fmtInt(m.plays)}</div></div>
        <div class="kv"><div class="k">完播率</div><div class="v">${fmtPct(m.completion_rate)}</div></div>
        <div class="kv"><div class="k">均播时长</div><div class="v">${fmtSec(m.avg_watch_sec)}</div></div>
        <div class="kv"><div class="k">涨粉数</div><div class="v">${fmtInt(m.follows)}</div></div>
        <div class="kv"><div class="k">总互动率</div><div class="v">${fmtPct(e.interaction_rate)}</div></div>
        <div class="kv"><div class="k">推荐 / 收藏</div><div class="v">${fmtInt(m.recommends)} / ${fmtInt(m.saves)}</div></div>
        <div class="kv"><div class="k">赞 / 评 / 转</div><div class="v">${fmtInt(m.likes)} / ${fmtInt(m.comments)} / ${fmtInt(m.shares)}</div></div>
        <div class="kv"><div class="k">成片时长</div><div class="v">${c.output_duration_sec ? esc(c.output_duration_sec + "s") : "—"}</div></div>
      </div>
    </div>

    <div class="d-section">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <h3 style="margin:0;">口播全文（给 AI 看的关键上下文）</h3>
        <button class="sm" onclick="toggleTranscriptEdit()">✏️ 编辑 / 补录</button>
      </div>
      ${c.opening_3s ? `<div class="opening-pill"><strong>开头前3秒：</strong>${esc(c.opening_3s)}</div>` : ""}
      <div class="transcript-box" id="transcriptRead">${esc(c.transcript || "（暂无口播文案，点击右上角可补录）")}</div>
      <div id="transcriptEditBox" style="display:none; margin-top:8px;">
        <textarea id="editTranscriptVal" rows="6" style="width:100%; padding:8px; border:1px solid var(--line); border-radius:8px;">${esc(c.transcript || "")}</textarea>
        <div class="actions" style="margin-top:6px;">
          <button class="primary sm" onclick="saveTranscript()">保存口播文案</button>
          <button class="sm" onclick="toggleTranscriptEdit()">取消</button>
        </div>
      </div>
    </div>

    <div class="d-section">
      <h3>人工复盘标注 &amp; 备注</h3>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
        <div class="form-row">
          <label>作品状态</label>
          <select id="revStatus">
            <option value="active" ${rev.status !== "excluded" ? "selected" : ""}>正式作品（纳入复盘）</option>
            <option value="excluded" ${rev.status === "excluded" ? "selected" : ""}>排除 / 测试片（不干扰分析）</option>
          </select>
        </div>
        <div class="form-row">
          <label>重点标签</label>
          <select id="revMark">
            <option value="" ${!rev.user_mark ? "selected" : ""}>无</option>
            <option value="标杆" ${rev.user_mark === "标杆" ? "selected" : ""}>⭐ 爆款标杆</option>
            <option value="想二创" ${rev.user_mark === "想二创" ? "selected" : ""}>♻ 值得二创</option>
            <option value="踩坑" ${rev.user_mark === "踩坑" ? "selected" : ""}>💥 反面教材/踩坑</option>
          </select>
        </div>
      </div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
        <div class="form-row"><label>题材标签（逗号分隔）</label><input id="revTopics" value="${esc((rev.topic_tags || []).join(", "))}"></div>
        <div class="form-row"><label>风格标签（逗号分隔）</label><input id="revStyles" value="${esc((rev.style_tags || []).join(", "))}"></div>
      </div>
      <div class="form-row">
        <label>我的复盘备注（告诉 AI 这条为什么好或为什么差）</label>
        <textarea id="revNotes" rows="2" placeholder="例如：这条蹭到了当天热点 / 开头剪得太碎影响了完播…">${esc(rev.user_notes || "")}</textarea>
      </div>
      <button class="primary sm" onclick="saveReviewMeta()">保存标注与备注</button>
    </div>

    ${winRows ? `<div class="d-section"><h3>窗口数据对照 (7天 / 30天 / 自定义)</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>窗口</th><th>范围</th><th>口径</th><th class="num">播放</th><th class="num">完播</th><th class="num">涨粉</th></tr></thead>
        <tbody>${winRows}</tbody>
      </table></div></div>` : ""}

    <div class="d-section">
      <h3>数据快照时间线</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>抓取时间</th><th>来源</th><th class="num">播放</th><th class="num">完播</th><th class="num">赞/评/转/藏</th><th class="num">涨粉</th></tr></thead>
        <tbody>${snapRows || `<tr><td colspan="6" class="subline">暂无快照</td></tr>`}</tbody>
      </table></div>
    </div>

    <div class="d-section">
      <h3>热门评论 (${(v.comments || []).length})</h3>
      ${commentsHtml}
    </div>
  `;
}

function toggleTranscriptEdit() {
  const box = document.getElementById("transcriptEditBox");
  box.style.display = box.style.display === "none" ? "block" : "none";
}

async function saveTranscript() {
  if (!STATE.currentVideo) return;
  const txt = document.getElementById("editTranscriptVal").value;
  const res = await api("/api/video/" + encodeURIComponent(STATE.currentVideo.id), {
    method: "PATCH",
    body: JSON.stringify({ transcript: txt })
  });
  STATE.currentVideo = res.video;
  STATE.index = res.overview.index;
  renderAll();
  renderDrawer(res.video);
  showToast("口播文案已保存");
}

async function saveReviewMeta() {
  if (!STATE.currentVideo) return;
  const payload = {
    status: document.getElementById("revStatus").value,
    user_mark: document.getElementById("revMark").value,
    topic_tags: document.getElementById("revTopics").value,
    style_tags: document.getElementById("revStyles").value,
    user_notes: document.getElementById("revNotes").value
  };
  const res = await api("/api/video/" + encodeURIComponent(STATE.currentVideo.id), {
    method: "PATCH",
    body: JSON.stringify(payload)
  });
  STATE.currentVideo = res.video;
  STATE.index = res.overview.index;
  renderAll();
  renderDrawer(res.video);
  showToast("标注与备注已保存进 JSON");
}

async function setRuleStatus(rid, status) {
  const res = await api("/api/playbook/rule/" + encodeURIComponent(rid), {
    method: "PATCH",
    body: JSON.stringify({ status })
  });
  STATE.playbook = res.playbook;
  renderPlaybook();
  showToast(status === "approved" ? "已采纳规则" : (status === "rejected" ? "已划掉规则（写文案与发片将自动忽略）" : "已恢复为建议"));
}

async function addUserRule() {
  const cat = document.getElementById("newRuleCat").value;
  const text = document.getElementById("newRuleText").value.trim();
  if (!text) return;
  const res = await api("/api/playbook/rule", {
    method: "POST",
    body: JSON.stringify({ category: cat, text, status: "approved" })
  });
  STATE.playbook = res.playbook;
  document.getElementById("newRuleText").value = "";
  renderPlaybook();
  showToast("规则已添加到账号策略记忆");
}

async function setTopicStatus(tid, status) {
  const res = await api("/api/playbook/topic/" + encodeURIComponent(tid), {
    method: "PATCH",
    body: JSON.stringify({ status })
  });
  STATE.playbook = res.playbook;
  renderPlaybook();
}

async function syncLocalRuns() {
  const res = await api("/api/sync-runs", { method: "POST", body: "{}" });
  STATE.index = res.overview.index;
  STATE.playbook = res.overview.playbook;
  renderAll();
  showToast(`已同步本地发布：处理 ${res.result.runs_processed} 条，新增 ${res.result.created} 条`);
}

function openImportModal() {
  const info = STATE.detectedCsv;
  const el = document.getElementById("detectedCsvInfo");
  const btn = document.getElementById("btnImportDownloads");
  if (info) {
    el.textContent = `检测到 ${info.filename}（修改于 ${info.modified_at.slice(0, 16).replace("T", " ")}，${Math.round(info.size / 1024)} KB）`;
    btn.disabled = false;
  } else {
    el.textContent = "未在 Downloads 目录检测到 视频号动态数据明细*.csv";
    btn.disabled = true;
  }
  const hist = (STATE.imports || []).slice(0, 5);
  document.getElementById("importHistoryList").innerHTML = hist.length
    ? hist.map((h) => `<div>• ${esc(h.imported_at.slice(0, 16).replace("T", " "))} · <strong>${esc(h.window_type)}</strong> (${esc(h.window_start)}~${esc(h.window_end)}) · ${h.row_count} 行（更新全周期 ${h.updated_lifetime_count}，仅窗口 ${h.window_only_count}）</div>`).join("")
    : "暂无导入记录";
  document.getElementById("importModal").classList.add("open");
}
function closeImportModal() { document.getElementById("importModal").classList.remove("open"); }
function toggleCustomDates() {
  document.getElementById("customDatesRow").style.display =
    document.getElementById("impWindow").value === "custom" ? "grid" : "none";
}

function getWindowPayload() {
  const w = document.getElementById("impWindow").value;
  return {
    window_type: w,
    window_start: w === "custom" ? (document.getElementById("impStart").value || null) : null,
    window_end: w === "custom" ? (document.getElementById("impEnd").value || null) : null
  };
}

async function importFromDownloads() {
  try {
    const res = await api("/api/import-csv", {
      method: "POST",
      body: JSON.stringify({ mode: "downloads", ...getWindowPayload() })
    });
    STATE.index = res.overview.index;
    STATE.imports = res.overview.imports;
    STATE.detectedCsv = res.overview.detected_downloads_csv;
    renderAll();
    closeImportModal();
    showToast(`导入成功：${res.result.row_count} 行（新增 ${res.result.new_count}，匹配 ${res.result.matched_count}）`);
  } catch (e) {
    alert("导入失败：" + e.message);
  }
}

async function handleFilePick(file) {
  if (!file) return;
  const text = await file.text();
  try {
    const res = await api("/api/import-csv", {
      method: "POST",
      body: JSON.stringify({ mode: "upload", filename: file.name, csv_text: text, ...getWindowPayload() })
    });
    STATE.index = res.overview.index;
    STATE.imports = res.overview.imports;
    renderAll();
    closeImportModal();
    showToast(`导入 ${file.name} 成功：${res.result.row_count} 行`);
  } catch (e) {
    alert("导入失败：" + e.message);
  }
}

async function startJob(kind) {
  const banner = document.getElementById("jobBanner");
  banner.className = "banner show";
  document.getElementById("jobText").textContent =
    kind === "analyze" ? "正在调用 AI 进行分阶段复盘诊断，请稍候…" : "正在启动 ego 浏览器采集最新评论与收藏…";
  const res = await api("/api/jobs/" + kind, { method: "POST", body: "{}" });
  STATE.activeJobId = res.job_id;
  pollJob(res.job_id);
}

async function pollJob(jobId) {
  try {
    const j = await api("/api/jobs/" + encodeURIComponent(jobId));
    if (j.status === "running") {
      const tail = (j.logs || []).slice(-1)[0];
      if (tail) document.getElementById("jobText").textContent = "执行中：" + tail;
      setTimeout(() => pollJob(jobId), 1500);
      return;
    }
    const banner = document.getElementById("jobBanner");
    if (j.status === "done") {
      banner.className = "banner show ok";
      document.getElementById("jobText").textContent =
        j.type === "analyze" ? "✅ AI 深度复盘已完成，策略记忆已更新。" : "✅ 数据与评论抓取完成并已写入 history/。";
      await loadOverview();
    } else {
      banner.className = "banner show err";
      document.getElementById("jobText").textContent = "❌ 任务失败：" + (j.error || "未知错误");
    }
  } catch (e) {
    document.getElementById("jobText").textContent = "任务轮询出错：" + e.message;
  }
}
function hideBanner() { document.getElementById("jobBanner").classList.remove("show"); }

async function copyContextBrief(videoIds = null) {
  const ids = videoIds || getFilteredVideos().slice(0, 12).map((v) => v.id);
  const res = await api("/api/export-context", {
    method: "POST",
    body: JSON.stringify({ video_ids: ids })
  });
  await navigator.clipboard.writeText(res.markdown);
  showToast(`已复制 ${res.count} 条视频的完整复盘上下文，可直接发给 AI`);
}

async function copySingleContext() {
  if (!STATE.currentVideo) return;
  await copyContextBrief([STATE.currentVideo.id]);
}

const dz = document.getElementById("dropZone");
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", (e) => {
  e.preventDefault();
  dz.classList.remove("drag");
  if (e.dataTransfer.files && e.dataTransfer.files[0]) handleFilePick(e.dataTransfer.files[0]);
});

loadOverview().catch((e) => showToast("加载失败：" + e.message));
</script>
</body>
</html>
"""


class WorkbenchState:
    def __init__(
        self,
        *,
        history_dir: Path,
        runs_dir: Path,
        downloads_dir: Path,
        root_dir: Path,
    ) -> None:
        self.history_dir = Path(history_dir)
        self.runs_dir = Path(runs_dir)
        self.downloads_dir = Path(downloads_dir)
        self.root_dir = Path(root_dir)
        self.lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}

    def detect_downloads_csv(self) -> dict[str, Any] | None:
        if not self.downloads_dir.is_dir():
            return None
        matches = sorted(
            self.downloads_dir.glob("视频号动态数据明细*.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not matches:
            return None
        p = matches[0]
        st = p.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=history_db.SHANGHAI_TZ).isoformat()
        return {
            "path": str(p),
            "filename": p.name,
            "size": st.st_size,
            "modified_at": mtime,
        }

    def overview_payload(self) -> dict[str, Any]:
        idx_path = self.history_dir / "index.json"
        if idx_path.is_file():
            try:
                index = history_db._read_json(idx_path)
            except (OSError, ValueError):
                index = history_db.rebuild_index(self.history_dir)
        else:
            index = history_db.rebuild_index(self.history_dir)

        playbook = history_db.load_playbook(self.history_dir)
        imports_path = self.history_dir / "imports.json"
        imports = []
        if imports_path.is_file():
            try:
                loaded = history_db._read_json(imports_path)
                if isinstance(loaded, list):
                    imports = loaded
            except (OSError, ValueError):
                imports = []

        return {
            "index": index,
            "playbook": playbook,
            "imports": imports,
            "detected_downloads_csv": self.detect_downloads_csv(),
        }

    def export_context_markdown(self, video_ids: list[str] | None = None) -> tuple[str, int]:
        pb = history_db.load_playbook(self.history_dir)
        idx = history_db.rebuild_index(self.history_dir)
        all_videos = {v["id"]: v for v in history_db.load_all_videos(self.history_dir)}

        selected: list[dict[str, Any]] = []
        if video_ids:
            for vid in video_ids:
                if vid in all_videos:
                    selected.append(all_videos[vid])
        else:
            for item in (idx.get("videos") or [])[:12]:
                if item.get("status") != "excluded" and item["id"] in all_videos:
                    selected.append(all_videos[item["id"]])

        lines = [
            "# 视频号复盘上下文快照",
            "",
            f"- 有效视频：{idx['stats_summary']['count']} 条 ｜ 累计播放：{idx['stats_summary']['total_plays']} ｜ 中位播放：{idx['stats_summary']['median_plays']} ｜ 累计涨粉：{idx['stats_summary']['total_follows']}",
            "",
            "## 当前有效策略与红线（未包含已划掉规则）",
            "",
        ]
        active_rules = [r for r in (pb.get("rules") or []) if r.get("status") != "rejected"]
        for r in active_rules[:20]:
            tag = "已采纳" if r.get("status") == "approved" else "AI建议"
            lines.append(f"- [{tag}·{r.get('category')}] {r.get('text')}")
        if not active_rules:
            lines.append("- （暂无规则）")

        lines += ["", f"## 选中视频详情（共 {len(selected)} 条）", ""]
        for v in selected:
            c = v.get("creative") or {}
            m = v.get("latest_metrics") or {}
            e = v.get("engagement") or {}
            rev = v.get("review") or {}
            comp = f"{m['completion_rate'] * 100:.2f}%" if m.get("completion_rate") is not None else "—"
            ir = f"{e['interaction_rate'] * 100:.2f}%" if e.get("interaction_rate") is not None else "—"
            lines += [
                f"### 《{c.get('publish_title') or v['id']}》（{v.get('publish_date') or '未知'} · {v.get('metrics_scope')}）",
                f"- 播放：{m.get('plays')} ｜ 完播率：{comp} ｜ 均播：{m.get('avg_watch_sec')}s ｜ 涨粉：{m.get('follows')} ｜ 赞/推/评/转/藏：{m.get('likes')}/{m.get('recommends')}/{m.get('comments')}/{m.get('shares')}/{m.get('saves')} ｜ 互动率：{ir}",
                f"- 首帧白字：{c.get('hook_context') or '—'} ｜ 首帧黄字：{c.get('hook_title') or '—'}",
                f"- 简介：{c.get('publish_desc') or '—'}",
            ]
            if rev.get("user_mark") or rev.get("user_notes"):
                lines.append(f"- 人工标注：{rev.get('user_mark') or '—'} ｜ 备注：{rev.get('user_notes') or '—'}")
            if c.get("transcript"):
                lines.append(f"- 口播全文：{c['transcript']}")
            if v.get("comments"):
                top_c = " / ".join(f"{cm.get('author', '观众')}: {cm.get('text', '')}" for cm in v["comments"][:5])
                lines.append(f"- 热门评论：{top_c}")
            lines.append("")
        return "\n".join(lines), len(selected)

    def start_job(self, kind: str, use_ai: bool = True) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        job = {"job_id": job_id, "type": kind, "status": "running", "logs": [], "result": None, "error": None}
        with self.lock:
            self.jobs[job_id] = job

        def _worker() -> None:
            try:
                if kind == "analyze":
                    job["logs"].append("正在读取 history/ 并执行分阶段归因分析…")
                    res = retro_analyze.analyze_history(self.history_dir, use_ai=use_ai)
                    job["result"] = {"summary": res["summary"], "source": res["analysis"].get("source")}
                elif kind == "collect-ego":
                    script = self.root_dir / "scripts" / "review-all-sph.ps1"
                    if not script.is_file():
                        raise FileNotFoundError(f"缺少脚本：{script}")
                    job["logs"].append("正在启动 PowerShell + ego 抓取作品评论与收藏…")
                    proc = subprocess.Popen(
                        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-NoOpen"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        cwd=str(self.root_dir),
                    )
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        t = line.strip()
                        if t:
                            job["logs"].append(t)
                            if len(job["logs"]) > 200:
                                job["logs"] = job["logs"][-200:]
                    rc = proc.wait()
                    if rc != 0:
                        raise RuntimeError(f"ego 采集脚本退出码 {rc}")
                    job["result"] = history_db.sync_publish_runs(
                        self.runs_dir, history_dir=self.history_dir, root=self.root_dir
                    )
                else:
                    raise ValueError(f"未知任务类型：{kind}")
                job["status"] = "done"
            except Exception as exc:
                job["error"] = str(exc)
                job["status"] = "error"

        threading.Thread(target=_worker, daemon=True).start()
        return job


def make_handler(state: WorkbenchState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, code: int, payload: Any) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            if length > 20 * 1024 * 1024:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length).decode("utf-8")
            if not raw.strip():
                return {}
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return obj

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/":
                    self._send_html(WORKBENCH_HTML)
                    return
                if path == "/api/overview":
                    self._send_json(HTTPStatus.OK, state.overview_payload())
                    return
                if path.startswith("/api/video/"):
                    vid = unquote(path[len("/api/video/"):])
                    video = history_db.load_video(state.history_dir, vid)
                    if video is None:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"找不到视频：{vid}"})
                        return
                    self._send_json(HTTPStatus.OK, video)
                    return
                if path.startswith("/api/jobs/"):
                    jid = unquote(path[len("/api/jobs/"):])
                    job = state.jobs.get(jid)
                    if job is None:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"找不到任务：{jid}"})
                        return
                    self._send_json(HTTPStatus.OK, job)
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def do_PATCH(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                body = self._read_json_body()
                if path.startswith("/api/video/"):
                    vid = unquote(path[len("/api/video/"):])
                    video = history_db.update_video_review(state.history_dir, vid, body)
                    self._send_json(HTTPStatus.OK, {"video": video, "overview": state.overview_payload()})
                    return
                if path.startswith("/api/playbook/rule/"):
                    rid = unquote(path[len("/api/playbook/rule/"):])
                    pb = history_db.update_playbook_rule(
                        state.history_dir, rid, status=body.get("status"), text=body.get("text")
                    )
                    self._send_json(HTTPStatus.OK, {"playbook": pb})
                    return
                if path.startswith("/api/playbook/topic/"):
                    tid = unquote(path[len("/api/playbook/topic/"):])
                    pb = history_db.update_topic_status(state.history_dir, tid, str(body.get("status") or ""))
                    self._send_json(HTTPStatus.OK, {"playbook": pb})
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except FileNotFoundError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                body = self._read_json_body()
                if path == "/api/import-csv":
                    mode = body.get("mode") or "upload"
                    win = str(body.get("window_type") or "auto")
                    w_start = body.get("window_start") or None
                    w_end = body.get("window_end") or None
                    if mode == "downloads":
                        det = state.detect_downloads_csv()
                        if not det:
                            raise FileNotFoundError("Downloads 目录没有找到 视频号动态数据明细*.csv")
                        res = history_db.import_csv(
                            Path(det["path"]),
                            history_dir=state.history_dir,
                            window_type=win,
                            window_start=w_start,
                            window_end=w_end,
                        )
                    else:
                        csv_text = body.get("csv_text")
                        if not isinstance(csv_text, str) or not csv_text.strip():
                            raise ValueError("缺少 CSV 内容")
                        res = history_db.import_csv(
                            body.get("filename") or "upload.csv",
                            history_dir=state.history_dir,
                            window_type=win,
                            window_start=w_start,
                            window_end=w_end,
                            csv_text=csv_text,
                            filename=str(body.get("filename") or "upload.csv"),
                        )
                    self._send_json(HTTPStatus.OK, {"result": res, "overview": state.overview_payload()})
                    return

                if path == "/api/sync-runs":
                    res = history_db.sync_publish_runs(
                        state.runs_dir, history_dir=state.history_dir, root=state.root_dir
                    )
                    self._send_json(HTTPStatus.OK, {"result": res, "overview": state.overview_payload()})
                    return

                if path == "/api/playbook/rule":
                    pb = history_db.add_playbook_rule(
                        state.history_dir,
                        category=str(body.get("category") or "copywriting"),
                        text=str(body.get("text") or ""),
                        status=str(body.get("status") or "approved"),
                    )
                    self._send_json(HTTPStatus.OK, {"playbook": pb})
                    return

                if path == "/api/export-context":
                    ids = body.get("video_ids")
                    md, cnt = state.export_context_markdown(ids if isinstance(ids, list) else None)
                    self._send_json(HTTPStatus.OK, {"markdown": md, "count": cnt})
                    return

                if path == "/api/jobs/analyze":
                    job = state.start_job("analyze", use_ai=bool(body.get("use_ai", True)))
                    self._send_json(HTTPStatus.ACCEPTED, job)
                    return

                if path == "/api/jobs/collect-ego":
                    job = state.start_job("collect-ego")
                    self._send_json(HTTPStatus.ACCEPTED, job)
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except FileNotFoundError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    return Handler


def create_server(
    *,
    history_dir: Path | None = None,
    runs_dir: Path | None = None,
    downloads_dir: Path | None = None,
    root_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    root = Path(root_dir) if root_dir is not None else history_db.DEFAULT_ROOT
    hdir = Path(history_dir) if history_dir is not None else (root / "history")
    rdir = Path(runs_dir) if runs_dir is not None else (root / "publish-runs")
    ddir = Path(downloads_dir) if downloads_dir is not None else (Path.home() / "Downloads")
    history_db._ensure_history_layout(hdir)
    if not (hdir / "index.json").is_file():
        history_db.rebuild_index(hdir)

    state = WorkbenchState(history_dir=hdir, runs_dir=rdir, downloads_dir=ddir, root_dir=root)
    handler = make_handler(state)

    candidates = [port] + (list(range(port + 1, port + 20)) + [0] if port != 0 else [])
    last_err: OSError | None = None
    for p in candidates:
        try:
            server = ThreadingHTTPServer((host, p), handler)
            server.daemon_threads = True
            return server
        except OSError as exc:
            last_err = exc
    assert last_err is not None
    raise last_err


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--history-dir", type=Path, default=None)
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--downloads-dir", type=Path, default=None)
    parser.add_argument("--open", action="store_true", help="启动后在默认浏览器打开工作台")
    args = parser.parse_args(argv)

    server = create_server(
        history_dir=args.history_dir,
        runs_dir=args.runs_dir,
        downloads_dir=args.downloads_dir,
        host=args.host,
        port=args.port,
    )
    actual_port = server.server_address[1]
    url = f"http://{args.host}:{actual_port}/"
    print(f"视频号复盘工作台已启动：{url}", flush=True)
    print("提示：保持本窗口开启即可交互操作；按 Ctrl+C 关闭工作台。", flush=True)
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n工作台已关闭。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
