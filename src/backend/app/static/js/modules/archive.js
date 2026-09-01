// 阶段 5：archive 模块。
import { Ptu, global } from "./state.js";
import { init as initFeedback } from "./feedback.js";
void initFeedback;

export function init(ctx = Ptu) {
  Object.assign(ctx, {
    setMode(mode) {
      // v1.1.2：保留函数为空（兼容调用方），不再有模式切换
    },

    // ====== 登录 ======

    toggleHistory(force) {
      const panel = Ptu.$('history-panel');
      if (!panel) return;
      const show = force === undefined ? panel.hidden : force;
      if (show) {
        panel.style.position = 'absolute';
        panel.style.backdropFilter = 'none';
        panel.style.webkitBackdropFilter = 'none';
        document.body.appendChild(panel);
      }
      panel.hidden = !show;
      Ptu.State.nativeBrowserSuppressed = show;
      Ptu.syncNativeBrowserFrame();
      if (!show && Ptu.State.historyPanelHome) {
        panel.style.removeProperty('position');
        panel.style.removeProperty('backdrop-filter');
        panel.style.removeProperty('-webkit-backdrop-filter');
        Ptu.State.historyPanelHome.appendChild(panel);
      }
      if (show && Ptu.State.historyFolder) {
        Ptu.refreshHistoryList();
      }
    },

    async pickHistoryFolder() {
      let dir = null;
      if (Ptu.State.isDesktop && global.pywebview && global.pywebview.api
          && global.pywebview.api.select_folder) {
        try { dir = await global.pywebview.api.select_folder(''); }
        catch (e) { /* dev 降级到 prompt */ }
      }
      if (!dir) {
        dir = (prompt('选择历史文件夹（粘贴绝对路径）') || '').trim();
      }
      if (!dir) {
        Ptu.toast('已取消');
        return;
      }
      Ptu.State.historyFolder = dir;
      const pathEl = Ptu.$('history-folder-path');
      if (pathEl) pathEl.textContent = dir;
      Ptu.renderHistoryList();
      await Ptu.refreshHistoryList();
    },

    async refreshHistoryList() {
      const ul = Ptu.$('history-list');
      if (!ul) return;
      if (!Ptu.State.historyFolder) {
        ul.innerHTML = '<p class="hint">选目录后这里显示历史微博书</p>';
        return;
      }
      try {
        const r = await Ptu.Api.backupList(Ptu.State.historyFolder);
        Ptu.renderHistoryList(r.entries || []);
      } catch (e) {
        ul.innerHTML = `<p class="hint">加载失败：${Ptu.escape(e.message)}</p>`;
      }
    },

    renderHistoryList(entries) {
      const ul = Ptu.$('history-list');
      if (!ul) return;
      if (!entries || !entries.length) {
        ul.innerHTML = '<p class="hint">该目录下没有历史微博书（微博书_*.md）</p>';
        return;
      }
      ul.innerHTML = entries.map((e) => {
        const dt = new Date((e.created_at || 0) * 1000);
        const dateStr = dt.toLocaleString('zh-CN', { hour12: false });
        return `<li class="history-item">
          <div class="history-name">${Ptu.escape(e.filename)}</div>
          <div class="history-meta">
            <span>📅 ${Ptu.escape(dateStr)}</span>
            <span>📦 ${Ptu.formatNum(e.size_bytes)} B</span>
            <span>📝 ${e.bids_count || 0} BID</span>
          </div>
        </li>`;
      }).join('');
    },

    async runSearch() {
      const inputEl = Ptu.$('search-input');
      const resultsBox = Ptu.$('search-results');
      const listEl = Ptu.$('search-list');
      if (!inputEl || !resultsBox || !listEl) return;
      const q = inputEl.value.trim();
      if (!q) {
        Ptu.toast('请输入搜索关键词');
        return;
      }
      if (!Ptu.State.historyFolder) {
        Ptu.toast('请先选历史目录');
        return;
      }
      try {
        const r = await Ptu.Api.backupSearch(Ptu.State.historyFolder, q);
        const hits = r.hits || [];
        resultsBox.hidden = false;
        if (!hits.length) {
          listEl.innerHTML = '<li class="hint">无命中</li>';
          return;
        }
        listEl.innerHTML = hits.map((h) => `<li class="search-hit">
          <div class="hit-file">${Ptu.escape(h.filename)} · 第 ${h.line_no} 行</div>
          ${h.context_before ? `<div class="hit-ctx hit-before">${Ptu.escape(h.context_before)}</div>` : ''}
          <div class="hit-line">${Ptu.escape(h.line_text)}</div>
          ${h.context_after ? `<div class="hit-ctx hit-after">${Ptu.escape(h.context_after)}</div>` : ''}
        </li>`).join('');
      } catch (e) {
        resultsBox.hidden = false;
        listEl.innerHTML = `<li class="hint">搜索失败：${Ptu.escape(e.message)}</li>`;
      }
    },

    // ====== 博主搜索与目标选择（备份他人微博） ======

    async searchOrResolve() {
      if (Ptu.guardActiveTask()) return;
      const input = Ptu.$('url-input');
      const query = input ? input.value.trim() : '';
      if (!query) { Ptu.toast('请输入博主昵称或微博主页链接'); return; }
      const btn = Ptu.$('btn-search-blogger');
      const originalText = btn ? btn.textContent : '';
      if (btn) { btn.disabled = true; btn.textContent = '搜索中…'; }
      try {
        const looksLikeLink = /https?:\/\/|weibo\.com|weibo\.cn|t\.cn/i.test(query) || /^\d{5,20}$/.test(query);
        if (looksLikeLink) {
          const target = await Ptu.Api.resolveTarget(query);
          Ptu.renderBloggerResults([target]);
          return;
        }
        const r = await Ptu.Api.searchUsers(query);
        Ptu.renderBloggerResults(r.results || []);
      } catch (e) {
        Ptu.toast(e.message);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = originalText; }
      }
    },

    renderBloggerResults(results) {
      const ul = Ptu.$('blogger-results');
      if (!ul) return;
      ul.hidden = false;
      if (!results.length) {
        ul.innerHTML = '<li class="hint">没有找到相关博主，换个关键词或粘贴主页链接试试</li>';
        return;
      }
      ul.innerHTML = '';
      results.forEach((u) => {
        const li = document.createElement('li');
        li.className = 'blogger-item';
        const verified = u.verified
          ? `<span class="blogger-v" title="${Ptu.escape(u.verified_reason || '已认证')}">V</span>`
          : '';
        const reason = u.verified && u.verified_reason
          ? `<span class="blogger-reason">${Ptu.escape(u.verified_reason)}</span>`
          : '';
        li.innerHTML = `
          <img class="blogger-avatar" alt="" src="/api/assets/img?url=${encodeURIComponent(u.avatar_url || '')}" />
          <div class="blogger-info">
            <div class="blogger-name-row">${verified}<span class="blogger-name">${Ptu.escape(u.screen_name)}</span>${reason}</div>
            <div class="blogger-meta">
              <span>${Ptu.formatNum(u.followers_count)} 粉丝</span>
              <span>${Ptu.formatNum(u.posts_count)} 微博</span>
            </div>
            ${u.description ? `<div class="blogger-desc">${Ptu.escape(u.description)}</div>` : ''}
          </div>
          <button class="btn btn-primary btn-sm blogger-pick" type="button">备份 TA</button>
        `;
        li.querySelector('.blogger-pick').addEventListener('click', () => {
          Ptu.selectBloggerTarget({ uid: String(u.uid), screen_name: u.screen_name });
        });
        ul.appendChild(li);
      });
    },

    async selectBloggerTarget(target) {
      if (Ptu.guardActiveTask()) return;
      Ptu.State.archiveTarget = target;
      const ul = Ptu.$('blogger-results');
      if (ul) ul.hidden = true;
      Ptu.toast(`已选中 @${target.screen_name}，请选择保存位置`);
      await Ptu.pickArchiveFolderAndInspect(null);
    },

    // ====== 微博书目录决策 ======

    async backupSelf(returnFocus = null) {
      Ptu.State.archiveTarget = null;
      await Ptu.pickArchiveFolderAndInspect(returnFocus);
    },

    async pickArchiveFolderAndInspect(returnFocus = null) {
      if (Ptu.guardActiveTask()) return;
      if (Ptu.State.backupFolderBusy) return;
      // 本人流程占用一键备份按钮做 loading；他人流程占用搜索按钮
      const isOtherTarget = !!Ptu.State.archiveTarget;
      const button = Ptu.$(isOtherTarget ? 'btn-search-blogger' : 'btn-backup-self');
      const originalText = button ? button.textContent : '';
      const selectionReturnFocus = returnFocus || button;
      let restoreFocusAfterSelection = false;
      Ptu.State.backupFolderBusy = true;
      if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.textContent = '正在检查文件夹…';
      }
      try {
        if (!global.pywebview || !global.pywebview.api || !global.pywebview.api.select_folder) {
          Ptu.toast('浏览器开发模式无法选择本地文件夹，请在桌面版中使用一键备份');
          restoreFocusAfterSelection = true;
          return;
        }
        const dir = await global.pywebview.api.select_folder('');
        if (!dir) {
          restoreFocusAfterSelection = true;
          return;
        }
        const inspectReq = { output_dir: dir };
        if (Ptu.State.archiveTarget) inspectReq.target_uid = Ptu.State.archiveTarget.uid;
        const inspection = await Ptu.Api.backupInspect(inspectReq);
        await Ptu.handleArchiveInspection(inspection, selectionReturnFocus);
        restoreFocusAfterSelection = !['archive', 'ordinary_nonempty', 'legacy_index'].includes(inspection.state)
          && !(inspection.state === 'empty' && Ptu.State.taskId);
      } catch (e) {
        Ptu.toast(`目录检查失败：${e.message}`);
        restoreFocusAfterSelection = true;
      } finally {
        Ptu.State.backupFolderBusy = false;
        if (button) {
          button.disabled = !!Ptu.State.taskId;
          button.setAttribute('aria-busy', 'false');
          button.textContent = originalText;
        }
        if (restoreFocusAfterSelection && selectionReturnFocus && selectionReturnFocus.focus) {
          selectionReturnFocus.focus();
        }
      }
    },

    async handleArchiveInspection(inspection, returnFocus) {
      if (!inspection || !inspection.state) {
        Ptu.toast('目录检查失败：服务未返回目录状态');
        if (returnFocus && returnFocus.focus) returnFocus.focus();
        return;
      }
      if (inspection.state === 'empty') {
        Ptu.State.backupFolderDir = inspection.path;
        Ptu.State.backupFolderInspection = inspection;
        await Ptu.startArchiveBackup('create');
        return;
      }
      if (['archive', 'ordinary_nonempty', 'legacy_index'].includes(inspection.state)) {
        Ptu.showArchiveFolderDecision(inspection, returnFocus);
        return;
      }
      if (inspection.state === 'uid_mismatch') {
        if (Ptu.State.archiveTarget) {
          Ptu.toast('该微博书属于其他博主，不能覆盖');
        } else if (inspection.screen_name) {
          Ptu.toast(`该微博书属于 @${inspection.screen_name}；如要更新 TA 的微博书，请先搜索并选中该博主`);
        } else {
          Ptu.toast('该微博书属于其他登录账号');
        }
        if (returnFocus && returnFocus.focus) returnFocus.focus();
        return;
      }
      if (inspection.state === 'damaged') {
        Ptu.toast('微博书档案不完整，请先复制目录后再修复');
        if (returnFocus && returnFocus.focus) returnFocus.focus();
        return;
      }
      Ptu.toast('目录检查失败：未知目录状态');
      if (returnFocus && returnFocus.focus) returnFocus.focus();
    },

    showArchiveFolderDecision(inspection, returnFocus) {
      const overlay = Ptu.$('archive-folder-decision');
      if (!overlay) return;
      const isArchive = inspection.state === 'archive';
      const isLegacy = inspection.state === 'legacy_index';
      Ptu.State.backupFolderDir = inspection.path;
      Ptu.State.backupFolderInspection = inspection;
      Ptu.State.backupFolderReturnFocus = returnFocus || null;
      Ptu.resetArchivePacingControls();

      const title = Ptu.$('archive-folder-decision-title');
      const eyebrow = Ptu.$('archive-folder-eyebrow');
      const summary = Ptu.$('archive-folder-summary');
      const warning = Ptu.$('archive-folder-warning');
      const target = Ptu.State.archiveTarget;
      if (eyebrow) {
        eyebrow.textContent = target ? `备份博主 @${target.screen_name}` : '本人微博书';
      }
      if (title) {
        title.textContent = isArchive
          ? '继续更新这本微博书？'
          : (isLegacy ? '建立完整微博书档案？' : '这个文件夹已有其他内容');
      }
      if (summary) summary.hidden = !isArchive;
      if (warning) {
        warning.hidden = isArchive;
        warning.textContent = isArchive
          ? ''
          : (isLegacy
            ? '旧版备份目录，需要首次建立完整档案'
            : '将自动新建子文件夹「昵称_UID」存放微博书，不影响现有文件。');
      }
      const account = Ptu.$('archive-folder-account');
      const total = Ptu.$('archive-folder-total');
      const lastSync = Ptu.$('archive-folder-last-sync');
      const path = Ptu.$('archive-folder-path');
      if (account) account.textContent = inspection.screen_name || '未记录';
      if (total) total.textContent = `${inspection.total_posts || 0} 条`;
      if (lastSync) lastSync.textContent = inspection.last_successful_sync_at || '尚未记录';
      if (path) path.textContent = inspection.path;

      const visibility = {
        'archive-action-create': isLegacy,
        'archive-action-incremental': isArchive,
        // 关注资料只有本人可见（接口是登录者视角，他人档案没有）
        'archive-action-following': isArchive && !target,
        'archive-action-rebuild': isArchive,
        'archive-action-create-subfolder': !isArchive && !isLegacy,
        'archive-action-reselect': true,
      };
      Object.entries(visibility).forEach(([id, visible]) => {
        const action = Ptu.$(id);
        if (action) action.hidden = !visible;
      });
      const createAction = Ptu.$('archive-action-create');
      if (createAction) createAction.textContent = isLegacy ? '建立完整档案' : '创建新微博书';
      overlay.hidden = false;
      requestAnimationFrame(() => {
        const first = isArchive
          ? Ptu.$('archive-action-incremental')
          : (isLegacy ? Ptu.$('archive-action-create') : Ptu.$('archive-action-create-subfolder'));
        if (first) first.focus();
      });
    },

    closeArchiveFolderDecision(restoreFocus = true, force = false) {
      if (Ptu.State.backupStartBusy && !force) return;
      const overlay = Ptu.$('archive-folder-decision');
      if (overlay) overlay.hidden = true;
      const returnFocus = Ptu.State.backupFolderReturnFocus;
      Ptu.State.backupFolderDir = null;
      Ptu.State.backupFolderInspection = null;
      Ptu.State.backupFolderReturnFocus = null;
      Ptu.State.archiveTarget = null;
      if (restoreFocus && returnFocus && returnFocus.focus) returnFocus.focus();
    },

    handleArchiveDialogKeydown(event) {
      const overlay = Ptu.$('archive-folder-decision');
      if (!overlay || overlay.hidden) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        Ptu.closeArchiveFolderDecision();
        return;
      }
      if (event.key !== 'Tab') return;
      const modal = overlay.querySelector('section');
      if (!modal) return;
      const focusable = Array.from(modal.querySelectorAll('button:not([hidden]):not([disabled]), input:not([hidden]):not([disabled])'));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },

    async reselectArchiveFolder() {
      const returnFocus = Ptu.State.backupFolderReturnFocus;
      const target = Ptu.State.archiveTarget;
      Ptu.closeArchiveFolderDecision(false);
      // 他人流程重新选目录时保留已选博主
      Ptu.State.archiveTarget = target;
      await Ptu.pickArchiveFolderAndInspect(returnFocus);
    },

    async createArchiveInSelectedFolder() {
      // 后端会在所选文件夹下自动新建「昵称_UID」子文件夹，无需再开系统面板。
      await Ptu.startArchiveBackup('create');
    },

    setArchiveActionsDisabled(disabled) {
      for (const id of [
        'archive-action-create', 'archive-action-incremental', 'archive-action-rebuild',
        'archive-action-following',
        'archive-action-create-subfolder', 'archive-action-reselect', 'archive-folder-close',
      ]) {
        const button = Ptu.$(id);
        if (button) button.disabled = disabled;
      }
      document.querySelectorAll('input[name="archive-pacing-mode"]').forEach((input) => {
        input.disabled = disabled;
      });
      const keepAwake = Ptu.$('archive-keep-awake');
      if (keepAwake) keepAwake.disabled = disabled || Ptu.State.archivePacingMode === 'standard';
    },

    resetArchivePacingControls() {
      document.querySelectorAll('input[name="archive-pacing-mode"]').forEach((input) => {
        input.checked = input.value === 'standard';
      });
      Ptu.State.archivePacingMode = 'standard';
      const keepAwake = Ptu.$('archive-keep-awake');
      if (keepAwake) {
        keepAwake.checked = false;
        keepAwake.disabled = true;
      }
    },

    syncArchivePacingControls() {
      const selected = document.querySelector('input[name="archive-pacing-mode"]:checked');
      const allowed = new Set(['standard', 'low_2_3_hours', 'low_4_6_hours', 'low_8_12_hours']);
      const mode = selected && allowed.has(selected.value) ? selected.value : 'standard';
      Ptu.State.archivePacingMode = mode;
      const keepAwake = Ptu.$('archive-keep-awake');
      if (keepAwake) {
        keepAwake.disabled = mode === 'standard';
        if (keepAwake.disabled) keepAwake.checked = false;
      }
      return mode;
    },

    getArchivePacingSelection() {
      const mode = Ptu.syncArchivePacingControls();
      const keepAwake = Ptu.$('archive-keep-awake');
      return {
        pacing_mode: mode,
        keep_awake_when_plugged: mode !== 'standard' && Boolean(keepAwake && keepAwake.checked),
      };
    },

    async startArchiveBackup(mode) {
      if (Ptu.guardActiveTask()) return;
      if (Ptu.State.backupStartBusy) return;
      const dir = Ptu.State.backupFolderDir;
      if (!dir) {
        Ptu.toast('未选择备份目录');
        return;
      }
      Ptu.State.backupStartBusy = true;
      Ptu.State.taskStartBusy = true;
      Ptu.updateTaskActionAvailability();
      Ptu.setArchiveActionsDisabled(true);
      const slowStartTimer = setTimeout(() => {
        Ptu.toast('仍在启动，请勿重复操作');
      }, 15000);
      try {
        const pacing = Ptu.getArchivePacingSelection();
        const startReq = {
          output_dir: dir,
          mode,
          pacing_mode: pacing.pacing_mode,
          keep_awake_when_plugged: pacing.keep_awake_when_plugged,
        };
        const target = Ptu.State.archiveTarget;
        if (target) startReq.target_uid = target.uid;
        const r = await Ptu.Api.backupStart(startReq);
        Ptu.setActiveTask(r.task_id);
        Ptu.State._backupOutputDir = dir;
        Ptu.$('step-progress').hidden = false;
        Ptu.$('result-card').hidden = true;
        Ptu.setTaskCancelVisible(true);
        const labels = { create: '首次建立', incremental: '新增备份', rebuild: '重新全部备份' };
        const modeLabel = target ? `备份博主` : '本人模式';
        Ptu.setProgress(0, `${modeLabel} · ${labels[mode]} · @${r.self_screen_name} · 输出目录：${dir}`);
        Ptu.renderPacingStatus({
          mode: pacing.pacing_mode,
          state: pacing.pacing_mode === 'standard' ? 'standard' : 'estimating',
          request_kind: null,
          next_wait_seconds: null,
          disclaimer: '目标区间不是完成时间承诺',
        });
        Ptu.subscribeTask(r.task_id);
        Ptu.closeArchiveFolderDecision(false, true);
        Ptu.toast(`${labels[mode]}已启动`);
      } catch (e) {
        Ptu.toast(`备份启动失败：${e.message}`);
      } finally {
        clearTimeout(slowStartTimer);
        Ptu.State.backupStartBusy = false;
        Ptu.State.taskStartBusy = false;
        Ptu.setArchiveActionsDisabled(false);
        Ptu.updateTaskActionAvailability();
      }
    },

    async startFollowingArchive() {
      if (Ptu.guardActiveTask()) return;
      if (Ptu.State.backupStartBusy) return;
      const dir = Ptu.State.backupFolderDir;
      if (!dir || !Ptu.State.backupFolderInspection
          || Ptu.State.backupFolderInspection.state !== 'archive') {
        Ptu.toast('更新关注资料必须使用现有微博书目录');
        return;
      }
      Ptu.State.backupStartBusy = true;
      Ptu.State.taskStartBusy = true;
      Ptu.updateTaskActionAvailability();
      Ptu.setArchiveActionsDisabled(true);
      const slowStartTimer = setTimeout(() => {
        Ptu.toast('仍在启动，请勿重复操作');
      }, 15000);
      try {
        const pacing = Ptu.getArchivePacingSelection();
        const r = await Ptu.Api.followingStart({
          output_dir: dir,
          pacing_mode: pacing.pacing_mode,
          keep_awake_when_plugged: pacing.keep_awake_when_plugged,
        });
        Ptu.setActiveTask(r.task_id);
        Ptu.State._backupOutputDir = dir;
        Ptu.$('step-progress').hidden = false;
        Ptu.$('result-card').hidden = true;
        Ptu.setTaskCancelVisible(true);
        Ptu.setProgress(0, `关注资料更新 · @${r.self_screen_name} · 输出目录：${dir}`);
        Ptu.renderPacingStatus({
          mode: pacing.pacing_mode,
          state: pacing.pacing_mode === 'standard' ? 'standard' : 'estimating',
          request_kind: null,
          next_wait_seconds: null,
          disclaimer: '目标区间不是完成时间承诺',
        });
        Ptu.subscribeTask(r.task_id);
        Ptu.closeArchiveFolderDecision(false, true);
        Ptu.toast('关注资料更新已启动');
      } catch (e) {
        Ptu.toast(`关注资料更新启动失败：${e.message}`);
      } finally {
        clearTimeout(slowStartTimer);
        Ptu.State.backupStartBusy = false;
        Ptu.State.taskStartBusy = false;
        Ptu.setArchiveActionsDisabled(false);
        Ptu.updateTaskActionAvailability();
      }
    },

    updateMainScopeUi() {
      const scope = (Ptu.$('set-scope') && Ptu.$('set-scope').value) || 'manual';
      document.querySelectorAll('[data-main-scope-panel]').forEach((panel) => {
        panel.hidden = panel.dataset.mainScopePanel !== scope;
      });
    },

    // ====== 预览 ======

    async preview() {
      Ptu.futureFeature();
      return;
      const url = Ptu.$('url-input').value.trim();
      if (!url) { Ptu.toast('请输入微博链接'); return; }
      const btn = Ptu.$('btn-preview');
      btn.disabled = true;
      btn.textContent = '加载中…';
      try {
        const data = await Ptu.Api.preview(url, 20);
        Ptu.State.preview = data;
        Ptu.renderUserCard(data.user);
        Ptu.renderPreviewList(data.previews);
        Ptu.$('step-3').hidden = false;
      } catch (e) {
        Ptu.toast(`预览失败：${e.message}`);
      } finally {
        btn.disabled = false;
        btn.textContent = '预览';
      }
    },

    renderUserCard(user) {
      const card = Ptu.$('user-card');
      card.hidden = false;
      Ptu.$('user-avatar').src = user.avatar_url;
      Ptu.$('user-name').textContent = user.screen_name;
      Ptu.$('user-posts').textContent = Ptu.formatNum(user.posts_count);
      Ptu.$('user-followers').textContent = Ptu.formatNum(user.followers_count);
    },

    renderPreviewList(previews) {
      Ptu.$('preview-section').hidden = false;
      const ul = Ptu.$('preview-list');
      ul.innerHTML = '';
      Ptu.State.selectedBids = new Set();
      previews.forEach((p) => {
        const li = document.createElement('li');
        li.className = 'preview-item';
        li.innerHTML = `
          <input type="checkbox" data-bid="${Ptu.escape(p.bid)}" />
          <div class="preview-body">
            <div class="preview-text">${Ptu.escape(p.text || '(无文字)')}</div>
            <div class="preview-meta">
              <span>${Ptu.escape(p.created_at)}</span>
              <span>👍 ${Ptu.formatNum(p.likes_count)}</span>
              <span>💬 ${Ptu.formatNum(p.comments_count)}</span>
              <span>🔁 ${Ptu.formatNum(p.reposts_count)}</span>
              ${p.media_count ? `<span>📷 ${p.media_count}</span>` : ''}
            </div>
          </div>
        `;
        const cb = li.querySelector('input');
        cb.addEventListener('change', (e) => {
          if (e.target.checked) Ptu.State.selectedBids.add(p.bid);
          else Ptu.State.selectedBids.delete(p.bid);
        });
        li.addEventListener('click', (e) => {
          if (e.target.tagName !== 'INPUT') cb.checked = !cb.checked;
          cb.dispatchEvent(new Event('change'));
        });
        ul.appendChild(li);
      });
    },

    toggleCheckAll(checked) {
      document.querySelectorAll('#preview-list input[type="checkbox"]').forEach(cb => {
        cb.checked = checked;
        cb.dispatchEvent(new Event('change'));
      });
    },

    // ====== 提取 ======

    async start() {
      Ptu.futureFeature();
      return;
      if (Ptu.guardActiveTask()) return;
      if (!Ptu.State.preview) { Ptu.toast('请先预览'); return; }
      const scope = Ptu.$('set-scope').value;
      if (scope === 'manual') {
        if (!Ptu.State.selectedBids.size) { Ptu.toast('请至少选择一条微博'); return; }
      }
      const scopeValues = {
        maxPosts: Ptu.$('set-max-posts').value,
        startDate: Ptu.$('set-start-date').value,
        endDate: Ptu.$('set-end-date').value,
        postIds: Ptu.State.selectedBids,
      };
      let scopeRequest;
      try {
        scopeRequest = Ptu.buildScopeRequest(scope, scopeValues);
      } catch (e) {
        Ptu.toast(e.message);
        return;
      }
      if (scope === 'all') {
        const postsCountRaw = Ptu.State.preview.user && Ptu.State.preview.user.posts_count;
        const postsCount = (typeof postsCountRaw === 'number' ||
          (typeof postsCountRaw === 'string' && postsCountRaw.trim() !== ''))
          ? Number(postsCountRaw)
          : Number.NaN;
        const estimate = Number.isFinite(postsCount) && postsCount >= 0
          ? `预计最多 ${Ptu.formatNum(postsCount)} 条`
          : '总量尚未知';
        if (!confirm(`将提取全部微博。${estimate}，可能需要较长时间，确认继续？`)) return;
      }
      const formats = Array.from(document.querySelectorAll('input[name="fmt"]:checked')).map(el => el.value);
      const rawCommentsCount = parseInt(Ptu.$('set-comments-count').value, 10);
      const req = {
        url: Ptu.$('url-input').value.trim(),
        ...scopeRequest,
        formats: formats.length ? formats : ['md'],
        comments: Ptu.$('sw-comments').checked,
        comments_count: Number.isFinite(rawCommentsCount) ? rawCommentsCount : 5,
        comments_type: Ptu.$('set-comments-type').value,
        download_media: Ptu.$('sw-media').checked,
        image_quality: (Ptu.$('set-quality') && Ptu.$('set-quality').value) || 'large',
        // v1.1.2：缓存无 cookie 时显式触发扫码（前端走 login_status 自动判断）
        login: !Ptu.State.loggedIn,
        only_original: Ptu.$('sw-original').checked,
        extract_type: Ptu.$('sw-favorites').checked ? 'favorites' : 'posts',
      };
      Ptu.State.taskStartBusy = true;
      Ptu.updateTaskActionAvailability();
      try {
        const r = await Ptu.Api.start(req);
        Ptu.setActiveTask(r.task_id);
        Ptu.$('step-progress').hidden = false;
        Ptu.$('result-card').hidden = true;
        Ptu.setTaskCancelVisible(true);
        Ptu.setProgress(0, '任务已创建...');
        Ptu.subscribeTask(r.task_id);
        Ptu.toast('任务已启动，进度实时推送');
      } catch (e) {
        Ptu.toast(`启动失败：${e.message}`);
      } finally {
        Ptu.State.taskStartBusy = false;
        Ptu.updateTaskActionAvailability();
      }
    },
  });
  return ctx;
}

init(Ptu);
