// 阶段 5：desktop 模块。
import { Ptu, global } from "./state.js";

export function init(ctx = Ptu) {
  Object.assign(ctx, {
    init() {
      const saved = Ptu.safeLocalGet('weishushu.theme');
      if (saved === 'dark' || saved === 'light') {
        Ptu.State.theme = saved;
        document.documentElement.setAttribute('data-theme', saved);
      }
      Ptu.State.isDesktop = !!(global.pywebview && global.pywebview.api);
      Ptu.bindEvents();
      Ptu.updateMainScopeUi();
      Ptu.initSplitView();  // v1.2.0 stage4 B+
      global.addEventListener('pywebviewready', () => Ptu.onDesktopReady(), { once: true });
      global.addEventListener('beforeunload', () => Ptu.cleanupQrcodeOnUnload());
      if (Ptu.State.isDesktop) Ptu.onDesktopReady();
      // v1.2.0 V120-1：先 check 首启风险须知（未接受则弹模态，阻塞其他 UI）
      Ptu.checkFirstRun().then((accepted) => {
        Ptu.State.firstRunAccepted = accepted;
        if (accepted) Ptu.checkRecoveryTask();
        Ptu.refreshLoginStatus();
      });
    },

    async onDesktopReady() {
      Ptu.State.isDesktop = true;
      try {
        Ptu.State.platform = await Ptu.Api.getPlatform();
      } catch (e) {
        Ptu.State.platform = null;
      }
      const qrBtn = Ptu.$('btn-qrcode');
      const chromeBtn = Ptu.$('btn-chrome');
      if (qrBtn) qrBtn.textContent = '扫码登录';
      if (chromeBtn) chromeBtn.hidden = false;
      Ptu.syncNativeBrowserFrame();
    },

    maybeOpenMacBrowser() {
      return false;
    },

    // ====== v1.2.0 V120-1: 首启风险须知 ======

    async copyCurrentUrl() {
      if (!Ptu.State.isDesktop) {
        Ptu.toast('仅桌面版可用');
        return;
      }
      try {
        const r = await Ptu.Api.copyUrlToMain();
        if (r && r.ok) {
          Ptu.$('url-input').value = r.url;
          Ptu.toast('已复制到主区');
        } else {
          Ptu.toast((r && r.error) || '复制失败');
        }
      } catch (e) {
        Ptu.toast('复制失败：' + e.message);
      }
    },

    // ====== Mac 同窗浏览器 / Windows 历史独立窗口 ======

    async openBrowser() {
      Ptu.futureFeature();
      return false;
      if (!Ptu.State.isDesktop) {
        Ptu.toast('浏览器窗口仅桌面版可用');
        return false;
      }
      try {
        Ptu.State.browserOpening = true;
        Ptu.setBrowserPanelVisible(true);
        const r = await Ptu.Api.openBrowserWindow();
        if (r && r.ok) {
          Ptu.toast(r.embedded ? '内置浏览器已展开' : '浏览器窗口已打开');
          // 更新状态徽标
          const badge = Ptu.$('browser-status');
          if (badge) {
            badge.textContent = '已打开';
            badge.className = 'badge badge-success';
          }
          Ptu.syncNativeBrowserFrame();
          // 拉一次 URL 显示
          await Ptu.refreshBrowserUrlDisplay();
          if (!Ptu.State.loggedIn) Ptu.startLoginStatusWatch();
          return true;
        }
        Ptu.setBrowserPanelVisible(false);
        Ptu.toast('打开失败：' + (r && r.error ? r.error : '未知错误'));
        return false;
      } catch (e) {
        Ptu.setBrowserPanelVisible(false);
        Ptu.toast('打开失败：' + e.message);
        return false;
      } finally {
        Ptu.State.browserOpening = false;
      }
    },

    // ====== v1.2.0 收口 A + stage4 B+: 浏览器控制台面板（已迁右栏常驻） ======

    toggleBrowserPanel(force) {
      const next = force === undefined ? !Ptu.State.browserPanelVisible : !!force;
      Ptu.setBrowserPanelVisible(next);
      if (next) Ptu.refreshBrowserUrlDisplay();
    },

    setBrowserPanelVisible(visible) {
      const root = Ptu.$('app-main');
      const area = Ptu.$('browser-area');
      if (!root || !area) return;
      Ptu.State.browserPanelVisible = !!visible;
      root.classList.toggle('browser-console-hidden', !visible);
      area.hidden = !visible;
      area.setAttribute('aria-hidden', visible ? 'false' : 'true');
      if (visible) {
        root.style.gridTemplateColumns = '';
        Ptu.applySplitRatio(Ptu.State.splitRatio);
      } else {
        root.style.gridTemplateColumns = 'minmax(0, 1fr) 0 0';
      }
      requestAnimationFrame(() => requestAnimationFrame(() => Ptu.syncNativeBrowserFrame()));
    },

    // ====== v1.2.0 stage4 B+: 主窗口分屏 splitter ======

    initSplitView() {
      const root = Ptu.$('app-main');
      const splitter = Ptu.$('splitter');
      if (!root || !splitter) return;

      const saved = Number(Ptu.safeLocalGet('weishushu.splitRatio'));
      if (Number.isFinite(saved) && saved >= 0.5 && saved <= 0.78) {
        Ptu.State.splitRatio = saved;
      }
      Ptu.setBrowserPanelVisible(false);

      const slot = Ptu.$('native-browser-slot');
      if (slot && typeof ResizeObserver !== 'undefined') {
        Ptu.nativeFrameObserver = new ResizeObserver(() => Ptu.syncNativeBrowserFrame());
        Ptu.nativeFrameObserver.observe(slot);
        Ptu.nativeFrameObserver.observe(root);
      }

      let dragging = false;

      splitter.addEventListener('mousedown', (event) => {
        dragging = true;
        splitter.classList.add('dragging');
        document.body.style.cursor = 'col-resize';
        event.preventDefault();
      });

      document.addEventListener('mousemove', (event) => {
        if (!dragging) return;
        const rect = root.getBoundingClientRect();
        const ratio = (event.clientX - rect.left) / rect.width;
        Ptu.applySplitRatio(ratio);
      });

      document.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        splitter.classList.remove('dragging');
        document.body.style.cursor = '';
        Ptu.safeLocalSet('weishushu.splitRatio', String(Ptu.State.splitRatio));
        Ptu.syncNativeBrowserFrame();
      });
    },

    applySplitRatio(ratio) {
      const root = Ptu.$('app-main');
      if (!root) return;
      const next = Math.max(0.5, Math.min(0.78, ratio));
      Ptu.State.splitRatio = next;
      // v2.0.0 阶段 4：用 innerWidth 替代 matchMedia，避免 Windows WebView2 高 DPI 误判
      const isCompact = global.innerWidth <= 1100;
      if (isCompact) {
        root.style.removeProperty('grid-template-columns');
      } else {
        root.style.gridTemplateColumns = `minmax(560px, ${next}fr) 7px minmax(360px, ${1 - next}fr)`;
      }
      Ptu.syncNativeBrowserFrame();
    },

    syncNativeBrowserFrame() {
      // v2.0.0 阶段 4：非 darwin 平台短路，避免 Windows WebView2 无效 RPC 噪音
      if (!Ptu.State.isDesktop || Ptu.State.platform !== 'darwin' || !Ptu.Api.setBrowserFrame) return;
      const slot = Ptu.$('native-browser-slot');
      const visible = !!(
        Ptu.State.browserPanelVisible
        && !Ptu.State.nativeBrowserSuppressed
        && slot
        && !slot.closest('[hidden]')
      );
      const rect = visible ? slot.getBoundingClientRect() : null;
      const frame = rect && rect.width > 0 && rect.height > 0
        ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height, visible: true }
        : { x: 0, y: 0, width: 1, height: 1, visible: false };
      Ptu.Api.setBrowserFrame(frame).catch(() => {});
    },

    async refreshBrowserUrlDisplay() {
      const el = Ptu.$('browser-current-url');
      if (!el) return;
      try {
        const url = await Ptu.Api.getBrowserCurrentUrl();
        if (url) {
          el.textContent = url;
          const badge = Ptu.$('browser-status');
          if (badge) {
            badge.textContent = '已打开';
            badge.className = 'badge badge-success';
          }
        } else {
          el.textContent = '未打开浏览器窗口';
          const badge = Ptu.$('browser-status');
          if (badge) {
            badge.textContent = '未打开';
            badge.className = 'badge badge-muted';
          }
        }
      } catch (e) {
        el.textContent = '获取 URL 失败';
      }
    },

    async browserAction(action) {
      if (!Ptu.State.isDesktop) {
        Ptu.toast('仅桌面版可用');
        return;
      }
      try {
        let r;
        if (action === 'back') r = await Ptu.Api.browserBack();
        else if (action === 'forward') r = await Ptu.Api.browserForward();
        else if (action === 'refresh') r = await Ptu.Api.refreshBrowser();
        else if (action === 'sync') r = await Ptu.Api.syncBrowserLogin();
        else if (action === 'inject') r = await Ptu.Api.injectCookies();
        else if (action === 'copy') {
          await Ptu.copyCurrentUrl();
          return;
        } else if (action === 'close') r = await Ptu.Api.closeBrowserWindow();
        if (r && !r.ok) {
          Ptu.toast(r.error || '操作失败');
          return;
        }
        // 刷新 URL 显示
        setTimeout(() => Ptu.refreshBrowserUrlDisplay(), 300);
        if (action === 'close') {
          Ptu.toggleBrowserPanel(false);
          const badge = Ptu.$('browser-status');
          if (badge) {
            badge.textContent = '已关闭';
            badge.className = 'badge badge-muted';
          }
          Ptu.toast('浏览器已关闭');
        } else if (action === 'sync' && r) {
          Ptu.toast('正在校验内置浏览器登录状态');
          setTimeout(() => Ptu.refreshLoginStatus(), 1200);
        } else if (action === 'inject' && r) {
          Ptu.toast(`已注入 ${r.success || 0} 个 cookie${r.failed ? `（${r.failed} 失败）` : ''}`);
        }
      } catch (e) {
        Ptu.toast('操作失败：' + e.message);
      }
    },

    bindEvents() {
      const themeToggle = Ptu.$('theme-toggle');
      if (themeToggle) themeToggle.addEventListener('click', () => Ptu.toggleTheme());
      const loginMenuToggle = Ptu.$('login-menu-toggle');
      if (loginMenuToggle) loginMenuToggle.addEventListener('click', () => Ptu.toggleLoginMenu());
      // 首屏博主搜索 / 链接识别
      const btnSearchBlogger = Ptu.$('btn-search-blogger');
      if (btnSearchBlogger) btnSearchBlogger.addEventListener('click', () => Ptu.searchOrResolve());
      const urlInput = Ptu.$('url-input');
      if (urlInput) {
        urlInput.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') Ptu.searchOrResolve();
        });
      }
      const checkAll = Ptu.$('check-all');
      if (checkAll) checkAll.addEventListener('change', (e) => Ptu.toggleCheckAll(e.target.checked));
      // 步骤 4
      const btnStart = Ptu.$('btn-start');
      if (btnStart) btnStart.addEventListener('click', () => Ptu.futureFeature());
      const btnCancelTask = Ptu.$('btn-cancel-task');
      if (btnCancelTask) btnCancelTask.addEventListener('click', () => Ptu.cancelTask());
      const btnOpenFolder = Ptu.$('btn-open-folder');
      if (btnOpenFolder) btnOpenFolder.addEventListener('click', () => Ptu.openOutputFolder());
      const recoveryResume = Ptu.$('recovery-task-resume');
      if (recoveryResume) recoveryResume.addEventListener('click', () => Ptu.resumeRecoveryTask());
      const recoveryLater = Ptu.$('recovery-task-later');
      if (recoveryLater) recoveryLater.addEventListener('click', () => Ptu.deferRecoveryTask());
      const recoveryOpen = Ptu.$('recovery-task-open-saved');
      if (recoveryOpen) recoveryOpen.addEventListener('click', () => Ptu.openRecoverySavedContent());
      const recoveryAbandon = Ptu.$('recovery-task-abandon');
      if (recoveryAbandon) recoveryAbandon.addEventListener('click', () => Ptu.abandonRecoveryTask());
      const closeMinimize = Ptu.$('close-protection-minimize');
      if (closeMinimize) closeMinimize.addEventListener('click', () => Ptu.minimizeAndContinue());
      const closePauseExit = Ptu.$('close-protection-pause-exit');
      if (closePauseExit) closePauseExit.addEventListener('click', () => Ptu.pauseThenClose());
      // 步骤 2 登录
      const qrBtn = Ptu.$('btn-qrcode');
      if (qrBtn) qrBtn.addEventListener('click', () => Ptu.loginQrcode());
      const chBtn = Ptu.$('btn-chrome');
      if (chBtn) chBtn.addEventListener('click', () => Ptu.futureFeature());
      const logoutBtn = Ptu.$('btn-logout');
      if (logoutBtn) logoutBtn.addEventListener('click', () => Ptu.logout());
      const qrcodeRetry = Ptu.$('qrcode-login-retry');
      if (qrcodeRetry) qrcodeRetry.addEventListener('click', () => Ptu.loginQrcode());
      const qrcodeCancel = Ptu.$('qrcode-login-cancel');
      if (qrcodeCancel) qrcodeCancel.addEventListener('click', () => Ptu.cancelQrcodeLogin());
      // v1.1.5 一键备份本人
      const backupBtn = Ptu.$('btn-backup-self');
      if (backupBtn) backupBtn.addEventListener('click', () => Ptu.backupSelf());
      const mainScope = Ptu.$('set-scope');
      if (mainScope) mainScope.addEventListener('change', () => Ptu.updateMainScopeUi());
      const archiveOverlay = Ptu.$('archive-folder-decision');
      if (archiveOverlay) archiveOverlay.addEventListener('click', (event) => {
        if (event.target === archiveOverlay) Ptu.closeArchiveFolderDecision();
      });
      const archiveClose = Ptu.$('archive-folder-close');
      if (archiveClose) archiveClose.addEventListener('click', () => Ptu.closeArchiveFolderDecision());
      document.querySelectorAll('[data-archive-mode]').forEach((button) => {
        button.addEventListener('click', () => Ptu.startArchiveBackup(button.dataset.archiveMode));
      });
      const archiveFollowing = Ptu.$('archive-action-following');
      if (archiveFollowing) archiveFollowing.addEventListener('click', () => Ptu.startFollowingArchive());
      const archiveReselect = Ptu.$('archive-action-reselect');
      if (archiveReselect) archiveReselect.addEventListener('click', () => Ptu.reselectArchiveFolder());
      const archiveCreateSubfolder = Ptu.$('archive-action-create-subfolder');
      if (archiveCreateSubfolder) archiveCreateSubfolder.addEventListener('click', () => Ptu.createArchiveInSelectedFolder());
      document.querySelectorAll('input[name="archive-pacing-mode"]').forEach((input) => {
        input.addEventListener('change', () => Ptu.syncArchivePacingControls());
      });
      document.addEventListener('keydown', (event) => Ptu.handleArchiveDialogKeydown(event));
      // v1.1.6 历史记录面板 + 搜索
      const historyPanel = Ptu.$('history-panel');
      if (historyPanel) Ptu.State.historyPanelHome = historyPanel.parentElement;
      const histBtn = Ptu.$('history-toggle');
      if (histBtn) histBtn.addEventListener('click', () => Ptu.toggleHistory(true));
      const histClose = Ptu.$('history-close');
      if (histClose) histClose.addEventListener('click', () => Ptu.toggleHistory(false));
      const pickFolderBtn = Ptu.$('btn-pick-history-folder');
      if (pickFolderBtn) pickFolderBtn.addEventListener('click', () => Ptu.pickHistoryFolder());
      const searchBtn = Ptu.$('btn-search');
      if (searchBtn) searchBtn.addEventListener('click', () => Ptu.runSearch());
      // 浮动日志
      const logBtn = Ptu.$('log-toggle');
      if (logBtn) {
        logBtn.addEventListener('click', () => Ptu.toggleLogPanel());
      }
      const logClose = Ptu.$('log-close');
      if (logClose) logClose.addEventListener('click', () => Ptu.toggleLogPanel(false));
      // v1.2.0 V120-2: 浏览器按钮
      const browserBtn = Ptu.$('btn-browser');
      if (browserBtn) browserBtn.addEventListener('click', () => Ptu.futureFeature());
      // v1.2.0 收口 A: 浏览器控制台面板按钮
      const browserPanelClose = Ptu.$('browser-panel-close');
      if (browserPanelClose) browserPanelClose.addEventListener('click', () => Ptu.toggleBrowserPanel(false));
      const btnBrowserBack = Ptu.$('btn-browser-back');
      if (btnBrowserBack) btnBrowserBack.addEventListener('click', () => Ptu.browserAction('back'));
      const btnBrowserForward = Ptu.$('btn-browser-forward');
      if (btnBrowserForward) btnBrowserForward.addEventListener('click', () => Ptu.browserAction('forward'));
      const btnBrowserRefresh = Ptu.$('btn-browser-refresh');
      if (btnBrowserRefresh) btnBrowserRefresh.addEventListener('click', () => Ptu.browserAction('refresh'));
      const btnBrowserSync = Ptu.$('btn-browser-sync');
      if (btnBrowserSync) btnBrowserSync.addEventListener('click', () => Ptu.browserAction('sync'));
      const btnBrowserCopy = Ptu.$('btn-browser-copy');
      if (btnBrowserCopy) btnBrowserCopy.addEventListener('click', () => Ptu.browserAction('copy'));
      const btnBrowserClose = Ptu.$('btn-browser-close');
      if (btnBrowserClose) btnBrowserClose.addEventListener('click', () => Ptu.browserAction('close'));
    },

    toggleTheme() {
      Ptu.State.theme = Ptu.State.theme === 'light' ? 'dark' : 'light';
      document.documentElement.setAttribute('data-theme', Ptu.State.theme);
      Ptu.safeLocalSet('weishushu.theme', Ptu.State.theme);
    },
  });
  return ctx;
}

init(Ptu);
if (!Ptu.__domReadyBound) {
  Ptu.__domReadyBound = true;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => Ptu.init(), { once: true });
  } else {
    queueMicrotask(() => Ptu.init());
  }
}
