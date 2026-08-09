# KFZCode Backend

> 内网 AI 编程助手后端 — 基于多 Agent 协同的智能代码生成与审查系统

## 目录

- [项目概述](#项目概述)
- [架构设计](#架构设计)
  - [整体架构](#整体架构)
  - [核心 Agent 循环](#核心-agent-循环)
  - [多 Agent 协同机制](#多-agent-协同机制)
  - [上下文压缩策略](#上下文压缩策略)
- [项目结构](#项目结构)
- [LLM 客户端与流式推理](#llm-客户端与流式推理)
- [工具系统设计](#工具系统设计)
- [配置系统设计](#配置系统设计)
- [快速开始](#快速开始)
- [API 文档](#api-文档)
- [配置说明](#配置说明)

---

## 项目概述

KFZCode 是一个面向企业内网的 AI 编程助手。后端基于 **Python asyncio** 构建，采用 **多 Agent 协同架构**，通过消息总线实现 Orchestrator（调度）、Coder（编码）、Reviewer（审查）三种角色的解耦协作。同时内置 **智能上下文压缩** 机制，解决 LLM 上下文窗口限制问题。

**技术栈：**

| 层级 | 技术 |
|------|------|
| Web 框架 | FastAPI + SSE Streaming |
| 异步通信 | asyncio + Queue |
| 持久化 | SQLite (aiosqlite) |
| LLM 客户端 | httpx (兼容 OpenAI API) |
| 配置管理 | 多层级配置（内置 → 全局 → 项目 → 环境变量 → CLI） |

---

## 架构设计

### 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                      FastAPI Server                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ /api/chat │  │/api/chat │  │ /api/health          │  │
│  │ (SSE流式) │  │/multi    │  │ /api/sessions        │  │
│  └─────┬─────┘  └────┬─────┘  └──────────┬───────────┘  │
│        │             │                   │               │
│  ┌─────▼─────┐ ┌─────▼──────────┐        │               │
│  │ SingleAgent│ │ KFZCodeEngine  │        │               │
│  │ Runner     │ │ (多Agent模式)  │        │               │
│  │            │ │                │        │               │
│  │ ┌───────┐  │ │ ┌────────────┐ │        │               │
│  │ │Context │  │ │ │Orchestrator│ │        │               │
│  │ │Manager │  │ │ └─────┬──────┘ │        │               │
│  │ └───────┘  │ │       │        │        │               │
│  │ ┌───────┐  │ │ ┌─────▼──────┐ │        │               │
│  │ │Conv.   │  │ │ │ MessageBus │◄┼────────┼── SSE/WS     │
│  │ │Store   │  │ │ └──┬────┬───┘ │        │   事件广播     │
│  │ └───────┘  │ │    │    │     │        │               │
│  └────────────┘ │ ┌──▼──┐ ┌▼───┐│        │               │
│                 │ │Coder│ │Rev. ││        │               │
│                 │ └─────┘ └────┘│        │               │
│                 └───────────────┘        │               │
└─────────────────────────────────────────────────────────┘
```

**两种运行模式：**

| 模式 | 类 | 适用场景 |
|------|-----|---------|
| 单 Agent | `SingleAgentRunner` | 快速调试、简单对话、完整上下文 |
| 多 Agent | `KFZCodeEngine` | 复杂任务、需要编码→审查→修复循环 |

---

### 核心 Agent 循环

每个 Agent 遵循统一的 **"分析 → 不确定就问 → 收集上下文 → 修改 → 验证 → 完成"** 循环：

```
                    ┌──────────────┐
                    │  接收用户输入  │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  ① 分析需求   │
                    │  理解意图     │
                    │  拆解任务     │
                    └──────┬───────┘
                           ▼
                   ┌─────┴──────┐
                   │ 有不确定性？ │
                   └─────┬──────┘
                   是 ▼      否 ▼
            ┌──────────┐  ┌──────────┐
            │② ask_user │  │③ 收集上下文│
            │ 向用户确认  │  │ read_file │
            │ ⚠ 禁止猜测 │  │ grep/glob │
            └──────────┘  └────┬─────┘
                               ▼
                        ┌──────────────┐
                        │ ④ 制定方案    │
                        │ 遵循现有风格   │
                        │ 最小化变更    │
                        └──────┬───────┘
                               ▼
                        ┌──────────────┐
                        │ ⑤ 执行修改    │
                        │ write_file   │
                        │ edit_file    │
                        └──────┬───────┘
                               ▼
                        ┌──────────────┐
                        │ ⑥ 验证结果    │
                        │ 跑测试/lint  │
                        │ execute_cmd  │
                        └──────┬───────┘
                               ▼
                        ┌──────┴──────┐
                        │ 任务完成？    │
                        └──────┬──────┘
                        否 ▼    是 ▼
                        继续③   ┌──────┐
                                │⑦ 汇报│
                                └──────┘
```

**五大行为准则：**

| # | 准则 | 说明 |
|---|------|------|
| 1 | **不确定必须反问** | 需求模糊、多方案、缺信息、有风险 → `ask_user` |
| 2 | **先理解再动手** | 读文件 → 分析结构 → 匹配风格 → 再改代码 |
| 3 | **最小化变更** | 只改必要部分，不顺手重构无关代码 |
| 4 | **验证工作** | 写完就跑测试/lint，不引入新错误 |
| 5 | **任务完成标准** | 不提前停止，所有步骤完成 + 验证通过后才汇报 |

**工具选择策略：**

```
读 → read_file
搜 → grep / glob / list_directory
改 → edit_file / write_file
验 → execute_command
问 → ask_user（触发条件：需求模糊 / 多方案 / 缺信息 / 破坏性操作）
```

---

### 多 Agent 协同机制

#### 角色定义

```
┌──────────────┐
│ Orchestrator │ ← 大脑：分析需求、派发任务、决策调度
│  (主调度器)   │
└──────┬───────┘
       │
   ┌───┴────┐
   ▼        ▼
┌─────┐  ┌──────┐
│Coder│  │Rev.  │
│ 编码 │  │ 审查  │
│ 执行 │  │ 测试  │
└─────┘  └──────┘
 双手      眼睛
```

| 角色 | AgentRole | 核心职责 |
|------|-----------|----------|
| **Orchestrator** | `orchestrator` | 分析任务 → 派发 Coder → 派发 Reviewer → 决策循环（最多 3 轮） |
| **Coder** | `coder` | 读取项目、编写代码、运行命令、必要时使用 `ask_user` 与用户交互 |
| **Reviewer** | `tester` | 执行测试命令 + LLM 代码审查 → 输出结构化 Issue（critical/warning/suggestion） |

#### 通信机制 — MessageBus

```
┌─────────────────────────────────────────┐
│              MessageBus                 │
│                                         │
│  _queues: {                             │
│    ORCHESTRATOR: Queue(max=200)         │
│    CODER:        Queue(max=200)         │
│    TESTER:       Queue(max=200)         │
│  }                                      │
│                                         │
│  _pending_requests: {msg_id → Future}   │  ← 请求-响应同步
│  _event_listeners:  {event → Queue[]}   │  ← 事件广播（供 SSE 推送）
└─────────────────────────────────────────┘
```

**两种通信模式：**

| 模式 | 方法 | 说明 |
|------|------|------|
| **异步发送** | `send(msg)` | 放入目标队列即返回，不等待 |
| **请求-响应** | `request(msg, timeout)` | 创建 Future → 发送 → 阻塞等待 → 对方 `reply()` 唤醒 |

#### 核心调度循环

```
用户提交任务
    │
    ▼
┌────────────────────────────────────────────┐
│           Orchestrator 调度循环              │
│                                            │
│  ① _analyze_task()                        │
│     LLM 分析 → 提取约束条件和项目规范        │
│                                            │
│  ╔══════════════════════════════════════╗  │
│  ║  while iteration < 3:               ║  │
│  ║                                      ║  │
│  ║  ② _dispatch_coder(task)            ║  │
│  ║     bus.request(timeout=300s)        ║  │
│  ║     → Coder 编码实现                 ║  │
│  ║     ← CoderResultPayload            ║  │
│  ║                                      ║  │
│  ║  ③ 失败? → ask_user 决策 → 退出     ║  │
│  ║                                      ║  │
│  ║  ④ _dispatch_tester(result)         ║  │
│  ║     bus.request(timeout=180s)        ║  │
│  ║     → Reviewer 测试+审查            ║  │
│  ║     ← TestResultPayload             ║  │
│  ║                                      ║  │
│  ║  ⑤ passed (无critical)?             ║  │
│  ║     → _report_success() → 完成      ║  │
│  ║                                      ║  │
│  ║  ⑥ 还有轮次?                        ║  │
│  ║     ├─ 是 → _dispatch_fix()         ║  │
│  ║     │       Coder修复 → 继续循环     ║  │
│  ║     └─ 否 → ask_user 人工决策       ║  │
│  ╚══════════════════════════════════════╝  │
│                                            │
│  finally: _reset() → 保存历史 + 清理       │
└────────────────────────────────────────────┘
```

#### Coder 执行流程

```
收到编码任务 (TASK_ASSIGN 或 FIX_INSTRUCTION)
    │
    ├─ _handle_coding_task()
    │    ├─ reset_context()           ← 清空对话历史
    │    ├─ _build_coding_prompt()    ← 组装描述+约束+上下文+规范
    │    ├─ call_llm(prompt, tools)   ← 首次 LLM 调用
    │    ├─ execute_tool_loop()       ← 多轮工具调用（最多 20 轮）
    │    ├─ _collect_changes()        ← 从历史中收集文件变更
    │    └─ send_result() → CoderResultPayload
    │
    └─ _handle_fix_task()
         ├─ _build_fix_prompt()       ← 组装 Issue 列表+修复建议
         ├─ call_llm → execute_tool_loop()
         └─ send_result()
```

#### Reviewer 审查流程

```
收到审查任务
    │
    ▼
两阶段审查：
    │
    ├─ Phase 1: 执行测试
    │    ├─ 构建测试 prompt（列出 test_commands）
    │    ├─ call_llm → execute_tool_loop() → 运行测试
    │    └─ _collect_test_output() → 收集测试输出
    │
    ├─ Phase 2: LLM 代码审查
    │    ├─ 输入：测试结果 + 变更文件内容（截断 4000 字符）
    │    ├─ call_llm（纯审查，无工具调用）
    │    └─ _parse_issues() → 解析 JSON → Issue 列表
    │
    └─ Phase 3: 判定
         ├─ 存在 critical? → passed = false
         ├─ 构建 TestResultPayload
         └─ send_result()
```

**Issue 严重级别：**

| 级别 | 含义 | 影响 |
|------|------|------|
| `critical` | 严重问题（测试失败、逻辑错误、安全漏洞） | 阻止通过 |
| `warning` | 警告（代码异味、潜在风险） | 不阻止，但记录 |
| `suggestion` | 建议（风格优化、命名改进） | 仅供参考 |

#### 关键设计决策

| 设计点 | 决策 | 理由 |
|--------|------|------|
| **解耦通信** | Agent 不互相调用，全部通过 MessageBus | 可独立开发、测试、替换 |
| **请求-响应** | `request/reply` 用 Future 实现 | 同步等待语义，超时可检测 |
| **审查门禁** | 只看 `critical` 级别 | 避免因小问题阻塞流程 |
| **渐进修复** | 3 轮自愈循环 | 大部分问题可自动修复，超限交由人工 |
| **双向超时** | Coder 300s，Reviewer 180s | 防止死等，及时暴露问题 |
| **事件外泄** | `publish_event` 实时广播 | 前端可展示进度、需要用户输入时弹框 |
| **人工兜底** | `ask_user` 在失败/耗尽时触发 | 机器做不来的交给人类 |

---

### 上下文压缩策略

LLM 的上下文窗口有限（如 32K tokens），长对话可能超出限制。单 Agent 模式下集成了智能上下文压缩：

#### 架构

```
SingleAgentRunner
├── self.history: list[LLMMessage]          ← 原始对话历史
├── self.context_manager: ContextManager    ← 压缩管理器
└── self._repair_history()                  ← 配对修复函数
```

#### Token 估算

```
总 tokens = 文本tokens + 图片tokens + 角色开销

文本:
  中文: 每字 × 1.5 token
  英文: 每4字符 ≈ 1 token

图片:
  每张 ≈ 85 tokens

角色开销:
  每条消息 +50 tokens
```

**阈值判定：**
- `effective_limit = max_tokens(32000) - reserve_for_response(4000) = 28000`
- 估算 tokens > 28000 → 触发压缩

#### 压缩算法（非急切触发）

压缩**不在每次对话时主动触发**，而是作为 LLM 请求连续失败的 **最后手段**：

```
LLM 请求重试策略（渐进式）：

重试 0 (首次):  max_tokens × 1.0   历史不处理
     ↓ 失败
重试 1:         max_tokens × 0.6   历史不处理
     ↓ 失败
重试 2:         max_tokens × 0.3   触发 compress() 智能压缩
```

#### 压缩主流程

```
compress(messages)
  │
  ├─ ① 前置检查
  │    ├─ 估算 tokens ≤ 28000? → 不压缩，直接返回
  │    └─ 消息 ≤ 12 条? → 不压缩
  │
  ├─ ② 分离消息
  │    ├─ system_msgs  → 永远保留
  │    └─ other_msgs   → 进入压缩
  │
  ├─ ③ 切分窗口
  │    ├─ recent = 最近 12 条（约 6 轮对话）
  │    └─ older  = 之前的全部消息
  │
  ├─ ④ 边界配对修复 (_repair_pairing)
  │    修复 recent/older 切割边界处的
  │    tool_calls ↔ tool_result 配对断裂
  │    （3 种场景：孤儿 tool / 缺失结果 / 悬空 assistant）
  │
  ├─ ⑤ 生成摘要 (_generate_summary)
  │    规则提取（不调用 LLM，零延迟）：
  │    ├─ user 消息 → 前 100 字符 + 图片数量
  │    ├─ assistant(tool_calls) → 工具名 + 涉及文件路径
  │    ├─ assistant(纯文本) → 前 100 字符
  │    └─ tool 消息 → 提取错误信息
  │    最多保留 15 个话题、10 个文件、3 个错误
  │
  └─ ⑥ 拼接返回
       system_msgs + [摘要 user 消息] + recent
```

#### 配对保护机制（三层防御）

压缩过程中最危险的副作用是破坏 `assistant(tool_calls)` 和 `tool_result` 的配对关系，导致 LLM API 返回 400 错误。系统在 **三个时间点** 进行防御：

```
┌──────────────────┐
│ ① 加载历史时      │ → repair_message_history()  全量扫描修复
├──────────────────┤
│ ② LLM 调用前      │ → _repair_tool_pairings()   预防性修复
├──────────────────┤
│ ③ compress() 时   │ → _repair_pairing()         边界修复
└──────────────────┘
```

三种配对破坏场景及修复：

| 场景 | 问题 | 修复 |
|------|------|------|
| **A: 孤儿 tool** | recent 开头是 tool 消息，但其 assistant 在 older 中 | 向前扩展 recent，拉入对应的 assistant |
| **B: 缺失部分结果** | assistant(tool_calls) 有 3 个 tool_call，但只有 2 个 tool 结果 | 从 older 末尾拉取缺失的 tool 消息 |
| **C: 悬空 assistant** | assistant(tool_calls) 后面完全没有 tool 结果 | 移除该 assistant + 其孤儿 tool |

#### 摘要示例

压缩后插入到对话中的摘要格式：

```
[对话历史摘要] 之前的对话中讨论了以下内容:
- 用户: 帮我写一个登录功能
- 助手: 调用了工具 read_file, glob, write_file
- 用户: 改一下密码加密方式
- 助手: 调用了工具 edit_file, execute_command
[涉及文件: src/auth.py, src/config.py, src/utils.py]
[关键错误: pytest 报错 AssertionError: assert False]
```

#### 辅助函数

| 函数 | 用途 |
|------|------|
| `repair_message_history()` | 全量扫描所有 `assistant(tool_calls)`，移除不完整的配对 |
| `truncate_content(text, max_chars=12000)` | 保留首尾各 6000 字符，中间省略 |

---

## 项目结构

```
backend/
├── src/
│   ├── agent/                    # Agent 核心模块
│   │   ├── __init__.py
│   │   ├── base_agent.py         # Agent 基类（消息循环 + LLM调用 + 工具执行）
│   │   ├── engine.py             # 引擎入口（KFZCodeEngine + SingleAgentRunner）
│   │   ├── orchestrator.py       # 调度 Agent（分析→Coder→Reviewer→修复 循环）
│   │   ├── coder.py              # 编码 Agent（读代码→写文件→运行命令）
│   │   ├── reviewer.py           # 审查 Agent（测试执行 + LLM 代码审查）
│   │   ├── context.py            # 上下文管理器（压缩 + 配对修复）
│   │   ├── history.py            # 对话持久化（SQLite）
│   │   ├── messages.py           # 消息协议定义（Message/Issue/Payload）
│   │   └── message_bus.py        # 消息总线（Queue + Future + 事件广播）
│   │
│   ├── llm/                      # LLM 客户端
│   │   ├── __init__.py
│   │   ├── client.py             # DeepV4Client（兼容 OpenAI API）
│   │   └── types.py              # LLM 类型定义（LLMMessage/LLMResponse/FunctionCall）
│   │
│   ├── tools/                    # 工具集
│   │   ├── __init__.py
│   │   ├── base.py               # 工具基类 + ToolRegistry
│   │   ├── file_read.py          # read_file + list_directory
│   │   ├── file_write.py         # write_file + edit_file
│   │   ├── shell.py              # execute_command
│   │   ├── search.py             # grep + glob
│   │   └── ask_user.py           # 向用户提问
│   │
│   ├── sandbox/                  # 沙箱执行器
│   │   ├── __init__.py
│   │   └── executor.py
│   │
│   ├── storage/                  # 存储层
│   │   ├── __init__.py
│   │   └── db.py
│   │
│   ├── __init__.py
│   ├── config.py                 # 多层级配置管理（内置→全局→项目→环境变量→CLI）
│   ├── main.py                   # FastAPI 服务入口 + Session 管理
│   └── utils.py                  # 工具函数
│
├── tests/
│   ├── conftest.py
│   └── test_core.py              # 核心测试套件
│
├── pyproject.toml                # 项目元数据 + 依赖
├── pytest.ini                    # Pytest 配置
├── run.py                        # 启动入口
├── start.bat / stop.bat / restart.bat
└── README.md
```

### 模块依赖关系

```
main.py ──→ engine.py ──→ orchestrator.py ──→ base_agent.py ──→ llm/client.py
               │                │                    │
               │                ├──→ coder.py ───────┤
               │                │                    │
               │                ├──→ reviewer.py ────┤
               │                │                    │
               │                ├──→ messages.py     ├──→ tools/
               │                ├──→ message_bus.py  │
               │                └──→ history.py      │
               │                                     │
               ├──→ context.py ──────────────────────┤
               └──→ config.py
```

---

## LLM 客户端与流式推理

### DeepV4Client 设计

`DeepV4Client` 是兼容所有 OpenAI 格式 API 的 LLM 客户端，支持 DeepSeek、智谱 GLM、DeepV4 等任何实现了 `/chat/completions` 的模型。

```
DeepV4Client
├── chat(messages, tools, stream)     # 统一入口，stream=True 默认
│   ├── _chat_sync()                  # 非流式（带重试）
│   └── _chat_stream()                # 流式（核心路径）
│       ├── _read_stream_lines()      # SSE 行解析 → LLMStreamChunk
│       └── _retry_request()          # HTTP 请求级重试
│
├── _build_body()                     # 请求体构建（含 tools 注入）
├── _serialize_msg()                  # 消息序列化（文本/多模态）
├── _should_retry()                   # 重试判定
└── _calc_delay()                     # 指数退避延迟计算
```

### 自动重试机制

| 层级 | 错误类型 | 策略 |
|------|---------|------|
| **HTTP 请求级** | 429 / 502 / 503 / 504 | 指数退避 (4s → 8s → 16s, 最大 120s) |
| **连接级** | ConnectError / ReadError / Timeout | 同上 |
| **不可重试** | 400 Bad Request | 直接抛出，不重试 |

```python
# 指数退避公式
delay = min(120.0, 4.0 × 2^attempt)
```

### SSE 流式解析

```
HTTP Response (chunked)
    │
    ▼
resp.aiter_lines()
    │
    ├─ "data: {...}"  → json.loads → 提取 delta
    ├─ "data: [DONE]" → 结束
    └─ 其他           → 跳过
    │
    ▼
LLMStreamChunk
    ├── content           → 文本增量
    ├── reasoning_content → 推理过程增量（DeepSeek/GLM 支持）
    ├── tool_call_delta   → 工具调用增量（index/id/name/arguments）
    └── finish_reason     → "stop" / "length" / "tool_calls"
```

**关键容错设计：**
- DeepSeek 等 API 在流式完成时主动断开连接导致 `ReadError` → 已接收的内容仍然有效，不报错
- 零数据时 re-raise，确保真正的连接失败不被吞掉
- 多个 tool_call delta 合并为最终 `FunctionCall` 列表

### 多模态支持

```python
# 类型系统 (types.py)
LLMMessage.content  →  str | list[dict] | None

# 纯文本: 直接字符串
content = "你好"

# 多模态: OpenAI content 数组
content = [
    {"type": "text", "text": "这张图里有什么？"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,...", "detail": "auto"}}
]
```

**图片处理管线：**

```
本地文件路径
    ↓
image_to_content_block(file_path)
    ├── 存在性检查
    ├── 大小检查 (≤ 20MB)
    ├── MIME 类型推断 + 白名单验证
    └── base64 编码 → data:image/xxx;base64,...
    ↓
build_multimodal_content(text, images)
    ↓
LLM API 请求
```

**存储优化：** 对话持久化时，`strip_images_from_content()` 将 base64 图片替换为 `[N 张图片(未保存)]` 占位符，避免数据库膨胀。

### `<thinking>` 标签流式解析

单 Agent 模式在引擎层实现了 `<thinking>` 标签的实时解析分流：

```
LLM 流式输出: "分析需求...\n<thinking>\n方案A...\n</thinking>\n推荐方案A"
                                                    │
                                          process_chunk() 状态机
                                                    │
                        ┌───────────────────────────┼───────────────────────────┐
                        ▼                           ▼                           ▼
                  "分析需求..."              "方案A..."（thinking）        "推荐方案A"
                  → type: message            → type: thinking            → type: message
```

- 使用 `content_buf` / `thinking_buf` 缓冲区防止跨 chunk 标签断裂
- 支持原生 `reasoning_content`（DeepSeek API 直接返回的推理字段），两种模式互斥

### 自适应降级重试

引擎层在 LLM 请求失败时的三级降级策略：

```
重试 0:  max_tokens × 1.0   ← 正常请求
重试 1:  max_tokens × 0.6   ← 缩减输出上限
重试 2:  max_tokens × 0.3   ← 再缩减 + compress() 智能压缩上下文
```

---

## 工具系统设计

### 架构

```
ToolRegistry (注册中心)
    │
    ├── register(tool)          # 注册单个工具
    ├── register_many(tools)    # 批量注册
    ├── get(name) → ToolBase    # 按名获取
    ├── get_definitions()       # 导出 OpenAI Function Calling 格式
    └── parse_tool_calls()      # LLM 返回 → ToolCallRequest
```

### ToolBase 基类

```python
class ToolBase(ABC):
    name: str                    # 工具名（LLM 看到的标识）
    description: str             # 描述（注入 LLM system prompt）
    parameters: dict             # JSON Schema 参数定义
    require_confirm: bool        # 是否需要用户确认

    async def execute(**kwargs) → ToolResult   # 子类实现
    async def cancel()                          # 可选：终止长时间操作
    def validate_args(**kwargs) → dict          # 校验 + 补全默认值
    def to_definition() → ToolDefinition        # 导出 OpenAI 格式
```

**设计要点：**
- `_get_required_params()` 自动从 `parameters` 中推断必填字段（无 `default` 即为必填）
- `validate_args()` 自动补全默认值，缺少必填参数时抛出 `ValueError`
- `cancel()` 提供钩子供 `ShellTool` 等长时间工具清理子进程

### 工具清单

| 工具 | 类 | require_confirm | 核心能力 |
|------|-----|-----------------|----------|
| `read_file` | FileReadTool | ❌ | 分页读取大文件，显示行号，UTF-8 容错 |
| `list_directory` | ListDirectoryTool | ❌ | 树形结构输出，自动忽略 `.git`/`node_modules` 等 |
| `write_file` | FileWriteTool | ✅ | 创建/覆盖文件，自动创建父目录 |
| `edit_file` | FileEditTool | ✅ | 精确字符串替换，唯一匹配检查，支持 `replace_all` |
| `execute_command` | ShellTool | ✅ | Shell 执行 + 危险命令拦截 + 超时控制 + 自保机制 |
| `grep` | GrepTool | ❌ | 正则搜索，支持文件名过滤、上下文行、结果上限 |
| `glob` | GlobTool | ❌ | 通配符文件查找，显示文件大小 |
| `ask_user` | AskUserTool | ❌ | 向用户提问（text/single_choice/multi_choice/confirm） |

### Shell 安全机制

Shell 工具是唯一可执行外部命令的工具，安全措施分四层：

**第一层：危险模式拦截**
```python
DANGEROUS_PATTERNS = [
    "rm -rf /", "mkfs.", "dd if=",
    "shutdown", "reboot", "halt",
    "chmod 777 /", "chown -R /",
]
```

**第二层：禁止名单**
```python
deny_commands = ["rm -rf /", "sudo", "shutdown", "reboot"]
```

**第三层：白名单（可选启用）**
```python
allow_commands = ["npm", "pip", "git", "python", "pytest", "go", "node", "cargo"]
# 不在白名单内的命令直接拒绝
```

**第四层：自保机制**
```python
# 防止 Agent 杀掉自身进程：
# - taskkill /F /IM python.exe  → 拒绝（批量杀进程）
# - taskkill /PID <当前PID>     → 拒绝（精确杀自身）
# - pkill / killall python      → 拒绝（批量杀进程）
# 正确的做法: taskkill /PID <指定PID>（非 KFZCode 自身进程）
```

### 服务启动命令特殊处理

Shell 工具能区分普通命令和服务启动命令：

```
检测到 "run" / "serve" / "uvicorn" / "npm start" 等关键词
    ↓
_execute_server_command()
    ├── 等待 15s
    ├── 进程仍在运行? → ✓ 服务已在后台启动
    └── 进程自行退出? → 返回输出 + 退出码
```

### Windows 超时可靠性

Windows 上 `asyncio.wait_for(proc.communicate(), timeout=N)` 无法可靠取消 I/O。改用：

```python
# 纯计时器 + proc.kill() 事后清理
comm_task = asyncio.ensure_future(proc.communicate())
done, pending = await asyncio.wait([comm_task], timeout=N)
if comm_task in pending:
    proc.kill()  # 暴力终止
```

---

## 配置系统设计

### 多层级配置架构

```
                    ┌─────────────────┐
                    │  CLI 参数       │  ← 最高优先级
                    │  profile/model  │
                    └───────┬─────────┘
                            │ 覆盖
                    ┌───────▼─────────┐
                    │  环境变量        │
                    │  KFZCODE_*      │
                    └───────┬─────────┘
                            │ 覆盖
                    ┌───────▼─────────┐
                    │  项目配置        │
                    │  .kfzcode.json  │
                    └───────┬─────────┘
                            │ 覆盖
                    ┌───────▼─────────┐
                    │  全局配置        │
                    │  ~/.kfzcode.json│
                    └───────┬─────────┘
                            │ 覆盖
                    ┌───────▼─────────┘
                    │  内置默认值       │  ← 最低优先级
                    │  ModelConfig()   │
                    └─────────────────┘
```

**加载流程：**

```python
config_loader.load(project_path, profile, model_name)
    │
    ├── 1. 读取 ~/.kfzcode.json（每次重新读取，支持热更新）
    ├── 2. 读取 <project>/.kfzcode.json
    ├── 3. 读取环境变量 (KFZCODE_API_KEY, KFZCODE_MODEL 等)
    ├── 4. 应用 CLI profile/model 参数
    │
    └── _dict_to_config() → KFZCodeConfig
```

### 配置类层次

```
KFZCodeConfig
├── model: ModelConfig          # LLM 模型参数
│   ├── provider, name, base_url, api_key
│   ├── max_tokens, temperature, top_p
│   ├── max_image_size_mb       # 图片大小上限
│   └── supports_vision         # 是否支持多模态
│
├── tools: ToolConfig           # 工具行为
│   ├── disabled: [str]         # 禁用的工具列表
│   ├── allow_commands: [str]   # Shell 白名单
│   ├── deny_commands: [str]    # Shell 黑名单
│   └── auto_confirm_tools: [str]  # 自动批准的 require_confirm 工具
│
├── display: DisplayConfig      # 前端显示
│   ├── thinking: "collapsed"   # expanded / collapsed / hidden
│   └── thinking_max_lines: 10
│
├── orchestrator: OrchestratorConfig  # 多 Agent 调度
│   ├── max_iterations: 3       # 最大修复轮次
│   ├── auto_confirm_on_pass: true
│   ├── parallel_review: true
│   └── review_timeout: 120     # 审查超时
│
├── behavior: BehaviorConfig    # 运行时行为
│   ├── ask_user_max_rounds: 5
│   ├── ask_user_auto_timeout: 7200
│   ├── max_turns: 60           # 单次对话最大轮次
│   └── session_ttl_seconds: 7200
│
└── profiles: dict              # 内置 Model Profile  + 用户自定义
```

### 内置 Model Profile

```python
_BUILTIN_PROFILES = {
    "glm-5v": {
        "provider": "zhipu",
        "name": "GLM-5V-Turbo",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "supports_vision": True,
    },
    "glm-5": {
        "provider": "zhipu",
        "name": "GLM-5",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "supports_vision": False,
    },
    "deepseek": {
        "provider": "deepseek",
        "name": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "supports_vision": False,
    },
}
```

**Profile 匹配规则：** 选择 profile 时，profile 中未设置的字段自动继承 model 顶层的配置。

### 环境变量映射

| 环境变量 | 对应配置路径 |
|----------|------------|
| `KFZCODE_BASE_URL` | `model.base_url` |
| `KFZCODE_API_KEY` | `model.api_key` |
| `KFZCODE_MODEL` | `model.name` |
| `KFZCODE_MAX_TOKENS` | `model.max_tokens` |
| `KFZCODE_MAX_IMAGE_SIZE_MB` | `model.max_image_size_mb` |
| `KFZCODE_SUPPORTS_VISION` | `model.supports_vision` |

### 类型安全保护

所有配置值在 `_dict_to_config()` 中通过安全转换函数处理，防止配置文件中的非预期值导致运行时崩溃：

```python
def _safe_int(value, default):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default
```

### Session 生命周期

```
POST /api/chat (带 session_id?)
    │
    ├── 无 session_id + 无项目映射  → 新建 UUID session
    ├── 无 session_id + 有项目映射  → 复用项目已有 session
    └── 有 session_id             → 复用或新建 runner
    │
    ▼
SingleAgentRunner 创建
    ├── 加载对话历史（SQLite）
    ├── 每个新消息: _touch_session()
    └── 背景任务: 每5分钟检查 TTL 过期 → 清理
```

**Session TTL：** 默认 7200 秒（2 小时），可通过 `behavior.session_ttl_seconds` 配置。同一项目路径只保留一个活跃 session，新建 session 时会关闭旧的防止泄漏。

---

## 快速开始

### 环境要求

- Python ≥ 3.10
- Windows / Linux / macOS

### 安装

```bash
cd backend

# 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/macOS

# 安装依赖
pip install -e ".[dev]"
```

### 配置

KFZCode 支持多层级配置（优先级从低到高）：

1. **内置默认值** — 代码中的 `ModelConfig` 默认值
2. **全局配置** — `~/.kfzcode.json`
3. **项目配置** — `<project>/.kfzcode.json`
4. **环境变量** — `KFZCODE_API_KEY` 等
5. **CLI 参数** — API 请求中传入的 `profile` / `model`

**最简配置（设置 API Key）：**

```bash
# 方式一：环境变量
export KFZCODE_API_KEY="your-api-key"

# 方式二：全局配置文件 ~/.kfzcode.json
{
  "model": {
    "api_key": "your-api-key"
  }
}
```

**内置 Model Profile：**

| Profile | 模型 | 特点 |
|---------|------|------|
| `glm-5v` | GLM-5V-Turbo | 智谱，支持多模态/视觉 |
| `glm-5` | GLM-5 | 智谱，纯文本 |
| `deepseek` | deepseek-chat | DeepSeek，纯文本 |

### 启动服务

```bash
# 开发模式（默认端口 8000）
python run.py

# 或直接
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

### 运行测试

```bash
pytest -v
```

---

## API 文档

### 健康检查

```
GET /api/health
```

**响应：**
```json
{
  "status": "ok",
  "llm": { "reachable": true },
  "tools": ["read_file", "write_file", "edit_file", "execute_command", "grep", "glob", "list_directory", "ask_user"],
  "workspace": "/path/to/workspace"
}
```

### 单 Agent 对话（SSE 流式）

```
POST /api/chat
```

**请求体：**
```json
{
  "message": "帮我写一个登录接口",
  "project_path": "/path/to/project",
  "session_id": "uuid-optional",
  "profile": "deepseek",
  "model": "deepseek-chat",
  "images": [],
  "auto_approve": false
}
```

**SSE 事件类型：**

| 事件 | 含义 |
|------|------|
| `start` | 对话开始，包含 session_id |
| `thinking` | 模型推理过程（可折叠显示） |
| `message` | 模型文本回复（增量） |
| `tool_start` | 开始执行工具 |
| `tool_result` | 工具执行结果 |
| `ask_user` | 需要用户交互（弹框） |
| `done` | 本轮对话完成 |

### 多 Agent 协同（SSE 流式）

```
POST /api/chat/multi
```

请求体与单 Agent 相同。响应包含额外的调度进度事件：

| 事件 | 含义 |
|------|------|
| `progress` | 调度阶段（analysis / iteration / fixing） |
| `task_complete` | 任务最终结果 |

### Session 管理

```
GET  /api/sessions          # 列出所有会话
POST /api/sessions/delete   # 删除会话
```

Session 默认 2 小时无活动后自动清理。

---

## 配置说明

### 完整配置项

```json
{
  "model": {
    "provider": "zhipu",
    "name": "GLM-5V-Turbo",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": "",
    "max_tokens": 8192,
    "temperature": 0.7,
    "top_p": 0.95,
    "max_image_size_mb": 20,
    "supports_vision": true
  },
  "tools": {
    "disabled": [],
    "allow_commands": ["npm", "pip", "git", "python", "pytest", "go", "node", "cargo"],
    "deny_commands": ["rm -rf /", "sudo", "shutdown", "reboot"],
    "auto_confirm_tools": []
  },
  "display": {
    "thinking": "collapsed",
    "thinking_max_lines": 10
  },
  "orchestrator": {
    "max_iterations": 3,
    "auto_confirm_on_pass": true,
    "parallel_review": true,
    "review_timeout": 120
  },
  "behavior": {
    "ask_user_max_rounds": 5,
    "ask_user_auto_timeout": 7200,
    "max_turns": 60,
    "session_ttl_seconds": 7200
  }
}
```

### 关键配置说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `model.max_tokens` | 8192 | LLM 上下文窗口大小 |
| `orchestrator.max_iterations` | 3 | Coder→Reviewer 自愈循环上限 |
| `orchestrator.review_timeout` | 120s | Reviewer 单次审查超时 |
| `behavior.max_turns` | 60 | 单次对话最大 tool-call 轮次 |
| `behavior.session_ttl_seconds` | 7200 | Session 无活动过期时间 |
| `tools.allow_commands` | npm, pip, git... | Shell 工具白名单 |
| `tools.deny_commands` | rm -rf /, sudo... | Shell 工具黑名单 |
| `display.thinking` | collapsed | 推理过程展示方式：expanded/collapsed/hidden |
