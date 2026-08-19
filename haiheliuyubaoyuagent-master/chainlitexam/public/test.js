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

// =====================================================================
// 图片滚轮缩放看图器（2026-08-19 用户要求：点开长图后可用滚轮放大/缩小）
// 纯前端注入：点聊天里的大图 → 全屏看图层；滚轮缩放（以鼠标为缩放中心）、
// 按住拖拽平移、双击复位（再次双击放大 2.5x）、Esc/✕/点击背景关闭。
// 对话内图片原本展示不变，仅在点开后提供缩放平移。
// 只对较大的内容图启用（头像/图标按尺寸过滤），不依赖 Chainlit 内部类名。
// =====================================================================
(() => {
  const PREFIX = "[IMG_ZOOM]";
  const MIN_NATURAL = 90; // 长宽都小于该像素的图（头像/小图标）不启用
  const MAX_ZOOM = 8;

  let v = null; // 看图器 DOM + 状态（懒创建，首次点图时才有）

  function ensureViewer() {
    if (v) return v;

    const overlay = document.createElement("div");
    overlay.className = "img-zoom-viewer";
    overlay.setAttribute("role", "dialog");
    overlay.style.cssText = [
      "position:fixed;inset:0;z-index:999999;",
      "background:rgba(8,8,10,.92);",
      "display:none;align-items:center;justify-content:center;",
      "touch-action:none;user-select:none;-webkit-user-select:none;",
      "cursor:grab;",
    ].join("");

    const img = document.createElement("img");
    img.alt = "";
    img.draggable = false;
    img.style.cssText = [
      "transform-origin:0 0;",
      "will-change:transform;",
      "box-shadow:0 0 48px rgba(0,0,0,.65);",
      "background:#fff;", // 长图多为白底，避免图片透明边缘露黑
    ].join("");

    const btn = document.createElement("button");
    btn.textContent = "✕";
    btn.title = "关闭 (Esc)";
    btn.setAttribute("aria-label", "关闭看图器");
    btn.style.cssText = [
      "position:fixed;top:16px;right:20px;width:44px;height:44px;",
      "line-height:42px;text-align:center;font-size:26px;color:#fff;",
      "background:rgba(255,255,255,.14);border:none;border-radius:50%;",
      "cursor:pointer;z-index:1;",
    ].join("");

    overlay.appendChild(img);
    overlay.appendChild(btn);
    document.body.appendChild(overlay);

    // 状态：z=缩放倍数(1..MAX_ZOOM)，tx/ty=平移 px；图片以原始像素布局，再整体
    // transform: translate(tx,ty) scale(fit*z)，其中 fit=1x 时适合屏幕的系数。
    const st = { z: 1, tx: 0, ty: 0 };
    let fit = 1, baseW = 0, baseH = 0, drag = null, open = false, dragged = false;

    const apply = () => {
      img.style.transform = `translate(${st.tx}px, ${st.ty}px) scale(${fit * st.z})`;
      overlay.style.cursor = drag ? "grabbing" : "grab";
    };
    const clampZ = (x) => Math.max(1, Math.min(MAX_ZOOM, x));
    const center = () => {
      st.tx = (overlay.clientWidth - baseW * fit * st.z) / 2;
      st.ty = (overlay.clientHeight - baseH * fit * st.z) / 2;
    };
    const layout = () => {
      baseW = img.naturalWidth || 1;
      baseH = img.naturalHeight || 1;
      fit = Math.min(overlay.clientWidth / baseW, overlay.clientHeight / baseH);
      img.style.width = baseW + "px";
      img.style.height = baseH + "px";
      center();
      apply();
    };
    const close = () => {
      if (!open) return;
      open = false;
      drag = null;
      dragged = false;
      overlay.style.display = "none";
      document.body.style.overflow = v?._savedOverflow ?? "";
      document.removeEventListener("keydown", onKey, true);
    };
    function onKey(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
    }
    const openWith = (src) => {
      v._savedOverflow = document.body.style.overflow;
      open = true;
      drag = null;
      dragged = false;
      document.body.style.overflow = "hidden";
      document.addEventListener("keydown", onKey, true);
      overlay.style.display = "flex";
      img.src = src;
      img.onload = layout;
      if (img.complete && img.naturalWidth) layout();
    };

    overlay.addEventListener(
      "wheel",
      (e) => {
        if (!open) return;
        e.preventDefault();
        const zNew = clampZ(st.z * (e.deltaY < 0 ? 1.18 : 1 / 1.18));
        if (zNew === st.z) return;
        // 保持鼠标下方的图像点不动
        const px = (e.clientX - st.tx) / (fit * st.z);
        const py = (e.clientY - st.ty) / (fit * st.z);
        st.z = zNew;
        st.tx = e.clientX - px * fit * st.z;
        st.ty = e.clientY - py * fit * st.z;
        apply();
      },
      { passive: false }
    );

    overlay.addEventListener("pointerdown", (e) => {
      if (!open) return;
      drag = { px: e.clientX, py: e.clientY, ox: st.tx, oy: st.ty };
      dragged = false;
      overlay.setPointerCapture(e.pointerId);
    });
    overlay.addEventListener("pointermove", (e) => {
      if (open && drag) {
        st.tx = drag.ox + (e.clientX - drag.px);
        st.ty = drag.oy + (e.clientY - drag.py);
        if (Math.abs(e.clientX - drag.px) + Math.abs(e.clientY - drag.py) > 3) dragged = true;
        apply();
      }
    });
    const endDrag = () => {
      if (drag) {
        drag = null;
        apply();
      }
    };
    overlay.addEventListener("pointerup", endDrag);
    overlay.addEventListener("pointercancel", endDrag);

    overlay.addEventListener("dblclick", (e) => {
      if (!open) return;
      if (st.z > 1.01) {
        st.z = 1;
        center();
        apply();
      } else {
        st.z = clampZ(2.5);
        const px = (e.clientX - st.tx) / (fit * st.z);
        const py = (e.clientY - st.ty) / (fit * st.z);
        st.tx = e.clientX - px * fit * st.z;
        st.ty = e.clientY - py * fit * st.z;
        apply();
      }
    });

    // 点击背景（图片矩形以外的遮罩）关闭；拖拽结束产生的 click 要吞掉，不能误关。
    // 注意不能用 e.target 判断：setPointerCapture 会把 click 重定向到 overlay，
    // 必须按坐标反算点击点是否落在图片矩形内。
    overlay.addEventListener("click", (e) => {
      if (dragged) {
        dragged = false;
        return;
      }
      const ix = (e.clientX - st.tx) / (fit * st.z);
      const iy = (e.clientY - st.ty) / (fit * st.z);
      if (ix < 0 || iy < 0 || ix > baseW || iy > baseH) close();
    });
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      close();
    });

    v = { overlay, img, btn, openWith };
    return v;
  }

  function openViewer(src) {
    ensureViewer().openWith(src);
  }

  function maybePatch(img) {
    if (!img || img.dataset.imgZoom === "1") return;
    // 只看聊天内容里的图片；排除自家看图器、页头/导航、输入区、头像等
    if (
      img.closest(
        ".img-zoom-viewer, header, nav, form, " +
          "[class*='avatar'], [class*='Avatar'], [class*='composer'], [class*='Composer']"
      )
    ) {
      return;
    }
    if (/\.svg(\?|#|$)/i.test(img.src)) return; // 图标不启用

    const arm = () => {
      if (img.dataset.imgZoom === "1") return;
      if (!img.naturalWidth || !img.naturalHeight) return;
      if (Math.max(img.naturalWidth, img.naturalHeight) < MIN_NATURAL) return;
      img.dataset.imgZoom = "1";
      img.style.cursor = "zoom-in";
      img.addEventListener(
        "click",
        (e) => {
          e.preventDefault();
          e.stopPropagation(); // 截获，避免 Chainlit 默认看图器也打开
          openViewer(img.currentSrc || img.src);
        },
        true
      );
    };
    img.addEventListener("load", arm);
    if (img.complete && img.naturalWidth) arm();
  }

  const scan = () => document.querySelectorAll("img").forEach(maybePatch);
  scan();
  const obs = new MutationObserver(scan);
  obs.observe(document.body, { childList: true, subtree: true });
  console.log(`${PREFIX} 滚轮缩放看图器已启用`);
})();
