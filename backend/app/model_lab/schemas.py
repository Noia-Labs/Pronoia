"""Public request schemas for the Pronoia Model Lab API."""
from __future__ import annotations

from typing import Literal, Optional
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator, model_serializer, model_validator

from ..model_endpoint_security import validate_secret_env_name
from .defaults import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_TIMEOUT_SECONDS


def _clean_api_key(value: SecretStr | None, *, allow_blank: bool) -> SecretStr | None:
    if value is None:
        return None
    secret = value.get_secret_value().strip()
    if not secret:
        if allow_blank:
            return None
        raise ValueError("API Key 不能为空")
    if len(secret) > 16_384 or any(unicodedata.category(char) == "Cc" for char in secret):
        raise ValueError("API Key 格式无效：请检查长度和控制字符")
    return SecretStr(secret)


class _CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, validate_default=True)

    # Request-only credential: repr is masked, and serialization always omits
    # the field. Routes hand the SecretStr directly to the local secret store.
    api_key: Optional[SecretStr] = None

    @model_serializer(mode="wrap")
    def without_api_key(self, handler):
        data = handler(self)
        data.pop("api_key", None)
        return data


class ModelProfileCreate(_CredentialInput):

    name: str = Field(..., min_length=1, max_length=120)
    provider: str = Field(..., min_length=1, max_length=80)
    base_url: str = Field(..., min_length=1, max_length=4_000)
    model_id: str = Field(..., min_length=1, max_length=200)
    secret_env_ref: Optional[str] = Field(None, max_length=256)
    max_output_tokens: int = Field(DEFAULT_MAX_OUTPUT_TOKENS, ge=32, le=200_000)
    timeout_seconds: float = Field(DEFAULT_TIMEOUT_SECONDS, gt=0, le=900)
    thinking_mode: Literal["auto", "disabled", "enabled"] = "auto"
    input_price_per_million: float = Field(0.0, ge=0, le=1_000_000)
    output_price_per_million: float = Field(0.0, ge=0, le=1_000_000)
    currency: str = Field("CNY", min_length=1, max_length=12)
    is_active: bool = True

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return _clean_api_key(value, allow_blank=False)

    @field_validator("name", "provider", "model_id", "currency")
    @classmethod
    def strip_nonempty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空")
        return cleaned

    @field_validator("secret_env_ref")
    @classmethod
    def validate_model_secret_ref(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        reference = None
        if value is not None and value.strip():
            reference = validate_secret_env_name(value, namespace="model", label="模型 API 密钥引用")
        has_key = info.data.get("api_key") is not None
        if has_key and reference:
            raise ValueError("API Key 与环境变量引用只能填写一项")
        if not has_key and not reference:
            raise ValueError("请填写 API Key 或服务端密钥环境变量名")
        return reference


class ModelProfileUpdate(_CredentialInput):

    name: Optional[str] = Field(None, min_length=1, max_length=120)
    provider: Optional[str] = Field(None, min_length=1, max_length=80)
    base_url: Optional[str] = Field(None, min_length=1, max_length=4_000)
    model_id: Optional[str] = Field(None, min_length=1, max_length=200)
    secret_env_ref: Optional[str] = Field(None, max_length=256)
    max_output_tokens: Optional[int] = Field(None, ge=32, le=200_000)
    timeout_seconds: Optional[float] = Field(None, gt=0, le=900)
    thinking_mode: Optional[Literal["auto", "disabled", "enabled"]] = None
    input_price_per_million: Optional[float] = Field(None, ge=0, le=1_000_000)
    output_price_per_million: Optional[float] = Field(None, ge=0, le=1_000_000)
    currency: Optional[str] = Field(None, min_length=1, max_length=12)
    is_active: Optional[bool] = None

    @field_validator("api_key")
    @classmethod
    def validate_optional_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return _clean_api_key(value, allow_blank=True)

    @field_validator("name", "provider", "model_id", "currency")
    @classmethod
    def strip_optional_nonempty(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空")
        return cleaned

    @field_validator("secret_env_ref")
    @classmethod
    def validate_optional_model_secret_ref(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        reference = None
        if value is not None and value.strip():
            reference = validate_secret_env_name(value, namespace="model", label="模型 API 密钥引用")
        if reference and info.data.get("api_key") is not None:
            raise ValueError("API Key 与环境变量引用只能填写一项")
        # Blank inputs preserve the existing credential; opaque local secret
        # references are deliberately not accepted through this public field.
        return reference


class QuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=40)
    experiment: Optional[str] = Field(None, max_length=120)
    category: Optional[str] = Field(None, max_length=120)
    role: Optional[str] = Field(None, max_length=120)
    method: Optional[str] = Field(None, max_length=120)
    scenario: Optional[str] = Field(None, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=100_000)
    skills: list[str] = Field(default_factory=list, max_length=100)
    deliverable: Optional[str] = Field(None, max_length=2_000)
    gold_standard: Optional[str] = Field(None, max_length=10_000)
    metrics: Optional[str] = Field(None, max_length=2_000)
    risk: Optional[str] = Field(None, max_length=80)
    priority: Optional[str] = Field(None, max_length=40)

    @field_validator("code", "prompt")
    @classmethod
    def strip_question_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空")
        return cleaned


class QuestionSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=2_000)
    version: str = Field("1", min_length=1, max_length=80)
    questions: list[QuestionInput] = Field(..., min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def unique_question_codes(self) -> "QuestionSetCreate":
        codes = [item.code.casefold() for item in self.questions]
        if len(codes) != len(set(codes)):
            raise ValueError("同一题库内 question code 不能重复")
        return self


class PredictionTaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    dataset_id: Optional[str] = Field(None, max_length=120)
    dataset_version: Optional[str] = Field(None, max_length=128)
    runner: Literal["team_prompt", "team_full"] = "team_full"
    horizon: Literal["t1", "t3", "t5", "t7", "t15", "t30", "t60"] = "t3"
    concurrency: int = Field(2, ge=1, le=10)
    prompt_variant: str = Field("v0", min_length=1, max_length=120)


class QATaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    question_set_id: Optional[str] = Field(None, max_length=120)
    question_ids: list[str] = Field(default_factory=list, max_length=2_000)
    repeats: int = Field(1, ge=1, le=3)
    variants: list[Literal["pronoia", "raw"]] = Field(default_factory=lambda: ["pronoia"])
    scoring_mode: Literal["auto", "manual", "mixed"] = "auto"
    judge_profile_id: Optional[str] = Field(None, max_length=120)
    concurrency: int = Field(2, ge=1, le=8)

    @model_validator(mode="after")
    def validate_qa_config(self) -> "QATaskConfig":
        if self.enabled and not self.question_set_id:
            raise ValueError("启用问答测试时必须提供 question_set_id")
        if self.enabled and not self.variants:
            raise ValueError("启用问答测试时 variants 不能为空")
        if self.scoring_mode in {"auto", "mixed"} and self.enabled and not self.judge_profile_id:
            raise ValueError("自动或混合评分必须提供 judge_profile_id")
        return self


class EvaluationBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    profile_ids: list[str] = Field(..., min_length=1, max_length=30)
    auto_start: bool = True
    dry_run: bool = Field(
        False,
        description="只验证任务编排与页面流程；结果会明确标记 demo，且不会生成可进入 Arena 的回测 Run",
    )
    prediction: PredictionTaskConfig = Field(default_factory=PredictionTaskConfig)
    qa: QATaskConfig = Field(default_factory=QATaskConfig)

    @model_validator(mode="after")
    def validate_batch(self) -> "EvaluationBatchCreate":
        if len(self.profile_ids) != len(set(self.profile_ids)):
            raise ValueError("profile_ids 不能重复")
        if not self.prediction.enabled and not self.qa.enabled:
            raise ValueError("预测测试和问答测试至少启用一项")
        if self.prediction.enabled and not self.prediction.dataset_id:
            raise ValueError("启用预测测试时必须提供 dataset_id")
        return self


class ManualScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: int = Field(..., ge=0, le=30)
    evidence: int = Field(..., ge=0, le=20)
    method: int = Field(..., ge=0, le=15)
    reasoning: int = Field(..., ge=0, le=10)
    risk: int = Field(..., ge=0, le=10)
    usability: int = Field(..., ge=0, le=5)
    reproducibility: int = Field(..., ge=0, le=5)
    user_value: int = Field(..., ge=0, le=5)
    major_error: bool = False
    rationale: str = Field("", max_length=2_000)
    reviewer: Optional[str] = Field(None, max_length=120)
