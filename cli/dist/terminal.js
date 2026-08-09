"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.Terminal = void 0;
/** 交互式终端 UI */
const readline = __importStar(require("readline"));
const chalk_1 = __importDefault(require("chalk"));
/** 默认输入超时 (秒) — 2 小时 */
const DEFAULT_INPUT_TIMEOUT_S = 7200;
/** 默认选择超时 (秒) — 2 小时 */
const DEFAULT_CHOOSE_TIMEOUT_S = 7200;
class Terminal {
    rl;
    prompt;
    /** 互斥锁 Promise 链 — 防止 rl.question() 被并发重入导致死锁 */
    _lock = Promise.resolve();
    /** readline 是否已关闭（防止关闭后继续调用 question） */
    _closed = false;
    /** 普通输入超时 (ms) */
    inputTimeoutMs;
    /** 交互式选择超时 (ms) */
    chooseTimeoutMs;
    constructor(prompt = chalk_1.default.green('❯ '), timeouts) {
        this.prompt = prompt;
        this.rl = readline.createInterface({
            input: process.stdin,
            output: process.stdout,
            terminal: true,
            prompt: this.prompt,
        });
        // timeouts 参数单位为秒，内部转为毫秒
        this.inputTimeoutMs = (timeouts?.input ?? DEFAULT_INPUT_TIMEOUT_S) * 1000;
        this.chooseTimeoutMs = (timeouts?.choose ?? DEFAULT_CHOOSE_TIMEOUT_S) * 1000;
    }
    /** 获取交互锁 — 调用方 await 后获得排他权，用完调用 release() */
    async _acquire() {
        let release;
        // 新的 Promise 链节点：后续调用者将等待当前节点
        const prev = this._lock;
        this._lock = new Promise((resolve) => { release = resolve; });
        await prev;
        return release;
    }
    /**
     * 安全包装 rl.question()，防止以下场景导致永久死锁：
     * 1. Windows 终端选中文本进入"选择模式"导致 stdin 暂停
     * 2. readline 内部 _questionCallback 被意外覆盖 (Node.js 已知问题)
     * 3. 终端/stdio 关闭后 question 回调永不触发
     *
     * 通过超时 + close 事件双重防护，确保 Promise 一定会 settle。
     */
    _safeQuestion(prompt, timeoutMs = this.inputTimeoutMs) {
        if (this._closed) {
            return Promise.reject(new Error('终端已关闭'));
        }
        let settled = false;
        let timer = null;
        let onClose = null;
        const cleanup = () => {
            settled = true;
            if (timer) {
                clearTimeout(timer);
                timer = null;
            }
            if (onClose) {
                this.rl.removeListener('close', onClose);
                onClose = null;
            }
        };
        return new Promise((resolve, reject) => {
            // 防护 1: 超时自动解除 (默认 120 秒)
            timer = setTimeout(() => {
                if (!settled) {
                    cleanup();
                    reject(new Error(`输入超时 (${Math.round(timeoutMs / 1000)}秒)`));
                }
            }, timeoutMs);
            // 防护 2: readline 关闭时立即 reject
            onClose = () => {
                if (!settled) {
                    cleanup();
                    reject(new Error('终端输入流已关闭'));
                }
            };
            this.rl.once('close', onClose);
            try {
                this.rl.question(prompt, (answer) => {
                    if (!settled) {
                        cleanup();
                        resolve(answer.trim());
                    }
                });
            }
            catch (err) {
                if (!settled) {
                    cleanup();
                    reject(new Error(`rl.question() 调用失败: ${err.message}`));
                }
            }
        });
    }
    async input(promptOverride) {
        const release = await this._acquire();
        try {
            const p = promptOverride || this.prompt;
            return await this._safeQuestion(p);
        }
        finally {
            release();
        }
    }
    async confirm(message) {
        const release = await this._acquire();
        try {
            const answer = await this._safeQuestion(chalk_1.default.yellow(`\n${message} [y/N] `));
            return answer.toLowerCase() === 'y' || answer.toLowerCase() === 'yes';
        }
        finally {
            release();
        }
    }
    async choose(options, message = '请选择:') {
        const release = await this._acquire();
        try {
            process.stdout.write(chalk_1.default.cyan(`\n${message}\n`));
            for (let i = 0; i < options.length; i++) {
                process.stdout.write(chalk_1.default.gray(`  (${i + 1}) `) + options[i] + '\n');
            }
            while (true) {
                const answer = await this._safeQuestion(chalk_1.default.gray(`[输入 1-${options.length}] `), this.chooseTimeoutMs);
                const num = parseInt(answer);
                if (num >= 1 && num <= options.length) {
                    return num - 1;
                }
                process.stdout.write(chalk_1.default.red('无效选择，请重试\n'));
            }
        }
        finally {
            release();
        }
    }
    /** 多选 — 返回选中项的索引数组 */
    async multiChoose(options, message = '请选择 (多选，逗号分隔):') {
        const release = await this._acquire();
        try {
            process.stdout.write(chalk_1.default.cyan(`\n${message}\n`));
            for (let i = 0; i < options.length; i++) {
                process.stdout.write(chalk_1.default.gray(`  (${i + 1}) `) + options[i] + '\n');
            }
            while (true) {
                const answer = await this._safeQuestion(chalk_1.default.gray(`[输入编号，逗号分隔，如 1,3] `), this.chooseTimeoutMs);
                const parts = answer.split(/[,，\s]+/).filter(s => s.length > 0);
                const indices = parts.map(p => parseInt(p) - 1).filter(i => i >= 0 && i < options.length);
                if (indices.length > 0 && new Set(indices).size === indices.length) {
                    return indices;
                }
                process.stdout.write(chalk_1.default.red('无效选择，请重试\n'));
            }
        }
        finally {
            release();
        }
    }
    write(text) {
        process.stdout.write(text);
    }
    writeln(text) {
        process.stdout.write(text + '\n');
    }
    /** 输出 AI 消息 */
    assistantMessage(text) {
        this.writeln(chalk_1.default.blue('\n🤖 Agent:'));
        this.writeln(text);
    }
    /** 输出系统消息 */
    systemMessage(text) {
        this.writeln(chalk_1.default.gray(`[系统] ${text}`));
    }
    /** 输出错误消息 */
    errorMessage(text) {
        this.writeln(chalk_1.default.red(`[错误] ${text}`));
    }
    /** 输出成功消息 */
    successMessage(text) {
        this.writeln(chalk_1.default.green(`[✓] ${text}`));
    }
    /** 输出进度 spinner（简单版） */
    spinner(text) {
        const frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
        let i = 0;
        process.stdout.write(chalk_1.default.gray(frames[0] + ' ' + text));
        const interval = setInterval(() => {
            i = (i + 1) % frames.length;
            readline.cursorTo(process.stdout, 0);
            process.stdout.write(chalk_1.default.gray(frames[i] + ' ' + text));
        }, 80);
        return interval;
    }
    stopSpinner(interval, finalText) {
        clearInterval(interval);
        readline.cursorTo(process.stdout, 0);
        readline.clearLine(process.stdout, 0);
        if (finalText) {
            process.stdout.write(finalText + '\n');
        }
    }
    /** 清屏 */
    clear() {
        process.stdout.write('\x1b[2J\x1b[0;0H');
    }
    /** 显示欢迎 */
    showWelcome(version) {
        this.clear();
        this.writeln(chalk_1.default.bold.cyan('\n  ╔══════════════════════════════════════════╗'));
        this.writeln(chalk_1.default.bold.cyan('  ║') + chalk_1.default.bold.white('     KFZCode - 内网 AI 编程助手       ') + chalk_1.default.bold.cyan('║'));
        this.writeln(chalk_1.default.bold.cyan('  ║') + chalk_1.default.gray(`             v${version}                        `) + chalk_1.default.bold.cyan('║'));
        this.writeln(chalk_1.default.bold.cyan('  ╚══════════════════════════════════════════╝'));
        this.writeln(chalk_1.default.gray('  输入消息开始对话，输入 /help 查看帮助，Ctrl+C 退出\n'));
    }
    showHelp() {
        this.writeln(chalk_1.default.cyan('\n  KFZCode 帮助'));
        this.writeln(chalk_1.default.gray('  ─────────────────────────────────────────'));
        this.writeln('  /help          - 显示此帮助');
        this.writeln('  /clear         - 清屏');
        this.writeln('  /image <path>  - 附加图片文件（视觉识别）');
        this.writeln('  /session [id]  - 列出/切换会话');
        this.writeln('  /config        - 显示当前配置');
        this.writeln('  /profile <name>- 切换模型预设');
        this.writeln('  /doctor        - 系统诊断');
        this.writeln('  /exit 或 Ctrl+C- 退出');
        this.writeln(chalk_1.default.gray('  ─────────────────────────────────────────\n'));
    }
    close() {
        this._closed = true;
        this.rl.close();
    }
    /** 捕获 Ctrl+C */
    onInterrupt(handler) {
        this.rl.on('SIGINT', handler);
    }
}
exports.Terminal = Terminal;
//# sourceMappingURL=terminal.js.map