#!/usr/bin/env node
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
/** KFZCode CLI — 主入口 */
const commander_1 = require("commander");
const chalk_1 = __importDefault(require("chalk"));
const crypto_1 = require("crypto");
const terminal_1 = require("./terminal");
const thinking_1 = require("./thinking");
const renderer_1 = require("./renderer");
const client_1 = require("./client");
const config_1 = require("./config");
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const readline = __importStar(require("readline"));
const VERSION = '0.1.0';
// ===== 全局异常防护 =====
// 未捕获的 Promise rejection / 异常不应静默杀死进程，
// 记录日志后让用户有机会看到错误信息而非直接闪退。
process.on('unhandledRejection', (reason) => {
    console.error(chalk_1.default.red('\n[致命] 未捕获的 Promise 拒绝:'), reason?.message || reason);
});
process.on('uncaughtException', (err) => {
    console.error(chalk_1.default.red('\n[致命] 未捕获的异常:'), err.message);
    console.error(chalk_1.default.gray(err.stack?.split('\n').slice(1, 4).join('\n') || ''));
});
async function interactiveMode(options) {
    const config = (0, config_1.loadConfig)(options.project, options.profile, options.model);
    const terminal = new terminal_1.Terminal(undefined, {
        input: config.behavior.input_timeout,
        choose: config.behavior.choose_timeout,
    });
    const renderer = new renderer_1.Renderer();
    const thinking = new thinking_1.ThinkingDisplay(config.display.thinking, config.display.thinking_max_lines);
    const client = new client_1.KFZCodeClient(config.backend_url, config.model.api_key);
    // Session 状态 — 用于 /session 命令切换
    const workspace = path.resolve(options.project || process.cwd());
    const sessionState = {
        localId: (0, crypto_1.randomUUID)(),
        serverId: null,
        workspace,
        pendingImages: options.images || [],
    };
    // 启动时如有图片，提示用户
    if (sessionState.pendingImages.length > 0) {
        terminal.systemMessage(`已附加 ${sessionState.pendingImages.length} 张图片，将在下一条消息中发送`);
    }
    terminal.showWelcome(VERSION);
    // 先做健康检查
    try {
        const health = await client.healthCheck();
        terminal.systemMessage(`后端连接正常 (llm: ${health.llm?.ok ? '✓' : '✗'}, tools: ${health.tools?.length || 0}个)`);
    }
    catch {
        terminal.errorMessage(`无法连接到后端 ${config.backend_url}，请检查后端是否启动。`);
        terminal.close();
        return;
    }
    // Ctrl+C 处理
    let running = true;
    let isRunning = false; // 当前是否有任务正在执行
    let lastInterruptTime = 0; // 上次 Ctrl+C 时间戳（用于双击强制退出）
    const CANCEL_DOUBLE_PRESS_MS = 2000; // 2秒内按两次 Ctrl+C 强制退出
    terminal.onInterrupt(() => {
        if (isRunning) {
            // 正在执行任务 — 发送取消请求
            const now = Date.now();
            if (now - lastInterruptTime < CANCEL_DOUBLE_PRESS_MS) {
                // 双击 Ctrl+C — 强制退出
                terminal.writeln(chalk_1.default.red('\n⏹ 强制退出...'));
                running = false;
                terminal.close();
                process.exit(1);
            }
            lastInterruptTime = now;
            const sid = sessionState.serverId || sessionState.localId;
            client.cancel(sid).catch(() => { });
            terminal.writeln(chalk_1.default.yellow('\n⏹ 正在取消当前任务... (再次按 Ctrl+C 强制退出)'));
        }
        else {
            // 空闲状态 — 正常退出
            terminal.writeln(chalk_1.default.yellow('\n\n👋 再见!'));
            running = false;
            terminal.close();
            process.exit(0);
        }
    });
    while (running) {
        let input = '';
        try {
            input = await terminal.input();
        }
        catch (err) {
            // 终端输入异常（超时、stdin 关闭、后端断开等），优雅退出而非崩溃
            if (err.message?.includes('超时')) {
                terminal.errorMessage(`输入超时，已自动退出`);
            }
            else if (err.message?.includes('关闭')) {
                terminal.writeln(chalk_1.default.yellow('\n👋 终端已关闭，再见!'));
            }
            else {
                terminal.errorMessage(`终端异常: ${err.message}`);
            }
            running = false;
            break;
        }
        if (!input)
            continue;
        // 处理斜杠命令
        if (input.startsWith('/')) {
            running = await handleCommand(input, terminal, config, options, sessionState);
            continue;
        }
        // 正常对话
        terminal.systemMessage('处理中...');
        // SSE 事件处理 — 必须串行化，防止 EventEmitter 同步 emit 导致 async handler 重入
        // 内核：EventEmitter.emit() 不等待 async listener，同一 TCP 数据块内的多个 SSE 事件会并发触发。
        // 当 need_confirm/ask_user handler 正在 await terminal.choose() 时，后续事件再进入 handler
        // 就会并发调用 rl.question()，触发 readline 内部 _questionCallback 覆盖 → 死锁。
        let assistantBuffer = '';
        let thinkingActive = false;
        let sseQueue = Promise.resolve();
        const handleSSEEvent = async (event) => {
            switch (event.type) {
                case 'start':
                    // 保存服务器返回的session_id
                    if (event.data.session_id) {
                        sessionState.serverId = event.data.session_id;
                    }
                    if (event.data.has_images) {
                        terminal.systemMessage(`已发送 ${event.data.image_count} 张图片给模型识别...`);
                    }
                    break;
                case 'thinking':
                    if (!thinkingActive) {
                        thinking.start();
                        thinkingActive = true;
                    }
                    thinking.append(event.data.content || event.data);
                    break;
                case 'message':
                    if (thinkingActive) {
                        thinking.stop();
                        thinkingActive = false;
                    }
                    assistantBuffer += event.data.content || '';
                    break;
                case 'tool_call':
                    if (thinkingActive) {
                        thinking.stop();
                        thinkingActive = false;
                    }
                    {
                        const toolRender = renderer.renderToolCall(event.data.name || 'unknown', event.data.arguments || {});
                        terminal.writeln('\n' + toolRender);
                    }
                    break;
                case 'tool_result':
                    {
                        // 清除可能还在显示的 tool_progress 进度行
                        readline.cursorTo(process.stdout, 0);
                        readline.clearLine(process.stdout, 0);
                        const resultIcon = event.data.success ? '✓' : '✗';
                        const resultColor = event.data.success ? chalk_1.default.green : chalk_1.default.red;
                        terminal.writeln(resultColor(`  ${resultIcon} ${event.data.name || ''}: ${(event.data.output || '').slice(0, 150)}`));
                    }
                    break;
                case 'tool_progress':
                    // 工具执行中 — 同一行原地刷新显示进度（后端每秒发一次）
                    {
                        const elapsed = event.data.elapsed || 0;
                        const mins = Math.floor(elapsed / 60);
                        const secs = elapsed % 60;
                        const timeStr = mins > 0 ? `${mins}分${secs}秒` : `${secs}秒`;
                        const name = (event.data.name || '').slice(0, 30);
                        readline.cursorTo(process.stdout, 0);
                        readline.clearLine(process.stdout, 0);
                        process.stdout.write(chalk_1.default.gray(`  ⏳ ${name}: 执行中... (${timeStr})`));
                    }
                    break;
                case 'need_confirm':
                    // 后端已暂停等待审批，CLI 展示确认框并将结果发回
                    {
                        const toolName = event.data.name || 'unknown';
                        const argsPreview = typeof event.data.arguments === 'string'
                            ? event.data.arguments.slice(0, 200)
                            : JSON.stringify(event.data.arguments || {}).slice(0, 200);
                        const sessionToUse = sessionState.serverId || sessionState.localId;
                        try {
                            // 用简单的 header 展示信息，让 terminal.choose() 负责交互提示
                            terminal.writeln(chalk_1.default.yellow(`\n┌─ ❓ 确认执行 ${'─'.repeat(36)}`));
                            terminal.writeln(chalk_1.default.white(`│ 即将执行: ${toolName}`));
                            terminal.writeln(chalk_1.default.white(`│ 参数: ${argsPreview}`));
                            terminal.writeln(chalk_1.default.yellow('└' + '─'.repeat(42)));
                            const choice = await terminal.choose(['确认', '跳过', '💬 附带说明']);
                            if (choice === 2) {
                                // 附带说明：弹文本输入
                                const customMsg = await terminal.input(chalk_1.default.gray('[请输入说明] '));
                                await client.respond(sessionToUse, {
                                    approved: true,
                                    custom_message: customMsg || '',
                                });
                                terminal.writeln(chalk_1.default.green(`  ✓ 已确认执行 (备注: ${customMsg})`));
                            }
                            else if (choice === 0) {
                                await client.respond(sessionToUse, { approved: true });
                                terminal.writeln(chalk_1.default.green('  ✓ 已确认执行'));
                            }
                            else {
                                await client.respond(sessionToUse, { approved: false });
                                terminal.writeln(chalk_1.default.yellow('  ⏭ 已跳过'));
                            }
                        }
                        catch (err) {
                            // 用户交互失败（终端超时、网络中断等），尽量通知后端避免其永久等待
                            terminal.errorMessage(`确认交互失败: ${err.message}`);
                            try {
                                await client.respond(sessionToUse, { approved: false });
                            }
                            catch {
                                // 后端可能已经不可达，忽略二次错误
                            }
                            throw err; // 重新抛出，让 sseQueue 的 .catch() 记录日志
                        }
                    }
                    break;
                case 'ask_user':
                    // 后端已暂停等待用户回复，CLI 展示交互式问题并将结果发回
                    {
                        const askData = event.data;
                        const sessionToUse = sessionState.serverId || sessionState.localId;
                        try {
                            // 用简单的 header 展示问题，让 terminal.choose() 负责交互提示
                            terminal.writeln(chalk_1.default.yellow(`\n┌─ ❓ ${askData.header || '需要确认'} ${'─'.repeat(36)}`));
                            terminal.writeln(chalk_1.default.white(`│ ${askData.question}`));
                            terminal.writeln(chalk_1.default.yellow('└' + '─'.repeat(42)));
                            let answer;
                            if (askData.question_type === 'single_choice' && askData.options?.length > 0) {
                                const labels = askData.options.map((o) => o.label);
                                // 追加"其他"选项
                                labels.push('📝 其他（自定义输入）');
                                const choice = await terminal.choose(labels);
                                if (choice === labels.length - 1) {
                                    answer = await terminal.input(chalk_1.default.gray('[请输入自定义回答] '));
                                }
                                else {
                                    answer = askData.options[choice]?.label || '';
                                }
                            }
                            else if (askData.question_type === 'multi_choice' && askData.options?.length > 0) {
                                const labels = askData.options.map((o) => o.label);
                                labels.push('📝 其他（自定义输入）');
                                const indices = await terminal.multiChoose(labels);
                                const otherIdx = labels.length - 1;
                                const selected = indices
                                    .filter((i) => i !== otherIdx)
                                    .map((i) => askData.options[i]?.label)
                                    .filter(Boolean);
                                // 如果选了"其他"，弹文本输入并拼接
                                if (indices.includes(otherIdx)) {
                                    const custom = await terminal.input(chalk_1.default.gray('[请输入自定义回答] '));
                                    if (custom)
                                        selected.push(custom);
                                }
                                answer = selected.join(', ');
                            }
                            else if (askData.question_type === 'confirm') {
                                const choice = await terminal.choose(['确认', '取消', '📝 其他（自定义输入）']);
                                if (choice === 2) {
                                    answer = await terminal.input(chalk_1.default.gray('[请输入自定义回答] '));
                                }
                                else {
                                    answer = choice === 0 ? '确认' : '取消';
                                }
                            }
                            else {
                                // text 类型 — 自由文本输入
                                answer = await terminal.input(chalk_1.default.gray('[你的回答] '));
                            }
                            await client.respond(sessionToUse, { answer });
                            terminal.systemMessage(`已回复: ${answer}`);
                        }
                        catch (err) {
                            // 用户交互失败，尽量通知后端避免其永久等待
                            terminal.errorMessage(`提问交互失败: ${err.message}`);
                            try {
                                await client.respond(sessionToUse, { answer: '' });
                            }
                            catch {
                                // 后端可能已经不可达，忽略二次错误
                            }
                            throw err;
                        }
                    }
                    break;
                case 'task_complete':
                    terminal.successMessage(event.data.summary || '任务完成');
                    break;
                case 'done':
                    if (event.data.status === 'cancelled') {
                        terminal.writeln(chalk_1.default.yellow('⏹ 任务已取消'));
                    }
                    else if (assistantBuffer) {
                        const rendered = renderer.render(assistantBuffer);
                        terminal.writeln(rendered);
                    }
                    terminal.writeln(''); // 空行分隔
                    break;
                case 'error':
                    terminal.errorMessage(event.data.error || '未知错误');
                    break;
            }
        };
        // 将每个 SSE 事件串入 Promise 链，确保逐个处理
        client.on('sse', (event) => {
            sseQueue = sseQueue.then(() => handleSSEEvent(event)).catch((err) => {
                terminal.errorMessage(`SSE 事件处理异常: ${err.message}`);
            });
        });
        client.on('end', () => {
            if (thinkingActive) {
                thinking.stop();
            }
        });
        try {
            const imagesToSend = sessionState.pendingImages.length > 0 ? [...sessionState.pendingImages] : undefined;
            isRunning = true;
            lastInterruptTime = 0; // 新任务开始时重置双击计时
            await client.chat({
                message: input,
                images: imagesToSend,
                project_path: sessionState.workspace,
                profile: options.profile,
                model: options.model,
                session_id: sessionState.serverId || sessionState.localId,
                auto_approve: options.autoApprove || false,
            });
            // 发送后清空 pending 图片
            if (sessionState.pendingImages.length > 0) {
                sessionState.pendingImages = [];
            }
        }
        catch (err) {
            terminal.errorMessage(`请求失败: ${err.message}`);
        }
        finally {
            isRunning = false;
        }
        client.removeAllListeners();
    }
}
// ===== 单次问答模式 =====
async function askMode(question, options) {
    const config = (0, config_1.loadConfig)(options.project, options.profile, options.model);
    const client = new client_1.KFZCodeClient(config.backend_url, config.model.api_key);
    const renderer = new renderer_1.Renderer();
    let buffer = '';
    client.on('sse', (event) => {
        if (event.type === 'message') {
            buffer += event.data.content || '';
        }
        else if (event.type === 'done') {
            // 流式期间会收到 thinking 和 message 事件
            // thinking 已经在终端实时展示，done 时只渲染 message 部分
            const rendered = renderer.render(buffer);
            console.log(rendered);
        }
        else if (event.type === 'error') {
            console.error(chalk_1.default.red(`错误: ${event.data.error}`));
        }
    });
    // 检查是否有管道输入
    let stdinData = '';
    try {
        // 只有明确非 TTY 且有数据时才读取 stdin（避免阻塞）
        const stat = fs.fstatSync(0);
        if (stat.isFile() || stat.isFIFO()) {
            stdinData = fs.readFileSync(0, 'utf-8').trim();
        }
    }
    catch {
        // fd 0 不是可读取的文件/管道，忽略
    }
    const message = stdinData ? `${question}\n\n上下文:\n${stdinData}` : question;
    try {
        await client.chat({ message, images: options.image, project_path: options.project, profile: options.profile, model: options.model, auto_approve: false });
    }
    catch (err) {
        console.error(chalk_1.default.red(`请求失败: ${err.message}`));
        process.exit(1);
    }
}
// ===== 工具函数 =====
function formatTime(timestamp) {
    if (!timestamp)
        return chalk_1.default.gray('(未知时间)');
    const date = new Date(timestamp * 1000);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    const diffHour = Math.floor(diffMs / 3600000);
    const diffDay = Math.floor(diffMs / 86400000);
    if (diffMin < 1)
        return chalk_1.default.gray('刚刚');
    if (diffMin < 60)
        return chalk_1.default.gray(`${diffMin} 分钟前`);
    if (diffHour < 24)
        return chalk_1.default.gray(`${diffHour} 小时前`);
    if (diffDay < 7)
        return chalk_1.default.gray(`${diffDay} 天前`);
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    const h = String(date.getHours()).padStart(2, '0');
    const min = String(date.getMinutes()).padStart(2, '0');
    return chalk_1.default.gray(`${y}-${m}-${d} ${h}:${min}`);
}
/** 渲染单条消息 */
function renderMessage(msg, renderer) {
    const lines = [];
    const role = msg.role || 'unknown';
    const content = msg.content || '';
    switch (role) {
        case 'user':
            lines.push(chalk_1.default.bold.blue('\n┌─ 👤 用户 ─────────────────────────────────────'));
            lines.push(chalk_1.default.blue(`│ ${content.slice(0, 500)}`));
            if (content.length > 500) {
                lines.push(chalk_1.default.gray(`│ ... (共 ${content.length} 字符)`));
            }
            lines.push(chalk_1.default.blue('└' + '─'.repeat(48)));
            break;
        case 'assistant':
            // 先渲染思考过程
            const reasoning = msg.reasoning_content || '';
            if (reasoning) {
                const maxReasoning = 600;
                const truncated = reasoning.length > maxReasoning
                    ? reasoning.slice(0, maxReasoning) : reasoning;
                lines.push(chalk_1.default.bold.yellow('\n┌─ 💭 思考过程 ───────────────────────────────────'));
                for (const line of truncated.split('\n')) {
                    lines.push(chalk_1.default.yellow(`│ `) + chalk_1.default.dim(line.slice(0, 120)));
                }
                if (reasoning.length > maxReasoning) {
                    lines.push(chalk_1.default.gray(`│ ... (思考内容共 ${reasoning.length} 字符)`));
                }
                lines.push(chalk_1.default.yellow('└' + '─'.repeat(48)));
            }
            // 再渲染回复正文
            if (content) {
                lines.push(chalk_1.default.bold.green('\n┌─ 🤖 助手 ─────────────────────────────────────'));
                const rendered = renderer.render(content.slice(0, 800));
                for (const line of rendered.split('\n')) {
                    lines.push(chalk_1.default.green(`│ `) + line);
                }
                if (content.length > 800) {
                    lines.push(chalk_1.default.gray(`│ ... (共 ${content.length} 字符)`));
                }
                lines.push(chalk_1.default.green('└' + '─'.repeat(48)));
            }
            // 渲染工具调用
            if (msg.tool_calls) {
                const calls = typeof msg.tool_calls === 'string'
                    ? JSON.parse(msg.tool_calls) : msg.tool_calls;
                if (Array.isArray(calls)) {
                    for (const tc of calls) {
                        const args = typeof tc.arguments === 'string' ? tc.arguments : JSON.stringify(tc.arguments || {});
                        lines.push(chalk_1.default.gray(`  🔧 ${tc.name || 'unknown'}: ${args.slice(0, 120)}`));
                    }
                }
            }
            break;
        case 'system':
            lines.push(chalk_1.default.gray(`[系统] ${content.slice(0, 200)}`));
            break;
        case 'tool':
            lines.push(chalk_1.default.gray(`  ${msg.name ? `[${msg.name}] ` : ''}${content.slice(0, 200)}`));
            break;
        default:
            lines.push(chalk_1.default.gray(`[${role}] ${content.slice(0, 200)}`));
    }
    return lines.join('\n');
}
// ===== 命令处理 =====
async function handleCommand(input, terminal, config, options, sessionState) {
    const parts = input.slice(1).split(/\s+/);
    const cmd = parts[0].toLowerCase();
    const renderer = new renderer_1.Renderer();
    switch (cmd) {
        case 'help':
            terminal.showHelp();
            break;
        case 'clear':
            terminal.clear();
            terminal.showWelcome(VERSION);
            break;
        case 'exit':
        case 'quit':
            terminal.writeln(chalk_1.default.yellow('👋 再见!'));
            terminal.close();
            return false;
        case 'config':
            terminal.writeln(chalk_1.default.cyan('\n当前配置:'));
            terminal.writeln(`  模型: ${config.model.name}`);
            terminal.writeln(`  地址: ${config.model.base_url}`);
            terminal.writeln(`  思考展示: ${config.display.thinking}`);
            terminal.writeln(`  输入超时: ${config.behavior.input_timeout}秒`);
            terminal.writeln(`  最大循环: ${config.orchestrator.max_iterations}`);
            terminal.writeln(`  配置文件: ${(0, config_1.getConfigPath)()}`);
            break;
        case 'profile':
            if (parts[1]) {
                const profileName = parts[1];
                if (config.profiles[profileName]) {
                    options.profile = profileName;
                    terminal.successMessage(`已切换到模型预设: ${profileName}`);
                }
                else {
                    terminal.errorMessage(`预设 "${profileName}" 不存在`);
                    const names = Object.keys(config.profiles);
                    if (names.length > 0) {
                        terminal.writeln(`可用预设: ${names.join(', ')}`);
                    }
                }
            }
            else {
                const names = Object.keys(config.profiles);
                if (names.length > 0) {
                    terminal.writeln(chalk_1.default.cyan('可用模型预设:'));
                    for (const [name, prof] of Object.entries(config.profiles)) {
                        const marker = config.defaults?.profile === name ? ' [默认]' : '';
                        terminal.writeln(`  ${name}: ${prof.name} @ ${prof.base_url}${marker}`);
                    }
                }
                else {
                    terminal.systemMessage('没有配置预设，请在 ~/.kfzcode.json 中添加');
                }
            }
            break;
        case 'model':
            if (!parts[1]) {
                // 无参数 — 显示当前模型和可用模型列表
                terminal.writeln(chalk_1.default.cyan(`\n当前模型: ${chalk_1.default.bold(config.model.name)}`));
                terminal.writeln(chalk_1.default.gray(`  服务商: ${config.model.provider}`));
                terminal.writeln(chalk_1.default.gray(`  地址: ${config.model.base_url}`));
                terminal.writeln(chalk_1.default.gray(`  多模态: ${config.model.supports_vision ? '✓ 支持' : '✗ 不支持'}`));
                // 显示所有 profiles 供参考
                const profileNames = Object.keys(config.profiles);
                if (profileNames.length > 0) {
                    terminal.writeln(chalk_1.default.cyan('\n可切换的模型预设 (profile):'));
                    for (const [name, prof] of Object.entries(config.profiles)) {
                        const marker = options.profile === name ? chalk_1.default.green(' [当前]') : '';
                        terminal.writeln(chalk_1.default.gray(`  /profile ${name} : ${prof.name} @ ${prof.base_url}${marker}`));
                    }
                }
                terminal.writeln(chalk_1.default.gray('\n使用 /model <模型名> 直接切换模型（将开启新会话）'));
                terminal.writeln(chalk_1.default.gray('或 /profile <预设名> 切换预定义配置'));
            }
            else {
                // 有参数 — 切换模型（开启新会话）
                const newModel = parts.slice(1).join(' ');
                // 检查是否匹配某个 profile 名
                if (config.profiles[newModel]) {
                    options.profile = newModel;
                    options.model = undefined;
                    terminal.successMessage(`已切换到预设: ${newModel}（${config.profiles[newModel].name}）`);
                }
                else {
                    options.model = newModel;
                    options.profile = undefined;
                    terminal.successMessage(`已切换模型为: ${newModel}`);
                }
                // 开始新会话以让模型切换生效
                sessionState.serverId = null;
                sessionState.localId = (0, crypto_1.randomUUID)();
                terminal.writeln(chalk_1.default.gray('  ℹ 模型切换需要新会话，已自动创建新会话'));
                terminal.writeln(chalk_1.default.gray(`  ℹ 新会话 ID: ${sessionState.localId.slice(0, 8)}...`));
            }
            break;
        case 'doctor':
            terminal.systemMessage('正在诊断...');
            const client = new client_1.KFZCodeClient(config.backend_url, config.model.api_key);
            try {
                const result = await client.doctor();
                terminal.writeln(chalk_1.default.cyan('\n诊断结果:'));
                for (const [key, check] of Object.entries(result.checks)) {
                    const icon = check.ok ? '✓' : '✗';
                    const color = check.ok ? chalk_1.default.green : chalk_1.default.red;
                    terminal.writeln(color(`  ${icon} ${key}: ${JSON.stringify(check)}`));
                }
            }
            catch (err) {
                terminal.errorMessage(`诊断失败: ${err.message}`);
            }
            break;
        case 'session':
            {
                const sessionClient = new client_1.KFZCodeClient(config.backend_url, config.model.api_key);
                try {
                    if (!parts[1]) {
                        // 无参数 — 列出当前项目的所有持久化会话
                        terminal.writeln(chalk_1.default.cyan('\n  会话列表 (当前项目):'));
                        terminal.writeln(chalk_1.default.gray('  ─────────────────────────────────────────'));
                        const result = await sessionClient.listSessions(sessionState.workspace);
                        const sessions = result.sessions || [];
                        if (sessions.length === 0) {
                            terminal.writeln(chalk_1.default.gray('  (无历史会话)'));
                        }
                        else {
                            for (const s of sessions) {
                                const idShort = s.id.slice(0, 8);
                                const title = (s.title || '(空)').slice(0, 40);
                                const updated = formatTime(s.updated_at);
                                const msgInfo = chalk_1.default.gray(`${s.message_count || 0} 条消息`);
                                const isActive = s.is_active
                                    ? chalk_1.default.green(' [当前]')
                                    : (sessionState.serverId === s.id || sessionState.localId === s.id
                                        ? chalk_1.default.yellow(' [本次]') : '');
                                terminal.writeln(`  ${chalk_1.default.cyan(idShort)}  ${title}  ${msgInfo}  ${updated}${isActive}`);
                            }
                        }
                        terminal.writeln(chalk_1.default.gray('  ─────────────────────────────────────────'));
                        terminal.writeln(chalk_1.default.gray(`  使用 /session <id> 切换到指定会话`));
                    }
                    else {
                        // 有参数 — 切换到指定会话
                        const targetId = parts[1];
                        const result = await sessionClient.listSessions(sessionState.workspace);
                        const sessions = result.sessions || [];
                        // 支持前缀匹配
                        const match = sessions.find((s) => s.id === targetId || s.id.startsWith(targetId));
                        if (!match) {
                            terminal.errorMessage(`未找到会话 "${targetId}"，请检查 ID 是否正确`);
                            terminal.writeln(chalk_1.default.gray('  使用 /session 查看所有可用会话'));
                        }
                        else {
                            sessionState.localId = match.id;
                            sessionState.serverId = match.id;
                            const idShort = match.id.slice(0, 8);
                            const title = (match.title || '(空)').slice(0, 50);
                            terminal.successMessage(`已切换到会话 ${idShort}: ${title}`);
                            // 加载并展示完整对话
                            terminal.systemMessage('加载对话历史...');
                            try {
                                const detail = await sessionClient.getSessionMessages(match.id);
                                const messages = detail.messages || [];
                                if (messages.length === 0) {
                                    terminal.writeln(chalk_1.default.gray('  (该会话无消息记录)'));
                                }
                                else {
                                    terminal.writeln(chalk_1.default.cyan(`\n  ═══ 对话历史 (${messages.length} 条消息) ═══`));
                                    for (const msg of messages) {
                                        terminal.writeln(renderMessage(msg, renderer));
                                    }
                                    terminal.writeln(chalk_1.default.cyan('  ═══ 对话历史结束 ═══\n'));
                                }
                            }
                            catch (err) {
                                terminal.errorMessage(`加载对话失败: ${err.message}`);
                            }
                        }
                    }
                }
                catch (err) {
                    terminal.errorMessage(`获取会话列表失败: ${err.message}`);
                }
            }
            break;
        case 'image':
            if (parts.length < 2) {
                terminal.writeln(chalk_1.default.gray('用法: /image <文件路径> [文件路径2 ...]'));
                terminal.writeln(chalk_1.default.gray('附加图片文件，将在下一条消息中发送给模型进行视觉识别'));
                if (sessionState.pendingImages.length > 0) {
                    terminal.writeln(chalk_1.default.yellow(`  当前已附加 ${sessionState.pendingImages.length} 张待发送图片`));
                }
                break;
            }
            {
                const added = [];
                const skipped = [];
                for (const imgPath of parts.slice(1)) {
                    // 展开通配符 / 检查文件存在
                    const resolved = path.resolve(sessionState.workspace, imgPath);
                    if (fs.existsSync(resolved) && fs.statSync(resolved).isFile()) {
                        sessionState.pendingImages.push(resolved);
                        added.push(resolved);
                    }
                    else {
                        skipped.push(imgPath);
                    }
                }
                if (added.length > 0) {
                    terminal.successMessage(`已附加 ${added.length} 张图片，将在下一条消息中发送`);
                    for (const p of added) {
                        terminal.writeln(chalk_1.default.gray(`  + ${path.basename(p)}`));
                    }
                }
                if (skipped.length > 0) {
                    terminal.errorMessage(`${skipped.length} 个文件未找到: ${skipped.join(', ')}`);
                }
                terminal.writeln(chalk_1.default.gray(`共 ${sessionState.pendingImages.length} 张待发送图片`));
            }
            break;
        default:
            terminal.writeln(chalk_1.default.gray(`未知命令: /${cmd}，输入 /help 查看帮助`));
    }
    return true;
}
// ===== CLI 入口 =====
const program = new commander_1.Command();
program
    .name('kfzcode')
    .description('KFZCode - 内网 AI 编程助手')
    .version(VERSION);
program
    .option('-p, --project <path>', '指定项目目录')
    .option('-a, --ask <text>', '单次问答模式')
    .option('-i, --image <paths...>', '附加图片文件（支持视觉识别，可多张）')
    .option('--profile <name>', '使用指定模型预设')
    .option('-m, --model <name>', '指定模型名')
    .option('--config <path>', '使用自定义配置文件')
    .option('--auto-approve', '跳过所有权限确认')
    .option('--no-sandbox', '禁用沙箱')
    .option('--verbose', '输出调试信息')
    .action(async (options) => {
    if (options.ask) {
        await askMode(options.ask, options);
    }
    else {
        await interactiveMode(options);
    }
});
// 子命令: config
const configCmd = program.command('config').description('配置管理');
configCmd
    .command('list')
    .description('列出所有模型预设')
    .action(() => {
    const config = (0, config_1.loadGlobalConfig)();
    console.log(chalk_1.default.cyan('\n模型预设:'));
    if (Object.keys(config.profiles).length === 0) {
        console.log(chalk_1.default.gray('  (空)'));
    }
    for (const [name, prof] of Object.entries(config.profiles)) {
        const marker = config.defaults?.profile === name ? chalk_1.default.green(' [默认]') : '';
        console.log(`  ${name}: ${prof.name} @ ${prof.base_url}${marker}`);
    }
});
configCmd
    .command('show')
    .description('查看当前生效配置')
    .action(() => {
    const config = (0, config_1.loadGlobalConfig)();
    console.log(chalk_1.default.cyan('\n当前配置:'));
    console.log(JSON.stringify({
        model: config.model,
        display: config.display,
        behavior: config.behavior,
        orchestrator: config.orchestrator,
        tools: { disabled: config.tools.disabled },
        defaults: config.defaults,
    }, null, 2));
});
configCmd
    .command('set <key> <value>')
    .description('设置配置项')
    .action((key, value) => {
    const config = (0, config_1.loadGlobalConfig)();
    const keys = key.split('.');
    let target = config;
    for (let i = 0; i < keys.length - 1; i++) {
        if (!target[keys[i]])
            target[keys[i]] = {};
        target = target[keys[i]];
    }
    // 尝试转换类型
    let typedValue = value;
    if (value === 'true')
        typedValue = true;
    else if (value === 'false')
        typedValue = false;
    else if (!isNaN(Number(value)))
        typedValue = Number(value);
    target[keys[keys.length - 1]] = typedValue;
    (0, config_1.saveGlobalConfig)(config);
    console.log(chalk_1.default.green(`✓ 已设置 ${key} = ${JSON.stringify(typedValue)}`));
});
// 子命令: history
program.command('history')
    .description('查看对话历史 (需要后端运行)')
    .action(async () => {
    const config = (0, config_1.loadGlobalConfig)();
    const client = new client_1.KFZCodeClient(config.backend_url, config.model.api_key);
    try {
        console.log(chalk_1.default.cyan('\n后端运行中，对话历史请通过后端 API 查询'));
        const health = await client.healthCheck();
        console.log(chalk_1.default.gray(`状态: ${JSON.stringify(health)}`));
    }
    catch {
        console.log(chalk_1.default.yellow('后端未运行，对话历史存储在:'));
        console.log(chalk_1.default.gray(`  ~/.kfzcode/conversations.db`));
    }
});
// 子命令: init
program.command('init')
    .description('初始化配置文件')
    .option('--profile <name>', '指定默认预设', 'glm-5v')
    .option('--global', '创建全局配置文件 (~/.kfzcode.json) 而非项目级配置')
    .action((options) => {
    if (options.global) {
        // 创建全局配置，包含内置 profiles
        const globalPath = (0, config_1.getConfigPath)();
        if (fs.existsSync(globalPath)) {
            console.log(chalk_1.default.yellow(`配置文件已存在: ${globalPath}`));
            console.log(chalk_1.default.gray('如需覆盖请手动编辑或删除后重新 init'));
            return;
        }
        const globalConfig = {
            model: {
                provider: 'zhipu',
                name: 'GLM-5V-Turbo',
                base_url: 'https://open.bigmodel.cn/api/paas/v4',
                api_key: '请替换为你的API Key',
                supports_vision: true,
            },
            profiles: {
                'glm-5v': {
                    provider: 'zhipu',
                    name: 'GLM-5V-Turbo',
                    base_url: 'https://open.bigmodel.cn/api/paas/v4',
                    api_key: '请替换为你的API Key',
                    supports_vision: true,
                },
                'glm-5': {
                    provider: 'zhipu',
                    name: 'GLM-5',
                    base_url: 'https://open.bigmodel.cn/api/paas/v4',
                    api_key: '请替换为你的API Key',
                    max_tokens: 4096,
                    supports_vision: false,
                },
                deepseek: {
                    provider: 'deepseek',
                    name: 'deepseek-chat',
                    base_url: 'https://api.deepseek.com',
                    api_key: '请替换为你的API Key',
                    supports_vision: false,
                },
            },
            defaults: { profile: options.profile || 'glm-5v' },
        };
        (0, config_1.saveGlobalConfig)(globalConfig);
        console.log(chalk_1.default.green(`✓ 已创建全局配置: ${globalPath}`));
        console.log(chalk_1.default.yellow('⚠ 请编辑配置文件填入你的 API Key:'));
        console.log(chalk_1.default.gray(`  kfzcode config set model.api_key "你的智谱API_Key"`));
    }
    else {
        // 创建项目级配置
        const projectPath = process.cwd();
        const configPath = path.join(projectPath, '.kfzcode.json');
        if (fs.existsSync(configPath)) {
            console.log(chalk_1.default.yellow(`配置文件已存在: ${configPath}`));
            return;
        }
        const projectConfig = {
            model: { name: '' }, // 留空 = 使用全局默认
            defaults: { profile: options.profile || '' },
            tools: { allow_commands: [] },
        };
        fs.writeFileSync(configPath, JSON.stringify(projectConfig, null, 2), 'utf-8');
        console.log(chalk_1.default.green(`✓ 已创建项目配置: ${configPath}`));
        console.log(chalk_1.default.gray('  模型设置继承自全局配置 (~/.kfzcode.json)'));
    }
});
// 子命令: doctor
program.command('doctor')
    .description('系统诊断')
    .action(async () => {
    const config = (0, config_1.loadGlobalConfig)();
    console.log(chalk_1.default.cyan('\n🔍 KFZCode 系统诊断\n'));
    console.log(chalk_1.default.gray('─'.repeat(40)));
    // 检查配置文件
    const configPath = (0, config_1.getConfigPath)();
    if (fs.existsSync(configPath)) {
        console.log(chalk_1.default.green(`✓ 配置文件: ${configPath}`));
    }
    else {
        console.log(chalk_1.default.yellow(`✗ 配置文件不存在: ${configPath}`));
    }
    // 检查后端连接
    const client = new client_1.KFZCodeClient(config.backend_url, config.model.api_key);
    try {
        const health = await client.healthCheck();
        if (health.status === 'ok') {
            console.log(chalk_1.default.green(`✓ 后端连接: ${config.model.base_url}`));
            console.log(chalk_1.default.green(`✓ LLM 状态: ${JSON.stringify(health.llm)}`));
            console.log(chalk_1.default.gray(`  工具数: ${health.tools?.length || 0}`));
        }
        else {
            console.log(chalk_1.default.red(`✗ 后端异常: ${JSON.stringify(health)}`));
        }
    }
    catch {
        console.log(chalk_1.default.yellow(`✗ 后端未运行 (${config.model.base_url})`));
        console.log(chalk_1.default.gray('  请启动后端: cd backend && python -m src.main'));
    }
    // 检查 workspace
    console.log(chalk_1.default.gray(`  工作目录: ${process.cwd()}`));
    console.log(chalk_1.default.gray('─'.repeat(40)));
});
program.parse(process.argv);
//# sourceMappingURL=index.js.map