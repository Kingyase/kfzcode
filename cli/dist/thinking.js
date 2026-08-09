"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.ThinkingDisplay = void 0;
/** 思考过程展示组件 */
const chalk_1 = __importDefault(require("chalk"));
class ThinkingDisplay {
    mode;
    maxLines;
    buffer = '';
    visible = false;
    active = false;
    constructor(mode = 'collapsed', maxLines = 10) {
        this.mode = mode;
        this.maxLines = maxLines;
    }
    get isActive() {
        return this.active;
    }
    start() {
        if (this.mode === 'hidden')
            return;
        this.active = true;
        this.buffer = '';
        this.visible = this.mode === 'expanded';
        this._drawHeader();
    }
    append(chunk) {
        if (this.mode === 'hidden')
            return;
        this.buffer += chunk;
        if (this.visible) {
            process.stdout.write(chalk_1.default.gray(chunk));
        }
    }
    stop() {
        if (this.mode === 'hidden') {
            this.active = false;
            return;
        }
        if (!this.visible && this.buffer) {
            // 折叠模式：展示摘要行
            const lines = this.buffer.split('\n');
            const previewLines = lines.slice(0, this.maxLines);
            for (const line of previewLines) {
                process.stdout.write(chalk_1.default.gray(`│ ${line.slice(0, 78)}\n`));
            }
            if (lines.length > this.maxLines) {
                process.stdout.write(chalk_1.default.gray(`│ ... (${lines.length - this.maxLines} 行更多，按 Enter 展开)\n`));
            }
            process.stdout.write(chalk_1.default.gray('└' + '─'.repeat(38) + '\n'));
        }
        if (this.visible) {
            process.stdout.write('\n');
        }
        this.active = false;
        this.buffer = '';
    }
    toggle() {
        if (!this.buffer)
            return;
        this.visible = !this.visible;
        if (this.visible) {
            process.stdout.write('\n' + chalk_1.default.gray(this.buffer) + '\n');
        }
    }
    _drawHeader() {
        const icon = '💭';
        const label = '思考';
        process.stdout.write('\n' + chalk_1.default.gray(`┌─ ${icon} ${label} ${'─'.repeat(35)}`) + '\n');
    }
    setMode(mode) {
        this.mode = mode;
    }
}
exports.ThinkingDisplay = ThinkingDisplay;
//# sourceMappingURL=thinking.js.map