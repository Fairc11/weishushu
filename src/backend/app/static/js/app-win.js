/* ============================================================
 * v2.0.0 阶段 4：Windows WebView2 专用兜底 JS
 * 仅在 platform == 'win32' 时通过 base.html 引入。
 * Mac 端不加载此文件。
 * ============================================================ */

(function (global) {
  'use strict';

  if (!global.Ptu) return;

  // Windows 高 DPI 下 splitter 拖拽节流，避免 mousemove 高频触发 layout 抖动
  const originalApply = Ptu.applySplitRatio;
  if (typeof originalApply === 'function') {
    let rafId = null;
    Ptu.applySplitRatio = function (ratio) {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(function () {
        originalApply.call(Ptu, ratio);
        rafId = null;
      });
    };
  }
})(window);
