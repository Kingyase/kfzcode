/** CLI 配置管理 */
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

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

const DEFAULT_CONFIG: KFZCodeConfig = {
  backend_url: 'http://localhost:8765',
  model: {
    provider: 'zhipu',
    name: 'GLM-5V-Turbo',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    // api_key 不在代码中设置默认值，仅从 ~/.kfzcode.json 或环境变量 KFZCODE_API_KEY 获取
    max_tokens: 8192,
    temperature: 0.7,
    top_p: 0.95,
    max_image_size_mb: 20,
    supports_vision: true,
  },
  profiles: {
    'glm-5v': {
      provider: 'zhipu',
      name: 'GLM-5V-Turbo',
      base_url: 'https://open.bigmodel.cn/api/paas/v4',
      // api_key 从 ~/.kfzcode.json 的 model 顶层或环境变量继承
      max_tokens: 8192,
      temperature: 0.7,
      top_p: 0.95,
      max_image_size_mb: 20,
      supports_vision: true,
    },
    'glm-5': {
      provider: 'zhipu',
      name: 'GLM-5',
      base_url: 'https://open.bigmodel.cn/api/paas/v4',
      max_tokens: 4096,
      temperature: 0.7,
      top_p: 0.95,
      max_image_size_mb: 20,
      supports_vision: false,
    },
    deepseek: {
      provider: 'deepseek',
      name: 'deepseek-chat',
      base_url: 'https://api.deepseek.com',
      max_tokens: 8192,
      temperature: 0.7,
      top_p: 0.95,
      max_image_size_mb: 20,
      supports_vision: false,
    },
  },
  defaults: {
    profile: 'glm-5v',
  },
  display: {
    thinking: 'expanded',
    thinking_max_lines: 10,
  },
  behavior: {
    input_timeout: 7200,
    choose_timeout: 7200,
  },
  orchestrator: {
    max_iterations: 3,
    auto_confirm_on_pass: true,
    parallel_review: true,
    review_timeout: 120,
  },
  tools: {
    disabled: [],
    allow_commands: [],
    deny_commands: [],
    auto_confirm_tools: [],
  },
};

const GLOBAL_CONFIG_PATH = path.join(os.homedir(), '.kfzcode.json');
const PROJECT_CONFIG_NAME = '.kfzcode.json';

export function loadGlobalConfig(): KFZCodeConfig {
  try {
    if (fs.existsSync(GLOBAL_CONFIG_PATH)) {
      const raw = JSON.parse(fs.readFileSync(GLOBAL_CONFIG_PATH, 'utf-8'));
      return mergeConfig(DEFAULT_CONFIG, raw);
    }
  } catch {
    // 忽略解析错误
  }
  return { ...DEFAULT_CONFIG };
}

export function loadProjectConfig(projectPath?: string): KFZCodeConfig {
  const global = loadGlobalConfig();
  if (!projectPath) {
    projectPath = process.cwd();
  }
  const projectConfigPath = path.join(projectPath, PROJECT_CONFIG_NAME);
  try {
    if (fs.existsSync(projectConfigPath)) {
      const raw = JSON.parse(fs.readFileSync(projectConfigPath, 'utf-8'));
      return mergeConfig(global, raw);
    }
  } catch {
    // 忽略
  }
  return global;
}

export function loadConfig(projectPath?: string, profile?: string, modelName?: string): KFZCodeConfig {
  let config = loadProjectConfig(projectPath);

  // 环境变量覆盖
  if (process.env.KFZCODE_BASE_URL) config.model.base_url = process.env.KFZCODE_BASE_URL;
  if (process.env.KFZCODE_API_KEY) config.model.api_key = process.env.KFZCODE_API_KEY;
  if (process.env.KFZCODE_MODEL) config.model.name = process.env.KFZCODE_MODEL;
  if (process.env.KFZCODE_INPUT_TIMEOUT) config.behavior.input_timeout = parseInt(process.env.KFZCODE_INPUT_TIMEOUT);
  if (process.env.KFZCODE_MAX_IMAGE_SIZE_MB) config.model.max_image_size_mb = parseInt(process.env.KFZCODE_MAX_IMAGE_SIZE_MB);
  if (process.env.KFZCODE_SUPPORTS_VISION) config.model.supports_vision = process.env.KFZCODE_SUPPORTS_VISION === 'true';

  // profile 覆盖
  if (profile && config.profiles[profile]) {
    config.model = { ...config.model, ...config.profiles[profile] };
  }
  // model name 覆盖
  if (modelName) {
    config.model.name = modelName;
  }

  return config;
}

function mergeConfig(base: any, override: any): any {
  const result = { ...base };
  for (const key of Object.keys(override)) {
    if (
      typeof base[key] === 'object' &&
      base[key] !== null &&
      !Array.isArray(base[key]) &&
      typeof override[key] === 'object' &&
      override[key] !== null
    ) {
      result[key] = mergeConfig(base[key], override[key]);
    } else {
      result[key] = override[key];
    }
  }
  return result;
}

export function saveGlobalConfig(config: Record<string, any>): void {
  const dir = path.dirname(GLOBAL_CONFIG_PATH);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  fs.writeFileSync(GLOBAL_CONFIG_PATH, JSON.stringify(config, null, 2), 'utf-8');
}

export function getConfigPath(): string {
  return GLOBAL_CONFIG_PATH;
}
