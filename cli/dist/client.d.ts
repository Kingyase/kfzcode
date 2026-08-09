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
export type SSEEventType = 'start' | 'done' | 'error' | 'timeout' | 'thinking' | 'message' | 'tool_call' | 'tool_result' | 'progress' | 'tool_progress' | 'need_confirm' | 'ask_user' | 'task_complete';
export interface SSEEvent {
    type: SSEEventType;
    data: any;
}
export declare class KFZCodeClient extends EventEmitter {
    private baseUrl;
    private apiKey;
    constructor(baseUrl: string, apiKey?: string);
    chat(req: ChatRequest): Promise<void>;
    chatMultiAgent(req: ChatRequest): Promise<void>;
    healthCheck(): Promise<any>;
    /** 向 Agent 发送用户回复，唤醒暂停的 Agent 循环 (Human-in-the-Loop) */
    respond(sessionId: string, response: Record<string, any>): Promise<void>;
    /** 取消当前会话正在运行的任务 */
    cancel(sessionId: string): Promise<void>;
    /** 列出持久化的历史会话，可按 workspace 过滤 */
    listSessions(workspace?: string): Promise<any>;
    /** 获取指定会话的完整消息历史 */
    getSessionMessages(sessionId: string): Promise<any>;
    doctor(): Promise<any>;
}
export declare function createClient(baseUrl: string, apiKey?: string): KFZCodeClient;
//# sourceMappingURL=client.d.ts.map