// =====================================================================
// 系统时间切换面板 — AgentWeb（海河流域数字预报员）适配版
// 2026-08-21。切换智能体的"当前时间"（全局"现在"锚定）：
//   - 输入 年-月-日 时:分（如 2026-07-10 15:00），点"设置" → 全局按该时刻回答；
//   - 点"恢复" → 还原真实时间；
//   - 顶部状态行常显"模拟中/真实时间"，防忘记恢复。
// 后端：调 8003 的 /api/v1/admin/system-time（与 /qa/ask 同机同端口不同服务）。
// 兼容性：无可选链/无箭头函数/用 XMLHttpRequest，兼容内网旧浏览器。
// 部署：本文件放 webapps/AgentWeb/sim-time-agentweb.js，
//       index.html 在 <!-- JS INJECTION PLACEHOLDER --> 处加一行：
//       <script src="./sim-time-agentweb.js"></script>
// =====================================================================
(function () {
  var PREFIX = "[SIM_TIME_AW]";
  // 后端 base：AgentWeb 与 8003 同主机不同端口，从 location.hostname 推导，不写死内网 IP。
  var API_BASE = location.protocol + "//" + location.hostname + ":8003";
  var SET_URL = API_BASE + "/api/v1/admin/system-time";
  var CLEAR_URL = SET_URL + "/clear";
  var GET_URL = SET_URL;

  function buildPanel() {
    if (document.getElementById("simTimePanel")) return document.getElementById("simTimePanel");

    var panel = document.createElement("div");
    panel.id = "simTimePanel";
    panel.style.cssText = [
      "position:fixed;right:14px;bottom:14px;z-index:99998;",
      "width:250px;font-size:12px;line-height:1.5;color:#333;",
      "background:#fff;border:1px solid #c8d2e0;border-radius:8px;",
      "box-shadow:0 4px 20px rgba(0,0,0,.18);font-family:sans-serif;",
    ].join("");

    var header = document.createElement("div");
    header.style.cssText = [
      "padding:6px 10px;font-weight:bold;color:#1f3a63;cursor:pointer;",
      "background:#eef3fa;border-radius:8px 8px 0 0;user-select:none;",
    ].join("");
    header.textContent = "🕒 系统时间";
    header.title = "点击折叠/展开";
    header.addEventListener("click", function () {
      body.style.display = body.style.display === "none" ? "block" : "none";
    });

    var body = document.createElement("div");
    body.id = "simTimeBody";
    body.style.cssText = "padding:8px 10px 10px;";

    var input = document.createElement("input");
    input.id = "simTimeInput";
    input.type = "text";
    input.placeholder = "2026-07-10 15:00";
    input.style.cssText = [
      "width:100%;box-sizing:border-box;padding:5px 7px;font-size:13px;",
      "border:1px solid #b9c4d6;border-radius:4px;",
    ].join("");

    var row = document.createElement("div");
    row.style.cssText = "margin-top:6px;display:flex;gap:6px;";
    var setBtn = document.createElement("button");
    setBtn.textContent = "设置";
    setBtn.style.cssText = "flex:1;padding:4px 0;font-size:12px;cursor:pointer;border:1px solid #2f6bb0;border-radius:4px;background:#2f6bb0;color:#fff;";
    var clearBtn = document.createElement("button");
    clearBtn.textContent = "恢复";
    clearBtn.style.cssText = "flex:1;padding:4px 0;font-size:12px;cursor:pointer;border:1px solid #b9c4d6;border-radius:4px;background:#fff;color:#333;";

    var status = document.createElement("div");
    status.id = "simTimeStatus";
    status.style.cssText = "margin-top:6px;font-size:11px;color:#666;word-break:break-all;";

    row.appendChild(setBtn);
    row.appendChild(clearBtn);
    body.appendChild(input);
    body.appendChild(row);
    body.appendChild(status);
    panel.appendChild(header);
    panel.appendChild(body);
    document.body.appendChild(panel);

    setBtn.addEventListener("click", function () {
      var v = (input.value || "").trim();
      if (!v) { setStatus("请输入时间，如 2026-07-10 15:00", "#c0392b"); return; }
      apiPost(SET_URL, { datetime: v }, function (data) {
        var display = data && data.data && data.data.display ? data.data.display : v;
        setStatus("已切换为模拟时间：" + display, "#2e7d32");
        input.value = display.slice(0, 16);
      }, function (detail) {
        setStatus("设置失败：" + detail, "#c0392b");
      });
    });

    clearBtn.addEventListener("click", function () {
      apiPost(CLEAR_URL, {}, function () {
        setStatus("已恢复真实时间", "#2e7d32");
      }, function (detail) {
        setStatus("恢复失败：" + detail, "#c0392b");
      });
    });

    return panel;
  }

  function setStatus(text, color) {
    var el = document.getElementById("simTimeStatus");
    if (!el) return;
    el.style.color = color || "#666";
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
      if (data && data.active && data.override_datetime) {
        setStatus("模拟中：" + data.override_datetime.replace("T", " ").slice(0, 16) + "（点恢复还原）", "#b7791f");
        var input = document.getElementById("simTimeInput");
        if (input && !input.value.trim()) {
          input.value = data.override_datetime.replace("T", " ").slice(0, 16);
        }
      } else {
        setStatus("当前：真实时间", "#2e7d32");
      }
    }, function (status) {
      setStatus("后端不可达（HTTP " + status + "）", "#c0392b");
    });
  }

  function init() {
    buildPanel();
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
