export declare class Terminal {
    private rl;
    private prompt;
    /** 互斥锁 Promise 链 — 防止 rl.question() 被并发重入导致死锁 */
    private _lock;
    /** readline 是否已关闭（防止关闭后继续调用 question） */
    private _closed;
    /** 普通输入超时 (ms) */
    private inputTimeoutMs;
    /** 交互式选择超时 (ms) */
    private chooseTimeoutMs;
    constructor(prompt?: string, timeouts?: {
        input?: number;
        choose?: number;
    });
    /** 获取交互锁 — 调用方 await 后获得排他权，用完调用 release() */
    private _acquire;
    /**
     * 安全包装 rl.question()，防止以下场景导致永久死锁：
     * 1. Windows 终端选中文本进入"选择模式"导致 stdin 暂停
     * 2. readline 内部 _questionCallback 被意外覆盖 (Node.js 已知问题)
     * 3. 终端/stdio 关闭后 question 回调永不触发
     *
     * 通过超时 + close 事件双重防护，确保 Promise 一定会 settle。
     */
    private _safeQuestion;
    input(promptOverride?: string): Promise<string>;
    confirm(message: string): Promise<boolean>;
    choose(options: string[], message?: string): Promise<number>;
    /** 多选 — 返回选中项的索引数组 */
    multiChoose(options: string[], message?: string): Promise<number[]>;
    write(text: string): void;
    writeln(text: string): void;
    /** 输出 AI 消息 */
    assistantMessage(text: string): void;
    /** 输出系统消息 */
    systemMessage(text: string): void;
    /** 输出错误消息 */
    errorMessage(text: string): void;
    /** 输出成功消息 */
    successMessage(text: string): void;
    /** 输出进度 spinner（简单版） */
    spinner(text: string): NodeJS.Timeout;
    stopSpinner(interval: NodeJS.Timeout, finalText?: string): void;
    /** 清屏 */
    clear(): void;
    /** 显示欢迎 */
    showWelcome(version: string): void;
    showHelp(): void;
    close(): void;
    /** 捕获 Ctrl+C */
    onInterrupt(handler: () => void): void;
}
//# sourceMappingURL=terminal.d.ts.map