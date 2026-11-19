// 阶段 5：archive 模块。
import { Ptu, global } from "./state.js";
import { init as initFeedback } from "./feedback.js";
void initFeedback;

async function chooseVideoUpgrade(ctx, request) {
  const response = await ctx.Api.backupVideoQualityCheck(request);
  if (!response.task_id && response.summary.available_count === 0 && response.summary.failed_count === 0) return 0;
  const overlay = ctx.$('video-quality-decision');
  const title = ctx.$('video-quality-title');
  const status = ctx.$('video-quality-status');
  const options = ctx.$('video-quality-options');
  const confirm = ctx.$('video-quality-confirm');
  const close = ctx.$('video-quality-close');
  const cancel = ctx.$('video-quality-cancel');
  const parent = ctx.$('archive-folder-decision');
  const previousInert = parent ? parent.inert : false;
  const returnFocus = document.activeElement;
  if (parent) parent.inert = true;
  overlay.hidden = false;
  options.hidden = true;
  confirm.disabled = true;
  close.disabled = cancel.disabled = false;
  title.textContent = '正在检查旧视频画质';
  status.textContent = '仅检查没有核验记录的视频，已核验的不会重复检查。';
  close.focus();
  let taskId = response.task_id;
  let closing = false;
  let terminal = !taskId;
  let finish;
  const choice = new Promise(resolve => { finish = resolve; });
  const pause = () => new Promise(resolve => setTimeout(resolve, 350));
  function clean(value) {
    overlay.hidden = true;
    if (parent) parent.inert = previousInert;
    close.removeEventListener('click', stop);
    cancel.removeEventListener('click', stop);
    confirm.removeEventListener('click', accept);
    document.removeEventListener('keydown', keydown, true);
    finish(value);
    setTimeout(() => { if (returnFocus && returnFocus.isConnected) returnFocus.focus(); }, 0);
  }
  async function stop() {
    if (closing) return;
    closing = true;
    close.disabled = cancel.disabled = confirm.disabled = true;
    if (taskId && !terminal) {
      status.textContent = '正在停止检查，请稍候。';
      try {
        await ctx.Api.cancelTask(taskId);
        while (!terminal) await pause();
      } catch (error) {
        closing = false;
        close.disabled = cancel.disabled = false;
        status.textContent = `停止检查失败：${error.message}`;
        return;
      }
    }
    clean(null);
  }
  function accept() {
    if (closing || !terminal) return;
    const selected = options.querySelector('input:checked');
    if (!selected) return;
    clean(selected.value === 'all' ? 'all' : Number(selected.value));
  }
  function keydown(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); void stop(); }
    if (event.key === 'Tab') {
      const controls = Array.from(overlay.querySelectorAll('button:not([disabled]), input:not([disabled])')).filter(node => node.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      event.stopPropagation();
    }
  }
  function show(summary, error = '') {
    const count = summary.available_count;
    if (!Number.isInteger(count) || count < 0) throw new Error('视频画质检查结果无效');
    if (!error && count === 0 && summary.failed_count === 0) { clean(0); return; }
    title.textContent = count ? `发现 ${count} 条旧视频可升级画质` : '旧视频画质检查未全部完成';
    status.textContent = error || `检查结果已保存，已核验视频不会重复检查。${summary.failed_count ? `另有 ${summary.failed_count} 条暂未完成检查。` : ''}`;
    options.replaceChildren();
    const legend = document.createElement('legend'); legend.textContent = '本次更新数量'; options.append(legend);
    const values = [0, ...[10, 50].filter(value => value < count), ...(count ? ['all'] : [])];
    values.forEach(value => {
      const label = document.createElement('label'), input = document.createElement('input'), span = document.createElement('span'), strong = document.createElement('strong');
      input.type = 'radio'; input.name = 'video-upgrade-limit'; input.value = String(value); input.checked = value === 0;
      strong.textContent = value === 0 ? '不更新旧视频' : value === 'all' ? `全部更新（${count} 条）` : `更新 ${value} 条`;
      span.append(strong); label.append(input, span); options.append(label);
    });
    options.hidden = false;
    confirm.disabled = false;
    confirm.focus();
  }
  close.addEventListener('click', stop);
  cancel.addEventListener('click', stop);
  confirm.addEventListener('click', accept);
  document.addEventListener('keydown', keydown, true);
  void (async () => {
    try {
      let summary = response.summary;
      while (!terminal) {
        const snapshot = await ctx.Api.taskStatus(taskId);
        if (['done', 'error', 'cancelled'].includes(snapshot.state)) {
          terminal = true;
          if (closing) return;
          if (snapshot.state === 'cancelled') { clean(null); return; }
          if (snapshot.state === 'error') throw new Error(snapshot.error || '检查未完成');
          summary = snapshot.result;
        } else {
          if (!closing) status.textContent = snapshot.progress_msg || '正在检查旧视频画质';
          await pause();
        }
      }
      if (!closing) show(summary);
    } catch (error) {
      terminal = true;
      if (!closing) show({available_count: 0, failed_count: 0}, `检查未完成，可先进行本次新增备份。${error.message}`);
    }
  })();
  return choice;
}

export function init(ctx = Ptu) {
  Object.assign(ctx, {
    setMode(mode) {
      // v1.1.2：保留函数为空（兼容调用方），不再有模式切换
    },

    // ====== 登录 ======

    switchWorkspaceTab(tab) {
      if (!['library', 'self', 'other'].includes(tab)) return;
      Ptu.State.workspaceTab = tab;
      if (tab === 'other' && Ptu.State.otherArchiveContext && !Ptu.State.taskStartBusy) {
        const saved = Ptu.State.otherArchiveContext;
        Ptu.State.archiveTarget = saved.target;
        Ptu.State.backupFolderDir = saved.dir;
        Ptu.State.backupFolderInspection = saved.inspection;
      }
      document.querySelectorAll('[data-workspace-tab]').forEach(button => {
        button.setAttribute('aria-selected', String(button.dataset.workspaceTab === tab));
      });
      for (const [id, visible] of [['history-panel', tab === 'library'], ['self-archive-card', tab === 'self'], ['step-2', tab === 'other'], ['output-format-card', tab !== 'library']]) {
        const node = Ptu.$(id);
        if (node) node.hidden = !visible;
      }
      if (tab === 'library') Ptu.refreshHistoryList();
      if (tab === 'self') Ptu.refreshSelfArchiveStatus();
      if (tab === 'other' && Ptu.State.backupFolderInspection) Ptu.applyOutputFormatsToCard(Ptu.State.backupFolderInspection.output_formats);
    },

    toggleHistory(force) {
      if (force !== false) Ptu.switchWorkspaceTab('library');
    },

    async selectLibraryDirectory() {
      if (Ptu.State.isDesktop && global.pywebview?.api?.select_folder) {
        return await global.pywebview.api.select_folder('');
      }
      return (prompt('粘贴微博书目录的绝对路径') || '').trim();
    },

    async pickHistoryFolder() {
      try {
        const dir = await Ptu.selectLibraryDirectory();
        if (!dir) return;
        const found = await Ptu.Api.backupArchives(dir);
        if (!found.entries?.length) { Ptu.toast('该目录下没有找到有效微博书'); return; }
        for (const entry of found.entries) await Ptu.Api.libraryRegister(entry.path, entry.uid);
        await Ptu.refreshHistoryList();
      } catch (e) { Ptu.toast('添加失败：' + e.message); }
    },

    async refreshHistoryList() {
      const list = Ptu.$('history-list');
      if (!list) return;
      try {
        const r = await Ptu.Api.libraryList();
        Ptu.renderArchiveLibrary(r.entries || []);
      } catch (e) {
        list.innerHTML = `<li class="hint">读取书库失败：${Ptu.escape(e.message)}</li>`;
      }
    },

    async relocateLibraryEntry(entry) {
      if (Ptu.guardActiveTask()) return;
      try {
        const dir = await Ptu.selectLibraryDirectory();
        if (!dir) return;
        const result = await Ptu.Api.libraryRelocate(entry, dir);
        Ptu.renderArchiveLibrary(result.entries);
      } catch (e) { Ptu.toast('重新选址失败：' + e.message); }
    },

    async removeLibraryEntry(entry) {
      if (Ptu.guardActiveTask()) return;
      if (!confirm('仅从书库移除此登记？微博书文件会保留。')) return;
      try { Ptu.renderArchiveLibrary((await Ptu.Api.libraryRemove(entry)).entries); }
      catch (e) { Ptu.toast('移除登记失败：' + e.message); }
    },

    getVerifiedBlogger(uid) {
      const key = String(uid);
      if (Ptu.State.verifiedBloggerCache.has(key)) return Ptu.State.verifiedBloggerCache.get(key);
      const next = (Ptu.verificationQueue || Promise.resolve()).catch(() => {}).then(async () => {
        if (Date.now() < (Ptu.verificationRetryAfter || 0)) throw new Error('认证资料暂不可用');
        try { return await Ptu.Api.resolveTarget(key); }
        catch (error) { Ptu.verificationRetryAfter = Date.now() + 60000; throw error; }
      });
      Ptu.verificationQueue = next;
      Ptu.State.verifiedBloggerCache.set(key, next);
      next.catch(() => Ptu.State.verifiedBloggerCache.delete(key));
      return next;
    },

    renderArchiveLibrary(entries) {
      const list = Ptu.$('history-list');
      if (!list) return;
      if (!entries || !entries.length) {
        list.innerHTML = '<li class="hint">书库暂无登记。可以添加已有微博书，或切换到备份本人、备份他人开始建档。</li>';
        return;
      }
      list.innerHTML = '';
      entries.forEach((entry) => {
        const li = document.createElement('li');
        li.className = 'history-item';
        const lastSync = entry.last_successful_sync_at ? new Date(entry.last_successful_sync_at).toLocaleString('zh-CN', {hour12:false}) : '尚未更新';
        const info = document.createElement('div');
        info.className = 'history-item-info';
        const name = document.createElement('div');
        name.className = 'history-name';
        name.textContent = `@${entry.screen_name || entry.uid}`;
        const avatar = document.createElement('span');
        avatar.className = 'library-avatar';
        avatar.setAttribute('aria-hidden', 'true');
        avatar.textContent = String(entry.screen_name || entry.uid || '?').slice(0, 1);
        const meta = document.createElement('div');
        meta.className = 'history-meta';
        meta.textContent = entry.available === false ? '目录不可用或身份不一致，请重新选址' : `${Ptu.formatNum(entry.total_posts || 0)} 条 · 上次更新 ${lastSync}`;
        const path = document.createElement('div');
        path.className = 'history-meta library-path';
        path.textContent = entry.path;
        path.title = entry.path;
        info.append(avatar, name, meta, path);
        const formatVersion = entry.book_format_version;
        if (typeof formatVersion === 'number' && formatVersion > 0) {
          const badge = document.createElement('div');
          badge.className = 'history-meta library-format';
          if (formatVersion === 1) badge.textContent = '版式 V1 · 需重新备份';
          else if (Ptu.canUpgradeBookFormat(formatVersion)) badge.textContent = `版式 V${formatVersion} · 可升级 V${Ptu.currentBookFormat()}`;
          else badge.textContent = `版式 V${formatVersion}`;
          info.appendChild(badge);
        }
        if (entry.available !== false) {
          li.dataset.archivePath = entry.path;
          const size = document.createElement('div');
          size.className = 'history-meta library-size';
          size.textContent = '占用计算中…';
          info.appendChild(size);
        }
        const actions = document.createElement('div');
        actions.className = 'history-item-actions';
        const openBtn = document.createElement('button');
        openBtn.className = 'btn btn-secondary btn-sm';
        openBtn.type = 'button';
        openBtn.textContent = '打开位置';
        openBtn.addEventListener('click', () => Ptu.openLibraryEntry(entry));
        const readBtn = document.createElement('button');
        readBtn.className = 'btn btn-primary btn-sm library-read';
        readBtn.type = 'button';
        readBtn.textContent = '阅读';
        readBtn.disabled = entry.available === false;
        readBtn.addEventListener('click', () => Ptu.readLibraryEntry(entry));
        const updateBtn = document.createElement('button');
        updateBtn.className = 'btn btn-secondary btn-sm';
        updateBtn.type = 'button';
        updateBtn.textContent = '新增备份';
        updateBtn.addEventListener('click', () => Ptu.updateLibraryEntry(entry));
        openBtn.disabled = entry.available === false;
        updateBtn.disabled = entry.available === false || !!Ptu.State.taskId || !!Ptu.State.taskStartBusy;
        updateBtn.dataset.libraryUpdate = 'true';
        updateBtn.dataset.unavailable = String(entry.available === false);
        actions.append(readBtn, updateBtn, openBtn);
        if (Ptu.State.loggedIn && entry.available !== false && String(entry.uid) !== String(Ptu.State.loginUid)
            && typeof Ptu.Api.resolveTarget === 'function') {
          Ptu.getVerifiedBlogger(entry.uid).then(profile => {
            if (!actions.isConnected || profile.verified !== true || String(profile.uid) !== String(entry.uid)) return;
            const supplement = document.createElement('button');
            supplement.type = 'button'; supplement.className = 'btn btn-secondary btn-sm';
            supplement.textContent = '超话补漏'; supplement.dataset.libraryUpdate = 'true';
            supplement.disabled = !!Ptu.State.taskId || !!Ptu.State.taskStartBusy;
            supplement.addEventListener('click', () => Ptu.updateLibraryEntry({...entry, verified:true}, true));
            actions.appendChild(supplement);
          }).catch(() => { if (meta.isConnected) meta.title = '认证状态暂未确认；可通过搜索博主后使用超话补漏。'; });
        }
        if (entry.available !== false) {
          const backfill = document.createElement('button');
          backfill.className = 'btn btn-secondary btn-sm';
          backfill.type = 'button';
          backfill.textContent = '备份设置';
          backfill.disabled = !!Ptu.State.taskId || !!Ptu.State.taskStartBusy;
          backfill.dataset.libraryUpdate = 'true';
          backfill.addEventListener('click', () => Ptu.openLibraryBackfill(entry));
          actions.appendChild(backfill);
          if (entry.history_complete === false) meta.textContent += ' · 历史尚未补全';
        }
        if (entry.available !== false && Ptu.canUpgradeBookFormat(entry.book_format_version)) {
          const regenerate = document.createElement('button');
          regenerate.className = 'btn btn-secondary btn-sm';
          regenerate.type = 'button';
          regenerate.textContent = '升级版式';
          regenerate.title = `用本地数据把 微博书.html 升级到 V${Ptu.currentBookFormat()} 版式，不联网、不改动数据`;
          regenerate.disabled = !!Ptu.State.taskId || !!Ptu.State.taskStartBusy;
          regenerate.dataset.libraryUpdate = 'true';
          regenerate.addEventListener('click', () => Ptu.regenerateArchiveHtml(
            entry.path,
            String(entry.uid) === String(Ptu.State.loginUid) ? null : entry.uid,
          ));
          actions.appendChild(regenerate);
        }
        if (entry.available === false) {
          const relocate = document.createElement('button');
          relocate.className = 'btn btn-secondary btn-sm';
          relocate.type = 'button';
          relocate.textContent = '重新选址';
          relocate.addEventListener('click', () => Ptu.relocateLibraryEntry(entry));
          actions.appendChild(relocate);
        }
        const remove = document.createElement('button');
        remove.className = 'btn btn-secondary btn-sm';
        remove.type = 'button';
        remove.textContent = '移除登记';
        remove.addEventListener('click', () => Ptu.removeLibraryEntry(entry));
        actions.appendChild(remove);
        li.append(info, actions);
        list.appendChild(li);
      });
      Ptu.loadLibrarySizes();
    },

    async loadLibrarySizes() {
      if (typeof Ptu.Api.librarySizes !== 'function') return;
      const targets = document.querySelectorAll('#history-list .history-item[data-archive-path]');
      if (!targets.length) return;
      let rows;
      try { rows = (await Ptu.Api.librarySizes()).sizes || []; }
      catch (error) { targets.forEach(li => { const box = li.querySelector('.library-size'); if (box) box.textContent = ''; }); return; }
      const sizes = new Map(rows.map(row => [row.path, row.size_bytes]));
      targets.forEach(li => {
        const box = li.querySelector('.library-size');
        if (!box) return;
        const value = sizes.get(li.dataset.archivePath);
        box.textContent = Number.isFinite(value) ? `占用 ${Ptu.formatSize(value)}` : '占用未知';
      });
    },

    async readLibraryEntry(entry) {
      const api = global.pywebview && global.pywebview.api;
      if (api && typeof api.open_book === 'function') {
        try {
          if (await api.open_book(entry.path)) return;
          Ptu.toast('无法打开微博书，请检查文件是否已移动或被删除。');
          return;
        }
        catch (e) { /* 降级到打开位置 */ }
      }
      Ptu.openLibraryEntry(entry);
    },

    async openLibraryEntry(entry) {
      const htmlPath = `${entry.path}/微博书.html`;
      if (global.pywebview && global.pywebview.api && global.pywebview.api.show_in_folder) {
        try {
          const opened = await global.pywebview.api.show_in_folder(htmlPath);
          if (!opened) Ptu.toast('无法打开微博书位置，请检查文件或目录是否已移动。');
          return;
        }
        catch (e) { /* 降级提示路径 */ }
      }
      Ptu.toast(`微博书位置：${htmlPath}`);
    },

    // v2.2.0：书库“备份设置”打开这本书的抽屉（原“补充更早微博”入口）
    async openLibraryBackfill(entry) {
      if (Ptu.guardActiveTask()) return;
      if (!Ptu.State.loggedIn) { Ptu.toast('请先登录，再调整备份设置'); return; }
      const target = String(entry.uid) === String(Ptu.State.loginUid) ? null : {uid:entry.uid, screen_name:entry.screen_name, verified:entry.verified === true};
      let inspection;
      Ptu.State.taskStartBusy = true;
      Ptu.updateTaskActionAvailability();
      try {
        inspection = await Ptu.Api.backupInspect({output_dir:entry.path, ...(target ? {target_uid:target.uid} : {})});
        if (inspection.state !== 'archive' || String(inspection.uid) !== String(entry.uid)) throw new Error('目录不可用或账号不一致，请重新选址');
      } catch (e) { Ptu.toast('备份设置检查失败：' + e.message); return; }
      finally { Ptu.State.taskStartBusy = false; Ptu.updateTaskActionAvailability(); }
      Ptu.openArchiveDrawer({target, dir:inspection.path, inspection});
    },

    // ====== v2.2.0 备份设置抽屉 ======
    // 规则：还没建档 → 首次范围；已建档未补全 → 往前补充；已补全 → 只显示状态。
    // 节奏按微博书（UID）记在本机，抽屉、目录确认框与书库“新增备份”读写同一份。
    pacingOwnerUid(target = Ptu.State.archiveTarget) {
      return String((target && target.uid) || Ptu.State.loginUid || '');
    },

    loadPacing(uid) {
      const allowed = new Set(['standard', 'low_2_3_hours', 'low_4_6_hours', 'low_8_12_hours']);
      try {
        const saved = JSON.parse(Ptu.safeLocalGet(`weishushu.pacing.${uid}`) || 'null');
        if (saved && allowed.has(saved.pacing_mode)) {
          return {pacing_mode:saved.pacing_mode, keep_awake_when_plugged:saved.pacing_mode !== 'standard' && saved.keep_awake_when_plugged === true};
        }
      } catch (e) { /* 损坏的记录按标准速度处理 */ }
      return {pacing_mode:'standard', keep_awake_when_plugged:false};
    },

    savePacing(uid, selection) {
      if (!uid || !selection) return;
      Ptu.safeLocalSet(`weishushu.pacing.${uid}`, JSON.stringify({pacing_mode:selection.pacing_mode, keep_awake_when_plugged:Boolean(selection.keep_awake_when_plugged)}));
    },

    applyPacingToDialog(selection) {
      document.querySelectorAll('input[name="archive-pacing-mode"]').forEach((input) => {
        input.checked = input.value === selection.pacing_mode;
      });
      const keepAwake = Ptu.$('archive-keep-awake');
      if (keepAwake) keepAwake.checked = Boolean(selection.keep_awake_when_plugged);
      Ptu.syncArchivePacingControls();
    },

    drawerChoice(groupId, fallback) {
      const chip = document.querySelector(`#${groupId} .chip.on`);
      const value = chip ? Number(chip.dataset.months) : fallback;
      return Number.isInteger(value) ? value : fallback;
    },

    drawerPacingSelection() {
      const selected = document.querySelector('input[name="drawer-pacing-mode"]:checked');
      const mode = selected ? selected.value : 'standard';
      const keepAwake = Ptu.$('drawer-keep-awake');
      return {pacing_mode:mode, keep_awake_when_plugged:mode !== 'standard' && Boolean(keepAwake && keepAwake.checked)};
    },

    syncDrawerPacing() {
      const keepAwake = Ptu.$('drawer-keep-awake');
      const mode = Ptu.drawerPacingSelection().pacing_mode;
      if (keepAwake) {
        keepAwake.disabled = mode === 'standard';
        if (keepAwake.disabled) keepAwake.checked = false;
      }
    },

    openArchiveDrawer({target = null, dir = null, inspection = null} = {}) {
      const overlay = Ptu.$('archive-drawer');
      if (!overlay) return;
      const isArchive = inspection && inspection.state === 'archive';
      const state = !isArchive ? 'create' : (inspection.history_complete === false ? 'backfill' : 'complete');
      Ptu.State.drawer = {target, dir, inspection, state};
      const name = target ? `@${target.screen_name || target.uid}` : (isArchive ? `@${inspection.screen_name || inspection.uid}` : '我的微博书');
      const title = Ptu.$('archive-drawer-title');
      if (title) title.textContent = `备份设置 · ${name}`;
      const sub = Ptu.$('archive-drawer-sub');
      if (sub) sub.textContent = dir || '尚未选择保存位置，开始时会请你选择文件夹';
      const status = Ptu.$('archive-drawer-status');
      if (status) {
        if (!isArchive) status.textContent = '还没有建立微博书。';
        else {
          const lastSync = inspection.last_successful_sync_at ? new Date(inspection.last_successful_sync_at).toLocaleString('zh-CN', {hour12:false}) : '尚未记录';
          const coverage = state === 'complete' ? '历史已补全'
            : (inspection.history_cutoff ? `已备份 ${String(inspection.history_cutoff).slice(0, 10)} 之后的微博` : '历史尚未补全');
          status.textContent = `${Ptu.formatNum(inspection.total_posts || 0)} 条 · 上次更新 ${lastSync} · ${coverage}`;
        }
      }
      for (const [id, visible] of [['drawer-range-create', state === 'create'], ['drawer-range-backfill', state === 'backfill'], ['drawer-range-complete', state === 'complete']]) {
        const node = Ptu.$(id);
        if (node) node.hidden = !visible;
      }
      const pacing = Ptu.loadPacing(Ptu.pacingOwnerUid(target));
      document.querySelectorAll('input[name="drawer-pacing-mode"]').forEach((input) => { input.checked = input.value === pacing.pacing_mode; });
      const keepAwake = Ptu.$('drawer-keep-awake');
      if (keepAwake) keepAwake.checked = pacing.keep_awake_when_plugged;
      Ptu.syncDrawerPacing();
      const formats = isArchive ? (inspection.output_formats || {}) : {};
      for (const [id, key] of [['drawer-fmt-pdf', 'pdf'], ['drawer-fmt-markdown', 'markdown']]) {
        const box = Ptu.$(id);
        if (box) { box.checked = Boolean(formats[key]); box.disabled = !isArchive; }
      }
      const hint = Ptu.$('drawer-formats-hint');
      if (hint) hint.textContent = isArchive ? '改动立即保存，下次生成文件时生效；关闭不会删除已有文件。' : '首次备份只生成互动 HTML，建档完成后可在这里开启 PDF 与 Markdown。';
      const primary = Ptu.$('drawer-primary');
      if (primary) primary.textContent = state === 'create' ? '开始首次备份' : '新增备份';
      overlay.hidden = false;
    },

    closeArchiveDrawer() {
      const overlay = Ptu.$('archive-drawer');
      if (overlay) overlay.hidden = true;
    },

    async saveDrawerFormats() {
      const drawer = Ptu.State.drawer;
      if (!drawer || !drawer.inspection || drawer.inspection.state !== 'archive') return;
      const next = {pdf:Boolean(Ptu.$('drawer-fmt-pdf')?.checked), markdown:Boolean(Ptu.$('drawer-fmt-markdown')?.checked)};
      try {
        const r = await Ptu.Api.backupSettingsSave({output_dir:drawer.dir, output_formats:next, ...(drawer.target ? {target_uid:drawer.target.uid} : {})});
        drawer.inspection.output_formats = {pdf:Boolean(r.output_formats.pdf), markdown:Boolean(r.output_formats.markdown)};
        Ptu.toast('保存格式已更新');
      } catch (e) {
        Ptu.toast(`保存格式更新失败：${e.message}`);
        const box = drawer.inspection.output_formats || {};
        if (Ptu.$('drawer-fmt-pdf')) Ptu.$('drawer-fmt-pdf').checked = Boolean(box.pdf);
        if (Ptu.$('drawer-fmt-markdown')) Ptu.$('drawer-fmt-markdown').checked = Boolean(box.markdown);
      }
    },

    // kind：'primary'（首次备份或新增备份）或 'backfill'（往前补充）
    async startFromDrawer(kind) {
      const drawer = Ptu.State.drawer;
      if (!drawer || Ptu.guardActiveTask()) return;
      const selection = Ptu.drawerPacingSelection();
      Ptu.savePacing(Ptu.pacingOwnerUid(drawer.target), selection);
      Ptu.closeArchiveDrawer();
      const original = {target:Ptu.State.archiveTarget, dir:Ptu.State.backupFolderDir, inspection:Ptu.State.backupFolderInspection};
      Ptu.State.archiveTarget = drawer.target;
      if (drawer.state === 'create' && kind === 'primary') {
        const months = Ptu.drawerChoice('drawer-create-chips', 12);
        if (drawer.target) Ptu.State.createRangeMonths = months;
        else Ptu.State.selfCreateRangeMonths = months;
        if (!drawer.dir) {
          // 本人还没有微博书目录：沿用原有选择文件夹与确认流程，确认框会带出刚才的节奏
          await Ptu.backupSelf();
          return;
        }
      }
      Ptu.State.backupFolderDir = drawer.dir;
      Ptu.State.backupFolderInspection = drawer.inspection;
      Ptu.applyPacingToDialog(selection);
      let mode = 'incremental';
      if (drawer.state === 'create') mode = 'create';
      if (kind === 'backfill') {
        mode = 'backfill';
        const months = Ptu.drawerChoice('drawer-backfill-chips', 12);
        if (drawer.target) Ptu.State.targetBackfillMonths = months;
        else Ptu.State.backfillMonths = months;
      }
      try {
        await Ptu.startArchiveBackup(mode);
      } finally {
        if (!drawer.target || drawer.target !== original.target) {
          Ptu.State.archiveTarget = original.target;
          Ptu.State.backupFolderDir = original.dir;
          Ptu.State.backupFolderInspection = original.inspection;
        }
      }
    },

    openSelfDrawer() {
      if (!Ptu.State.loggedIn) { Ptu.toast('请先扫码登录'); return; }
      const inspection = Ptu.State.selfArchiveInspection;
      Ptu.openArchiveDrawer({target:null, dir:inspection?.path || null, inspection:inspection || null});
    },

    openTargetDrawer() {
      const target = Ptu.State.archiveTarget;
      const inspection = Ptu.State.backupFolderInspection;
      if (!target || !inspection) { Ptu.toast('请先选择博主和保存文件夹'); return; }
      Ptu.openArchiveDrawer({target, dir:inspection.path || Ptu.State.backupFolderDir, inspection});
    },

    async updateLibraryEntry(entry, includeSupertopics = false) {
      if (Ptu.guardActiveTask()) return;
      if (!Ptu.State.loggedIn) { Ptu.toast('请先登录，再新增备份'); return; }
      const original = {target:Ptu.State.archiveTarget, dir:Ptu.State.backupFolderDir, inspection:Ptu.State.backupFolderInspection};
      Ptu.State.taskStartBusy = true;
      Ptu.updateTaskActionAvailability();
      let inspection;
      try {
        const target = String(entry.uid) === String(Ptu.State.loginUid) ? null : {uid: entry.uid, screen_name: entry.screen_name, verified:entry.verified === true};
        inspection = await Ptu.Api.backupInspect({output_dir: entry.path, ...(target ? {target_uid: target.uid} : {})});
        if (inspection.state !== 'archive' || String(inspection.uid) !== String(entry.uid)) throw new Error('目录不可用或账号不一致，请重新选址');
        Ptu.State.archiveTarget = target;
        Ptu.State.backupFolderDir = inspection.path;
        Ptu.State.backupFolderInspection = inspection;
      } catch (e) {
        Ptu.toast('续备检查失败：' + e.message);
        return;
      } finally {
        Ptu.State.taskStartBusy = false;
        Ptu.updateTaskActionAvailability();
      }
      Ptu.applyPacingToDialog(Ptu.loadPacing(String(entry.uid)));
      try {
        const started = await Ptu.startArchiveBackup('incremental', includeSupertopics);
        if (started && !includeSupertopics && inspection.history_complete === false) {
          Ptu.toast('该微博书历史尚未补全，可在书库中点「备份设置」往前补充');
        }
      } finally {
        Ptu.State.archiveTarget = original.target;
        Ptu.State.backupFolderDir = original.dir;
        Ptu.State.backupFolderInspection = original.inspection;
      }
    },

    // ====== 博主搜索与目标选择（备份他人微博） ======

    async searchOrResolve() {
      if (Ptu.guardActiveTask()) return;
      const input = Ptu.$('url-input');
      const query = input ? input.value.trim() : '';
      if (!query) { Ptu.toast('请输入博主昵称或微博主页链接'); return; }
      const btn = Ptu.$('btn-search-blogger');
      const generation = ++Ptu.State.bloggerSearchGeneration;
      Ptu.State.bloggerSearchBusy = true;
      const isCurrent = () => generation === Ptu.State.bloggerSearchGeneration && input.value.trim() === query;
      if (btn) { btn.disabled = true; btn.textContent = '搜索中…'; }
      try {
        const looksLikeLink = /https?:\/\/|weibo\.com|weibo\.cn|t\.cn/i.test(query) || /^\d{5,20}$/.test(query);
        if (looksLikeLink) {
          const target = await Ptu.Api.resolveTarget(query);
          if (isCurrent()) Ptu.renderBloggerResults([target]);
          return;
        }
        const r = await Ptu.Api.searchUsers(query);
        if (isCurrent()) Ptu.renderBloggerResults(r.results || []);
      } catch (e) {
        if (isCurrent()) Ptu.toast(e.message);
      } finally {
        if (generation === Ptu.State.bloggerSearchGeneration) {
          Ptu.State.bloggerSearchBusy = false;
          if (btn) { btn.disabled = !!Ptu.State.taskId || !!Ptu.State.taskStartBusy; btn.textContent = '搜索'; }
        }
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
          Ptu.selectBloggerTarget({ uid: String(u.uid), screen_name: u.screen_name, verified: u.verified === true });
        });
        ul.appendChild(li);
      });
    },

    async selectBloggerTarget(target) {
      if (Ptu.guardActiveTask()) return;
      Ptu.State.archiveTarget = target;
      if (typeof target.verified === 'boolean') Ptu.State.verifiedBloggerCache.set(String(target.uid), Promise.resolve(target));
      const ul = Ptu.$('blogger-results');
      if (ul) ul.hidden = true;
      const picked = Ptu.$('picked-blogger');
      const pickedName = Ptu.$('picked-blogger-name');
      if (pickedName) pickedName.textContent = `@${target.screen_name}`;
      Ptu.resetPickedTargetFolder();
      if (picked) picked.hidden = false;
      Ptu.State.otherArchiveContext = {target, dir:null, inspection:null};
    },

    resetPickedTargetFolder() {
      Ptu.State.backupFolderDir = null;
      Ptu.State.backupFolderInspection = null;
      const path = Ptu.$('picked-folder-path');
      if (path) path.textContent = '未选择文件夹';
      const status = Ptu.$('picked-folder-status');
      if (status) { status.hidden = true; status.textContent = ''; }
      const supplement = Ptu.$('target-supertopic-row');
      if (supplement) supplement.hidden = true;
      const backfill = Ptu.$('target-backfill-row');
      if (backfill) backfill.hidden = true;
      const regenerateRow = Ptu.$('target-regenerate-row');
      if (regenerateRow) regenerateRow.hidden = true;
      const createBlock = Ptu.$('picked-create-block');
      if (createBlock) createBlock.hidden = true;
      const updateBlock = Ptu.$('picked-update-block');
      if (updateBlock) updateBlock.hidden = true;
    },

    async pickTargetFolder() {
      if (Ptu.guardActiveTask()) return;
      const target = Ptu.State.archiveTarget;
      if (!target) { Ptu.toast('请先搜索并选中博主'); return; }
      if (!global.pywebview || !global.pywebview.api || !global.pywebview.api.select_folder) {
        Ptu.toast('浏览器开发模式无法选择本地文件夹，请在桌面版中使用');
        return;
      }
      const btn = Ptu.$('btn-pick-target-folder');
      const originalText = btn ? btn.textContent : '';
      if (btn) { btn.disabled = true; btn.textContent = '正在检查文件夹…'; }
      try {
        const dir = await global.pywebview.api.select_folder('');
        if (!dir) return;
        const inspection = await Ptu.Api.backupInspect({ output_dir: dir, target_uid: target.uid });
        if (target !== Ptu.State.archiveTarget) return;
        Ptu.showPickedTargetInspection(inspection);
      } catch (e) {
        Ptu.toast(`目录检查失败：${e.message}`);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = originalText; }
      }
    },

    showPickedTargetInspection(inspection) {
      const supplement = Ptu.$('target-supertopic-row');
      if (supplement) supplement.hidden = true;
      const backfill = Ptu.$('target-backfill-row');
      if (backfill) backfill.hidden = true;
      const regenerateRow = Ptu.$('target-regenerate-row');
      if (regenerateRow) regenerateRow.hidden = true;
      const path = Ptu.$('picked-folder-path');
      const status = Ptu.$('picked-folder-status');
      const createBlock = Ptu.$('picked-create-block');
      const updateBlock = Ptu.$('picked-update-block');
      const setStatus = (text) => {
        if (status) { status.hidden = !text; status.textContent = text || ''; }
      };
      Ptu.State.backupFolderDir = null;
      Ptu.State.backupFolderInspection = null;
      const context = {target:Ptu.State.archiveTarget, dir:null, inspection:null};
      Ptu.State.otherArchiveContext = context;
      if (createBlock) createBlock.hidden = true;
      if (updateBlock) updateBlock.hidden = true;
      if (!inspection || !inspection.state) { setStatus('目录检查失败：服务未返回目录状态'); return; }
      if (inspection.state === 'archive') {
        Ptu.State.backupFolderDir = inspection.path;
        Ptu.State.backupFolderInspection = inspection;
        context.dir = inspection.path;
        context.inspection = inspection;
        if (path) path.textContent = inspection.path;
        const lastSync = inspection.last_successful_sync_at ? new Date(inspection.last_successful_sync_at).toLocaleString('zh-CN', {hour12:false}) : '尚未记录';
        const formatVersion = inspection.book_format_version;
        const formatText = typeof formatVersion === 'number' && formatVersion > 0
          ? ` · 版式 V${formatVersion}${Ptu.canUpgradeBookFormat(formatVersion) ? `（可升级 V${Ptu.currentBookFormat()}）` : ''}` : '';
        setStatus(`已有微博书 · 累计 ${inspection.total_posts || 0} 条 · 上次更新 ${lastSync}${formatText}`);
        if (updateBlock) updateBlock.hidden = false;
        const regenerateRow = Ptu.$('target-regenerate-row');
        if (regenerateRow) regenerateRow.hidden = !Ptu.canUpgradeBookFormat(formatVersion);
        if (supplement) supplement.hidden = Ptu.State.archiveTarget?.verified !== true;
        if (backfill) backfill.hidden = inspection.history_complete !== false;
        const historyStatus = Ptu.$('target-backfill-status');
        if (historyStatus) historyStatus.textContent = inspection.history_cutoff
          ? `当前已备份 ${inspection.history_cutoff} 起的微博，可继续向前扩展。`
          : '历史尚未补全，可继续回填更早内容。';
        if (Ptu.State.workspaceTab === 'other') Ptu.applyOutputFormatsToCard(inspection.output_formats);
        return;
      }
      if (inspection.state === 'empty' || inspection.state === 'ordinary_nonempty'
          || inspection.state === 'legacy_index') {
        Ptu.State.backupFolderDir = inspection.path;
        Ptu.State.backupFolderInspection = inspection;
        context.dir = inspection.path;
        context.inspection = inspection;
        if (path) path.textContent = inspection.path;
        setStatus(inspection.state === 'empty' ? ''
          : inspection.state === 'legacy_index' ? '旧版备份目录，将首次建立完整档案'
          : '将自动新建子文件夹「昵称_UID」存放微博书，不影响现有文件。');
        if (createBlock) createBlock.hidden = false;
        return;
      }
      if (inspection.state === 'uid_mismatch') {
        if (path) path.textContent = inspection.path;
        setStatus(inspection.screen_name
          ? `该微博书属于 @${inspection.screen_name}；如要更新 TA 的微博书，请先搜索并选中该博主`
          : '该微博书属于其他博主，不能覆盖');
        return;
      }
      if (inspection.state === 'damaged') { setStatus('微博书档案不完整，请先复制目录后再修复'); return; }
      setStatus('目录检查失败：未知目录状态');
    },

    // ====== 微博书目录决策 ======

    async backupSelf(returnFocus = null) {
      if (Ptu.guardActiveTask()) return;
      Ptu.State.archiveTarget = null;
      const remembered = Ptu.State.selfArchiveDir;
      if (remembered) {
        try {
          const inspection = await Ptu.Api.backupInspect({ output_dir: remembered });
          await Ptu.handleArchiveInspection(inspection, returnFocus);
          return;
        } catch (e) {
          Ptu.State.selfArchiveDir = null;
          Ptu.safeLocalSet('weishushu.selfArchiveDir', '');
        }
      }
      await Ptu.pickArchiveFolderAndInspect(returnFocus);
    },

    async refreshSelfArchiveStatus() {
      const statusEl = Ptu.$('self-archive-status');
      const backfillRow = Ptu.$('backfill-row');
      const loginHint = Ptu.$('self-archive-login-hint');
      const block = Ptu.$('backup-self-block');
      const formatCard = Ptu.$('output-format-card');
      if (!Ptu.State.loggedIn) {
        if (block) block.hidden = true;
        if (loginHint) loginHint.hidden = false;
        // 未登录时没有可备份的对象，不显示保存格式
        if (formatCard && Ptu.State.workspaceTab === 'self') formatCard.hidden = true;
        return;
      }
      if (loginHint) loginHint.hidden = true;
      if (block) block.hidden = false;
      if (formatCard && Ptu.State.workspaceTab === 'self') formatCard.hidden = false;
      const dir = Ptu.State.selfArchiveDir
        || (Ptu.safeLocalGet('weishushu.selfArchiveDir') || '').trim();
      Ptu.State.selfArchiveDir = dir || null;
      if (!dir) {
        Ptu.State.selfArchiveInspection = null;
        if (statusEl) statusEl.textContent = '尚未选择微博书目录，点击按钮开始首次建档';
        if (backfillRow) backfillRow.hidden = true;
        if (Ptu.State.workspaceTab !== 'other') Ptu.applyOutputFormatsToCard({ pdf: false, markdown: false });
        return;
      }
      try {
        const inspection = await Ptu.Api.backupInspect({ output_dir: dir });
        if (inspection.state !== 'archive') {
          Ptu.State.selfArchiveInspection = null;
          if (statusEl) statusEl.textContent = '所选目录还不是微博书，点击按钮重新选择';
          if (backfillRow) backfillRow.hidden = true;
          if (Ptu.State.workspaceTab !== 'other') Ptu.applyOutputFormatsToCard({ pdf: false, markdown: false });
          return;
        }
        // 旧版「我的微博」文件夹被后端改名为「昵称_UID」后，本地记忆跟随新路径
        if (inspection.path && inspection.path !== dir) {
          Ptu.State.selfArchiveDir = inspection.path;
          Ptu.safeLocalSet('weishushu.selfArchiveDir', inspection.path);
        }
        Ptu.State.selfArchiveInspection = inspection;
        const lastSync = inspection.last_successful_sync_at ? new Date(inspection.last_successful_sync_at).toLocaleString('zh-CN', {hour12:false}) : '尚未记录';
        const formatVersion = inspection.book_format_version;
        const formatText = typeof formatVersion === 'number' && formatVersion > 0
          ? ` · 版式 V${formatVersion}${Ptu.canUpgradeBookFormat(formatVersion) ? `（可升级 V${Ptu.currentBookFormat()}）` : ''}` : '';
        if (statusEl) {
          statusEl.textContent =
            `@${inspection.screen_name} · 累计 ${inspection.total_posts || 0} 条 · 上次更新 ${lastSync}${formatText}`;
        }
        if (backfillRow) backfillRow.hidden = !!inspection.history_complete;
        if (statusEl && inspection.history_complete === false) statusEl.textContent += ' · 历史尚未补全，可在“备份设置”中往前补充';
        if (Ptu.State.workspaceTab !== 'other') Ptu.applyOutputFormatsToCard(inspection.output_formats || { pdf: false, markdown: false });
      } catch (e) {
        Ptu.State.selfArchiveInspection = null;
        if (statusEl) statusEl.textContent = `微博书状态检查失败：${e.message}`;
        if (backfillRow) backfillRow.hidden = true;
      }
    },

    applyOutputFormatsToCard(formats) {
      const merged = {
        pdf: Boolean(formats && formats.pdf),
        markdown: Boolean(formats && formats.markdown),
      };
      if (Ptu.State.workspaceTab === 'other' && !Ptu.State.archiveTarget) return;
      Ptu.State.outputFormats = merged;
      const targetHint = Ptu.$('output-format-target');
      const other = Ptu.State.workspaceTab === 'other';
      const inspection = other ? Ptu.State.backupFolderInspection : Ptu.State.selfArchiveInspection;
      if (targetHint) targetHint.textContent = inspection?.state === 'archive' ? `应用于 @${inspection.screen_name || inspection.uid} · ${inspection.path}` : '首次建档仅生成互动 HTML；建档后可在此启用更多格式。';
      const pdf = Ptu.$('fmt-pdf');
      const markdown = Ptu.$('fmt-markdown');
      if (pdf) {
        pdf.checked = merged.pdf;
        const row = Ptu.$('fmt-pdf-row');
        if (row) row.classList.toggle('on', merged.pdf);
      }
      if (markdown) {
        markdown.checked = merged.markdown;
        const row = Ptu.$('fmt-markdown-row');
        if (row) row.classList.toggle('on', merged.markdown);
      }
    },

    async saveOutputFormats() {
      const pdf = Ptu.$('fmt-pdf');
      const markdown = Ptu.$('fmt-markdown');
      const next = {
        pdf: Boolean(pdf && pdf.checked),
        markdown: Boolean(markdown && markdown.checked),
      };
      if (Ptu.guardActiveTask()) { Ptu.applyOutputFormatsToCard(Ptu.State.outputFormats); return; }
      const other = Ptu.State.workspaceTab === 'other';
      const inspection = other ? Ptu.State.backupFolderInspection : Ptu.State.selfArchiveInspection;
      const dir = inspection?.state === 'archive' ? inspection.path : null;
      if (!dir) {
        Ptu.toast('请先完成微博书建档，再调整保存格式');
        Ptu.applyOutputFormatsToCard(Ptu.State.outputFormats);
        return;
      }
      try {
        const r = await Ptu.Api.backupSettingsSave({
          output_dir: dir,
          output_formats: next,
          ...(other && Ptu.State.archiveTarget ? {target_uid: Ptu.State.archiveTarget.uid} : {}),
        });
        const saved = {pdf:Boolean(r.output_formats.pdf), markdown:Boolean(r.output_formats.markdown)};
        inspection.output_formats = saved;
        const current = Ptu.State.workspaceTab === 'other' ? Ptu.State.backupFolderInspection : Ptu.State.selfArchiveInspection;
        if (current === inspection) {
          Ptu.State.outputFormats = saved;
          Ptu.applyOutputFormatsToCard(saved);
        }
        Ptu.toast('保存格式已更新');
      } catch (e) {
        Ptu.toast(`保存格式更新失败：${e.message}`);
        Ptu.applyOutputFormatsToCard(Ptu.State.outputFormats);
      }
    },

    async refreshArchiveAfterTask(result) {
      if (!result?.uid || !result.output_dir) return;
      if (String(result.uid) === String(Ptu.State.loginUid)) {
        Ptu.State.selfArchiveDir = result.output_dir;
        Ptu.safeLocalSet('weishushu.selfArchiveDir', result.output_dir);
      }
      const saved = Ptu.State.otherArchiveContext;
      if (!saved?.target || String(saved.target.uid) !== String(result.uid)) return;
      try {
        const inspection = await Ptu.Api.backupInspect({output_dir:result.output_dir, target_uid:saved.target.uid});
        if (Ptu.State.otherArchiveContext !== saved || inspection.state !== 'archive'
            || String(inspection.uid) !== String(saved.target.uid)) return;
        saved.dir = inspection.path;
        saved.inspection = inspection;
        if (Ptu.State.workspaceTab === 'other' && !Ptu.State.taskId && !Ptu.State.taskStartBusy) {
          Ptu.State.archiveTarget = saved.target;
          Ptu.showPickedTargetInspection(inspection);
        }
      } catch (e) { Ptu.toast('备份已完成，档案状态刷新失败：' + e.message); }
    },

    async startSupertopicSupplement() {
      if (Ptu.guardActiveTask()) return;
      if (!Ptu.State.loggedIn) { Ptu.toast('请先登录，再超话补漏'); return; }
      const target = Ptu.State.archiveTarget;
      const inspection = Ptu.State.backupFolderInspection;
      if (target?.verified !== true || inspection?.state !== 'archive' || String(inspection.uid) !== String(target.uid)) {
        Ptu.toast('请先选择已有的带认证 V 标识博主档案'); return;
      }
      await Ptu.startArchiveBackup('incremental', true);
    },

    async startTargetBackfill() {
      if (Ptu.guardActiveTask()) return;
      const target = Ptu.State.archiveTarget;
      const inspection = Ptu.State.backupFolderInspection;
      if (!target || inspection?.state !== 'archive' || String(inspection.uid) !== String(target.uid)
          || inspection.history_complete !== false) {
        Ptu.toast('请先选择历史尚未补全的博主微博书');
        return;
      }
      await Ptu.startArchiveBackup('backfill');
    },

    async startBackfill() {
      if (Ptu.guardActiveTask()) return;
      const dir = Ptu.State.selfArchiveDir;
      if (!dir) {
        Ptu.toast('请先选择微博书目录');
        return;
      }
      Ptu.State.archiveTarget = null;
      Ptu.State.backupFolderDir = dir;
      Ptu.State.archivePacingMode = 'standard';
      await Ptu.startArchiveBackup('backfill');
    },

    async startArchivePreview() {
      return Ptu.regenerateArchiveHtml(Ptu.State.selfArchiveDir, null);
    },

    async regenerateArchiveHtml(path, targetUid = null) {
      if (Ptu.guardActiveTask()) return;
      if (!path) {
        Ptu.toast('请先选择微博书目录');
        return;
      }
      Ptu.State.taskStartBusy = true;
      Ptu.updateTaskActionAvailability();
      try {
        const r = await Ptu.Api.backupPreview({ output_dir: path, ...(targetUid ? { target_uid: targetUid } : {}) });
        Ptu.setActiveTask(r.task_id);
        Ptu.State._backupOutputDir = path;
        Ptu.$('step-progress').hidden = false;
        Ptu.$('result-card').hidden = true;
        Ptu.setProgress(0, '更新 HTML · 输出目录：' + path);
        Ptu.subscribeTask(r.task_id);
        Ptu.toast('更新 HTML 已启动');
      } catch (e) {
        Ptu.toast(`重新生成失败：${e.message}`);
      } finally {
        Ptu.State.taskStartBusy = false;
        Ptu.updateTaskActionAvailability();
      }
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
        if (!Ptu.State.archiveTarget && inspection.state === 'archive') {
          Ptu.State.selfArchiveDir = inspection.path;
          Ptu.safeLocalSet('weishushu.selfArchiveDir', inspection.path);
        }
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
      // v2.2.0：带出这本书上次的节奏；没有记录时为标准速度
      const saved = typeof Ptu.loadPacing === 'function' ? Ptu.loadPacing(Ptu.pacingOwnerUid()) : {pacing_mode:'standard', keep_awake_when_plugged:false};
      document.querySelectorAll('input[name="archive-pacing-mode"]').forEach((input) => {
        input.checked = input.value === saved.pacing_mode;
      });
      Ptu.State.archivePacingMode = saved.pacing_mode;
      const keepAwake = Ptu.$('archive-keep-awake');
      if (keepAwake) {
        keepAwake.checked = saved.keep_awake_when_plugged;
        keepAwake.disabled = saved.pacing_mode === 'standard';
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

    async startArchiveBackup(mode, includeSupertopics = false) {
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
      let slowStartTimer = null;
      try {
        const pacing = Ptu.getArchivePacingSelection();
        Ptu.savePacing(Ptu.pacingOwnerUid(), pacing);
        const startReq = {
          output_dir: dir,
          mode,
          pacing_mode: pacing.pacing_mode,
          keep_awake_when_plugged: pacing.keep_awake_when_plugged,
        };
        const target = Ptu.State.archiveTarget;
        if (target) startReq.target_uid = target.uid;
        if (includeSupertopics) {
          if (target?.verified !== true) throw new Error('超话补漏仅适用于带认证 V 标识的博主');
          startReq.include_supertopics = true;
        }
        if (mode === 'create') {
          // 他人沿用首次范围选择；本人只有在抽屉中选过范围才带上，未选时保持备份全部
          const range = target ? Ptu.State.createRangeMonths : Ptu.State.selfCreateRangeMonths;
          if (Number.isInteger(range)) startReq.range_months = range;
        }
        if (mode === 'backfill') {
          startReq.backfill_months = target ? Ptu.State.targetBackfillMonths : Ptu.State.backfillMonths;
        }
        if (mode === 'incremental' && !includeSupertopics) {
          const choice = await chooseVideoUpgrade(Ptu, {output_dir: dir, pacing_mode: pacing.pacing_mode, ...(target ? {target_uid: target.uid} : {})});
          if (choice === null) return;
          startReq.video_upgrade_limit = choice;
        }
        slowStartTimer = setTimeout(() => Ptu.toast('仍在启动，请勿重复操作'), 15000);
        const r = await Ptu.Api.backupStart(startReq);
        if (!target) {
          Ptu.State.selfArchiveDir = dir;
          Ptu.safeLocalSet('weishushu.selfArchiveDir', dir);
        }
        Ptu.setActiveTask(r.task_id);
        Ptu.State._backupOutputDir = dir;
        Ptu.$('step-progress').hidden = false;
        Ptu.$('result-card').hidden = true;
        Ptu.setTaskCancelVisible(true);
        const labels = { create: '首次建立', incremental: '新增备份', rebuild: '重新全部备份', backfill: '历史回填' };
        if (includeSupertopics) labels.incremental = '超话补漏';
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
        if (!Ptu.$('archive-folder-decision')?.hidden) Ptu.closeArchiveFolderDecision(false, true);
        Ptu.toast(`${labels[mode]}已启动`);
        return true;
      } catch (e) {
        Ptu.toast(`备份启动失败：${e.message}`);
        return false;
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
        if (!Ptu.$('archive-folder-decision')?.hidden) Ptu.closeArchiveFolderDecision(false, true);
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
