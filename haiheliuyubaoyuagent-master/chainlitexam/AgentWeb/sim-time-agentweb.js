// =====================================================================
// 系统时间切换面板 — AgentWeb（海河流域数字预报员）适配版
// 2026-08-21 首版；2026-09-20 改为「请求级锚定」。
//
// 做什么：把"现在"锚定到指定 年-月-日 时:分（如 2026-07-10 15:00），
//   - 点"设置" → 只有**这个浏览器**后续的提问按该时刻回答；
//   - 点"恢复" → 回到真实时间；
//   - 收起态胶囊常显"模拟中/真实"小标，防忘记恢复。
//
// 为什么要改成请求级（2026-09-20）：旧版调 POST /admin/system-time {"datetime":...}
//   写的是**服务端全局覆盖文件**——一人切时间，所有用户（含天河小程序调 /qa/ask）
//   一起变，而且恢复没生效就会一直锚在过去。现在锚点由本面板存在浏览器里、
//   逐请求随 user_message 的 metadata 带上，服务端不落任何跨用户状态。
//
// 三个关键点（缺一不可）：
//   1) 锚点存 localStorage["haihe_reference_time"]（"real" 或 ISO 串）；
//   2) 本文件用 WebSocket.send 钩子把它塞进 client_message 的 metadata；
//   3) 面板**不再**调旧式 {"datetime":...}，改调无状态的 metadata 契约。
//   第 3 点最容易漏：只要还在写全局文件，别的客户端就仍会被一起改时间。
//
// 为什么钩 WebSocket 而不是改 bundle：assets/index-*.js 是打包产物，前端同事
//   重建后会整体覆盖它（见 deploy_agentweb/DEPLOY-sim-time.md）；本文件是
//   "重建后必须回拷"的两个自定义 JS 之一，改动能活下来。
//
// 兼容性：无可选链/无箭头函数/Object.assign/用 XMLHttpRequest，兼容内网旧浏览器。
// 部署：放 webapps/AgentWeb/sim-time-agentweb.js（webapp 根级，与 img-zoom-agentweb.js 同位置）；
//       index.html 的 <head> 里 <script src="./sim-time-agentweb.js"></script>。无需重启 Tomcat。
// =====================================================================
(function () {
  var PREFIX = "[SIM_TIME_AW]";
  // 后端 base：AgentWeb 与 8003 同主机不同端口，从 location.hostname 推导，不写死内网 IP。
  var API_BASE = location.protocol + "//" + location.hostname + ":8003";
  var SET_URL = API_BASE + "/api/v1/admin/system-time";

  // 浏览器侧锚点：ISO 串 = 锚定到该时刻；"real" = 显式要求真实时间（压过服务端
  // 可能遗留的全局覆盖文件）；键不存在 = 本浏览器没设过，走服务端全局兜底。
  var LS_KEY = "haihe_reference_time";

  // ---------------------------------------------------------------- 锚点存取

  function readAnchor() {
    try {
      var v = localStorage.getItem(LS_KEY);
      if (!v) return null;
      v = String(v).replace(/^\s+|\s+$/g, "");
      if (!v) return null;
      if (v.toLowerCase() === "real") return { time_mode: "real" };
      return { time_mode: "fixed", reference_time: v };
    } catch (e) {
      return null; // 隐私模式/被禁用：当作没设过，绝不影响正常问答
    }
  }

  function writeAnchor(value) {
    try {
      if (value) localStorage.setItem(LS_KEY, value);
      else localStorage.removeItem(LS_KEY);
      return true;
    } catch (e) {
      return false;
    }
  }

  // ---------------------------------------------------------------- 请求注入

  // socket.io over Engine.IO 的客户端事件帧形如：42["client_message",{...}]
  // （42 后可能跟 ack id 数字）。只认这一种事件，其余一律原样放行。
  var CLIENT_EVENT = "client_message";

  function patchFrame(frame) {
    if (typeof frame !== "string" || frame.length < 3) return null;
    if (frame.charAt(0) !== "4" || frame.charAt(1) !== "2") return null; // 只要 socket.io EVENT
    var rest = frame.slice(2);
    var i = 0;
    while (i < rest.length && rest.charAt(i) >= "0" && rest.charAt(i) <= "9") i++; // 跳 ack id
    var payloadText = rest.slice(i);
    if (payloadText.charAt(0) !== "[") return null;

    // 原样保留 "42" + ack id 前缀：ack id 被吞掉会让该次 emit 的回调对不上号。
    var prefix = frame.slice(0, 2 + i);

    var arr;
    try {
      arr = JSON.parse(payloadText);
    } catch (e) {
      return null; // 不是我们能理解的帧：原样放行
    }
    if (!arr || arr.length < 2 || arr[0] !== CLIENT_EVENT) return null;

    var anchor = readAnchor();
    if (!anchor) return null; // 没设锚点 → 一字节不改，行为与改造前逐字一致

    var envelope = arr[1];
    if (!envelope || typeof envelope !== "object") return null;
    var msg = envelope.message;
    if (!msg || typeof msg !== "object") return null;

    // 前端自己已经带了锚点时尊重它（谁带谁优先），避免两处打架。
    var md = msg.metadata;
    if (!md || typeof md !== "object") md = {};
    if (md.time_mode || md.reference_time) return null;

    var merged = {};
    for (var k in md) {
      if (Object.prototype.hasOwnProperty.call(md, k)) merged[k] = md[k];
    }
    for (var k2 in anchor) {
      if (Object.prototype.hasOwnProperty.call(anchor, k2)) merged[k2] = anchor[k2];
    }
    msg.metadata = merged;

    try {
      return prefix + JSON.stringify(arr);
    } catch (e) {
      return null;
    }
  }

  var injectedOnce = false;

  function installAnchorInjector() {
    if (typeof WebSocket === "undefined" || !WebSocket.prototype) return;
    if (WebSocket.prototype.__haiheAnchorPatched) return;
    var origSend = WebSocket.prototype.send;
    WebSocket.prototype.send = function (data) {
      var next = data;
      try {
        var patched = patchFrame(data);
        if (patched !== null) {
          next = patched;
          if (!injectedOnce) {
            injectedOnce = true;
            console.log(PREFIX + " 锚点已随请求下发（后续提问按锚定时间回答）");
          }
        }
      } catch (e) {
        next = data; // 注入失败绝不阻断原始发送
      }
      return origSend.call(this, next);
    };
    WebSocket.prototype.__haiheAnchorPatched = true;
    console.log(PREFIX + " WebSocket 锚点注入钩子已安装");
  }

  // 现场排查用：控制台执行 __haiheSimTime.selfTest() 可看到各分支结果。
  // 也便于在服务器上不开面板就确认脚本已生效。
  window.__haiheSimTime = {
    readAnchor: readAnchor,
    patchFrame: patchFrame,
    selfTest: function () {
      var probe = '42["client_message",{"message":{"id":"x","output":"hi","metadata":{"location":"L"}},"fileReferences":[]}]';
      return { anchor: readAnchor(), patched: patchFrame(probe) };
    },
  };

  // 必须在任何 socket 发出第一帧之前装好，所以放在 IIFE 同步执行（不等 DOMContentLoaded）。
  installAnchorInjector();

  // ---------------------------------------------------------------- 面板 UI

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
      var v = (input.value || "").replace(/^\s+|\s+$/g, "");
      if (!v) { setStatus("请输入时间，如 2026-07-10 15:00", "#c0392b"); return; }
      // 交给服务端解析并回显规范化后的时刻——日期口径只有一份实现，
      // 不在 JS 里重复一遍（仅日期形式还要取真实时分）。
      apiPost(SET_URL, { metadata: { location: location.href, time_mode: "fixed", reference_time: v } },
        function (data) {
          var anchor = (data && data.data && data.data.override_datetime) || v;
          var display = (data && data.data && data.data.display) || v;
          if (!writeAnchor(anchor)) {
            setStatus("浏览器本地存储不可用，无法只对本机生效", "#c0392b");
            return;
          }
          setStatus("已切换为模拟时间：" + display + "（仅本浏览器生效）", "#2e7d32");
          input.value = display.slice(0, 16);
          refreshStatus();
        },
        function (detail) {
          setStatus("设置失败：" + detail, "#c0392b");
        });
    });

    clearBtn.addEventListener("click", function () {
      // 先落本地（一定成功），再让服务端把可能遗留的**全局**覆盖文件清掉。
      // 旧版把锚点写进了全局文件，不清的话别的客户端仍会被一起改时间。
      writeAnchor("real");
      apiPost(SET_URL, { metadata: { location: location.href, time_mode: "real" } },
        function () {
          setStatus("已恢复真实时间", "#2e7d32");
          refreshStatus();
        },
        function (detail) {
          setStatus("本机已恢复真实时间；服务端清理失败：" + detail, "#b7791f");
          refreshStatus();
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
    xhr.open("GET", SET_URL, true);
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

  // 面板状态以**本浏览器**的锚点为准（那才是真正影响本机提问的东西）。
  // 顺带查一次服务端全局兜底：它还开着的话别的客户端会被一起改时间，值得提示。
  function refreshStatus() {
    var anchor = readAnchor();
    var input = document.getElementById("simTimeInput");

    if (anchor && anchor.time_mode === "fixed" && anchor.reference_time) {
      var dt = String(anchor.reference_time).replace("T", " ").slice(0, 16);
      setChipState("模拟中", "#b7791f");
      setStatus("模拟中：" + dt + "（仅本浏览器，点恢复还原）", "#b7791f");
      if (input && !input.value.replace(/^\s+|\s+$/g, "")) input.value = dt;
    } else {
      setChipState("真实", "#2e7d32");
      setStatus("当前：真实时间", "#2e7d32");
    }

    apiGet(function (data) {
      if (!data || !data.active) return;
      var g = String(data.override_datetime || "").replace("T", " ").slice(0, 16);
      setStatus("注意：服务端全局锚点生效中（" + g + "），会影响所有客户端；" +
        "请用「恢复」清理", "#c0392b");
    }, function () { /* 服务端不可达不影响本机锚点 */ });
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
    console.log(PREFIX + " 系统时间切换面板已启用（请求级锚定）");
  }

  // 本文件被 index.html 的 <head> 里同步 <script> 引用，须等 DOMContentLoaded。
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
