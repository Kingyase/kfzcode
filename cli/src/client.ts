/** SSE 客户端 — 与后端通信 */
import * as http from 'http';
import * as https from 'https';
import { URL } from 'url';
import { EventEmitter } from 'events';

export interface ChatRequest {
  message: string;
  /** 图片文件路径列表，支持视觉识别（模型需支持多模态） */
  images?: string[];
  project_path?: string;
  profile?: string;
  model?: string;
  session_id?: string;
  auto_approve?: boolean;
}

export type SSEEventType =
  | 'start' | 'done' | 'error' | 'timeout'
  | 'thinking' | 'message' | 'tool_call' | 'tool_result'
  | 'progress' | 'tool_progress'
  | 'need_confirm' | 'ask_user' | 'task_complete';

export interface SSEEvent {
  type: SSEEventType;
  data: any;
}

/** SSE 流式请求超时时间 (ms)，默认 10 分钟，防止 TCP 半开连接永久挂起 */
const STREAM_TIMEOUT_MS = 600_000;
/** 短请求超时时间 (ms) */
const SHORT_TIMEOUT_MS = 30_000;

export class KFZCodeClient extends EventEmitter {
  private baseUrl: string;
  private apiKey: string | undefined;

  constructor(baseUrl: string, apiKey?: string) {
    super();
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.apiKey = apiKey;
  }

  async chat(req: ChatRequest): Promise<void> {
    const url = new URL(`${this.baseUrl}/api/chat`);
    const body = JSON.stringify(req);

    const options: http.RequestOptions = {
      hostname: url.hostname,
      port: url.port || 8765,
      path: url.pathname,
      method: 'POST',
      timeout: STREAM_TIMEOUT_MS,
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        'Content-Length': Buffer.byteLength(body),
        ...(this.apiKey ? { 'Authorization': `Bearer ${this.apiKey}` } : {}),
      },
    };

    const transport = url.protocol === 'https:' ? https : http;

    return new Promise((resolve, reject) => {
      let settled = false;
      const once = (fn: () => void) => {
        if (!settled) { settled = true; fn(); }
      };

      const req = transport.request(options, (res) => {
        if (res.statusCode !== 200) {
          let errData = '';
          res.on('data', (chunk) => (errData += chunk));
          res.on('end', () => once(() => reject(new Error(`HTTP ${res.statusCode}: ${errData}`))));
          return;
        }

        let buffer = '';

        res.on('data', (chunk: Buffer) => {
          buffer += chunk.toString();
          // SSE 标准: 事件由双换行分隔（兼容 \r\n 和 \n）
          // 先规范化 CRLF → LF，再按 \n\n 分割
          const normalized = buffer.replace(/\r\n/g, '\n');
          const events = normalized.split('\n\n');
          buffer = events.pop() || '';

          for (const block of events) {
            if (!block.trim()) continue;

            let eventType = 'message';
            let dataStr = '';

            for (const line of block.split('\n')) {
              const trimmed = line.trimEnd();
              if (trimmed.startsWith('event: ')) {
                eventType = trimmed.slice(7).trim();
              } else if (trimmed.startsWith('data: ')) {
                dataStr = trimmed.slice(6);
              }
            }

            if (dataStr) {
              try {
                const data = JSON.parse(dataStr);
                this.emit('sse', { type: eventType as SSEEventType, data });
              } catch {
                // 忽略 JSON 解析错误
              }
            }
          }
        });

        res.on('end', () => {
          once(() => {
            this.emit('end');
            resolve();
          });
        });

        res.on('error', (err) => once(() => reject(err)));

        // 关键修复: 当后端服务器关闭/崩溃时，TCP 连接断开可能只触发 close
        // 不触发 error 或 end（尤其在 Windows 上），导致 Promise 永久挂起
        res.on('close', () => {
          once(() => {
            this.emit('end');
            reject(new Error('SSE 连接已断开（后端可能已关闭或重启）'));
          });
        });
      });

      req.on('error', (err) => once(() => reject(err)));
      req.on('timeout', () => {
        once(() => {
          req.destroy();
          reject(new Error(`SSE 请求超时 (${Math.round(STREAM_TIMEOUT_MS / 1000)}秒)`));
        });
      });
      req.write(body);
      req.end();
    });
  }

  async chatMultiAgent(req: ChatRequest): Promise<void> {
    const url = new URL(`${this.baseUrl}/api/chat/multi-agent`);
    const body = JSON.stringify(req);

    const options: http.RequestOptions = {
      hostname: url.hostname,
      port: url.port || 8765,
      path: url.pathname,
      method: 'POST',
      timeout: STREAM_TIMEOUT_MS,
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        'Content-Length': Buffer.byteLength(body),
        ...(this.apiKey ? { 'Authorization': `Bearer ${this.apiKey}` } : {}),
      },
    };

    const transport = url.protocol === 'https:' ? https : http;

    return new Promise((resolve, reject) => {
      const req = transport.request(options, (res) => {
        let buffer = '';

        res.on('data', (chunk: Buffer) => {
          buffer += chunk.toString();
          // CRLF → LF 规范化后按双换行分割
          const normalized = buffer.replace(/\r\n/g, '\n');
          const events = normalized.split('\n\n');
          buffer = events.pop() || '';

          for (const block of events) {
            if (!block.trim()) continue;

            let eventType = 'progress';
            let dataStr = '';
            for (const line of block.split('\n')) {
              const trimmed = line.trimEnd();
              if (trimmed.startsWith('event: ')) {
                eventType = trimmed.slice(7).trim();
              } else if (trimmed.startsWith('data: ')) {
                dataStr = trimmed.slice(6);
              }
            }

            if (dataStr) {
              try {
                const data = JSON.parse(dataStr);
                this.emit('sse', { type: eventType as SSEEventType, data });
              } catch {
                // ignore
              }
            }
          }
        });

        res.on('end', () => resolve());
        res.on('error', reject);
      });

      req.on('error', reject);
      req.on('timeout', () => {
        req.destroy();
        reject(new Error(`SSE 请求超时 (${Math.round(STREAM_TIMEOUT_MS / 1000)}秒)`));
      });
      req.write(body);
      req.end();
    });
  }

  async healthCheck(): Promise<any> {
    const url = `${this.baseUrl}/api/health`;
    const transport = url.startsWith('https') ? https : http;

    return new Promise((resolve, reject) => {
      transport.get(url, (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          try {
            resolve(JSON.parse(data));
          } catch {
            reject(new Error(`Invalid response: ${data}`));
          }
        });
      }).on('error', reject);
    });
  }

  /** 向 Agent 发送用户回复，唤醒暂停的 Agent 循环 (Human-in-the-Loop) */
  async respond(sessionId: string, response: Record<string, any>): Promise<void> {
    const url = `${this.baseUrl}/api/chat/respond`;
    const body = JSON.stringify({ session_id: sessionId, response });
    const transport = url.startsWith('https') ? https : http;
    const urlObj = new URL(url);

    return new Promise((resolve, reject) => {
      const req = transport.request({
        hostname: urlObj.hostname,
        port: urlObj.port || 8765,
        path: urlObj.pathname,
        method: 'POST',
        timeout: SHORT_TIMEOUT_MS,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
          ...(this.apiKey ? { 'Authorization': `Bearer ${this.apiKey}` } : {}),
        },
      }, (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          if (res.statusCode === 200) {
            resolve();
          } else {
            reject(new Error(`respond failed: HTTP ${res.statusCode}: ${data}`));
          }
        });
      });
      req.on('error', reject);
      req.on('timeout', () => {
        req.destroy();
        reject(new Error(`respond 请求超时 (${Math.round(SHORT_TIMEOUT_MS / 1000)}秒)`));
      });
      req.write(body);
      req.end();
    });
  }

  /** 取消当前会话正在运行的任务 */
  async cancel(sessionId: string): Promise<void> {
    const url = `${this.baseUrl}/api/chat/cancel`;
    const body = JSON.stringify({ session_id: sessionId });
    const transport = url.startsWith('https') ? https : http;
    const urlObj = new URL(url);

    return new Promise((resolve, reject) => {
      const req = transport.request({
        hostname: urlObj.hostname,
        port: urlObj.port || 8765,
        path: urlObj.pathname,
        method: 'POST',
        timeout: SHORT_TIMEOUT_MS,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
          ...(this.apiKey ? { 'Authorization': `Bearer ${this.apiKey}` } : {}),
        },
      }, (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          if (res.statusCode === 200) {
            resolve();
          } else {
            reject(new Error(`cancel failed: HTTP ${res.statusCode}: ${data}`));
          }
        });
      });
      req.on('error', reject);
      req.on('timeout', () => {
        req.destroy();
        reject(new Error(`cancel 请求超时`));
      });
      req.write(body);
      req.end();
    });
  }

  /** 列出持久化的历史会话，可按 workspace 过滤 */
  async listSessions(workspace?: string): Promise<any> {
    const url = new URL(`${this.baseUrl}/api/sessions/history`);
    if (workspace) {
      url.searchParams.set('workspace', workspace);
    }

    const transport = url.protocol === 'https:' ? https : http;

    return new Promise((resolve, reject) => {
      transport.get(url, (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          try {
            resolve(JSON.parse(data));
          } catch {
            reject(new Error(`Invalid response: ${data}`));
          }
        });
      }).on('error', reject);
    });
  }

  /** 获取指定会话的完整消息历史 */
  async getSessionMessages(sessionId: string): Promise<any> {
    const url = `${this.baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/messages`;
    const transport = url.startsWith('https') ? https : http;

    return new Promise((resolve, reject) => {
      transport.get(url, (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          if (res.statusCode === 200) {
            try {
              resolve(JSON.parse(data));
            } catch {
              reject(new Error(`Invalid response: ${data}`));
            }
          } else {
            try {
              const err = JSON.parse(data);
              reject(new Error(err.detail || `HTTP ${res.statusCode}`));
            } catch {
              reject(new Error(`HTTP ${res.statusCode}`));
            }
          }
        });
      }).on('error', reject);
    });
  }

  async doctor(): Promise<any> {
    const url = `${this.baseUrl}/api/doctor`;
    const transport = url.startsWith('https') ? https : http;

    return new Promise((resolve, reject) => {
      transport.get(url, (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          try {
            resolve(JSON.parse(data));
          } catch {
            reject(new Error(`Invalid response: ${data}`));
          }
        });
      }).on('error', reject);
    });
  }
}

export function createClient(baseUrl: string, apiKey?: string): KFZCodeClient {
  return new KFZCodeClient(baseUrl, apiKey);
}
