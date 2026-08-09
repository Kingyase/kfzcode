# KFZCode

开源的 内网部署 AI 编程助手 — 基于 OpenAI 兼容 API（智谱 GLM / DeepSeek 等），支持 CLI 交互，后续扩展 Web。

## 架构

```
用户 → CLI (TypeScript/Node.js) → REST API + SSE → Backend (Python/FastAPI, :8765)
                                                     ├── SingleAgentRunner (单 Agent 模式)
                                                     └── Orchestrator → Coder → Tester (多 Agent 协同)
                                                           ↑ 最多 3 轮自动修正循环 ↑
                                                     ├── Tool System (文件读写、Shell、搜索、反问)
                                                     └── LLM Client → OpenAI 兼容 API
```

**两种对话模式：**

| 模式 | API 路由 | 说明 |
|------|----------|------|
| 单 Agent | `/api/chat` | CLI 默认使用，`SingleAgentRunner` 直接处理 tool-use 循环 |
| 多 Agent | `/api/chat/multi-agent` | Orchestrator 调度 Coder 编写代码，Tester 自动测试审查 |

**配置层级（优先级由高到低）：**

```
CLI 参数  >  环境变量 KFZCODE_*  >  .kfzcode.json（项目级）  >  ~/.kfzcode.json（全局）
```

---

## 安装与配置

### 环境要求

| 组件 | 要求 |
|------|------|
| Python | >= 3.10 |
| Node.js | >= 18.0.0 |
| pip | 随 Python 安装 |
| npm | 随 Node.js 安装 |

---

### 第一步：获取 API Key

KFZCode 依赖大模型 API，在使用前需要先获取 API Key：

| 平台 | 获取地址 | 说明 |
|------|----------|------|
| 智谱 AI | [open.bigmodel.cn](https://open.bigmodel.cn/) | 注册后进入「API 密钥」页面创建 |
| DeepSeek | [platform.deepseek.com](https://platform.deepseek.com/) | 注册后进入「API Keys」页面创建 |

---

### 第二步：安装后端

```bash
# 进入后端目录
cd backend

# 安装依赖（可编辑模式，修改代码无需重新安装）
pip install -e .

# 验证安装
python -c "from src.config import KFZCodeConfig; print('后端安装成功')"
```

**启动后端：**

```bash
# 方式一：直接启动（终端会被占用，适合调试）
python -m src.main

# 方式二：进程管理脚本（后台运行，适合日常使用）
python run.py start     # 启动
python run.py stop      # 停止
python run.py restart   # 重启

# Windows 用户也可使用 bat 脚本
start.bat               # 启动
stop.bat                # 停止
restart.bat             # 重启
```

启动成功后：
- API 服务：`http://localhost:8765`
- API 文档：`http://localhost:8765/docs`
- 健康检查：`http://localhost:8765/api/health`
- 日志文件：`backend/backend.log`

---

### 第三步：安装 CLI

```bash
# 进入 CLI 目录
cd cli

# 安装依赖
npm install

# 编译 TypeScript → dist/
npm run build

# 全局注册命令（需要管理员/root 权限）
npm link
```

注册成功后，全局可用以下命令：

```bash
kfzcode   # 主命令
kc        # 短别名
```

> **注意：** 不要升级 chalk 到 v5+（ESM-only，不兼容当前 CJS 项目）

---

### 第四步：创建配置文件

配置文件**不会自动生成**，需要手动创建。有两种配置方式：

#### 方式 A：交互式创建（推荐）

```bash
# 创建全局配置文件 ~/.kfzcode.json
kfzcode init --global

# 创建项目级配置文件 ./.kfzcode.json（在当前目录）
kfzcode init
```

执行后会生成带占位符的配置文件，提示你填入 API Key。

#### 方式 B：手动创建

直接编辑配置文件：

```bash
# Linux / macOS
vim ~/.kfzcode.json

# Windows
notepad %USERPROFILE%\.kfzcode.json
```

---

### 第五步：填入 API Key

打开 `~/.kfzcode.json`，将 `api_key` 替换为你的真实 Key：

```json
{
  "model": {
    "provider": "zhipu",
    "name": "GLM-5V-Turbo",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": "你的智谱API_Key",
    "supports_vision": true
  },
  "profiles": {
    "glm-5v": {
      "provider": "zhipu",
      "name": "GLM-5V-Turbo",
      "base_url": "https://open.bigmodel.cn/api/paas/v4",
      "api_key": "你的智谱API_Key",
      "supports_vision": true
    },
    "glm-5": {
      "provider": "zhipu",
      "name": "GLM-5",
      "base_url": "https://open.bigmodel.cn/api/paas/v4",
      "api_key": "你的智谱API_Key",
      "max_tokens": 4096,
      "supports_vision": false
    },
    "deepseek": {
      "provider": "deepseek",
      "name": "deepseek-chat",
      "base_url": "https://api.deepseek.com",
      "api_key": "你的DeepSeek_API_Key",
      "supports_vision": false
    }
  },
  "defaults": {
    "profile": "glm-5v"
  }
}
```

> ⚠️ **API Key 不会硬编码在代码中**，只能通过配置文件或环境变量 `KFZCODE_API_KEY` 提供。不配置的话后端 LLM 调用会失败。

#### 使用环境变量（可选）

如果不希望 Key 写入配置文件，可以设置环境变量：

```bash
# Linux / macOS
export KFZCODE_API_KEY="你的API_Key"
export KFZCODE_BASE_URL="https://open.bigmodel.cn/api/paas/v4"

# Windows (CMD)
set KFZCODE_API_KEY=你的API_Key
set KFZCODE_BASE_URL=https://open.bigmodel.cn/api/paas/v4

# Windows (PowerShell)
$env:KFZCODE_API_KEY="你的API_Key"
$env:KFZCODE_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
```

环境变量优先级高于配置文件。

#### 项目级配置（可选）

在项目根目录创建 `.kfzcode.json`，可以为不同项目使用不同的模型：

```bash
# 在项目目录下
kfzcode init          # 创建项目级 .kfzcode.json
```

项目级配置会覆盖全局配置中的同名字段。

> **配置存放位置：**
> - 全局配置：`~/.kfzcode.json`（`C:\Users\<用户名>\.kfzcode.json` 或 `/home/<用户名>/.kfzcode.json`）
> - 项目配置：`<项目根目录>/.kfzcode.json`

---

### 第六步：开始使用

```bash
# 交互式对话
kfzcode

# 单次问答
kfzcode --ask "这个项目怎么编译？"

# 指定项目目录 + 模型预设
kfzcode --project /path/to/project --profile glm-5v

# 指定模型名（覆盖默认配置）
kfzcode --model GLM-5

# 关闭所有安全确认（信任模式）
kfzcode --auto-approve

# 管道输入
cat error.log | kfzcode "分析这段错误"

# 诊断检查
kfzcode doctor
```

**交互式会话中的命令：**

| 命令 | 说明 |
|------|------|
| `/model` | 查看当前模型 |
| `/model GLM-5` | 切换到 GLM-5（开启新会话） |
| `/profile glm-5v` | 切换到 glm-5v 预设 |
| `/help` | 查看帮助 |
| `/exit` 或 `Ctrl+C` | 退出 |
| 双击 `Ctrl+C` | 强制退出（任务执行中时） |

---

## 项目结构

```
kfzcode/
├── backend/                    # Python 后端 (FastAPI)
│   ├── pyproject.toml
│   ├── run.py                  # 进程管理脚本 (start/stop/restart)
│   ├── pytest.ini              # pytest 配置 (asyncio_mode=auto)
│   ├── start.bat               # Windows 启动脚本
│   ├── stop.bat                # Windows 停止脚本
│   ├── restart.bat             # Windows 重启脚本
│   └── src/
│       ├── main.py             # FastAPI 入口 + 路由
│       ├── config.py           # 配置管理 (环境变量 / 文件)
│       ├── utils.py            # 工具函数
│       ├── agent/              # Agent 系统
│       │   ├── engine.py           # Agent 循环引擎 (核心)
│       │   ├── orchestrator.py     # 多 Agent 调度器
│       │   ├── coder.py            # 编码 Agent
│       │   ├── reviewer.py         # 审查 Agent
│       │   ├── message_bus.py      # 异步消息总线
│       │   ├── messages.py         # Agent 间通信协议
│       │   ├── base_agent.py       # Agent 基类
│       │   ├── context.py          # 上下文窗口管理
│       │   └── history.py          # 对话历史持久化
│       ├── tools/              # 工具系统
│       │   ├── base.py             # 工具抽象基类 + ToolRegistry
│       │   ├── file_read.py        # 文件读取（自动放行）
│       │   ├── file_write.py       # 文件写入（需确认）
│       │   ├── file_edit.py        # 精确字符串替换编辑（需确认）
│       │   ├── shell.py            # Shell 命令执行（需确认）
│       │   ├── search.py           # grep / glob 搜索
│       │   └── ask_user.py         # Agent 反问用户
│       ├── llm/                # LLM 客户端
│       │   ├── client.py           # OpenAI 兼容客户端 (HTTPX)
│       │   └── types.py            # 消息 / 工具类型定义
│       ├── sandbox/            # 沙箱执行
│       └── storage/            # 数据存储 (SQLite)
│
├── cli/                        # CLI 前端 (TypeScript, Node.js)
│   ├── package.json
│   ├── tsconfig.json
│   ├── dist/                   # 编译产物 (已提交)
│   └── src/
│       ├── index.ts                # 入口 + 命令路由 (commander)
│       ├── client.ts               # SSE 流式客户端
│       ├── terminal.ts             # 交互式终端 UI
│       ├── renderer.ts             # Markdown 渲染 (marked + marked-terminal)
│       ├── thinking.ts             # 思考过程折叠展示
│       └── config.ts               # ~/.kfzcode.json 配置管理
│
├── docs/                       # 文档
├── AGENTS.md                   # AI 助手指南（本项目专用）
├── CLAUDE.md                   # Claude Code 集成指南
└── plan.md                     # 原始开发计划
```

---

## 核心功能

### Agent 循环引擎

```
用户消息 → [System Prompt + 上下文 + 工具列表]
  → LLM 响应（流式 SSE）:
      ├── reasoning_content → 思考过程（折叠/灰色展示）
      ├── text_delta → 回答内容（正常展示）
      ├── tool_calls → 工具调用
      │     ├── 需确认 → 等待用户批准
      │     └── 自动放行 → 执行并追加结果
      └── finish → 本轮结束，保存历史
```

**SSE 事件流：**

| 事件 | 说明 |
|------|------|
| `thinking` | 思考内容增量（折叠/灰色） |
| `message` | 回复内容增量（正常展示） |
| `tool_call` | 工具调用请求（确认卡片） |
| `tool_result` | 工具执行结果 |
| `done` | 本轮结束 |

### 多 Agent 协同

Orchestrator 调度 Coder + Tester，最多 3 轮自动修正：

1. Orchestrator 拆解任务 → 派发给 Coder
2. Coder 编写代码 → 通知 Orchestrator
3. Orchestrator → 派发测试给 Tester
4. Tester 运行测试 / lint → 反馈结果
5. 测试通过 → 汇报用户 ✓
6. 测试失败且未满 3 轮 → 回到步骤 1
7. 测试失败已满 3 轮 → 与用户确认是否验收

Agent 间通过**异步消息总线（MessageBus）**通信，支持请求-回复（Future）和发布-订阅。

### 工具集

| 工具 | 安全策略 | 说明 |
|------|----------|------|
| `read_file` | 自动放行 | 读取文件，支持分页 |
| `write_file` | 需确认 | 创建/覆盖文件 |
| `edit_file` | 需确认 | 精确字符串替换 |
| `execute_command` | 需确认 | Shell 命令执行 |
| `grep` / `glob` | 自动放行 | 正则搜索 / 文件名匹配 |
| `ask_user` | — | Agent 反问用户（单选/多选/确认） |

### 思考过程展示

模型在 `<thinking>` 标签中的推理过程，CLI 以折叠形式展示，支持展开/收起/隐藏，提升透明度和信任感。

### 动态模型配置

- 全局 + 项目级配置文件
- 多模型预设（profiles），运行时热切换
- 环境变量覆盖（`KFZCODE_*`）

---

## 安全

| 操作 | 策略 |
|------|------|
| 文件读取 | 自动放行 |
| 文件写入 | 需用户逐一确认 |
| Shell 命令 | 需用户逐一确认，可配置白名单/黑名单 |
| `--auto-approve` | 信任模式，跳过所有确认 |

命令在子进程中执行，后端通过 PID 文件管理进程生命周期。

---

## 测试

```bash
cd backend

# 全部测试
pytest

# 单个文件
pytest tests/test_core.py

# 按名称过滤
pytest -k "test_add"
```

> 注意：测试导入使用裸路径（如 `from tools.base import ToolRegistry`），依赖 `conftest.py` 注入 `src/` 到 `sys.path`，请勿在其他位置添加 `sys.path` 操作。

---

## 注意事项

- **语言：** 所有用户可见字符串、注释、文档均为中文。
- **CLI 依赖勿升级：** chalk v4（CJS）、marked v4、marked-terminal v5。chalk v5 是 ESM-only。
- **端口 8765：** 硬编码于 `run.py`、`src/main.py`、CLI 默认配置。如需修改请全部同步。
- **`cli/dist/` 已提交：** 修改 TypeScript 后需执行 `npm run build`。
- **无 CI 流水线：** 手动执行 `pytest` 和 `npm run build` 验证变更。
- **API Key 不在代码中：** 必须通过 `~/.kfzcode.json` 或环境变量 `KFZCODE_API_KEY` 提供，代码不包含任何默认 Key。
- **DeepV4 API 兼容：** 流式响应可能在完成时触发 `httpx.ReadError`，客户端通过判断是否收到至少一个 chunk 来决定是否忽略该错误。`SingleAgentRunner` 对 429、502-504、连接错误有自适应重试。
- **思考内容解析：** 引擎同时支持 `<thinking>` XML 标签和原生 `reasoning_content` API delta 字段，优先使用原生推理内容。

---

## License

Internal use only.
