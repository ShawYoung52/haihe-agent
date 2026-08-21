// =====================================================================
// 系统时间切换面板 — AgentWeb（海河流域数字预报员）适配版
// 2026-08-21。切换智能体的"当前时间"（全局"现在"锚定）：
//   - 输入 年-月-日 时:分（如 2026-07-10 15:00），点"设置" → 全局按该时刻回答；
//   - 点"恢复" → 还原真实时间；
//   - 收起态胶囊上常显"模拟中/真实"小标，防忘记恢复；点胶囊展开/收起完整控件。
// 后端：调 8003 的 /api/v1/admin/system-time（与 /qa/ask 同机同端口不同服务）。
// 挂载位置（2026-08-21 用户要求）：锚在页面"说明"元素【左侧、垂直居中】；找不到回退右下角悬浮。
// 比例（2026-08-21 用户反馈"比例有问题"）：面板字号/宽度在运行时按"说明"元素的
//   computed font-size 等比推导（em 内距），不写死像素——说明大则面板大、说明小则面板小。
//   默认收起为小胶囊（避免大卡片压住说明旁），点胶囊展开完整控件。
// 兼容性：无可选链/无箭头函数/用 XMLHttpRequest，兼容内网旧浏览器。
// 部署：本文件放 webapps/AgentWeb/sim-time-agentweb.js（webapp 根级，与 img-zoom-agentweb.js 同位置，
//       index.html 加一行 <script src="./sim-time-agentweb.js"></script>；2026-08-21 用户确认放根级，
//       无需 public/ 子目录）。无需重启 Tomcat。
// =====================================================================
(function () {
  var PREFIX = "[SIM_TIME_AW]";
  // 后端 base：AgentWeb 与 8003 同主机不同端口，从 location.hostname 推导，不写死内网 IP。
  var API_BASE = location.protocol + "//" + location.hostname + ":8003";
  var SET_URL = API_BASE + "/api/v1/admin/system-time";
  var CLEAR_URL = SET_URL + "/clear";
  var GET_URL = SET_URL;

  function clamp(n, lo, hi) { return n < lo ? lo : (n > hi ? hi : n); }

  function buildPanel() {
    if (document.getElementById("simTimePanel")) return document.getElementById("simTimePanel");

    var panel = document.createElement("div");
    panel.id = "simTimePanel";
    // 默认右下角悬浮（被 placePanel 覆盖）。字号/宽度由 scalePanelToAnchor 按"说明"推导；
    // 内距用 em，整体随字号等比缩放。默认收起（body 隐藏），呈小胶囊。
    panel.style.cssText = [
      "position:fixed;right:14px;bottom:14px;z-index:99998;",
      "width:180px;font-size:13px;line-height:1.4;color:#333;",
      "background:#fff;border:1px solid #c8d2e0;border-radius:0.6em;",
      "box-shadow:0 0.2em 0.9em rgba(0,0,0,.16);font-family:sans-serif;",
      "overflow:hidden;",
    ].join("");

    // —— 胶囊头（常显）：🕒 系统时间 + 状态小标；点击展开/收起 ——
    var chip = document.createElement("div");
    chip.id = "simTimeChip";
    chip.style.cssText = [
      "display:flex;align-items:center;justify-content:space-between;gap:0.5em;",
      "padding:0.32em 0.6em;font-weight:bold;color:#1f3a63;cursor:pointer;",
      "background:#eef3fa;user-select:none;white-space:nowrap;",
    ].join("");
    chip.title = "点击展开/收起系统时间设置";

    var chipLabel = document.createElement("span");
    chipLabel.textContent = "🕒 系统时间";

    var chipState = document.createElement("span");
    chipState.id = "simTimeChipState";
    chipState.style.cssText = "font-weight:normal;font-size:0.85em;color:#2e7d32;";
    chipState.textContent = "真实";

    chip.appendChild(chipLabel);
    chip.appendChild(chipState);
    chip.addEventListener("click", function () {
      var body = document.getElementById("simTimeBody");
      if (!body) return;
      body.style.display = body.style.display === "none" ? "block" : "none";
    });

    // —— 展开体（默认收起）——
    var body = document.createElement("div");
    body.id = "simTimeBody";
    body.style.cssText = "display:none;padding:0.5em 0.6em 0.6em;";

    var input = document.createElement("input");
    input.id = "simTimeInput";
    input.type = "text";
    input.placeholder = "2026-07-10 15:00";
    input.style.cssText = [
      "width:100%;box-sizing:border-box;padding:0.3em 0.45em;font-size:0.95em;",
      "border:1px solid #b9c4d6;border-radius:0.3em;",
    ].join("");

    var row = document.createElement("div");
    row.style.cssText = "margin-top:0.45em;display:flex;gap:0.4em;";
    var setBtn = document.createElement("button");
    setBtn.textContent = "设置";
    setBtn.style.cssText = "flex:1;padding:0.28em 0;font-size:0.9em;cursor:pointer;border:1px solid #2f6bb0;border-radius:0.3em;background:#2f6bb0;color:#fff;";
    var clearBtn = document.createElement("button");
    clearBtn.textContent = "恢复";
    clearBtn.style.cssText = "flex:1;padding:0.28em 0;font-size:0.9em;cursor:pointer;border:1px solid #b9c4d6;border-radius:0.3em;background:#fff;color:#333;";

    var status = document.createElement("div");
    status.id = "simTimeStatus";
    status.style.cssText = "margin-top:0.45em;font-size:0.82em;color:#666;word-break:break-all;";

    row.appendChild(setBtn);
    row.appendChild(clearBtn);
    body.appendChild(input);
    body.appendChild(row);
    body.appendChild(status);
    panel.appendChild(chip);
    panel.appendChild(body);
    document.body.appendChild(panel);

    setBtn.addEventListener("click", function () {
      var v = (input.value || "").trim();
      if (!v) { setStatus("请输入时间，如 2026-07-10 15:00", "#c0392b"); return; }
      apiPost(SET_URL, { datetime: v }, function (data) {
        var display = data && data.data && data.data.display ? data.data.display : v;
        setStatus("已切换为模拟时间：" + display, "#2e7d32");
        input.value = display.slice(0, 16);
        refreshStatus();
      }, function (detail) {
        setStatus("设置失败：" + detail, "#c0392b");
      });
    });

    clearBtn.addEventListener("click", function () {
      apiPost(CLEAR_URL, {}, function () {
        setStatus("已恢复真实时间", "#2e7d32");
        refreshStatus();
      }, function (detail) {
        setStatus("恢复失败：" + detail, "#c0392b");
      });
    });

    return panel;
  }

  // 找页面里含"说明"的最紧凑可见元素作锚点（标题/标签类，文本短）；
  // textContent 长度过滤排除整页容器，取含"说明"的最短文本元素。
  function findShuomingAnchor() {
    var best = null;
    var bestLen = 9999;
    var nodes = document.querySelectorAll("h1,h2,h3,h4,h5,h6,a,button,div,span,p,li,label,b,strong");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var t = (el.textContent || "").replace(/\s+/g, "");
      if (t.indexOf("说明") < 0 || t.length > 40) continue;
      var r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue; // 不可见跳过
      if (t.length < bestLen) { bestLen = t.length; best = el; }
    }
    return best;
  }

  // 比例自适应：读取"说明"元素的 computed font-size，据此推导面板字号与宽度（内距随字号 em 缩放）。
  // 说明大 → 面板大；说明小 → 面板小。无需硬编码像素，适配任何真实页面上的"说明"。
  function scalePanelToAnchor(panel, anchor) {
    var fontSize = 13;
    try {
      var cs = window.getComputedStyle(anchor);
      var fs = parseFloat(cs.fontSize);
      if (isFinite(fs) && fs > 0) fontSize = fs;
    } catch (e) { /* 保持默认 */ }
    fontSize = clamp(fontSize, 12, 16);
    panel.style.fontSize = fontSize + "px";
    // 宽度随字号等比：约 13 个字号宽，钳 150~220px，保证输入框可用又不压住说明。
    var width = Math.round(clamp(fontSize * 13, 150, 220));
    panel.style.width = width + "px";
    return width;
  }

  // 面板直挂 document.body（在 Vue root 之外，不被 Vue 重渲染清掉）。
  // 定位到"说明"【左侧、垂直居中】：面板右缘离说明左缘一个间隙；顶部按胶囊高度对说明垂直居中。
  function placePanel() {
    var panel = document.getElementById("simTimePanel");
    if (!panel) return false;
    var anchor = findShuomingAnchor();
    if (anchor && anchor.parentNode) {
      var r = anchor.getBoundingClientRect();
      if (r.width >= 1 && r.height >= 1) {
        var width = scalePanelToAnchor(panel, anchor);
        panel.style.position = "absolute";
        panel.style.right = "auto";
        panel.style.bottom = "auto";
        // 左侧：面板右缘 = 说明左缘 - 间隙；防左溢出。
        var gap = Math.round(clamp(parseFloat(panel.style.fontSize) || 13, 12, 16) * 0.8);
        var left = window.pageXOffset + r.left - width - gap;
        if (left < 8) left = 8;
        // 垂直居中：以胶囊（chip）高度对说明垂直中心对齐，展开体向下延伸不影响锚点。
        var chip = document.getElementById("simTimeChip");
        var chipH = chip && chip.offsetHeight ? chip.offsetHeight : Math.round((parseFloat(panel.style.fontSize) || 13) * 2);
        var top = window.pageYOffset + r.top + (r.height - chipH) / 2;
        if (top < 8) top = 8;
        panel.style.left = left + "px";
        panel.style.top = top + "px";
        return true;
      }
    }
    // 回退：右下角悬浮（恢复默认比例，无"说明"可缩放）
    panel.style.position = "fixed";
    panel.style.right = "14px";
    panel.style.bottom = "14px";
    panel.style.left = "auto";
    panel.style.top = "auto";
    return false;
  }

  function setStatus(text, color) {
    var el = document.getElementById("simTimeStatus");
    if (!el) return;
    el.style.color = color || "#666";
    el.textContent = text;
  }

  // 胶囊上的迷你状态：模拟中=橙、真实=绿、后端不可达=红。
  function setChipState(text, color) {
    var el = document.getElementById("simTimeChipState");
    if (!el) return;
    el.style.color = color || "#2e7d32";
    el.textContent = text;
  }

  function apiPost(url, body, ok, fail) {
    var xhr = new XMLHttpRequest();
    xhr.open("POST", url, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status >= 200 && xhr.status < 300) {
        var parsed = safeParse(xhr.responseText);
        ok(parsed || {});
      } else {
        var detail = xhr.responseText || "";
        var p = safeParse(detail);
        fail((p && p.detail) ? p.detail : ("HTTP " + xhr.status));
      }
    };
    xhr.send(JSON.stringify(body || {}));
  }

  function apiGet(ok, fail) {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", GET_URL, true);
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status >= 200 && xhr.status < 300) {
        var parsed = safeParse(xhr.responseText);
        ok((parsed && parsed.data) || {});
      } else {
        fail(xhr.status);
      }
    };
    xhr.send();
  }

  function safeParse(s) {
    try { return JSON.parse(s); } catch (e) { return null; }
  }

  function refreshStatus() {
    apiGet(function (data) {
      var input = document.getElementById("simTimeInput");
      if (data && data.active && data.override_datetime) {
        var dt = data.override_datetime.replace("T", " ").slice(0, 16);
        setStatus("模拟中：" + dt + "（点恢复还原）", "#b7791f");
        setChipState("模拟中", "#b7791f");
        if (input && !input.value.trim()) input.value = dt;
      } else {
        setStatus("当前：真实时间", "#2e7d32");
        setChipState("真实", "#2e7d32");
      }
    }, function (status) {
      setStatus("后端不可达（HTTP " + status + "）", "#c0392b");
      setChipState("离线", "#c0392b");
    });
  }

  function init() {
    buildPanel();
    // Vue 渲染是异步的：先尝试锚到"说明"左侧，找不到则延时重试（页面渐进加载），最后回退悬浮角。
    var tries = 0;
    function attempt() {
      var ok = placePanel();
      tries++;
      if (!ok && tries < 12) { setTimeout(attempt, 800); return; }
      if (ok) {
        window.addEventListener("resize", placePanel);
        console.log(PREFIX + " 面板已锚定到'说明'元素左侧（比例随说明字号自适应）");
      } else {
        console.log(PREFIX + " 未找到'说明'元素，面板按右下角悬浮显示");
      }
    }
    attempt();
    refreshStatus();
    console.log(PREFIX + " 系统时间切换面板已启用");
  }

  // 本文件被 index.html 的 <head> 里同步 <script> 引用，须等 DOMContentLoaded。
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
