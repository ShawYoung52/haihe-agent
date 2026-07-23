(() => {
  const PREFIX = "[GIS_FRONTEND]";

  function safeParse(jsonStr) {
    try {
      return JSON.parse(jsonStr);
    } catch (e) {
      return null;
    }
  }

  function patchSocketEmit() {
    const socket = window.socket;
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
          console.log(`${PREFIX} event=${event} payload=`, payload);
          console.log(`${PREFIX} typeof payload.message =`, typeof msg);

          if (typeof msg === "string") {
            const parsed = safeParse(msg);
            console.log(`${PREFIX} parsed json =`, parsed ?? msg);

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
          }
        }
      } catch (e) {
        console.warn(`${PREFIX} inspect socket event failed`, e);
      }

      return originalOnevent(packet);
    };

    socket.__gisPatched = true;
    console.log(`${PREFIX} socket hook installed`);
    return true;
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
  let tries = 0;
  const timer = setInterval(() => {
    tries += 1;
    if (patchSocketEmit() || tries > 120) {
      clearInterval(timer);
      if (tries > 120) {
        console.warn(`${PREFIX} page socket not found, hook skipped`);
      }
    }
  }, 500);
})();
