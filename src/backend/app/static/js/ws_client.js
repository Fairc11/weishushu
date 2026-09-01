// WebSocket 客户端封装。订阅 → 收消息 → 自动重连（指数退避）。
// 阶段 1 只做基础订阅，阶段 2 已经在用。
(function (global) {
  'use strict';

  class WsClient {
    constructor(url) {
      this.url = url;
      this.ws = null;
      this.handlers = new Set();
      this.backoff = 500;
      this.maxBackoff = 8000;
      this.shouldRun = false;
    }

    connect() {
      this.shouldRun = true;
      this._open();
    }

    _open() {
      try {
        this.ws = new WebSocket(this.url);
      } catch (e) {
        console.warn('[WsClient] new WebSocket 失败', e);
        this._scheduleReconnect();
        return;
      }
      this.ws.onopen = () => {
        console.log('[WsClient] connected', this.url);
        this.backoff = 500;
      };
      this.ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        for (const h of this.handlers) {
          try { h(msg); } catch (e) { console.error('[WsClient] handler err', e); }
        }
      };
      this.ws.onclose = () => {
        if (this.shouldRun) this._scheduleReconnect();
      };
      this.ws.onerror = (e) => {
        console.warn('[WsClient] error', e);
        // onerror 后必触发 onclose，reconnect 在那里处理
      };
    }

    _scheduleReconnect() {
      const delay = this.backoff;
      this.backoff = Math.min(this.backoff * 2, this.maxBackoff);
      setTimeout(() => {
        if (this.shouldRun) this._open();
      }, delay);
    }

    on(handler) { this.handlers.add(handler); return () => this.handlers.delete(handler); }

    send(obj) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify(obj));
      }
    }

    close() {
      this.shouldRun = false;
      if (this.ws) {
        try { this.ws.close(); } catch (e) { /* ignore */ }
        this.ws = null;
      }
    }
  }

  global.WsClient = WsClient;
})(window);
