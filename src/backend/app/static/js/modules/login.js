// 阶段 5：login 模块。
import { Ptu, global } from "./state.js";
import { init as initFeedback } from "./feedback.js";
void initFeedback;

export function init(ctx = Ptu) {
  Object.assign(ctx, {
    async checkFirstRun() {
      try {
        const r = await Ptu.Api.firstRunCheck();
        if (r.accepted) return true;  // 已接受，跳过模态
        Ptu.showRiskModal();
        return false;
      } catch (e) {
        // API 失败时不阻塞（dev 模式可能没启动后端）
        console.warn('first-run check failed', e);
        return false;
      }
    },

    showRiskModal() {
      const overlay = Ptu.$('risk-modal-overlay');
      const scroller = Ptu.$('risk-scroll');
      const hint = Ptu.$('risk-scroll-hint');
      const confirmBtn = Ptu.$('risk-confirm-btn');
      const cancelBtn = Ptu.$('risk-cancel-btn');
      if (!overlay || !scroller || !confirmBtn || !cancelBtn) return;

      overlay.hidden = false;

      // 滚动阅读至底部才可确认；内容无需滚动时（大窗口）直接放行
      const updateConfirmState = () => {
        const noOverflow = scroller.scrollHeight <= scroller.clientHeight + 1;
        const reachedEnd = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
        if (noOverflow || reachedEnd) {
          confirmBtn.disabled = false;
          if (hint) hint.hidden = true;
        }
      };
      updateConfirmState();

      if (Ptu.State.riskModalBound) return;
      Ptu.State.riskModalBound = true;

      scroller.addEventListener('scroll', updateConfirmState, { passive: true });
      global.addEventListener('resize', updateConfirmState);

      confirmBtn.addEventListener('click', async () => {
        try {
          await Ptu.Api.firstRunAccept();
          Ptu.State.firstRunAccepted = true;
          overlay.hidden = true;
          Ptu.toast('已接受 v2.0.1 风险须知');
        } catch (e) {
          Ptu.toast('接受失败：' + e.message);
        }
      });

      cancelBtn.addEventListener('click', () => {
        if (!confirm('拒绝风险须知将无法使用。继续？')) return;
        if (Ptu.State.isDesktop && global.pywebview && global.pywebview.api && global.pywebview.api.close_window) {
          try { global.pywebview.api.close_window(); return; } catch (e) { /* fallthrough */ }
        }
        // 回落：dev 模式或桌面 close_window 不可用时，关 overlay 放行部分 UI
        overlay.hidden = true;
        Ptu.toast('已拒绝风险须知，部分功能不可用');
      });
    },

    // ====== 从内置浏览器复制 URL ======

    toggleLoginMenu(force) {
      const menu = Ptu.$('login-menu');
      const toggle = Ptu.$('login-menu-toggle');
      if (!menu || !toggle) return;
      const open = force === undefined ? !Ptu.State.loginMenuOpen : !!force;
      Ptu.State.loginMenuOpen = open;
      menu.hidden = !open;
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    },

    async refreshLoginStatus() {
      const badge = Ptu.$('login-badge');
      const logoutBtn = Ptu.$('btn-logout');
      try {
        const r = await Ptu.Api.loginStatus();
        Ptu.State.loggedIn = r.logged_in;
        Ptu.State.loginSource = r.cookie_source;
        if (logoutBtn) logoutBtn.hidden = !r.logged_in;
        if (r.logged_in) {
          if (Ptu._loginWatchTimer) {
            clearInterval(Ptu._loginWatchTimer);
            Ptu._loginWatchTimer = null;
          }
          if (badge) {
            badge.textContent = `已登录（${r.cookie_source}）`;
            badge.className = 'badge badge-success';
          }
          // v1.1.5 已登录 → 显示一键备份按钮
          const backupBlock = Ptu.$('backup-self-block');
          if (backupBlock) backupBlock.hidden = false;
        } else {
          if (badge) {
            badge.textContent = '未登录';
            badge.className = 'badge badge-muted';
          }
        }
      } catch (e) {
        if (badge) {
          badge.textContent = '状态未知';
          badge.className = 'badge badge-warn';
        }
      }
    },

    async logout() {
      if (!confirm('退出登录将删除本机保存的微博登录态，下次使用需要重新扫码。继续？')) return;
      Ptu.toggleLoginMenu(false);
      try {
        await Ptu.Api.logout();
        Ptu.toast('已退出登录');
      } catch (e) {
        Ptu.toast('退出登录失败：' + e.message);
      }
      await Ptu.refreshLoginStatus();
    },

    async syncBuiltinBrowserLoginStatus() {
      try {
        await Ptu.Api.syncBrowserLogin();
      } catch (e) {
        // 浏览器尚未形成有效登录态属于正常等待阶段，状态由后端统一显示。
      }
      await Ptu.refreshLoginStatus();
    },

    startLoginStatusWatch() {
      if (Ptu._loginWatchTimer) clearInterval(Ptu._loginWatchTimer);
      const startedAt = Date.now();
      Ptu._loginWatchBusy = false;
      const tick = async () => {
        if (
          Ptu.State.loggedIn
          || !Ptu.State.browserPanelVisible
          || Date.now() - startedAt > 180000
        ) {
          clearInterval(Ptu._loginWatchTimer);
          Ptu._loginWatchTimer = null;
          return;
        }
        if (Ptu._loginWatchBusy) return;
        Ptu._loginWatchBusy = true;
        try {
          await Ptu.syncBuiltinBrowserLoginStatus();
        } finally {
          Ptu._loginWatchBusy = false;
        }
      };
      Ptu._loginWatchTimer = setInterval(tick, 2000);
      tick();
    },

    async loginWithBuiltinBrowser() {
      return Ptu.loginQrcode();
    },

    stopQrcodePolling() {
      if (Ptu._qrcodePollTimer) {
        clearInterval(Ptu._qrcodePollTimer);
        Ptu._qrcodePollTimer = null;
      }
      Ptu.State.qrcodePollBusy = false;
    },

    resetQrcodeImage() {
      if (
        Ptu.State.qrcodeBlobUrl
        && globalThis.URL
        && typeof globalThis.URL.revokeObjectURL === 'function'
      ) {
        globalThis.URL.revokeObjectURL(Ptu.State.qrcodeBlobUrl);
      }
      Ptu.State.qrcodeBlobUrl = null;
      const image = Ptu.$('qrcode-login-image');
      if (image) {
        image.src = '';
        image.hidden = true;
      }
    },

    showQrcodeModal() {
      const overlay = Ptu.$('qrcode-login-overlay');
      const message = Ptu.$('qrcode-login-message');
      const remaining = Ptu.$('qrcode-login-remaining');
      const retry = Ptu.$('qrcode-login-retry');
      const cancel = Ptu.$('qrcode-login-cancel');
      const placeholder = Ptu.$('qrcode-login-placeholder');
      Ptu.resetQrcodeImage();
      if (overlay) overlay.hidden = false;
      if (message) message.textContent = '正在准备二维码';
      if (remaining) remaining.textContent = '';
      if (retry) retry.hidden = true;
      if (cancel) cancel.textContent = '取消';
      if (placeholder) {
        placeholder.hidden = false;
        placeholder.textContent = '正在连接微博…';
      }
    },

    closeQrcodeModalLocally() {
      Ptu.stopQrcodePolling();
      Ptu.resetQrcodeImage();
      const overlay = Ptu.$('qrcode-login-overlay');
      if (overlay) overlay.hidden = true;
      Ptu.State.qrcodeSessionId = null;
      Ptu.State.qrcodeTaskId = null;
      Ptu.State.qrcodeTerminal = false;
      Ptu.State.qrcodeCreating = false;
    },

    async pollQrcodeStatus() {
      const sessionId = Ptu.State.qrcodeSessionId;
      if (!sessionId || Ptu.State.qrcodePollBusy) return;
      Ptu.State.qrcodePollBusy = true;
      try {
        const status = await Ptu.Api.qrcodeStatus(sessionId);
        if (sessionId !== Ptu.State.qrcodeSessionId) return;
        const message = Ptu.$('qrcode-login-message');
        const remaining = Ptu.$('qrcode-login-remaining');
        const retry = Ptu.$('qrcode-login-retry');
        const cancel = Ptu.$('qrcode-login-cancel');
        const placeholder = Ptu.$('qrcode-login-placeholder');
        if (message) message.textContent = status.message || '正在等待扫码';
        if (remaining) {
          remaining.textContent = status.remaining_seconds > 0
            ? `二维码剩余 ${status.remaining_seconds} 秒`
            : '';
        }
        if (status.image_ready && !Ptu.State.qrcodeBlobUrl) {
          const blob = await Ptu.Api.qrcodeImage(sessionId);
          if (sessionId !== Ptu.State.qrcodeSessionId) return;
          const image = Ptu.$('qrcode-login-image');
          Ptu.State.qrcodeBlobUrl = globalThis.URL.createObjectURL(blob);
          if (image) {
            image.src = Ptu.State.qrcodeBlobUrl;
            image.hidden = false;
          }
          if (placeholder) placeholder.hidden = true;
        }
        if (status.state === 'authenticated') {
          Ptu.closeQrcodeModalLocally();
          await Ptu.refreshLoginStatus();
          Ptu.toast('登录成功，现在可以备份本人微博');
          return;
        }
        if (status.state === 'expired' || status.state === 'error') {
          Ptu.stopQrcodePolling();
          Ptu.resetQrcodeImage();
          Ptu.State.qrcodeTerminal = true;
          if (placeholder) {
            placeholder.hidden = false;
            placeholder.textContent = status.state === 'expired' ? '二维码已过期' : '二维码登录失败';
          }
          if (retry) retry.hidden = false;
          if (cancel) cancel.textContent = '关闭';
          return;
        }
        if (status.state === 'cancelled') {
          Ptu.closeQrcodeModalLocally();
        }
      } catch (e) {
        const message = Ptu.$('qrcode-login-message');
        if (message) message.textContent = `登录状态暂时无法获取，将自动重试：${e.message}`;
      } finally {
        Ptu.State.qrcodePollBusy = false;
      }
    },

    async loginQrcode() {
      if (
        Ptu.State.qrcodeCreating
        || (Ptu.State.qrcodeSessionId && !Ptu.State.qrcodeTerminal)
      ) {
        Ptu.toast('二维码登录正在进行中');
        Ptu.toggleLoginMenu(false);
        return;
      }
      Ptu.closeQrcodeModalLocally();
      Ptu.showQrcodeModal();
      Ptu.toggleLoginMenu(false);
      const requestGeneration = Ptu.State.qrcodeRequestGeneration + 1;
      Ptu.State.qrcodeRequestGeneration = requestGeneration;
      Ptu.State.qrcodeCreating = true;
      try {
        const r = await Ptu.Api.loginQrcode();
        if (requestGeneration !== Ptu.State.qrcodeRequestGeneration) {
          await Ptu.Api.qrcodeCancel(r.session_id);
          return;
        }
        Ptu.State.qrcodeSessionId = r.session_id;
        Ptu.State.qrcodeTaskId = r.task_id;
        Ptu.State.qrcodeTerminal = false;
        await Ptu.pollQrcodeStatus();
        if (Ptu.State.qrcodeSessionId && !Ptu.State.qrcodeTerminal) {
          Ptu._qrcodePollTimer = setInterval(() => Ptu.pollQrcodeStatus(), 1000);
        }
      } catch (e) {
        if (requestGeneration !== Ptu.State.qrcodeRequestGeneration) return;
        Ptu.stopQrcodePolling();
        Ptu.State.qrcodeTerminal = true;
        const message = Ptu.$('qrcode-login-message');
        const retry = Ptu.$('qrcode-login-retry');
        if (message) message.textContent = `登录失败：${e.message}`;
        if (retry) retry.hidden = false;
      } finally {
        if (requestGeneration === Ptu.State.qrcodeRequestGeneration) {
          Ptu.State.qrcodeCreating = false;
        }
      }
    },

    async cancelQrcodeLogin() {
      const sessionId = Ptu.State.qrcodeSessionId;
      const terminal = Ptu.State.qrcodeTerminal;
      Ptu.State.qrcodeRequestGeneration += 1;
      Ptu.closeQrcodeModalLocally();
      if (!sessionId || terminal) return;
      try {
        await Ptu.Api.qrcodeCancel(sessionId);
      } catch (e) {
        Ptu.toast(`取消扫码登录失败：${e.message}`);
      }
    },

    cleanupQrcodeOnUnload() {
      const sessionId = Ptu.State.qrcodeSessionId;
      const terminal = Ptu.State.qrcodeTerminal;
      Ptu.State.qrcodeRequestGeneration += 1;
      Ptu.closeQrcodeModalLocally();
      if (!sessionId || terminal) return;
      const request = Ptu.Api.qrcodeCancel(sessionId, { keepalive: true });
      if (request && typeof request.catch === 'function') request.catch(() => {});
    },

    async loginChrome() {
      Ptu.futureFeature();
      return;
      try {
        const r = await Ptu.Api.loginChrome();
        Ptu.toggleLoginMenu(false);
        Ptu.toast('Chrome 导入任务已创建');
        Ptu.watchTask(r.task_id, 'Chrome 导入');
      } catch (e) {
        Ptu.toast(`导入失败：${e.message}`);
      }
    },

    // ====== v1.1.6 历史记录 + 搜索 ======
  });
  return ctx;
}

init(Ptu);
