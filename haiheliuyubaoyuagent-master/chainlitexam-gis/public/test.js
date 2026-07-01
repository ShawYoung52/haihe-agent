(() => {
  const PREFIX = "[GIS_FRONTEND]";
  /** 父页面 origin 白名单；空数组表示不校验 origin（仅开发环境建议收紧） */
  const ALLOWED_PARENT_ORIGINS = [];

  /** 新版 Chainlit 的 socket.io 实例在 React Context 内，不一定有 window.socket */
  let _gisResolvedSocket = null;
  let _lastSocketFiberWalk = 0;
  /** 记录最近从 Chainlit 发往父页的 linkage_id，避免父页原样回灌造成死循环 */
  const recentOutboundLinkageIds = new Map();
  const OUTBOUND_LINKAGE_TTL_MS = 30000;

  function isProbablySocketIoSocket(x) {
    return !!(
      x &&
      typeof x === "object" &&
      typeof x.emit === "function" &&
      typeof x.on === "function" &&
      typeof x.connected === "boolean" &&
      x.io &&
      typeof x.io === "object"
    );
  }

  function scanPropsForSocket(obj, depth, maxDepth, seenObjs) {
    if (!obj || typeof obj !== "object" || depth > maxDepth) return null;
    if (seenObjs.has(obj)) return null;
    seenObjs.add(obj);
    if (isProbablySocketIoSocket(obj)) return obj;
    if (obj.socket && isProbablySocketIoSocket(obj.socket)) return obj.socket;
    if (Array.isArray(obj)) {
      for (let i = 0; i < obj.length; i++) {
        const r = scanPropsForSocket(obj[i], depth + 1, maxDepth, seenObjs);
        if (r) return r;
      }
      return null;
    }
    const keys = Object.keys(obj);
    for (let i = 0; i < keys.length; i++) {
      const v = obj[keys[i]];
      if (v && typeof v === "object") {
        const r = scanPropsForSocket(v, depth + 1, maxDepth, seenObjs);
        if (r) return r;
      }
    }
    return null;
  }

  function findSocketViaReactFiber() {
    const el = document.getElementById("root");
    if (!el) return null;
    const domKey = Object.keys(el).find(
      (k) => k.startsWith("__reactFiber$") || k.startsWith("__reactContainer$")
    );
    if (!domKey) return null;
    const container = el[domKey];
    const head = container.stateNode?.current || container.current || container.child || container;
    if (!head || typeof head !== "object") return null;
    const queue = [head];
    const seen = new Set();
    let steps = 0;
    const MAX_STEPS = 12000;
    while (queue.length && steps++ < MAX_STEPS) {
      const node = queue.shift();
      if (!node || typeof node !== "object" || seen.has(node)) continue;
      seen.add(node);
      const s1 = scanPropsForSocket(node.memoizedProps, 0, 6, new Set());
      if (s1) return s1;
      const s2 = scanPropsForSocket(node.pendingProps, 0, 6, new Set());
      if (s2) return s2;
      let st = node.memoizedState;
      let sd = 0;
      while (st && sd++ < 40) {
        const s3 = scanPropsForSocket(st.memoizedState, 0, 4, new Set());
        if (s3) return s3;
        st = st.next;
      }
      if (node.child) queue.push(node.child);
      if (node.sibling) queue.push(node.sibling);
    }
    return null;
  }

  function getChainlitSocket(forceFiberWalk) {
    if (_gisResolvedSocket?.connected) return _gisResolvedSocket;
    if (typeof window.socket === "object" && isProbablySocketIoSocket(window.socket)) {
      _gisResolvedSocket = window.socket;
      return _gisResolvedSocket;
    }
    const now = Date.now();
    if (forceFiberWalk || now - _lastSocketFiberWalk > 1500) {
      _lastSocketFiberWalk = now;
      const found = findSocketViaReactFiber();
      if (found) {
        _gisResolvedSocket = found;
        window.__gisChainlitSocket = found;
        if (typeof window.socket === "undefined") {
          window.socket = found;
        }
        return found;
      }
    }
    return _gisResolvedSocket && _gisResolvedSocket.connected ? _gisResolvedSocket : null;
  }

  function safeParse(jsonStr) {
    try {
      return JSON.parse(jsonStr);
    } catch (e) {
      return null;
    }
  }

  function pruneOutboundLinkageIds(nowMs) {
    for (const [k, ts] of recentOutboundLinkageIds.entries()) {
      if (nowMs - ts > OUTBOUND_LINKAGE_TTL_MS) {
        recentOutboundLinkageIds.delete(k);
      }
    }
  }

  function rememberOutboundLinkageId(payload) {
    if (!payload || typeof payload !== "object") return;
    if (payload.type !== "gis_linkage") return;
    const id = typeof payload.linkage_id === "string" ? payload.linkage_id : "";
    if (!id) return;
    const now = Date.now();
    pruneOutboundLinkageIds(now);
    recentOutboundLinkageIds.set(id, now);
  }

  function isLikelyChainlitLinkageEcho(data) {
    if (!data || typeof data !== "object") return false;
    if (data.source === "chainlit") return true;
    if (data.type !== "gis_linkage") return false;
    const id = typeof data.linkage_id === "string" ? data.linkage_id : "";
    if (!id) return false;
    const now = Date.now();
    pruneOutboundLinkageIds(now);
    const ts = recentOutboundLinkageIds.get(id);
    return typeof ts === "number" && now - ts <= OUTBOUND_LINKAGE_TTL_MS;
  }

  function normalizeIncomingPostMessageData(raw) {
    if (typeof raw === "string") {
      const parsed = safeParse(raw);
      return parsed ?? raw;
    }
    return raw;
  }

  function patchSocketEmit() {
    const socket = getChainlitSocket(true);
    if (!socket || socket.__gisPatched) return false;

    const originalOnevent = socket.onevent?.bind(socket);
    if (!originalOnevent) return false;

    socket.onevent = function (packet) {
      try {
        const args = packet?.data || [];
        const event = args[0];
        const payload = args[1];

        if (
          event === "window_message" ||
          event === "gis_linkage" ||
          event === "gis_linkage_broadcast"
        ) {
          const msg = payload?.message;
          const payloadIsLinkage =
            payload &&
            typeof payload === "object" &&
            payload.type === "gis_linkage" &&
            typeof payload.linkage_id === "string";
          console.log(`${PREFIX} event=${event} payload=`, payload);
          console.log(`${PREFIX} typeof payload.message =`, typeof msg);

          if (typeof msg === "string") {
            const parsed = safeParse(msg);
            console.log(`${PREFIX} parsed json =`, parsed ?? msg);
            if (parsed && typeof parsed === "object") {
              rememberOutboundLinkageId(parsed);
            }

            // 转发给 iframe 外层页面（父页面可以直接 window.addEventListener('message') 接收）
            try {
              window.parent?.postMessage(
                {
                  type: "gis_linkage",
                  source: "chainlit",
                  event,
                  payload: parsed ?? msg,
                  raw: msg,
                },
                "*"
              );
            } catch (e) {
              console.warn(`${PREFIX} postMessage failed`, e);
            }
          } else if (payloadIsLinkage) {
            // 兼容后端直接把 gis_linkage 对象放在 payload（无 payload.message）时的回环去重
            rememberOutboundLinkageId(payload);
            try {
              window.parent?.postMessage(
                {
                  type: "gis_linkage",
                  source: "chainlit",
                  event,
                  payload,
                },
                "*"
              );
            } catch (e) {
              console.warn(`${PREFIX} postMessage failed`, e);
            }
          }
        }
      } catch (e) {
        console.warn(`${PREFIX} inspect socket event failed`, e);
      }

      return originalOnevent(packet);
    };

    socket.__gisPatched = true;
    socket.on("connect", () => {
      _gisResolvedSocket = socket;
      flushPendingWindowMessages();
    });
    socket.on("disconnect", () => {
      _gisResolvedSocket = null;
    });
    socket.io?.on?.("reconnect", () => {
      _gisResolvedSocket = socket;
      flushPendingWindowMessages();
    });
    flushPendingWindowMessages();
    if (pendingWindowMessages.length > 0) {
      schedulePendingFlush();
    }
    console.log(`${PREFIX} socket hook installed`);
    return true;
  }

  const pendingWindowMessages = [];
  let pendingFlushTimer = null;
  /** 最后一条来自父页的 postMessage 的 origin，用于回执 targetOrigin */
  let lastParentOrigin = null;

  /** 在 iframe 控制台执行，查看桥接状态（给联调同事用） */
  window.__gisBridgeStatus = function gisBridgeStatus() {
    const socket = getChainlitSocket();
    return {
      hasSocket: !!socket,
      socketConnected: !!socket?.connected,
      socketHookPatched: !!socket?.__gisPatched,
      resolvedVia: socket ? (window.socket === socket ? "window.socket" : "react_fiber_or_cache") : "none",
      pendingQueueLength: pendingWindowMessages.length,
      lastParentOrigin,
      hint:
        "父页发 JSON 后：① 应有 iframe received postMessage；② 应有 emitted 或 flushed；③ 父页可收 gis_chainlit_bridge；④ 服务端终端应有 [GIS parent postMessage]",
    };
  };

  /** 父页发得比 Chainlit socket 连上早时入队；仅靠 connect 可能错过（已连接后再注册监听不触发） */
  function schedulePendingFlush() {
    if (pendingFlushTimer) return;
    let ticks = 0;
    pendingFlushTimer = setInterval(() => {
      ticks += 1;
      flushPendingWindowMessages();
      if (pendingWindowMessages.length === 0 || ticks >= 150) {
        clearInterval(pendingFlushTimer);
        pendingFlushTimer = null;
        if (pendingWindowMessages.length > 0 && ticks >= 150) {
          console.warn(
            `${PREFIX} 队列里仍有 window_message 未发出（约 30s 内 socket 未就绪），请刷新 iframe 后再从父页发送`
          );
        }
      }
    }, 200);
  }

  function ackParentToGis(stage, extra) {
    if (window.parent === window) return;
    const target = lastParentOrigin || "*";
    try {
      window.parent.postMessage(
        {
          type: "gis_chainlit_bridge",
          stage,
          source: "chainlit_iframe",
          ts: Date.now(),
          ...extra,
        },
        target
      );
    } catch (e) {
      console.warn(`${PREFIX} ack postMessage to parent failed`, e);
    }
  }

  function emitWindowMessageToChainlit(payload) {
    const socket = getChainlitSocket(true);
    if (socket && socket.connected) {
      try {
        // 与 Chainlit 服务端 @sio.on("window_message") 对齐，Python 侧用 @cl.on_window_message 接收
        socket.emit("window_message", payload);
        console.log(`${PREFIX} emitted window_message to Chainlit`, payload);
        ackParentToGis("emitted_to_socket", { ok: true });
      } catch (e) {
        console.warn(`${PREFIX} socket.emit(window_message) failed`, e);
        ackParentToGis("emit_error", { ok: false, error: String(e) });
      }
    } else {
      pendingWindowMessages.push(payload);
      console.log(`${PREFIX} socket not ready, queued window_message`, {
        hasSocket: !!socket,
        connected: !!socket?.connected,
        hasRoot: !!document.getElementById("root"),
        tip: "Chainlit 新版 socket 不在 window.socket；已用 React Fiber 查找。稍后会自动 flush，或执行 __gisBridgeStatus()",
      });
      schedulePendingFlush();
    }
  }

  function flushPendingWindowMessages() {
    const socket = getChainlitSocket(false);
    if (!socket || !socket.connected || pendingWindowMessages.length === 0) return;
    while (pendingWindowMessages.length) {
      const p = pendingWindowMessages.shift();
      try {
        socket.emit("window_message", p);
        console.log(`${PREFIX} flushed queued window_message`, p);
        ackParentToGis("emitted_to_socket", { ok: true, fromQueue: true });
      } catch (e) {
        console.warn(`${PREFIX} flush emit failed`, e);
        pendingWindowMessages.unshift(p);
        break;
      }
    }
  }

  function installParentPostMessageBridge() {
    window.addEventListener(
      "message",
      (ev) => {
        if (ALLOWED_PARENT_ORIGINS.length && !ALLOWED_PARENT_ORIGINS.includes(ev.origin)) {
          return;
        }
        const incomingData = normalizeIncomingPostMessageData(ev.data);
        // 忽略本页自己 post 出去的（可选）
        if (incomingData && typeof incomingData === "object" && incomingData.source === "chainlit") {
          return;
        }
        // 关键防回环：若父页回传的是后端刚产出的 gis_linkage（meta.source=chain_gzt），
        // 说明这是“展示回执”而不是“新的用户指令”，不能再灌回 Chainlit socket。
        if (
          incomingData &&
          typeof incomingData === "object" &&
          incomingData.type === "gis_linkage" &&
          incomingData.meta &&
          typeof incomingData.meta === "object" &&
          incomingData.meta.source === "chain_gzt"
        ) {
          console.log(`${PREFIX} ignored loopback gis_linkage from chain_gzt`, {
            linkage_id: incomingData.linkage_id,
            scene: incomingData.scene,
            origin: ev.origin,
          });
          return;
        }
        // 防回环：父页把 Chainlit 发出的 gis_linkage 原样(或近似原样)再发回 iframe 时，不再回灌 socket。
        // 同源场景下（如 origin=当前 Chainlit 页面），这类 gis_linkage 必定是页面内回流，也直接忽略。
        if (
          incomingData &&
          typeof incomingData === "object" &&
          incomingData.type === "gis_linkage" &&
          ev.origin === window.location.origin
        ) {
          console.log(`${PREFIX} ignored same-origin gis_linkage loopback`, {
            origin: ev.origin,
            linkage_id: incomingData.linkage_id,
            scene: incomingData.scene,
          });
          return;
        }
        if (isLikelyChainlitLinkageEcho(incomingData)) {
          const wmsCount = Array.isArray(incomingData?.map?.wms_layers) ? incomingData.map.wms_layers.length : 0;
          const scene = incomingData?.scene || "";
          const linkageId = incomingData?.linkage_id || "";
          console.log(`${PREFIX} ignored echoed gis_linkage from parent`, incomingData);
          console.log(`${PREFIX} echoed summary: linkage_id=${linkageId}, scene=${scene}, wms_layers=${wmsCount}`);
          return;
        }
        // 嵌入父页时：只认「直接父窗口」发来的消息。堆栈里的 postMessage 多为 React Scheduler /
        // Chainlit 内部（如 useUpload），ev.source 是本 iframe 而非 parent，一律忽略以免误转发。
        if (window.parent !== window && ev.source !== window.parent) {
          return;
        }
        // 调试用：能走到这里，在嵌入场景下才是父页发来的 GIS postMessage
        lastParentOrigin = ev.origin;
        console.log(`${PREFIX} iframe received postMessage (from parent)`, {
          origin: ev.origin,
          data: incomingData,
        });
        const envelope = {
          source: "parent_postmessage",
          origin: ev.origin,
          data: incomingData,
        };
        const payload =
          typeof incomingData === "string" ? incomingData : JSON.stringify(envelope);
        emitWindowMessageToChainlit(payload);
      },
      false
    );
    console.log(`${PREFIX} parent postMessage -> window_message bridge installed`);
  }

  function forwardToParent(event, msg) {
    const parsed = typeof msg === "string" ? safeParse(msg) : null;
    console.log(`${PREFIX} event=${event}`);
    console.log(`${PREFIX} typeof message =`, typeof msg);
    console.log(`${PREFIX} parsed json =`, parsed ?? msg);
    try {
      window.parent?.postMessage(
        {
          type: "gis_linkage",
          source: "chainlit",
          event,
          payload: parsed ?? msg,
          raw: msg,
        },
        "*"
      );
    } catch (e) {
      console.warn(`${PREFIX} postMessage failed`, e);
    }
  }

  // 仅复用 Chainlit 页面内部 socket（不创建匿名 socket，避免 400/认证报错）
  installParentPostMessageBridge();

  let tries = 0;
  const timer = setInterval(() => {
    tries += 1;
    if (patchSocketEmit() || tries > 120) {
      clearInterval(timer);
      if (tries > 120) {
        console.warn(
          `${PREFIX} 120 次重试后仍未找到可 patch 的 socket（需 onevent）。父页消息仍可入队；若一直无法 emit 请把 __gisBridgeStatus() 结果发给后端同事。`
        );
      }
    }
  }, 500);
})();
