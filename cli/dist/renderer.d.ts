export declare class Renderer {
    /**
     * 渲染 AI 回复内容，支持:
     * - 代码块语法高亮（基础）
     * - 标题、列表
     * - 文件路径链接
     */
    render(content: string): string;
    private renderCodeBlock;
    /** 渲染 Diff 对比 */
    renderDiff(oldContent: string, newContent: string): string;
    /** 渲染工具调用 */
    renderToolCall(name: string, args: Record<string, any>): string;
    /** 渲染工具结果 */
    renderToolResult(name: string, success: boolean, output: string): string;
    /** 渲染确认对话框 */
    renderConfirmBox(header: string, question: string, options?: Array<{
        label: string;
        description: string;
    }>): string;
}
//# sourceMappingURL=renderer.d.ts.map