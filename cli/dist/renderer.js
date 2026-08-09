"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.Renderer = void 0;
/** Markdown + 代码渲染 */
const chalk_1 = __importDefault(require("chalk"));
class Renderer {
    /**
     * 渲染 AI 回复内容，支持:
     * - 代码块语法高亮（基础）
     * - 标题、列表
     * - 文件路径链接
     */
    render(content) {
        const lines = content.split('\n');
        const result = [];
        let inCodeBlock = false;
        let codeLang = '';
        let codeBuffer = [];
        for (const line of lines) {
            // 代码块开始/结束
            if (line.startsWith('```')) {
                if (inCodeBlock) {
                    // 结束代码块
                    result.push(this.renderCodeBlock(codeBuffer.join('\n'), codeLang));
                    codeBuffer = [];
                    inCodeBlock = false;
                    codeLang = '';
                }
                else {
                    inCodeBlock = true;
                    codeLang = line.slice(3).trim();
                }
                continue;
            }
            if (inCodeBlock) {
                codeBuffer.push(line);
                continue;
            }
            // 标题
            if (line.startsWith('### ')) {
                result.push(chalk_1.default.bold.cyan(line));
            }
            else if (line.startsWith('## ')) {
                result.push(chalk_1.default.bold.yellow(line));
            }
            else if (line.startsWith('# ')) {
                result.push(chalk_1.default.bold.green(line));
            }
            // 列表项
            else if (line.match(/^[\s]*[-*+]\s/)) {
                result.push('  ' + chalk_1.default.white('•') + ' ' + line.replace(/^[\s]*[-*+]\s/, ''));
            }
            else if (line.match(/^[\s]*\d+\.\s/)) {
                result.push('  ' + chalk_1.default.white(line.trim()));
            }
            // 引用 — 同时处理 "> text" 和 ">" 两种变体
            else if (line.startsWith('>')) {
                const text = line.startsWith('> ') ? line.slice(2) : line.slice(1);
                result.push(chalk_1.default.gray('  │ ') + chalk_1.default.italic.gray(text || ''));
            }
            // 表格分隔线
            else if (line.match(/^\|[-| ]+\|$/)) {
                // 跳过表格分隔线
                continue;
            }
            // 加粗
            else {
                result.push(line
                    .replace(/\*\*(.+?)\*\*/g, (_, text) => chalk_1.default.bold(text))
                    .replace(/`([^`]+)`/g, (_, code) => chalk_1.default.bgGray.white(` ${code} `)));
            }
        }
        // 未关闭的代码块
        if (codeBuffer.length > 0) {
            result.push(this.renderCodeBlock(codeBuffer.join('\n'), codeLang));
        }
        return result.join('\n');
    }
    renderCodeBlock(code, lang) {
        const border = chalk_1.default.gray('┌' + '─'.repeat(40));
        const langLabel = lang ? chalk_1.default.gray(` ${lang} `) : '';
        const lines = code.split('\n');
        const maxLineNum = String(lines.length).length;
        const numbered = lines.map((line, i) => {
            const num = String(i + 1).padStart(maxLineNum);
            return chalk_1.default.gray(`${num} │ `) + line;
        }).join('\n');
        return `\n${border}${langLabel}\n${numbered}\n${chalk_1.default.gray('└' + '─'.repeat(40))}\n`;
    }
    /** 渲染 Diff 对比 */
    renderDiff(oldContent, newContent) {
        const oldLines = oldContent.split('\n');
        const newLines = newContent.split('\n');
        const result = [chalk_1.default.gray('┌─ 📝 变更对比 ─' + '─'.repeat(35))];
        // 简单的逐行对比
        const maxLen = Math.max(oldLines.length, newLines.length);
        for (let i = 0; i < Math.min(maxLen, 30); i++) {
            const oldLine = oldLines[i];
            const newLine = newLines[i];
            if (oldLine !== newLine) {
                if (oldLine !== undefined) {
                    result.push(chalk_1.default.red(`- ${oldLine.slice(0, 75)}`));
                }
                if (newLine !== undefined) {
                    result.push(chalk_1.default.green(`+ ${newLine.slice(0, 75)}`));
                }
            }
        }
        result.push(chalk_1.default.gray('└' + '─'.repeat(40)));
        return result.join('\n');
    }
    /** 渲染工具调用 */
    renderToolCall(name, args) {
        const desc = args.file_path || args.command || args.pattern || JSON.stringify(args).slice(0, 60);
        return chalk_1.default.gray(`┌─ 🔧 ${name} ──────────────────────────────────\n│ ${desc}\n└` + '─'.repeat(40));
    }
    /** 渲染工具结果 */
    renderToolResult(name, success, output) {
        const icon = success ? '✓' : '✗';
        const color = success ? chalk_1.default.green : chalk_1.default.red;
        return color(`${icon} ${name}: ${output.slice(0, 200)}`);
    }
    /** 渲染确认对话框 */
    renderConfirmBox(header, question, options) {
        const lines = [];
        lines.push(chalk_1.default.yellow(`\n┌─ ❓ ${header} ${'─'.repeat(Math.max(0, 36 - header.length))}`));
        lines.push(chalk_1.default.white(`│ ${question}`));
        lines.push(chalk_1.default.white('│'));
        if (options) {
            for (let i = 0; i < options.length; i++) {
                const opt = options[i];
                lines.push(chalk_1.default.cyan(`│   (${i + 1}) ${opt.label}`));
                if (opt.description) {
                    lines.push(chalk_1.default.gray(`│       ${opt.description}`));
                }
            }
        }
        lines.push(chalk_1.default.white('│'));
        lines.push(chalk_1.default.gray('│  [输入编号或直接回复]'));
        lines.push(chalk_1.default.yellow('└' + '─'.repeat(40)));
        return lines.join('\n');
    }
}
exports.Renderer = Renderer;
//# sourceMappingURL=renderer.js.map