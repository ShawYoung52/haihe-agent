"use strict";

const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const scriptPath = process.argv[2];
if (!scriptPath) throw new Error("missing browser script path");

const roots = {};
const assistants = [];
const titles = [];
const windowListeners = {};
const observerCallbacks = [];
let timerId = 0;
let taskRunning = true;

function makeRoot(id) {
  const trigger = { clicks: 0, click() { this.clicks += 1; } };
  const attributes = {};
  const root = {
    id: "step-" + id,
    trigger,
    compareDocumentPosition(node) { return node.following === false ? 0 : 4; },
    querySelector(selector) {
      return selector === 'button[aria-expanded="true"]' ? trigger : null;
    },
    setAttribute(name, value) { attributes[name] = value; },
    getAttribute(name) { return attributes[name] || null; },
  };
  roots[root.id] = root;
  return root;
}

function makeAssistant(text, streaming, following) {
  return {
    textContent: text,
    following: following !== false,
    streaming: !!streaming,
    querySelector(selector) {
      return selector === ".loading-cursor" && this.streaming ? {} : null;
    },
  };
}

global.window = {
  Node: { DOCUMENT_POSITION_FOLLOWING: 4 },
  PointerEvent: undefined,
  __CHAINLIT_REASONING_TEST_MODE__: true,
  addEventListener(name, callback) { windowListeners[name] = callback; },
};
global.Node = global.window.Node;
global.document = {
  body: {},
  readyState: "complete",
  getElementById(id) {
    if (id === "stop-button") return taskRunning ? {} : null;
    return roots[id] || null;
  },
  addEventListener() {},
  querySelectorAll(selector) {
    if (selector === '[data-step-type="assistant_message"]') return assistants;
    if (selector === '[id="step-🤔 思考过程"]') return titles;
    if (selector === "img") return [];
    return [];
  },
};
global.MutationObserver = class {
  constructor(callback) { this.callback = callback; observerCallbacks.push(callback); }
  observe() {}
  disconnect() {}
};
global.setTimeout = function () { timerId += 1; return timerId; };
global.clearTimeout = function () {};

vm.runInThisContext(fs.readFileSync(scriptPath, "utf8"), { filename: scriptPath });
const api = window.__CHAINLIT_REASONING_TEST_API__;
assert(api, "test API must be exposed in test mode");

// 1. 兼容事件到达时只登记目标，答案未出来不折叠。
const eventRoot = makeRoot("event-step");
windowListeners.message({
  data: JSON.stringify({ type: "chainlit_reasoning_complete", step_id: "event-step" }),
});
assert.strictEqual(eventRoot.trigger.clicks, 0);

// 2. 项目用 Message.update() 分块输出，即使没有 loading-cursor，
//    stop-button 存在也说明任务未结束，不能在首个 chunk 后提前折叠。
const streamed = makeAssistant("已输出首个32字分块", false);
assistants.push(streamed);
observerCallbacks.forEach((callback) => callback());
assert.strictEqual(eventRoot.trigger.clicks, 0);

// 空的主回答后即使先出现图表旁路消息，任务运行期间也不折叠。
assistants.unshift(makeAssistant("", false));
assistants.push(makeAssistant("📊 图表已生成", false));
observerCallbacks.forEach((callback) => callback());
assert.strictEqual(eventRoot.trigger.clicks, 0);

// 3. task_end 后 stop-button 消失，此时且仅此时折叠。
taskRunning = false;
observerCallbacks.forEach((callback) => callback());
assert.strictEqual(eventRoot.trigger.clicks, 1);
observerCallbacks.forEach((callback) => callback());
assert.strictEqual(eventRoot.trigger.clicks, 1);

// 4. 没收到 window_message 时，也可从当前打开的“思考过程”DOM安全降级。
assistants.length = 0;
const fallbackRoot = makeRoot("fallback-step");
const title = {
  closest() {
    return {
      querySelector(selector) {
        return selector === '[id^="step-"]' ? fallbackRoot : null;
      },
    };
  },
};
titles.push(title);
assistants.push(makeAssistant("完整答案", false));
taskRunning = true;
api.scanOpenReasoningSteps();
assert.strictEqual(fallbackRoot.trigger.clicks, 0);
taskRunning = false;
observerCallbacks.forEach((callback) => callback());
assert.strictEqual(fallbackRoot.trigger.clicks, 1);

// 用户之后手动重新展开旧思考时，后续 DOM 变化不应再次强制折叠。
api.scanOpenReasoningSteps();
assert.strictEqual(fallbackRoot.trigger.clicks, 1);

// 页面空闲/恢复历史会话后，手动展开的无标记历史 step 也不得自动关闭。
titles.length = 0;
const historyRoot = makeRoot("history-step");
titles.push({
  closest() {
    return { querySelector() { return historyRoot; } };
  },
});
api.scanOpenReasoningSteps();
assert.strictEqual(historyRoot.trigger.clicks, 0);

// 5. 空答案节点和目标之前的旧消息均不能触发折叠。
assistants.length = 0;
titles.length = 0;
const guardedRoot = makeRoot("guarded-step");
api.schedule("guarded-step");
assistants.push(makeAssistant("", false), makeAssistant("旧回答", false, false));
api.processRequest("guarded-step");
assert.strictEqual(guardedRoot.trigger.clicks, 0);

console.log("reasoning auto-collapse behavior: ok");
