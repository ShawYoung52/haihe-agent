// =====================================================================
// sim-time-agentweb.js 单元测试（帧注入 + 锚点存取）
//
// 为什么值得测：这个脚本会给 AgentWeb 的**每一帧出站 WebSocket 数据**过一遍钩子。
// 一旦它改坏了不该改的帧，整个前端就哑了，而且这种故障在浏览器控制台里很难定位。
// 这里把"该改的改对、不该改的原样放行"逐条锁住。
//
// 跑法（无需依赖，Node 即可）：
//     node chainlitexam/tests/test_sim_time_agentweb.js
// 期望末行：ALL PASS
// =====================================================================
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = path.join(__dirname, "..", "AgentWeb", "sim-time-agentweb.js");

let failures = 0;
let checks = 0;

function check(name, actual, expected) {
  checks++;
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    console.log("  ok   " + name);
  } else {
    failures++;
    console.log("  FAIL " + name + "\n        期望 " + e + "\n        实际 " + a);
  }
}

// 在最小沙箱里加载真实脚本（不 mock 脚本本身，只 mock 它的宿主环境）。
function load(storage) {
  const store = Object.assign({}, storage || {});
  const sandbox = {
    console: { log() {} },
    setTimeout() {},
    location: { protocol: "http:", hostname: "h", href: "http://h/AgentWeb/" },
    localStorage: {
      getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    },
    document: {
      readyState: "loading", // 不让 init() 跑起来（面板 UI 不是本测试的目标）
      addEventListener() {},
    },
    XMLHttpRequest: function () {},
  };
  function FakeWebSocket() {}
  FakeWebSocket.prototype.send = function (data) { FakeWebSocket.lastSent = data; };
  sandbox.WebSocket = FakeWebSocket;
  sandbox.window = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SRC, "utf8"), sandbox);

  return {
    api: sandbox.__haiheSimTime,
    store,
    sendFrame(frame) {
      FakeWebSocket.lastSent = undefined;
      new FakeWebSocket().send(frame);
      return FakeWebSocket.lastSent;
    },
    get patched() { return !!FakeWebSocket.prototype.__haiheAnchorPatched; },
  };
}

function clientFrame(message, extraTop) {
  const envelope = { message: message, fileReferences: [] };
  if (extraTop) Object.assign(envelope, extraTop);
  return '42["client_message",' + JSON.stringify(envelope) + "]";
}

// 与源码同款：剥掉 "42" + 可选 ack id 再解析。
function parse(frame) {
  const rest = frame.slice(2);
  let i = 0;
  while (i < rest.length && rest[i] >= "0" && rest[i] <= "9") i++;
  return JSON.parse(rest.slice(i))[1].message;
}

const BASE_MSG = { id: "abc", output: "今天下午有雨吗", metadata: { location: "http://h/AgentWeb/" } };

console.log("\n[1] 未设锚点：一字节不改（旧行为逐字不变）");
{
  const h = load({});
  check("钩子已安装", h.patched, true);
  const frame = clientFrame(BASE_MSG);
  check("帧原样放行", h.sendFrame(frame), frame);
}

console.log("\n[2] 锚定为 ISO：metadata 注入 time_mode/reference_time");
{
  const h = load({ haihe_reference_time: "2026-07-10T15:00:00+08:00" });
  const out = h.sendFrame(clientFrame(BASE_MSG));
  const msg = parse(out);
  check("time_mode", msg.metadata.time_mode, "fixed");
  check("reference_time", msg.metadata.reference_time, "2026-07-10T15:00:00+08:00");
  check("原有字段保留", msg.metadata.location, "http://h/AgentWeb/");
  check("正文未被动", msg.output, "今天下午有雨吗");
  check("id 未被动", msg.id, "abc");
  check("前缀仍是 42", out.slice(0, 2), "42");
}

console.log("\n[3] 锚点为 real：显式要求真实时间（压过服务端遗留的全局文件）");
{
  const h = load({ haihe_reference_time: "real" });
  const msg = parse(h.sendFrame(clientFrame(BASE_MSG)));
  check("time_mode", msg.metadata.time_mode, "real");
  check("不带 reference_time", "reference_time" in msg.metadata, false);
}

console.log("\n[4] 不该碰的帧一律原样放行");
{
  const h = load({ haihe_reference_time: "2026-07-10T15:00:00+08:00" });
  const others = [
    '40{"sid":"x"}',                                     // socket.io connect
    '42["other_event",{"message":{"metadata":{}}}]',     // 别的客户端事件
    '42["client_message"]',                              // 缺 payload
    '42["client_message",{}]',                           // 缺 message
    '42["client_message",{"message":"not-an-object"}]',  // message 不是对象
    'not-json-at-all',
    '',
    '42["client_message",{bad json]',                    // JSON 坏掉
  ];
  for (const f of others) {
    check("原样放行 " + JSON.stringify(f.slice(0, 34)), h.sendFrame(f), f);
  }
}

console.log("\n[5] 前端自己带了锚点：谁带谁优先，不覆盖");
{
  const h = load({ haihe_reference_time: "2026-07-10T15:00:00+08:00" });
  const frame = clientFrame({
    id: "abc", output: "x",
    metadata: { location: "L", time_mode: "fixed", reference_time: "2020-01-01T00:00:00+08:00" },
  });
  check("不覆盖", h.sendFrame(frame), frame);
}

console.log("\n[6] 带 ack id 的帧也要认（42N[...]）");
{
  const h = load({ haihe_reference_time: "2026-07-10T15:00:00+08:00" });
  const envelope = { message: BASE_MSG, fileReferences: [] };
  const out = h.sendFrame('425["client_message",' + JSON.stringify(envelope) + "]");
  check("ack id 保留", out.slice(0, 3), "425");
  check("注入了", parse(out).metadata.reference_time, "2026-07-10T15:00:00+08:00");
}

console.log("\n[7] message 没有 metadata 字段时补一个");
{
  const h = load({ haihe_reference_time: "2026-07-10T15:00:00+08:00" });
  const msg = parse(h.sendFrame(clientFrame({ id: "a", output: "x" })));
  check("补出 metadata", msg.metadata.time_mode, "fixed");
}

console.log("\n[8] 空白/异常 localStorage 值当成没设锚点");
{
  for (const v of ["", "   "]) {
    const h = load({ haihe_reference_time: v });
    const frame = clientFrame(BASE_MSG);
    check("空白值放行 " + JSON.stringify(v), h.sendFrame(frame), frame);
  }
}

console.log("\n[9] localStorage 抛异常（隐私模式）不影响正常发送");
{
  const sandbox = {
    console: { log() {} }, setTimeout() {},
    location: { protocol: "http:", hostname: "h", href: "http://h/x" },
    localStorage: { getItem() { throw new Error("denied"); } },
    document: { readyState: "loading", addEventListener() {} },
    XMLHttpRequest: function () {},
  };
  function FakeWebSocket() {}
  FakeWebSocket.prototype.send = function (d) { FakeWebSocket.lastSent = d; };
  sandbox.WebSocket = FakeWebSocket;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SRC, "utf8"), sandbox);

  const frame = clientFrame(BASE_MSG);
  FakeWebSocket.lastSent = undefined;
  new FakeWebSocket().send(frame);
  check("原样发送", FakeWebSocket.lastSent, frame);
}

console.log("\n[10] 重复加载不重复包装 send");
{
  const h = load({});
  const once = h.api ? 1 : 0;
  check("只包装一次", h.patched && once, 1);
}

console.log("\n" + (failures === 0 ? "ALL PASS" : failures + " FAILED") + " (" + checks + " 项)");
process.exit(failures === 0 ? 0 : 1);
