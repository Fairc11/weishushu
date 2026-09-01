// 阶段 5：tasks 模块。
import { Ptu, global } from "./state.js";
import { init as initFeedback } from "./feedback.js";
void initFeedback;

export function init(ctx = Ptu) {
  Object.assign(ctx, {
    setProgress(pct, msg) {
      Ptu.$('progress-fill').style.width = `${Math.max(0, Math.min(1, pct)) * 100}%`;
      Ptu.$('progress-text').textContent = msg || `${(pct * 100).toFixed(0)}%`;
    },

    renderProgressEvent(event) {
      if (!event || !event.phase) return;
      const phases = ['identify', 'extract', 'media', 'generate', 'report', 'complete'];
      // 归档同步还会发 discover（发现分页）与 comments（处理评论），
      // 都属于「抓取微博」阶段；不映射会让 indexOf 得到 -1，把已完成的步骤全部清空。
      const aliases = { discover: 'extract', comments: 'extract' };
      const stepperPhase = aliases[event.phase] || event.phase;
      const activeIndex = phases.indexOf(stepperPhase);
      if (activeIndex !== -1) {
        document.querySelectorAll('[data-progress-phase]').forEach((item) => {
          const index = phases.indexOf(item.dataset.progressPhase);
          item.classList.toggle('is-active', index === activeIndex && stepperPhase !== 'complete');
          item.classList.toggle('is-done', stepperPhase === 'complete' || index < activeIndex);
        });
      }
      const detail = Ptu.$('progress-stage-detail');
      const count = Ptu.$('progress-stage-count');
      const elapsed = Ptu.$('progress-stage-elapsed');
      const track = Ptu.$('progress-stage-track');
      if (detail) detail.textContent = event.detail || '正在处理…';
      const unitNames = { account: '个账号', post: '条', media: '个媒体', file: '个文件' };
      const unit = unitNames[event.unit] || '';
      if (count) {
        const pageText = event.current_page ? `第 ${event.current_page} 页 · ` : '';
        if (event.current == null) count.textContent = pageText || '尚无计数';
        else if (event.total == null) count.textContent = `${pageText}已处理 ${event.current}${unit} · 总量未知`;
        else count.textContent = `${pageText}${event.current}/${event.total}${unit}`;
      }
      if (elapsed) elapsed.textContent = `${Number(event.elapsed_seconds || 0).toFixed(1)} 秒`;
      if (track) {
        const indeterminate = event.total == null;
        track.classList.toggle('is-indeterminate', indeterminate);
        const fill = track.querySelector('span');
        if (fill && !indeterminate) {
          const ratio = event.total > 0 ? event.current / event.total : (event.phase === 'complete' ? 1 : 0);
          fill.style.width = `${Math.max(0, Math.min(1, ratio)) * 100}%`;
        }
      }
    },

    setTaskCancelVisible(visible) {
      const button = Ptu.$('btn-cancel-task');
      if (button) button.hidden = !visible;
    },

    guardActiveTask() {
      if (!Ptu.State.taskId && !Ptu.State.taskStartBusy) return false;
      Ptu.toast('已有任务正在运行，请先等待完成或取消');
      return true;
    },

    updateTaskActionAvailability() {
      const disabled = !!Ptu.State.taskId || Ptu.State.taskStartBusy;
      const backupButton = Ptu.$('btn-backup-self');
      const startButton = Ptu.$('btn-start');
      const searchButton = Ptu.$('btn-search-blogger');
      if (backupButton) backupButton.disabled = disabled || Ptu.State.backupFolderBusy;
      if (startButton) startButton.disabled = disabled;
      if (searchButton) searchButton.disabled = disabled || Ptu.State.backupFolderBusy;
    },

    setActiveTask(taskId) {
      Ptu.State.taskId = taskId;
      Ptu.updateTaskActionAvailability();
    },

    clearActiveTask() {
      Ptu.State.taskId = null;
      Ptu.updateTaskActionAvailability();
    },

    async checkRecoveryTask() {
      try {
        const response = await Ptu.Api.recoveryTask();
        if (response && response.task) Ptu.showRecoveryTask(response.task);
      } catch (error) {
        console.warn('recovery task check failed', error);
      }
    },

    showRecoveryTask(task) {
      const overlay = Ptu.$('recovery-task-overlay');
      const summary = Ptu.$('recovery-task-summary');
      if (!overlay || !summary || !task) return;
      Ptu.State.recoveryTask = task;
      const modes = {
        create: '首次建立',
        incremental: '增量更新',
        rebuild: '重新全量',
        update: '更新关注资料',
      };
      const units = { post: '条', page: '页', file: '个文件' };
      const unit = units[task.progress_unit] || '';
      const progress = task.progress_total == null
        ? `已完成 ${task.progress_current}${unit ? ` ${unit}` : ''} · 总量未知`
        : `已完成 ${task.progress_current} / ${task.progress_total}${unit ? ` ${unit}` : ''}`;
      const savedAt = new Date(task.saved_at);
      const savedText = Number.isNaN(savedAt.getTime())
        ? task.saved_at
        : savedAt.toLocaleString('zh-CN', { hour12: false });
      const pacingLabels = {
        standard: '标准速度',
        low_2_3_hours: '低强度模式 · 约 2～3 小时',
        low_4_6_hours: '低强度模式 · 约 4～6 小时',
        low_8_12_hours: '低强度模式 · 约 8～12 小时',
      };
      const taskLabel = task.task_kind === 'following_archive'
        ? '关注资料'
        : (task.target_label ? `@${task.target_label} 的微博书` : '本人微博书');
      summary.textContent = [
        `${taskLabel} · ${modes[task.mode] || task.mode}`,
        pacingLabels[task.pacing_mode] || task.pacing_mode,
        progress,
        `上次安全保存：${savedText}`,
        task.saved_content,
      ].join('\n');
      overlay.hidden = false;
      const resume = Ptu.$('recovery-task-resume');
      const nonrecoverable = Ptu.$('recovery-task-nonrecoverable');
      const canResume = task.state !== 'error' || task.error_recoverable === true;
      if (nonrecoverable) nonrecoverable.hidden = canResume;
      Ptu.setRecoveryActionBusy(Ptu.State.recoveryActionBusy);
      const focusTarget = canResume ? resume : Ptu.$('recovery-task-abandon');
      if (focusTarget) focusTarget.focus();
    },

    setRecoveryActionBusy(busy) {
      Ptu.State.recoveryActionBusy = Boolean(busy);
      const task = Ptu.State.recoveryTask;
      const canResume = Boolean(task)
        && (task.state !== 'error' || task.error_recoverable === true);
      const resume = Ptu.$('recovery-task-resume');
      const abandon = Ptu.$('recovery-task-abandon');
      if (resume) resume.disabled = Ptu.State.recoveryActionBusy || !canResume;
      if (abandon) abandon.disabled = Ptu.State.recoveryActionBusy || !task;
    },

    deferRecoveryTask() {
      const overlay = Ptu.$('recovery-task-overlay');
      if (overlay) overlay.hidden = true;
    },

    async resumeRecoveryTask() {
      const task = Ptu.State.recoveryTask;
      if (!task) return;
      if (Ptu.State.recoveryActionBusy) return;
      if (task.state === 'error' && task.error_recoverable !== true) {
        Ptu.toast('本次错误不能继续任务，请放弃未完成部分');
        return;
      }
      Ptu.setRecoveryActionBusy(true);
      Ptu.setActiveTask(task.task_id);
      try {
        await Ptu.Api.resumeTask(task.task_id);
        Ptu.$('step-progress').hidden = false;
        Ptu.renderPacingStatus({
          mode: task.pacing_mode,
          state: task.pacing_state,
          request_kind: task.pacing_request_kind,
          next_wait_seconds: task.next_wait_seconds,
          disclaimer: '目标区间不是完成时间承诺',
        });
        Ptu.setTaskCancelVisible(true);
        Ptu.setProgress(0, '正在从上次安全保存处继续…');
        Ptu.deferRecoveryTask();
        Ptu.subscribeTask(task.task_id);
        Ptu.toast('任务已继续');
      } catch (error) {
        if (Ptu.State.taskId === task.task_id) Ptu.clearActiveTask();
        Ptu.toast('继续失败：' + error.message);
      } finally {
        Ptu.setRecoveryActionBusy(false);
      }
    },

    async openRecoverySavedContent() {
      const task = Ptu.State.recoveryTask;
      if (!task) return;
      if (!Ptu.State.isDesktop) {
        Ptu.toast('请在桌面版中查看已保存内容');
        return;
      }
      try {
        const opened = await Ptu.Api.openActiveTaskFolder(task.task_id);
        if (!opened) Ptu.toast('打开已保存内容失败');
      } catch (error) {
        Ptu.toast('打开失败：' + error.message);
      }
    },

    async abandonRecoveryTask() {
      const task = Ptu.State.recoveryTask;
      if (!task) return;
      if (Ptu.State.recoveryActionBusy) return;
      if (!confirm('确定放弃本次未完成部分？上一份正式数据会保留。')) return;
      Ptu.setRecoveryActionBusy(true);
      try {
        await Ptu.Api.abandonTask(task.task_id);
        Ptu.State.recoveryTask = null;
        Ptu.deferRecoveryTask();
        Ptu.toast('已放弃本次未完成部分');
      } catch (error) {
        Ptu.toast('放弃失败：' + error.message);
      } finally {
        Ptu.setRecoveryActionBusy(false);
      }
    },

    showCloseProtection() {
      const overlay = Ptu.$('close-protection-overlay');
      if (!overlay) return;
      overlay.hidden = false;
      const minimize = Ptu.$('close-protection-minimize');
      if (minimize) minimize.focus();
    },

    async minimizeAndContinue() {
      try {
        if (!global.pywebview || !global.pywebview.api || !global.pywebview.api.minimize) {
          throw new Error('桌面窗口接口不可用');
        }
        await global.pywebview.api.minimize();
        const overlay = Ptu.$('close-protection-overlay');
        if (overlay) overlay.hidden = true;
      } catch (error) {
        Ptu.toast('最小化失败：' + error.message);
      }
    },

    async pauseThenClose() {
      const taskId = Ptu.State.taskId;
      if (!taskId) {
        Ptu.toast('未找到正在运行的持久任务');
        return;
      }
      const button = Ptu.$('close-protection-pause-exit');
      if (button) {
        button.disabled = true;
        button.textContent = '正在等待安全保存点…';
      }
      try {
        await Ptu.Api.pauseTask(taskId);
        let waiting = false;
        for (let attempt = 0; attempt < 80; attempt += 1) {
          const snapshot = await Ptu.Api.taskStatus(taskId);
          if (snapshot.state === 'waiting_resume') {
            waiting = true;
            break;
          }
          if (snapshot.state === 'error' || snapshot.state === 'cancelled' || snapshot.state === 'done') {
            throw new Error(`任务已进入 ${snapshot.state} 状态`);
          }
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
        if (!waiting) throw new Error('暂停超时，窗口已保留');
        const closed = await Ptu.Api.closeAfterPause(taskId);
        if (!closed) throw new Error('后端未确认安全暂停，窗口已保留');
      } catch (error) {
        Ptu.toast('暂停后退出失败：' + error.message);
        if (button) {
          button.disabled = false;
          button.textContent = '暂停后退出';
        }
      }
    },

    async cancelTask() {
      if (!Ptu.State.taskId) return;
      try {
        const result = await Ptu.Api.cancelTask(Ptu.State.taskId);
        if (result.cancelled) {
          Ptu.setProgress(0, '正在停止任务...');
          Ptu.setTaskCancelVisible(false);
        } else {
          Ptu.toast(`无法取消：任务已是 ${result.state || '终态'}`);
        }
      } catch (e) {
        Ptu.toast(`取消失败：${e.message}`);
      }
    },

    // ====== WS 订阅 ======

    subscribeTask(taskId) {
      if (Ptu.State.ws) Ptu.State.ws.close();
      Ptu.maybeShowLogButton();
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const url = `${proto}://${location.host}/ws/tasks/${taskId}`;
      const ws = new WsClient(url);
      ws.on((msg) => Ptu.handleTaskMessage(msg));
      ws.connect();
      Ptu.State.ws = ws;
    },

    handleTaskMessage(msg) {
      switch (msg.type) {
        case 'snapshot':
          Ptu.setProgress(msg.progress_pct || 0, msg.progress_msg || '已订阅');
          if (msg.progress_event) Ptu.renderProgressEvent(msg.progress_event);
          if (msg.pacing_mode) Ptu.renderPacingStatus(msg);
          break;
        case 'progress':
          Ptu.setProgress(msg.pct, msg.msg);
          if (msg.event) Ptu.renderProgressEvent(msg.event);
          break;
        case 'pacing':
          Ptu.renderPacingStatus(msg);
          break;
        case 'log':
          (msg.entries || []).forEach(e => Ptu.appendLog(e));
          break;
        case 'done':
          Ptu.setProgress(1, msg.result && msg.result.task_kind === 'following_archive'
            ? '关注资料更新完成 ✓' : '生成完成 ✓');
          Ptu.renderResult(msg.result || {});
          Ptu.toast(msg.result && msg.result.task_kind === 'following_archive'
            ? '关注资料更新完成' : '微博书生成完成');
          Ptu.setTaskCancelVisible(false);
          Ptu.clearActiveTask();
          Ptu._closeTaskWs();
          Ptu.hidePacingStatus();
          break;
        case 'error':
          Ptu.setProgress(0, '失败：' + (msg.error || '未知错误'));
          Ptu.toast('任务失败，详见日志');
          Ptu.setTaskCancelVisible(false);
          Ptu.clearActiveTask();
          Ptu._closeTaskWs();
          Ptu.hidePacingStatus();
          break;
        case 'cancelled':
          Ptu.setProgress(0, '任务已取消');
          Ptu.toast('任务已取消');
          Ptu.setTaskCancelVisible(false);
          Ptu.clearActiveTask();
          Ptu._closeTaskWs();
          Ptu.hidePacingStatus();
          break;
      }
    },

    renderPacingStatus(status) {
      const card = Ptu.$('low-intensity-progress');
      if (!card || !status) return;
      const mode = status.pacing_mode || status.mode || 'standard';
      const modeLabels = {
        low_2_3_hours: '约 2～3 小时',
        low_4_6_hours: '约 4～6 小时',
        low_8_12_hours: '约 8～12 小时',
      };
      if (!modeLabels[mode]) {
        card.hidden = true;
        return;
      }
      const stateLabels = {
        estimating: '正在估算请求量',
        waiting: '正在等待下一次请求',
        requesting: '正在请求',
        power_saving: '电源条件已延长等待',
        paused: '任务已暂停',
      };
      const modeNode = Ptu.$('low-intensity-mode');
      const stateNode = Ptu.$('low-intensity-state');
      const waitNode = Ptu.$('low-intensity-next-wait');
      const disclaimer = Ptu.$('low-intensity-disclaimer');
      if (modeNode) modeNode.textContent = modeLabels[mode];
      if (stateNode) stateNode.textContent = stateLabels[status.state] || '准备中';
      if (waitNode) {
        const seconds = Number(status.next_wait_seconds);
        waitNode.textContent = Number.isFinite(seconds) && seconds >= 0
          ? `下次等待：约 ${Math.ceil(seconds)} 秒`
          : '下次等待：—';
      }
      if (disclaimer) disclaimer.textContent = status.disclaimer || '目标区间不是完成时间承诺';
      card.hidden = false;
    },

    hidePacingStatus() {
      const card = Ptu.$('low-intensity-progress');
      if (card) card.hidden = true;
    },

    // 关闭任务 WS + 清 State.ws（终态共用，防 8s 无限重连）

    _closeTaskWs() {
      if (Ptu.State.ws) {
        try { Ptu.State.ws.close(); } catch (e) { /* ignore */ }
        Ptu.State.ws = null;
      }
    },

    renderResult(result) {
      const card = Ptu.$('result-card');
      const ul = Ptu.$('result-list');
      ul.innerHTML = '';
      const add = (label, path) => {
        if (!path) return;
        const li = document.createElement('li');
        li.innerHTML = `<strong>${label}：</strong> ${Ptu.escape(path)}`;
        ul.appendChild(li);
      };
      const outputDir = result.output_dir || Ptu.State._backupOutputDir;
      add('输出目录', outputDir);
      if (result.task_kind === 'following_archive') {
        add('关注博主', `${result.blogger_count || 0} 个`);
        add('关注超话', `${result.supertopic_count || 0} 个`);
        add('新增关注', `${result.followed_count || 0} 个`);
        add('取消关注', `${result.unfollowed_count || 0} 个`);
        add('名称变化', `${result.renamed_count || 0} 个`);
        add('重新关注', `${result.refollowed_count || 0} 个`);
        if (result.unconfirmed_count) {
          add('状态未确认', `${result.unconfirmed_count} 个（平台未返回，未计入取消关注）`);
        }
        add('关注时长来源', result.duration_source === 'local_minimum'
          ? '本地首次完整记录起的最短时长' : '微博原始值');
        card.hidden = false;
        Ptu.State._lastResult = { ...result, output_dir: outputDir };
        return;
      }
      add('Markdown', result.markdown);
      add('PDF', result.pdf);
      add('HTML', result.html);
      add('报告', result.report);
      add('档案总数', `${result.total_posts || 0} 条`);
      add('本次新增', `${result.new_posts || 0} 条`);
      add('已刷新', `${result.refreshed_posts || 0} 条`);
      add('发生变化', `${result.changed_posts || 0} 条`);
      (result.generated_files || []).forEach((path, index) => add(`生成文件 ${index + 1}`, path));
      card.hidden = false;
      Ptu.State._lastResult = { ...result, output_dir: outputDir };
    },

    async openOutputFolder() {
      const r = Ptu.State._lastResult;
      if (!r || !r.output_dir) { Ptu.toast('还没有结果'); return; }
      if (Ptu.State.isDesktop) {
        try {
          await global.pywebview.api.open_folder(r.output_dir);
        } catch (e) { Ptu.toast('打开失败：' + e.message); }
      } else {
        Ptu.toast('浏览器模式下请手动打开：' + r.output_dir);
      }
    },

    // 通用：跟登录任务

    watchTask(taskId, label) {
      if (Ptu.State.ws) Ptu.State.ws.close();
      Ptu.maybeShowLogButton();
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WsClient(`${proto}://${location.host}/ws/tasks/${taskId}`);
      ws.on((msg) => {
        if (msg.type === 'log') (msg.entries || []).forEach(e => Ptu.appendLog(e));
        if (msg.type === 'done') {
          Ptu.toast(`${label}成功`);
          Ptu.refreshLoginStatus();
          ws.close();
        }
        if (msg.type === 'error') {
          Ptu.toast(`${label}失败：${msg.error}`);
          ws.close();
        }
      });
      ws.connect();
      Ptu.State.ws = ws;
    },

    // ====== 浮动日志 ======
  });
  return ctx;
}

init(Ptu);
