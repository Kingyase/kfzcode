export interface ModelConfig {
    provider: string;
    name: string;
    base_url: string;
    /** API Key — 仅从 ~/.kfzcode.json 或环境变量 KFZCODE_API_KEY 获取，不提供代码级默认值 */
    api_key?: string;
    max_tokens: number;
    temperature: number;
    top_p: number;
    /** 单张图片最大大小 (MB)，超过此大小的图片将被拒绝 */
    max_image_size_mb: number;
    /** 该模型是否支持多模态 / 视觉识别 */
    supports_vision: boolean;
}
export interface KFZCodeConfig {
    backend_url: string;
    model: ModelConfig;
    profiles: Record<string, any>;
    defaults: Record<string, any>;
    display: {
        thinking: 'expanded' | 'collapsed' | 'hidden';
        thinking_max_lines: number;
    };
    behavior: {
        /** 普通输入超时时间（秒），超时后自动退出 */
        input_timeout: number;
        /** 交互式选择超时时间（秒） */
        choose_timeout: number;
    };
    orchestrator: {
        max_iterations: number;
        auto_confirm_on_pass: boolean;
        parallel_review: boolean;
        review_timeout: number;
    };
    tools: {
        disabled: string[];
        allow_commands: string[];
        deny_commands: string[];
        auto_confirm_tools: string[];
    };
}
export declare function loadGlobalConfig(): KFZCodeConfig;
export declare function loadProjectConfig(projectPath?: string): KFZCodeConfig;
export declare function loadConfig(projectPath?: string, profile?: string, modelName?: string): KFZCodeConfig;
export declare function saveGlobalConfig(config: Record<string, any>): void;
export declare function getConfigPath(): string;
//# sourceMappingURL=config.d.ts.map