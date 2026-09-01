// 阶段 5：共享状态与纯函数。
export const global = globalThis.window || globalThis;
const FUTURE_FEATURE_MESSAGE = '该功能正在开发中。';

export const Ptu = {
    State: {
      theme: 'light',
      loggedIn: false,
      loginSource: null,
      preview: null,            // {user, previews: [...]}
      selectedBids: new Set(),
      backupFolderBusy: false,
      backupStartBusy: false,
      taskStartBusy: false,
      backupFolderDir: null,
      backupFolderInspection: null,
      backupFolderReturnFocus: null,
      archiveTarget: null,        // 备份他人微博：{uid, screen_name}，null = 本人
      archivePacingMode: 'standard',
      taskId: null,
      recoveryTask: null,
      recoveryActionBusy: false,
      ws: null,
      isDesktop: false,
      platform: null,
      historyFolder: null,      // v1.1.6 历史目录
      historyPanelHome: null,
      splitRatio: 0.62,         // 统一工作区默认左/右 62/38
      riskModalBound: false,
      browserPanelVisible: false,
      loginMenuOpen: false,
      nativeBrowserSuppressed: false,
      firstRunAccepted: false,
      browserOpening: false,
      qrcodeSessionId: null,
      qrcodeTaskId: null,
      qrcodeTerminal: false,
      qrcodePollBusy: false,
      qrcodeBlobUrl: null,
      qrcodeRequestGeneration: 0,
      qrcodeCreating: false,
    },
    nativeFrameObserver: null,

    // ====== API 客户端 ======
    Api: global.WeishushuApi || {},
};

export function init(ctx = Ptu) {
  Object.assign(ctx, {
    $(id) { return document.getElementById(id); },

    safeLocalGet(key) {
      try { return localStorage.getItem(key); } catch (e) { return null; }
    },

    safeLocalSet(key, value) {
      try { localStorage.setItem(key, value); return true; } catch (e) { return false; }
    },

    formatNum(n) {
      if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
      return String(n || 0);
    },

    buildScopeRequest(scope, values) {
      const request = {
        max_posts: 0,
        start_date: null,
        end_date: null,
        post_ids: null,
      };
      if (scope === 'recent') {
        const count = Number.parseInt(values.maxPosts, 10);
        if (!Number.isInteger(count) || count < 1 || count > 100000) {
          throw new Error('最近条数必须是 1 到 100000 之间的整数');
        }
        request.max_posts = count;
        return request;
      }
      if (scope === 'date') {
        const startDate = values.startDate || null;
        const endDate = values.endDate || null;
        if (!startDate && !endDate) throw new Error('请至少填写一个日期');
        if (startDate && endDate && startDate > endDate) {
          throw new Error('开始日期不能晚于结束日期');
        }
        request.start_date = startDate;
        request.end_date = endDate;
        return request;
      }
      if (scope === 'manual') {
        const postIds = Array.from(values.postIds || []);
        if (!postIds.length) throw new Error('请至少选择一条微博');
        request.post_ids = postIds;
        return request;
      }
      if (scope === 'all') return request;
      throw new Error('未知的保存范围');
    },

    // ====== 初始化 ======
  });
  global.Ptu = ctx;
  return ctx;
}

init(Ptu);
