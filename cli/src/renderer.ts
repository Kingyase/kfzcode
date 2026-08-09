/** Markdown + 代码渲染 */
import chalk from 'chalk';

export class Renderer {
  /**
   * 渲染 AI 回复内容，支持:
   * - 代码块语法高亮（基础）
   * - 标题、列表
   * - 文件路径链接
   */
  render(content: string): string {
    const lines = content.split('\n');
    const result: string[] = [];
    let inCodeBlock = false;
    let codeLang = '';
    let codeBuffer: string[] = [];

    for (const line of lines) {
      // 代码块开始/结束
      if (line.startsWith('```')) {
        if (inCodeBlock) {
          // 结束代码块
          result.push(this.renderCodeBlock(codeBuffer.join('\n'), codeLang));
          codeBuffer = [];
          inCodeBlock = false;
          codeLang = '';
        } else {
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
        result.push(chalk.bold.cyan(line));
      } else if (line.startsWith('## ')) {
        result.push(chalk.bold.yellow(line));
      } else if (line.startsWith('# ')) {
        result.push(chalk.bold.green(line));
      }
      // 列表项
      else if (line.match(/^[\s]*[-*+]\s/)) {
        result.push('  ' + chalk.white('•') + ' ' + line.replace(/^[\s]*[-*+]\s/, ''));
      } else if (line.match(/^[\s]*\d+\.\s/)) {
        result.push('  ' + chalk.white(line.trim()));
      }
      // 引用 — 同时处理 "> text" 和 ">" 两种变体
      else if (line.startsWith('>')) {
        const text = line.startsWith('> ') ? line.slice(2) : line.slice(1);
        result.push(chalk.gray('  │ ') + chalk.italic.gray(text || ''));
      }
      // 表格分隔线
      else if (line.match(/^\|[-| ]+\|$/)) {
        // 跳过表格分隔线
        continue;
      }
      // 加粗
      else {
        result.push(line
          .replace(/\*\*(.+?)\*\*/g, (_: string, text: string) => chalk.bold(text))
          .replace(/`([^`]+)`/g, (_: string, code: string) => chalk.bgGray.white(` ${code} `))
        );
      }
    }

    // 未关闭的代码块
    if (codeBuffer.length > 0) {
      result.push(this.renderCodeBlock(codeBuffer.join('\n'), codeLang));
    }

    return result.join('\n');
  }

  private renderCodeBlock(code: string, lang: string): string {
    const border = chalk.gray('┌' + '─'.repeat(40));
    const langLabel = lang ? chalk.gray(` ${lang} `) : '';
    const lines = code.split('\n');
    const maxLineNum = String(lines.length).length;

    const numbered = lines.map((line, i) => {
      const num = String(i + 1).padStart(maxLineNum);
      return chalk.gray(`${num} │ `) + line;
    }).join('\n');

    return `\n${border}${langLabel}\n${numbered}\n${chalk.gray('└' + '─'.repeat(40))}\n`;
  }

  /** 渲染 Diff 对比 */
  renderDiff(oldContent: string, newContent: string): string {
    const oldLines = oldContent.split('\n');
    const newLines = newContent.split('\n');
    const result: string[] = [chalk.gray('┌─ 📝 变更对比 ─' + '─'.repeat(35))];

    // 简单的逐行对比
    const maxLen = Math.max(oldLines.length, newLines.length);
    for (let i = 0; i < Math.min(maxLen, 30); i++) {
      const oldLine = oldLines[i];
      const newLine = newLines[i];
      if (oldLine !== newLine) {
        if (oldLine !== undefined) {
          result.push(chalk.red(`- ${oldLine.slice(0, 75)}`));
        }
        if (newLine !== undefined) {
          result.push(chalk.green(`+ ${newLine.slice(0, 75)}`));
        }
      }
    }

    result.push(chalk.gray('└' + '─'.repeat(40)));
    return result.join('\n');
  }

  /** 渲染工具调用 */
  renderToolCall(name: string, args: Record<string, any>): string {
    const desc = args.file_path || args.command || args.pattern || JSON.stringify(args).slice(0, 60);
    return chalk.gray(`┌─ 🔧 ${name} ──────────────────────────────────\n│ ${desc}\n└` + '─'.repeat(40));
  }

  /** 渲染工具结果 */
  renderToolResult(name: string, success: boolean, output: string): string {
    const icon = success ? '✓' : '✗';
    const color = success ? chalk.green : chalk.red;
    return color(`${icon} ${name}: ${output.slice(0, 200)}`);
  }

  /** 渲染确认对话框 */
  renderConfirmBox(header: string, question: string, options?: Array<{label: string, description: string}>): string {
    const lines: string[] = [];
    lines.push(chalk.yellow(`\n┌─ ❓ ${header} ${'─'.repeat(Math.max(0, 36 - header.length))}`));
    lines.push(chalk.white(`│ ${question}`));
    lines.push(chalk.white('│'));

    if (options) {
      for (let i = 0; i < options.length; i++) {
        const opt = options[i];
        lines.push(chalk.cyan(`│   (${i + 1}) ${opt.label}`));
        if (opt.description) {
          lines.push(chalk.gray(`│       ${opt.description}`));
        }
      }
    }

    lines.push(chalk.white('│'));
    lines.push(chalk.gray('│  [输入编号或直接回复]'));
    lines.push(chalk.yellow('└' + '─'.repeat(40)));
    return lines.join('\n');
  }
}
