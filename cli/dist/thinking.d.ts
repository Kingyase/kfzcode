export type ThinkingMode = 'expanded' | 'collapsed' | 'hidden';
export declare class ThinkingDisplay {
    private mode;
    private maxLines;
    private buffer;
    private visible;
    private active;
    constructor(mode?: ThinkingMode, maxLines?: number);
    get isActive(): boolean;
    start(): void;
    append(chunk: string): void;
    stop(): void;
    toggle(): void;
    private _drawHeader;
    setMode(mode: ThinkingMode): void;
}
//# sourceMappingURL=thinking.d.ts.map