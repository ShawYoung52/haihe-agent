(function () {
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

    const originalOnevent = socket.onevent ? socket.onevent.bind(socket) : null;
    if (!originalOnevent) return false;

    socket.onevent = function (packet) {
      try {
        const args = packet ? packet.data || [] : [];
        const event = args[0];
        const payload = args[1];

        if (
          event === "window_message" ||
          event === "gis_linkage" ||
          event === "gis_linkage_broadcast"
        ) {
          const msg = payload ? payload.message : undefined;
          console.log(PREFIX + " event=" + event + " payload=", payload);
          console.log(PREFIX + " typeof payload.message =", typeof msg);

          if (typeof msg === "string") {
            const parsed = safeParse(msg);
            console.log(PREFIX + " parsed json =", parsed !== null ? parsed : msg);

            // 转发给 iframe 外层页面（父页面可以直接 window.addEventListener('message') 接收）
            try {
              if (window.parent) {
                window.parent.postMessage(
                  {
                    type: "gis_linkage",
                    source: "chainlit",
                    event: event,
                    payload: parsed !== null ? parsed : msg,
                    raw: msg,
                  },
                  "*"
                );
              }
            } catch (e) {
              console.warn(PREFIX + " postMessage failed", e);
            }
          }
        }
      } catch (e) {
        console.warn(PREFIX + " inspect socket event failed", e);
      }

      return originalOnevent(packet);
    };

    socket.__gisPatched = true;
    console.log(PREFIX + " socket hook installed");
    return true;
  }

  function forwardToParent(event, msg) {
    const parsed = typeof msg === "string" ? safeParse(msg) : null;
    console.log(PREFIX + " event=" + event);
    console.log(PREFIX + " typeof message =", typeof msg);
    console.log(PREFIX + " parsed json =", parsed !== null ? parsed : msg);
    try {
      if (window.parent) {
        window.parent.postMessage(
          {
            type: "gis_linkage",
            source: "chainlit",
            event: event,
            payload: parsed !== null ? parsed : msg,
            raw: msg,
          },
          "*"
        );
      }
    } catch (e) {
      console.warn(PREFIX + " postMessage failed", e);
    }
  }

  // 仅复用 Chainlit 页面内部 socket（不创建匿名 socket，避免 400/认证报错）
  let tries = 0;
  const timer = setInterval(function () {
    tries += 1;
    if (patchSocketEmit() || tries > 120) {
      clearInterval(timer);
      if (tries > 120) {
        console.warn(PREFIX + " page socket not found, hook skipped");
      }
    }
  }, 500);
})();

// =====================================================================
// 图片滚轮缩放看图器 v2（2026-08-19，兼容加固版）
// 兼容性：无可选链/无 ?. 新语法；指针事件不可用时自动退化为鼠标事件；
// 图片加载成功才显示遮罩，加载失败自动用新标签页打开原图（绝不黑屏）。
// 页面加载后短暂显示绿色"看图器已启用"角标，便于肉眼确认 JS 已生效。
// =====================================================================
(function () {
  var PREFIX = "[IMG_ZOOM]";
  var MIN_NATURAL = 90; // 实际渲染尺寸小于该像素的图（头像/图标）不启用
  var MAX_ZOOM = 8;
  var HAS_POINTER = typeof window.PointerEvent !== "undefined";

  var v = null; // 看图器 DOM + 状态（懒创建，首次点图时才有）

  function ensureViewer() {
    if (v) return v;

    var overlay = document.createElement("div");
    overlay.className = "img-zoom-viewer";
    overlay.setAttribute("role", "dialog");
    overlay.style.cssText = [
      "position:fixed;top:0;left:0;right:0;bottom:0;width:100vw;height:100vh;",
      "z-index:999999;",
      "background:rgba(8,8,10,.92);",
      "display:none;align-items:center;justify-content:center;",
      "touch-action:none;user-select:none;-webkit-user-select:none;",
      "cursor:grab;",
    ].join("");

    var img = document.createElement("img");
    img.alt = "";
    img.draggable = false;
    img.style.cssText = [
      "transform-origin:0 0;",
      "will-change:transform;",
      "box-shadow:0 0 48px rgba(0,0,0,.65);",
      "background:#fff;", // 长图多为白底，避免图片透明边缘露黑
    ].join("");

    var btn = document.createElement("button");
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
    var st = { z: 1, tx: 0, ty: 0 };
    var fit = 1,
      baseW = 0,
      baseH = 0,
      drag = null,
      open = false,
      dragged = false,
      loadTimer = null;

    function apply() {
      img.style.transform = "translate(" + st.tx + "px, " + st.ty + "px) scale(" + (fit * st.z) + ")";
      overlay.style.cursor = drag ? "grabbing" : "grab";
    }
    function clampZ(x) {
      return Math.max(1, Math.min(MAX_ZOOM, x));
    }
    function center() {
      st.tx = (overlay.clientWidth - baseW * fit * st.z) / 2;
      st.ty = (overlay.clientHeight - baseH * fit * st.z) / 2;
    }
    function layout() {
      baseW = img.naturalWidth || 1;
      baseH = img.naturalHeight || 1;
      fit = Math.min(overlay.clientWidth / baseW, overlay.clientHeight / baseH);
      img.style.width = baseW + "px";
      img.style.height = baseH + "px";
      // 超窄长图（如 660x4610）fit 后宽度过小几乎不可见——初始至少显示 MIN_VIEW_W 像素宽
      var MIN_VIEW_W = 260;
      var need = MIN_VIEW_W / (baseW * fit);
      if (need > 1) st.z = Math.min(MAX_ZOOM, need);
      center();
      apply();
    }
    function close() {
      if (!open) return;
      open = false;
      drag = null;
      dragged = false;
      if (loadTimer) {
        clearTimeout(loadTimer);
        loadTimer = null;
      }
      overlay.style.display = "none";
      document.body.style.overflow = (v && v._savedOverflow) || "";
      document.removeEventListener("keydown", onKey, true);
    }
    function onKey(e) {
      if (e.key === "Escape") {
        if (e.preventDefault) e.preventDefault();
        close();
      }
    }
    function fallbackOpen(src) {
      // 图片加载失败/超时：改用新标签页打开原图，保证用户永远能看到图
      try {
        window.open(src, "_blank");
      } catch (e) {
        console.warn(PREFIX + " window.open fallback failed", e);
      }
    }
    function openWith(src) {
      v._savedOverflow = document.body.style.overflow;
      open = true;
      drag = null;
      dragged = false;
      document.body.style.overflow = "hidden";
      document.addEventListener("keydown", onKey, true);
      overlay.style.display = "flex";

      img.onload = function () {
        if (loadTimer) {
          clearTimeout(loadTimer);
          loadTimer = null;
        }
        layout();
      };
      img.onerror = function () {
        if (loadTimer) {
          clearTimeout(loadTimer);
          loadTimer = null;
        }
        close();
        fallbackOpen(src);
      };
      img.src = src;
      if (img.complete && img.naturalWidth) {
        if (loadTimer) {
          clearTimeout(loadTimer);
          loadTimer = null;
        }
        layout();
      } else {
        // 8 秒仍未加载完成（既没 load 也没 error）→ 兜底打开原图，避免卡在黑屏
        loadTimer = setTimeout(function () {
          if (!open) return;
          if (!img.complete || !img.naturalWidth) {
            close();
            fallbackOpen(src);
          }
        }, 8000);
      }
    }

    function onWheel(e) {
      if (!open) return;
      var deltaY = typeof e.deltaY !== "undefined" ? e.deltaY : e.wheelDelta;
      var up = deltaY < 0;
      if (e.preventDefault) {
        e.preventDefault();
      } else {
        e.returnValue = false;
      }
      var zNew = clampZ(st.z * (up ? 1.18 : 1 / 1.18));
      if (zNew === st.z) return;
      var cx = e.clientX,
        cy = e.clientY;
      var px = (cx - st.tx) / (fit * st.z);
      var py = (cy - st.ty) / (fit * st.z);
      st.z = zNew;
      st.tx = cx - px * fit * st.z;
      st.ty = cy - py * fit * st.z;
      apply();
    }

    function onDown(e) {
      if (!open) return;
      drag = { px: e.clientX, py: e.clientY, ox: st.tx, oy: st.ty };
      dragged = false;
      if (overlay.setPointerCapture && typeof e.pointerId !== "undefined") {
        try {
          overlay.setPointerCapture(e.pointerId);
        } catch (err) {
          /* 老浏览器无此能力，忽略 */
        }
      }
    }
    function onMove(e) {
      if (!open || !drag) return;
      st.tx = drag.ox + (e.clientX - drag.px);
      st.ty = drag.oy + (e.clientY - drag.py);
      if (Math.abs(e.clientX - drag.px) + Math.abs(e.clientY - drag.py) > 3) dragged = true;
      apply();
    }
    function endDrag() {
      if (drag) {
        drag = null;
        apply();
      }
    }

    // 滚轮：现代浏览器用 wheel，老浏览器/IE 用 mousewheel
    if ("onwheel" in document) {
      overlay.addEventListener("wheel", onWheel, { passive: false });
    } else if ("onmousewheel" in document) {
      overlay.addEventListener("mousewheel", onWheel);
    } else if (document.addEventListener) {
      overlay.addEventListener("DOMMouseScroll", onWheel);
    }

    // 拖拽：优先指针事件，老浏览器退化鼠标事件
    if (HAS_POINTER) {
      overlay.addEventListener("pointerdown", onDown);
      overlay.addEventListener("pointermove", onMove);
      overlay.addEventListener("pointerup", endDrag);
      overlay.addEventListener("pointercancel", endDrag);
    } else {
      overlay.addEventListener("mousedown", onDown);
      overlay.addEventListener("mousemove", onMove);
      overlay.addEventListener("mouseup", endDrag);
    }

    overlay.addEventListener("dblclick", function (e) {
      if (!open) return;
      if (st.z > 1.01) {
        st.z = 1;
        center();
        apply();
      } else {
        st.z = clampZ(2.5);
        var px = (e.clientX - st.tx) / (fit * st.z);
        var py = (e.clientY - st.ty) / (fit * st.z);
        st.tx = e.clientX - px * fit * st.z;
        st.ty = e.clientY - py * fit * st.z;
        apply();
      }
    });

    // 点击背景（图片矩形以外的遮罩）关闭；拖拽结束产生的 click 要吞掉，不能误关。
    // 不能用 e.target 判断：setPointerCapture/指针事件会把 click 重定向到 overlay，
    // 必须按坐标反算点击点是否落在图片矩形内。
    overlay.addEventListener("click", function (e) {
      if (dragged) {
        dragged = false;
        return;
      }
      var ix = (e.clientX - st.tx) / (fit * st.z);
      var iy = (e.clientY - st.ty) / (fit * st.z);
      if (ix < 0 || iy < 0 || ix > baseW || iy > baseH) close();
    });
    btn.addEventListener("click", function (e) {
      if (e.stopPropagation) e.stopPropagation();
      close();
    });

    v = { overlay: overlay, img: img, btn: btn, openWith: openWith };
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

    function arm() {
      if (img.dataset.imgZoom === "1") return;
      if (!img.naturalWidth || !img.naturalHeight) return;
      // 按【实际渲染尺寸】判断：头像等 asset 的自然尺寸可能很大（如 928x928），
      // 但实际只显示 20px——自然尺寸过滤会误挂。渲染尺寸为 0（未布局）时退回自然尺寸。
      var r = img.getBoundingClientRect();
      var disp = Math.max(r.width, r.height);
      var natural = Math.max(img.naturalWidth, img.naturalHeight);
      if (disp > 0 ? disp < MIN_NATURAL : natural < MIN_NATURAL) return;
      img.dataset.imgZoom = "1";
      img.style.cursor = "zoom-in";
      img.addEventListener(
        "click",
        function (e) {
          if (e.preventDefault) e.preventDefault();
          if (e.stopPropagation) e.stopPropagation(); // 截获，避免 Chainlit 默认看图器也打开
          openViewer(img.currentSrc || img.src);
        },
        true
      );
    }
    img.addEventListener("load", arm);
    if (img.complete && img.naturalWidth) arm();
  }

  function scan() {
    var imgs = document.querySelectorAll("img");
    for (var i = 0; i < imgs.length; i++) maybePatch(imgs[i]);
  }
  scan();
  var obs = new MutationObserver(scan);
  obs.observe(document.body, { childList: true, subtree: true });
  console.log(PREFIX + " 滚轮缩放看图器已启用");

  // 自检角标：页面加载后短暂显示，肉眼确认 JS 已生效
  try {
    var badge = document.createElement("div");
    badge.textContent = "看图器已启用";
    badge.style.cssText = [
      "position:fixed;bottom:56px;right:12px;z-index:999998;",
      "background:#1a7f37;color:#fff;padding:4px 10px;border-radius:12px;",
      "font:12px/1.4 sans-serif;opacity:0;transition:opacity .6s;",
      "pointer-events:none;",
    ].join("");
    document.body.appendChild(badge);
    setTimeout(function () {
      badge.style.opacity = "1";
    }, 600);
    setTimeout(function () {
      badge.style.opacity = "0";
    }, 6000);
  } catch (e) {
    /* 角标失败不影响功能 */
  }
})();
