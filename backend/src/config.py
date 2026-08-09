"""配置管理 — 支持多层级配置加载与热切换"""
import os
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelConfig:
    """单个模型配置"""
    provider: str = "zhipu"
    name: str = "GLM-5V-Turbo"
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    api_key: str = ""  # 默认无 key，必须从 ~/.kfzcode.json 或环境变量 KFZCODE_API_KEY 提供
    max_tokens: int = 8192
    temperature: float = 0.7
    top_p: float = 0.95
    max_image_size_mb: int = 20
    """单张图片最大大小 (MB)，超过此大小的图片将被拒绝"""
    supports_vision: bool = True
    """该模型是否支持多模态 / 视觉识别"""


@dataclass
class ToolConfig:
    """工具配置"""
    disabled: list[str] = field(default_factory=list)
    allow_commands: list[str] = field(default_factory=lambda: ["npm", "pip", "git", "python", "pytest", "go", "node", "cargo"])
    deny_commands: list[str] = field(default_factory=lambda: ["rm -rf /", "sudo", "shutdown", "reboot"])
    auto_confirm_tools: list[str] = field(default_factory=list)
    """工具名列表，这些工具的 require_confirm 会被自动批准，不弹确认框。
    例如: ["execute_command", "write_file"]。
    注意: 设为空列表则不自动批准任何工具（默认行为），设为 ["*"] 则批准全部。"""


@dataclass
class DisplayConfig:
    """显示配置"""
    thinking: str = "collapsed"      # expanded | collapsed | hidden
    thinking_max_lines: int = 10


@dataclass
class OrchestratorConfig:
    """调度器配置"""
    max_iterations: int = 3
    auto_confirm_on_pass: bool = True
    parallel_review: bool = True
    review_timeout: int = 120


@dataclass
class BehaviorConfig:
    """行为配置"""
    ask_user_max_rounds: int = 5
    ask_user_auto_timeout: int = 7200
    max_turns: int = 60
    """单次对话最大 tool-call 轮次，超出后自动终止并提示用户拆分任务"""
    session_ttl_seconds: int = 7200
    """Session 无活动过期时间（秒），默认 2 小时"""


# ---- 内置 Profiles -----------------------------------------------------------
_BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
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
        "max_tokens": 4096,
        "supports_vision": False,
    },
    "deepseek": {
        "provider": "deepseek",
        "name": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "supports_vision": False,
    },
}


@dataclass
class KFZCodeConfig:
    """KFZCode 主配置"""
    model: ModelConfig = field(default_factory=ModelConfig)
    profiles: dict[str, dict[str, Any]] = field(default_factory=lambda: dict(_BUILTIN_PROFILES))
    defaults: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""
    tools: ToolConfig = field(default_factory=ToolConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)


class ConfigLoader:
    """多层级配置加载器"""

    GLOBAL_CONFIG_PATH = Path.home() / ".kfzcode.json"
    PROJECT_CONFIG_NAME = ".kfzcode.json"

    def __init__(self):
        self._cache: dict[str, Any] = {}
        self._global_config: dict[str, Any] = {}
        self._project_config: dict[str, Any] = {}
        self._load_global()

    def _load_global(self) -> None:
        """加载全局配置（每次 load() 都会重新读取，确保热更新）"""
        if self.GLOBAL_CONFIG_PATH.exists():
            try:
                self._global_config = json.loads(self.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._global_config = {}

    def load(self, project_path: str | None = None,
             profile: str | None = None, model_name: str | None = None) -> KFZCodeConfig:
        """加载配置，按优先级合并"""
        self._load_global()  # 每次调用都重新读取，支持热更新
        merged = self._merge_configs(project_path, profile, model_name)
        return self._dict_to_config(merged)

    def _merge_configs(self, project_path: str | None,
                       profile: str | None, model_name: str | None) -> dict:
        """按优先级合并配置"""
        # 1. 内置默认值
        merged: dict[str, Any] = {}

        # 2. 全局配置
        merged = self._deep_merge(merged, self._global_config)

        # 3. 项目级配置
        if project_path:
            project_config_path = Path(project_path) / self.PROJECT_CONFIG_NAME
            if project_config_path.exists():
                try:
                    self._project_config = json.loads(project_config_path.read_text("utf-8"))
                    merged = self._deep_merge(merged, self._project_config)
                except (json.JSONDecodeError, OSError):
                    pass

        # 4. 环境变量覆盖
        env_overrides = self._load_env_overrides()
        merged = self._deep_merge(merged, env_overrides)

        # 5. CLI 参数覆盖
        if profile and "profiles" in merged and profile in merged["profiles"]:
            # 浅合并：profile 覆盖其明确指定的字段
            # 如果 profile 未设置 api_key，继承自 model 顶层；否则使用 profile 的设置
            profile_data = dict(merged["profiles"][profile])
            if not profile_data.get("api_key"):
                profile_data.pop("api_key", None)  # profile 未设置 key，用 model 顶层
            merged.setdefault("model", {})
            merged["model"].update(profile_data)
        if model_name:
            merged.setdefault("model", {})["name"] = model_name

        return merged

    def _load_env_overrides(self) -> dict:
        """从环境变量读取覆盖配置"""
        overrides: dict[str, Any] = {}
        env_map = {
            "KFZCODE_BASE_URL": "model.base_url",
            "KFZCODE_API_KEY": "model.api_key",
            "KFZCODE_MODEL": "model.name",
            "KFZCODE_MAX_TOKENS": "model.max_tokens",
            "KFZCODE_MAX_IMAGE_SIZE_MB": "model.max_image_size_mb",
            "KFZCODE_SUPPORTS_VISION": "model.supports_vision",
        }
        for env_key, config_path in env_map.items():
            value = os.environ.get(env_key)
            if value:
                keys = config_path.split(".")
                target = overrides
                for key in keys[:-1]:
                    target = target.setdefault(key, {})
                target[keys[-1]] = value
        return overrides

    def _deep_merge(self, base: dict, override: dict) -> dict:
        """深度合并两个字典"""
        result = dict(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _dict_to_config(self, data: dict) -> KFZCodeConfig:
        """将字典转换为配置对象（类型转换带保护）"""
        model_data = data.get("model", {})

        # 安全类型转换辅助
        def _safe_int(value, default):
            try:
                return int(value)
            except (ValueError, TypeError):
                return default

        def _safe_float(value, default):
            try:
                return float(value)
            except (ValueError, TypeError):
                return default

        model = ModelConfig(
            provider=str(model_data.get("provider", "zhipu")),
            name=str(model_data.get("name", "GLM-5V-Turbo")),
            base_url=str(model_data.get("base_url", "https://open.bigmodel.cn/api/paas/v4")),
            api_key=str(model_data.get("api_key", "")),  # 必须从配置文件或环境变量获取
            max_tokens=_safe_int(model_data.get("max_tokens", 8192), 8192),
            temperature=_safe_float(model_data.get("temperature", 0.7), 0.7),
            top_p=_safe_float(model_data.get("top_p", 0.95), 0.95),
            max_image_size_mb=_safe_int(model_data.get("max_image_size_mb", 20), 20),
            supports_vision=bool(model_data.get("supports_vision", True)),
        )

        tools_data = data.get("tools", {})
        tools = ToolConfig(
            disabled=tools_data.get("disabled", []) or [],
            allow_commands=tools_data.get("allow_commands", []) or [],
            deny_commands=tools_data.get("deny_commands", []) or [],
            auto_confirm_tools=tools_data.get("auto_confirm_tools", []) or [],
        )

        display_data = data.get("display", {})
        display = DisplayConfig(
            thinking=str(display_data.get("thinking", "collapsed")),
            thinking_max_lines=_safe_int(display_data.get("thinking_max_lines", 10), 10),
        )

        orch_data = data.get("orchestrator", {})
        orchestrator = OrchestratorConfig(
            max_iterations=_safe_int(orch_data.get("max_iterations", 3), 3),
            auto_confirm_on_pass=bool(orch_data.get("auto_confirm_on_pass", True)),
            parallel_review=bool(orch_data.get("parallel_review", True)),
            review_timeout=_safe_int(orch_data.get("review_timeout", 120), 120),
        )

        behavior_data = data.get("behavior", {})
        behavior = BehaviorConfig(
            ask_user_max_rounds=_safe_int(behavior_data.get("ask_user_max_rounds", 5), 5),
            ask_user_auto_timeout=_safe_int(behavior_data.get("ask_user_auto_timeout", 7200), 7200),
            max_turns=_safe_int(behavior_data.get("max_turns", 60), 60),
            session_ttl_seconds=_safe_int(behavior_data.get("session_ttl_seconds", 7200), 7200),
        )

        return KFZCodeConfig(
            model=model,
            profiles=dict(_BUILTIN_PROFILES, **(data.get("profiles", {}) or {})),
            defaults=data.get("defaults", {}) or {},
            system_prompt=str(data.get("system_prompt", "")),
            tools=tools,
            display=display,
            orchestrator=orchestrator,
            behavior=behavior,
        )

    def save_global(self, config: dict) -> None:
        """保存全局配置到文件"""
        self.GLOBAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.GLOBAL_CONFIG_PATH.write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._global_config = config

    def save_project(self, project_path: str, config: dict) -> None:
        """保存项目级配置"""
        path = Path(project_path) / self.PROJECT_CONFIG_NAME
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


# 全局单例
config_loader = ConfigLoader()
