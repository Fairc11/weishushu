// 微书薯前端 API 客户端：HTTP 请求与桌面桥调用统一从这里出入口。
(function (global) {
  'use strict';

  const desktop = global.WeishushuDesktopBridge;
  const BACKUP_REQUEST_TIMEOUT_MS = 15000;

  const apiClient = {
    async _fetch(path, opts = {}) {
      const r = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        ...opts,
      });
      if (!r.ok) {
        let detail = r.statusText;
        try { detail = (await r.json()).detail || detail; } catch (e) { /* ignore */ }
        throw new Error(`${r.status} ${detail}`);
      }
      return r.json();
    },

    profileResolve(url) {
      return this._fetch('/api/profile/resolve', { method: 'POST', body: JSON.stringify({ url }) });
    },

    preview(url, count = 20) {
      return this._fetch('/api/scrape/preview', { method: 'POST', body: JSON.stringify({ url, count }) });
    },

    start(req) {
      return this._fetch('/api/scrape/start', { method: 'POST', body: JSON.stringify(req) });
    },

    cancelTask(taskId) {
      return this._fetch(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' });
    },

    recoveryTask() {
      return this._fetch('/api/tasks/recovery');
    },

    pauseTask(taskId) {
      return this._fetch(`/api/tasks/${encodeURIComponent(taskId)}/pause`, { method: 'POST' });
    },

    taskStatus(taskId) {
      return this._fetch(`/api/tasks/${encodeURIComponent(taskId)}`);
    },

    resumeTask(taskId) {
      return this._fetch(`/api/tasks/${encodeURIComponent(taskId)}/resume`, { method: 'POST' });
    },

    abandonTask(taskId) {
      return this._fetch(`/api/tasks/${encodeURIComponent(taskId)}/abandon`, { method: 'POST' });
    },

    openActiveTaskFolder(taskId) {
      return desktop.openActiveTaskFolder(taskId);
    },

    closeAfterPause(taskId) {
      return desktop.closeAfterPause(taskId);
    },

    loginStatus() {
      return this._fetch('/api/login/status');
    },

    logout() {
      return this._fetch('/api/login/logout', { method: 'POST' });
    },

    loginQrcode() {
      return this._fetch('/api/login/qrcode', { method: 'POST' });
    },

    qrcodeStatus(sessionId) {
      return this._fetch(`/api/login/qrcode/${encodeURIComponent(sessionId)}/status`);
    },

    async qrcodeImage(sessionId) {
      const r = await fetch(`/api/login/qrcode/${encodeURIComponent(sessionId)}/image`, {
        headers: { 'Cache-Control': 'no-store', Pragma: 'no-cache' },
      });
      if (!r.ok) {
        let detail = r.statusText;
        try { detail = (await r.json()).detail || detail; } catch (e) { /* ignore */ }
        throw new Error(`${r.status} ${detail}`);
      }
      return r.blob();
    },

    qrcodeCancel(sessionId, options = {}) {
      return this._fetch(`/api/login/qrcode/${encodeURIComponent(sessionId)}/cancel`, {
        method: 'POST',
        keepalive: !!options.keepalive,
      });
    },

    loginChrome() {
      return this._fetch('/api/login/chrome', { method: 'POST' });
    },

    tailLogs() {
      return this._fetch('/api/logs/?tail=200');
    },

    whoami() {
      return this._fetch('/api/login/whoami');
    },

    async _backupFetch(path, req) {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), BACKUP_REQUEST_TIMEOUT_MS);
      try {
        return await this._fetch(path, {
          method: 'POST',
          body: JSON.stringify(req),
          signal: controller.signal,
        });
      } catch (error) {
        if (error && error.name === 'AbortError') {
          throw new Error('请求超时，请重试');
        }
        throw error;
      } finally {
        clearTimeout(timeoutId);
      }
    },

    backupInspect(req) {
      return this._backupFetch('/api/backup/inspect', req);
    },

    backupStart(req) {
      return this._fetch('/api/backup/start', { method: 'POST', body: JSON.stringify(req) });
    },

    searchUsers(query) {
      return this._fetch('/api/search/users', { method: 'POST', body: JSON.stringify({ query }) });
    },

    resolveTarget(text) {
      return this._fetch('/api/search/resolve', { method: 'POST', body: JSON.stringify({ text }) });
    },

    followingStart(req) {
      return this._fetch('/api/following/start', { method: 'POST', body: JSON.stringify(req) });
    },

    backupList(path) {
      return this._fetch(`/api/backup/list?path=${encodeURIComponent(path)}`, { method: 'POST' });
    },

    backupSearch(path, q) {
      return this._fetch(`/api/backup/search?path=${encodeURIComponent(path)}&q=${encodeURIComponent(q)}`, { method: 'POST' });
    },

    firstRunCheck() {
      return this._fetch('/api/first-run/check', { method: 'POST' });
    },

    firstRunAccept() {
      return this._fetch('/api/first-run/accept', { method: 'POST' });
    },

    openBrowserWindow() {
      return desktop.openBrowserWindow();
    },

    setBrowserFrame(frame) {
      return desktop.setBrowserFrame(frame);
    },

    getPlatform() {
      return desktop.getPlatform();
    },

    copyUrlToMain() {
      return desktop.copyUrlToMain();
    },

    getCopiedUrl() {
      return desktop.getCopiedUrl();
    },

    refreshBrowser() {
      return desktop.refreshBrowser();
    },

    closeBrowserWindow() {
      return desktop.closeBrowserWindow();
    },

    getBrowserCurrentUrl() {
      return desktop.getBrowserCurrentUrl();
    },

    browserBack() {
      return desktop.browserBack();
    },

    browserForward() {
      return desktop.browserForward();
    },

    injectCookies() {
      return desktop.injectCookies();
    },

    syncBrowserLogin() {
      return desktop.syncBrowserLogin();
    },
  };

  global.WeishushuApi = apiClient;
})(window);
