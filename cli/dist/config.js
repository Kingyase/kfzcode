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
Object.defineProperty(exports, "__esModule", { value: true });
exports.loadGlobalConfig = loadGlobalConfig;
exports.loadProjectConfig = loadProjectConfig;
exports.loadConfig = loadConfig;
exports.saveGlobalConfig = saveGlobalConfig;
exports.getConfigPath = getConfigPath;
/** CLI 配置管理 */
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const os = __importStar(require("os"));
const DEFAULT_CONFIG = {
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
function loadGlobalConfig() {
    try {
        if (fs.existsSync(GLOBAL_CONFIG_PATH)) {
            const raw = JSON.parse(fs.readFileSync(GLOBAL_CONFIG_PATH, 'utf-8'));
            return mergeConfig(DEFAULT_CONFIG, raw);
        }
    }
    catch {
        // 忽略解析错误
    }
    return { ...DEFAULT_CONFIG };
}
function loadProjectConfig(projectPath) {
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
    }
    catch {
        // 忽略
    }
    return global;
}
function loadConfig(projectPath, profile, modelName) {
    let config = loadProjectConfig(projectPath);
    // 环境变量覆盖
    if (process.env.KFZCODE_BASE_URL)
        config.model.base_url = process.env.KFZCODE_BASE_URL;
    if (process.env.KFZCODE_API_KEY)
        config.model.api_key = process.env.KFZCODE_API_KEY;
    if (process.env.KFZCODE_MODEL)
        config.model.name = process.env.KFZCODE_MODEL;
    if (process.env.KFZCODE_INPUT_TIMEOUT)
        config.behavior.input_timeout = parseInt(process.env.KFZCODE_INPUT_TIMEOUT);
    if (process.env.KFZCODE_MAX_IMAGE_SIZE_MB)
        config.model.max_image_size_mb = parseInt(process.env.KFZCODE_MAX_IMAGE_SIZE_MB);
    if (process.env.KFZCODE_SUPPORTS_VISION)
        config.model.supports_vision = process.env.KFZCODE_SUPPORTS_VISION === 'true';
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
function mergeConfig(base, override) {
    const result = { ...base };
    for (const key of Object.keys(override)) {
        if (typeof base[key] === 'object' &&
            base[key] !== null &&
            !Array.isArray(base[key]) &&
            typeof override[key] === 'object' &&
            override[key] !== null) {
            result[key] = mergeConfig(base[key], override[key]);
        }
        else {
            result[key] = override[key];
        }
    }
    return result;
}
function saveGlobalConfig(config) {
    const dir = path.dirname(GLOBAL_CONFIG_PATH);
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
    fs.writeFileSync(GLOBAL_CONFIG_PATH, JSON.stringify(config, null, 2), 'utf-8');
}
function getConfigPath() {
    return GLOBAL_CONFIG_PATH;
}
//# sourceMappingURL=config.js.map