// pywebview 桌面桥集中层。普通浏览器模式下只返回中文错误或 null。
(function (global) {
  'use strict';

  function nativeApi() {
    return global.pywebview && global.pywebview.api ? global.pywebview.api : null;
  }

  function unavailable() {
    return Promise.reject(new Error('pywebview 不可用'));
  }

  const bridge = {
    isAvailable() {
      return !!nativeApi();
    },

    call(name, ...args) {
      const api = nativeApi();
      if (!api || typeof api[name] !== 'function') {
        return unavailable();
      }
      return api[name](...args);
    },

    openBrowserWindow() {
      return this.call('open_browser_window');
    },

    setBrowserFrame(frame) {
      return this.call('set_browser_frame', frame);
    },

    getPlatform() {
      return this.call('get_platform');
    },

    copyUrlToMain() {
      return this.call('copy_url_to_main');
    },

    getCopiedUrl() {
      const api = nativeApi();
      if (!api || typeof api.get_copied_url !== 'function') {
        return Promise.resolve(null);
      }
      return api.get_copied_url();
    },

    refreshBrowser() {
      return this.call('refresh_browser');
    },

    closeBrowserWindow() {
      return this.call('close_browser_window');
    },

    getBrowserCurrentUrl() {
      const api = nativeApi();
      if (!api || typeof api.get_browser_current_url !== 'function') {
        return Promise.resolve(null);
      }
      return api.get_browser_current_url();
    },

    browserBack() {
      return this.call('browser_back');
    },

    browserForward() {
      return this.call('browser_forward');
    },

    injectCookies() {
      return this.call('inject_cookies');
    },

    syncBrowserLogin() {
      return this.call('sync_browser_login');
    },

    openActiveTaskFolder(taskId) {
      return this.call('open_active_task_folder', taskId);
    },

    closeAfterPause(taskId) {
      return this.call('close_after_pause', taskId);
    },
  };

  global.WeishushuDesktopBridge = bridge;
})(window);
