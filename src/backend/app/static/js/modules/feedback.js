// 阶段 5：feedback 模块。
import { Ptu, global } from "./state.js";

export function init(ctx = Ptu) {
  Object.assign(ctx, {
    toast(msg, ms = 2400) {
      const el = Ptu.$('toast');
      if (!el) return;
      el.textContent = msg;
      el.hidden = false;
      clearTimeout(Ptu._toastT);
      Ptu._toastT = setTimeout(() => { el.hidden = true; }, ms);
    },

    futureFeature() {
      Ptu.toast(FUTURE_FEATURE_MESSAGE);
      return false;
    },

    maybeShowLogButton() {
      if (!Ptu.$('log-toggle')) {
        const btn = document.createElement('button');
        btn.id = 'log-toggle';
        btn.className = 'log-toggle';
        btn.title = '显示/隐藏日志';
        btn.setAttribute('aria-label', '显示或隐藏任务日志');
        btn.textContent = '📋';
        btn.addEventListener('click', () => Ptu.toggleLogPanel());
        document.body.appendChild(btn);
      }
    },

    toggleLogPanel(force) {
      const panel = Ptu.$('log-panel');
      const show = force === undefined ? panel.hidden : force;
      panel.hidden = !show;
      Ptu.State.nativeBrowserSuppressed = show;
      Ptu.syncNativeBrowserFrame();
      if (show) Ptu.refreshLogPanel();
    },

    async refreshLogPanel() {
      try {
        const r = await Ptu.Api.tailLogs();
        const el = Ptu.$('log-content');
        el.innerHTML = (r.entries || [])
          .map(e => `<div class="log-entry-${(e.level || 'log').toLowerCase()}">[${Ptu.fmtTs(e.ts)}] ${Ptu.escape(e.msg)}</div>`)
          .join('');
        el.scrollTop = el.scrollHeight;
      } catch (e) { /* ignore */ }
    },

    appendLog(entry) {
      const el = Ptu.$('log-content');
      if (!el) return;
      const line = document.createElement('div');
      line.className = `log-entry-${(entry.level || 'log').toLowerCase()}`;
      line.textContent = `[${Ptu.fmtTs(entry.ts)}] ${entry.msg}`;
      el.appendChild(line);
      // 截断到 500 行
      while (el.children.length > 500) el.removeChild(el.firstChild);
      el.scrollTop = el.scrollHeight;
    },

    fmtTs(ts) {
      const d = new Date(ts * 1000);
      return d.toTimeString().slice(0, 8);
    },

    escape(s) {
      if (s == null) return '';
      return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },
  });
  return ctx;
}

init(Ptu);
