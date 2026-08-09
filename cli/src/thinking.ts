/** 思考过程展示组件 */
import chalk from 'chalk';

export type ThinkingMode = 'expanded' | 'collapsed' | 'hidden';

export class ThinkingDisplay {
  private mode: ThinkingMode;
  private maxLines: number;
  private buffer: string = '';
  private visible: boolean = false;
  private active: boolean = false;

  constructor(mode: ThinkingMode = 'collapsed', maxLines: number = 10) {
    this.mode = mode;
    this.maxLines = maxLines;
  }

  get isActive(): boolean {
    return this.active;
  }

  start(): void {
    if (this.mode === 'hidden') return;
    this.active = true;
    this.buffer = '';
    this.visible = this.mode === 'expanded';
    this._drawHeader();
  }

  append(chunk: string): void {
    if (this.mode === 'hidden') return;
    this.buffer += chunk;
    if (this.visible) {
      process.stdout.write(chalk.gray(chunk));
    }
  }

  stop(): void {
    if (this.mode === 'hidden') {
      this.active = false;
      return;
    }

    if (!this.visible && this.buffer) {
      // 折叠模式：展示摘要行
      const lines = this.buffer.split('\n');
      const previewLines = lines.slice(0, this.maxLines);
      for (const line of previewLines) {
        process.stdout.write(chalk.gray(`│ ${line.slice(0, 78)}\n`));
      }
      if (lines.length > this.maxLines) {
        process.stdout.write(chalk.gray(`│ ... (${lines.length - this.maxLines} 行更多，按 Enter 展开)\n`));
      }
      process.stdout.write(chalk.gray('└' + '─'.repeat(38) + '\n'));
    }

    if (this.visible) {
      process.stdout.write('\n');
    }

    this.active = false;
    this.buffer = '';
  }

  toggle(): void {
    if (!this.buffer) return;
    this.visible = !this.visible;
    if (this.visible) {
      process.stdout.write('\n' + chalk.gray(this.buffer) + '\n');
    }
  }

  private _drawHeader(): void {
    const icon = '💭';
    const label = '思考';
    process.stdout.write('\n' + chalk.gray(`┌─ ${icon} ${label} ${'─'.repeat(35)}`) + '\n');
  }

  setMode(mode: ThinkingMode): void {
    this.mode = mode;
  }
}
