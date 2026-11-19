// v2.1.0：设置面板（检查更新 + 其他入口）。
import { Ptu } from "./state.js";

export function init(ctx = Ptu) {
  Object.assign(ctx, {
    initializeSettingsPanel() {
      const brand = document.querySelector('.brand-version');
      const version = Ptu.$('settings-version');
      if (version && brand) version.textContent = brand.textContent;
      const status = Ptu.$('update-status');
      if (status && !Ptu.State.updateInfo) status.textContent = '手动检查是否有新版本。';
      const installBtn = Ptu.$('btn-install-update');
      if (installBtn) installBtn.hidden = true;
      const notes = Ptu.$('update-notes');
      if (notes && !(Ptu.State.updateInfo && Ptu.State.updateInfo.has_update)) notes.hidden = true;
      const autoCheck = Ptu.$('settings-auto-update-check');
      if (autoCheck) autoCheck.checked = Ptu.settingsFlag('weishushu.autoCheckUpdate', true);
      const notifyTask = Ptu.$('settings-notify-task');
      if (notifyTask) notifyTask.checked = Ptu.settingsFlag('weishushu.notifyOnTaskDone', false);
      Ptu.refreshUpdateDot();
      Ptu.loadMaintenanceCache();
      Ptu.loadStorageOverview();
    },

    settingsFlag(key, fallback) {
      const saved = Ptu.safeLocalGet(key);
      if (saved === '1') return true;
      if (saved === '0') return false;
      return fallback;
    },

    setSettingsFlag(key, enabled) {
      Ptu.safeLocalSet(key, enabled ? '1' : '0');
    },

    refreshUpdateDot() {
      const dot = Ptu.$('settings-update-dot');
      if (dot) dot.hidden = !(Ptu.State.updateInfo && Ptu.State.updateInfo.has_update);
    },

    async maybeAutoCheckUpdate() {
      Ptu.refreshUpdateDot();
      if (!Ptu.settingsFlag('weishushu.autoCheckUpdate', true)) return;
      if (Ptu.State.updateInfo || typeof Ptu.Api.updateCheck !== 'function') return;
      try {
        Ptu.State.updateInfo = await Ptu.Api.updateCheck();
      } catch (e) {
        // 取不到发布信息（例如网络无法访问 GitHub）时静默跳过，不提示
        return;
      }
      Ptu.refreshUpdateDot();
      Ptu.maybeShowUpdatePrompt();
    },

    // 发布说明是 Markdown 原文，弹窗只显示纯文本：去掉标题井号与加粗星号
    plainReleaseNotes(notes) {
      return String(notes || '')
        .replace(/\r\n/g, '\n')
        .split('\n')
        .map(line => line.replace(/^\s{0,3}#{1,6}\s+/, '').replace(/\*\*(.+?)\*\*/g, '$1').replace(/^\s*>\s?/, ''))
        .join('\n')
        .trim();
    },

    renderUpdateNotes(element, info) {
      if (!element) return;
      const text = Ptu.plainReleaseNotes(info && info.notes);
      element.textContent = text || '本次发布没有附带更新说明。';
      element.hidden = false;
    },

    maybeShowUpdatePrompt() {
      const info = Ptu.State.updateInfo;
      const overlay = Ptu.$('update-overlay');
      if (!overlay || !info || !info.has_update || Ptu.State.updatePromptShown) return;
      if (Ptu.safeLocalGet('weishushu.skippedUpdateVersion') === info.latest) return;
      // 任务进行中或风险须知尚未确认时不打断，只保留设置按钮红点
      if (Ptu.State.taskId) return;
      const risk = Ptu.$('risk-modal-overlay');
      if (risk && risk.hidden === false) return;
      Ptu.State.updatePromptShown = true;
      const version = Ptu.$('update-modal-version');
      if (version) version.textContent = `新版本 ${info.latest}（当前 v${info.current}）`;
      Ptu.renderUpdateNotes(Ptu.$('update-modal-notes'), info);
      const canInstall = !!(info.can_auto_install && info.dmg_asset);
      const install = Ptu.$('update-modal-install');
      const open = Ptu.$('update-modal-open');
      if (install) install.hidden = !canInstall;
      if (open) open.hidden = canInstall;
      overlay.hidden = false;
    },

    closeUpdatePrompt(skip) {
      const overlay = Ptu.$('update-overlay');
      if (overlay) overlay.hidden = true;
      const info = Ptu.State.updateInfo;
      if (skip && info && info.latest) Ptu.safeLocalSet('weishushu.skippedUpdateVersion', info.latest);
    },

    installFromUpdatePrompt() {
      Ptu.closeUpdatePrompt(false);
      Ptu.toggleSettings(true);
      Ptu.installUpdate({ confirmed: true });
    },

    async openReleasePage() {
      const api = globalThis.pywebview && globalThis.pywebview.api;
      if (api && typeof api.open_release_page === 'function') {
        try { if (await api.open_release_page()) return; } catch (e) { /* 降级提示地址 */ }
      }
      const url = (Ptu.State.updateInfo && Ptu.State.updateInfo.releases_page) || 'https://github.com/Fairc11/weishushu/releases';
      Ptu.toast(`请在浏览器中打开：${url}`);
    },

    async loadMaintenanceCache() {
      const box = Ptu.$('settings-cache-list');
      if (!box || typeof Ptu.Api.maintenanceCache !== 'function') return;
      try {
        const r = await Ptu.Api.maintenanceCache();
        box.innerHTML = '';
        (r.items || []).forEach((item) => {
          const row = document.createElement('div');
          row.className = 'settings-row';
          const label = document.createElement('span');
          label.textContent = `${item.label} ${Ptu.formatSize(item.size_bytes || 0)}`;
          const clear = document.createElement('button');
          clear.type = 'button';
          clear.className = 'btn btn-secondary btn-sm';
          clear.textContent = '清理';
          clear.disabled = !item.size_bytes;
          clear.addEventListener('click', () => Ptu.clearMaintenanceCache(item.key));
          row.append(label, clear);
          box.appendChild(row);
        });
      } catch (e) {
        box.innerHTML = '<p class="hint">缓存统计不可用。</p>';
      }
    },

    async clearMaintenanceCache(key) {
      try {
        const r = await Ptu.Api.maintenanceCacheClear(key);
        Ptu.toast(`已释放 ${Ptu.formatSize(r.freed_bytes || 0)}`);
      } catch (e) {
        Ptu.toast('清理失败：' + (e.message || e));
      }
      Ptu.loadMaintenanceCache();
    },

    async loadStorageOverview() {
      const box = Ptu.$('settings-storage-list');
      if (!box || typeof Ptu.Api.libraryList !== 'function') return;
      try {
        const requests = [Ptu.Api.libraryList()];
        if (typeof Ptu.Api.librarySizes === 'function') requests.push(Ptu.Api.librarySizes());
        const [listing, sizes] = await Promise.all(requests);
        const sizeMap = new Map(((sizes && sizes.sizes) || []).map((row) => [row.path, row.size_bytes]));
        const entries = (listing.entries || []).filter((entry) => entry.available !== false);
        box.innerHTML = '';
        if (!entries.length) {
          box.innerHTML = '<p class="hint">书库暂无登记档案。</p>';
          return;
        }
        entries.forEach((entry) => {
          const row = document.createElement('div');
          row.className = 'settings-row';
          const label = document.createElement('span');
          const size = sizeMap.get(entry.path);
          label.textContent = `@${entry.screen_name || entry.uid} · ${Number.isFinite(size) ? Ptu.formatSize(size) : '占用未知'}`;
          const locate = document.createElement('button');
          locate.type = 'button';
          locate.className = 'btn btn-secondary btn-sm';
          locate.textContent = '定位';
          locate.addEventListener('click', () => Ptu.locateArchive(entry.path));
          row.append(label, locate);
          box.appendChild(row);
        });
      } catch (e) {
        box.innerHTML = '<p class="hint">档案占用统计不可用。</p>';
      }
    },

    async locateArchive(path) {
      if (Ptu.State.isDesktop && global.pywebview && global.pywebview.api) {
        try {
          const opened = await global.pywebview.api.open_folder(path);
          if (!opened) Ptu.toast('无法打开档案目录，请检查目录是否仍然存在。');
        } catch (e) {
          Ptu.toast('无法打开档案目录：' + (e.message || e));
        }
      } else {
        Ptu.toast('浏览器模式下请手动打开：' + path);
      }
    },

    toggleSettings(force) {
      const overlay = Ptu.$('settings-overlay');
      if (!overlay) return;
      const show = force === undefined ? overlay.hidden : Boolean(force);
      overlay.hidden = !show;
      if (show) Ptu.initializeSettingsPanel();
    },

    async checkForUpdates() {
      const status = Ptu.$('update-status');
      const installBtn = Ptu.$('btn-install-update');
      const checkBtn = Ptu.$('btn-check-update');
      if (installBtn) installBtn.hidden = true;
      if (status) status.textContent = '正在检查更新…';
      if (checkBtn) checkBtn.disabled = true;
      try {
        const info = await Ptu.Api.updateCheck();
        Ptu.State.updateInfo = info;
        if (!status) return;
        if (info.has_update) {
          status.textContent = `发现新版本 ${info.latest}（当前 v${info.current}）。`;
          Ptu.renderUpdateNotes(Ptu.$('update-notes'), info);
          if (info.can_auto_install && info.dmg_asset) {
            if (installBtn) installBtn.hidden = false;
          } else {
            status.textContent += '请前往 GitHub 发布页下载安装。';
          }
        } else {
          status.textContent = `当前已是最新版本（v${info.current}）。`;
        }
      } catch (e) {
        Ptu.State.updateInfo = null;
        if (status) status.textContent = e.message || '检查更新失败，请稍后重试。';
      } finally {
        if (checkBtn) checkBtn.disabled = false;
        Ptu.refreshUpdateDot();
      }
    },

    async openDataDirFromSettings() {
      try {
        const opened = await Ptu.Api.openDataDir();
        if (opened === false) Ptu.toast('打开应用数据目录失败');
      } catch (e) {
        Ptu.toast('打开应用数据目录失败：' + e.message);
      }
    },

    async installUpdate(options = {}) {
      if (Ptu.guardActiveTask()) return;
      const info = Ptu.State.updateInfo;
      if (!info || !info.has_update) return;
      if (!options.confirmed && !confirm(`下载并安装 ${info.latest}？\n下载校验完成后应用会自动退出，替换旧版并重新打开。`)) return;
      const status = Ptu.$('update-status');
      const installBtn = Ptu.$('btn-install-update');
      const checkBtn = Ptu.$('btn-check-update');
      if (installBtn) installBtn.disabled = true;
      if (checkBtn) checkBtn.disabled = true;
      try {
        if (status) status.textContent = '正在下载并校验更新包，请保持网络连接…';
        const staged = await Ptu.Api.updateInstall();
        if (status) status.textContent = '下载完成，应用即将退出并自动完成更新…';
        await Ptu.Api.updateApply(staged.staged_path);
        await Ptu.Api.closeWindow();
      } catch (e) {
        if (status) status.textContent = e.message || '自动更新失败，请前往发布页手动下载。';
        if (installBtn) installBtn.disabled = false;
        if (checkBtn) checkBtn.disabled = false;
      }
    },
  });
}

init(Ptu);
