// 阶段 5：共享状态与纯函数。
export const global = globalThis.window || globalThis;
const FUTURE_FEATURE_MESSAGE = '该功能正在开发中。';

export const Ptu = {
    // 当前互动 HTML 版式号由后端渲染进 <body data-book-format>；低于它的档案可本地重新生成。
    currentBookFormat() {
      const value = Number(globalThis.document?.body?.dataset?.bookFormat);
      return Number.isInteger(value) && value > 0 ? value : 0;
    },
    canUpgradeBookFormat(version) {
      const current = this.currentBookFormat();
      return typeof version === 'number' && version >= 2 && current > 0 && version < current;
    },
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
      verifiedBloggerCache: new Map(),
      bloggerSearchGeneration: 0,
      bloggerSearchBusy: false,
      archiveTarget: null,        // 备份他人微博：{uid, screen_name}，null = 本人
      loginName: '',            // 当前登录账号昵称（whoami 缓存）
      loginUid: '',             // 当前登录账号 UID（whoami 缓存）
      archivePacingMode: 'standard',
      selfArchiveDir: null,       // v2.1.0 本人微博书目录（localStorage 记忆）
      selfArchiveInspection: null,
      createRangeMonths: 12,      // 他人首次建档范围
      backfillMonths: 12,         // 本人历史回填档位
      targetBackfillMonths: 12,   // 他人历史回填档位，与本人独立
      outputFormats: { pdf: true, markdown: true },
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
      riskModalReadonly: false,
      updateInfo: null,
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

    formatSize(bytes) {
      if (!Number.isFinite(bytes) || bytes < 0) return '';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let value = bytes;
      let index = 0;
      while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
      return `${index === 0 ? value : value.toFixed(1)} ${units[index]}`;
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
