"""配置子系统：Pydantic 模型树 + YAML 读写（设计文档 §4）。

- 字段与默认值与设计文档 §4 / 需求文档 第 10 节（出厂默认值表）字段级一致；
- ``load_config``：YAML 文件与内置默认值合并（未写字段回落默认值）；
- ``write_back``：全量重写 YAML，不保留手写注释 [B14]；
- S1a 约定校验：``auto_reply_limit >= 1``、端口 1-65535、
  ``ai.preferences`` 字段存在但 v1 恒空不使用 [N5]。

跨 SubStage 契约：控制台（S5b）读写配置、引擎各模块读取配置快照，
一律经由本模块的类型，不得绕过自行解析 YAML。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
)

# ---- 应用根目录（R6-2：路径一律锚定应用根，与当前工作目录无关）----


def app_root() -> Path:
    """应用根目录：环境变量 ``JOB_AGENT_HOME`` 优先，默认仓库根（src/job_agent 的上两级）。

    所有相对路径（配置/数据库/日志/浏览器 profile）均基于此解析，
    防止"换目录启动→静默新建空库→去重历史丢失→重复投递"[R6-2]。
    """
    env = os.environ.get("JOB_AGENT_HOME")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


def resolve_path(p: str | Path) -> Path:
    """相对路径基于应用根解析（绝对路径原样返回）[R6-2]。"""
    path = Path(p)
    return path if path.is_absolute() else app_root() / path


def default_config_path() -> Path:
    """默认配置文件位置（基于应用根，非当前工作目录）[R6-2]。"""
    return app_root() / "config.yaml"


# polling.schedule 的"与投递时段共用"标记（设计文档 §4 字面值）
SAME_AS_DELIVERY = "same_as_delivery"

# ---- 基础约束类型 ----

# 24 小时制时间字符串 HH:MM（如 "10:00"）；调度器（S2c）负责进一步解析
HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]

# TCP 端口范围
Port = Annotated[int, Field(ge=1, le=65535)]

# 星期：1=周一 … 7=周日（FR3.5 "工作日"=周一至周五，不区分节假日/调休）
Weekday = Annotated[int, Field(ge=1, le=7)]


def _check_range_pair(value: list[int]) -> list[int]:
    """[下限, 上限] 二元范围：恰好两个元素且下限 <= 上限。"""
    if len(value) != 2:
        raise ValueError("范围必须为恰好两个元素的列表：[下限, 上限]")
    if value[0] > value[1]:
        raise ValueError("范围下限不能大于上限")
    return value


def _check_positive_range_pair(value: list[int]) -> list[int]:
    """安全类范围（拟人延时等）：双值 >= 1，防"0,0"悄悄关掉拟人节奏 [R6-1]。"""
    checked = _check_range_pair(value)
    if checked[0] < 1:
        raise ValueError("安全类范围下限必须 >= 1（0 会关闭拟人节奏保护）")
    return checked


RangePair = Annotated[list[int], AfterValidator(_check_range_pair)]
SafeRangePair = Annotated[list[int], AfterValidator(_check_positive_range_pair)]


class StrictModel(BaseModel):
    """配置模型公共基类：未知字段直接报错（防拼写错误被静默忽略）。"""

    model_config = ConfigDict(extra="forbid")


# ---- site 节 ----


class SiteConfig(StrictModel):
    """站点与浏览器会话（设计文档 §2/§5.3）。枚举字段用 Literal，拼写错误在加载期暴露 [R6-1]。"""

    adapter: Literal["zhipin", "mock"] = "zhipin"  # 真实 / 模拟测试（§12）
    browser_strategy: Literal["S1b_patchright"] = "S1b_patchright"  # v1 定型路线（§5.3）
    channel: Literal["chrome", "msedge"] = "chrome"  # 本机浏览器通道
    headless: bool = False  # 需可见窗口（登录态/风控检测依赖页面可见性）


# ---- target 节 ----


class TargetConfig(StrictModel):
    """求职目标（值来自首次引导，属个人信息；不入公开文档）。"""

    city: str = ""  # 求职目标城市；薪资维度 v1 不做 [R13]


# ---- delivery 节 ----


class RunSchedule(StrictModel):
    """运行时段（投递默认时段；polling.schedule 可独立配置）。"""

    weekdays: list[Weekday] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    start: HHMM = "10:00"
    end: HHMM = "16:00"


class PollingConfig(StrictModel):
    """HR 消息轮询（FR3.5：默认共用投递时段，可独立配置）。"""

    interval_range_min: RangePair = Field(default_factory=lambda: [3, 5])  # 轮询间隔（分钟，随机）
    schedule: RunSchedule | None = None  # None = 与投递时段共用（YAML 写 same_as_delivery）

    @field_validator("schedule", mode="before")
    @classmethod
    def _map_same_as_delivery(cls, value: Any) -> Any:
        """YAML 中的 ``schedule: same_as_delivery`` 标记等价于 None（共用投递时段）。"""
        if isinstance(value, str) and value.strip() == SAME_AS_DELIVERY:
            return None
        return value

    @field_serializer("schedule")
    def _serialize_schedule(self, value: RunSchedule | None) -> Any:
        """写回时 None 还原为 same_as_delivery 标记，保持 §4 字面形态。"""
        if value is None:
            return SAME_AS_DELIVERY
        return value.model_dump(mode="json")


class LoginPatrolConfig(StrictModel):
    """登录态周期检测（FR1.3）。"""

    every_loop: bool = True  # 每任务循环前检测
    interval_min: int = 10  # 循环中每 N 分钟检测


class DedupConfig(StrictModel):
    """同公司去重（FR3.6）。"""

    mode: Literal["permanent_blacklist", "cooldown"] = "permanent_blacklist"
    cooldown_days: int = 30


class BrowseLimitsConfig(StrictModel):
    """浏览上限（FR3.4；初值保守，X4/试运行校准后修订）。硬下限 >= 1 [R6-1]。"""

    detail_hourly: int = Field(default=40, ge=1)
    detail_daily: int = Field(default=200, ge=1)
    search_page_hourly: int = Field(default=30, ge=1)


class MinIntervalConfig(StrictModel):
    """操作级最小间隔（H2，随机延时之外的下限约束）。硬下限 >= 1 [R6-1]；
    低于实测验证值（30s）时由 safety_warnings 提示。"""

    detail: int = Field(default=30, ge=1)  # 打开职位详情 >= N 秒
    search: int = Field(default=30, ge=1)  # 搜索列表加载/翻页/滚动加载 >= N 秒


class BrowseRampupFirstDay(StrictModel):
    """试运行第 1 天浏览上限（H2 档位）。"""

    detail_hourly: int = 15
    detail_daily: int = 60


class BrowseRampupConfig(StrictModel):
    """试运行浏览放量（H2：无风控日逐日翻倍至配置值）。"""

    enabled: bool = True
    first_day: BrowseRampupFirstDay = Field(default_factory=BrowseRampupFirstDay)
    double_per_clean_day: bool = True


class RampUpConfig(StrictModel):
    """首次实投逐步放量（FR3.9）。"""

    enabled_days: int = Field(default=3, ge=1)  # 有效运行日数（G10 口径）
    daily_limit: int = Field(default=10, ge=1)  # 放量期每日投递上限
    auto_recover: bool = True  # 期满自动恢复配置值 + 低级通知


class DeliveryConfig(StrictModel):
    """投递流水线参数（设计文档 §4 delivery 节）。数值约束 [R6-1]。"""

    run_schedule: RunSchedule = Field(default_factory=RunSchedule)
    hourly_limit: int = Field(default=25, ge=0)  # 每小时投递上限（滑动 60 分钟窗口）
    daily_limit: int = Field(default=100, ge=0)  # 每日投递上限（Asia/Shanghai 自然日）
    action_delay_range_s: SafeRangePair = Field(default_factory=lambda: [8, 20])  # 拟人随机延时
    polling: PollingConfig = Field(default_factory=PollingConfig)
    login_patrol: LoginPatrolConfig = Field(default_factory=LoginPatrolConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    browse_limits: BrowseLimitsConfig = Field(default_factory=BrowseLimitsConfig)
    min_interval_s: MinIntervalConfig = Field(default_factory=MinIntervalConfig)
    browse_rampup: BrowseRampupConfig = Field(default_factory=BrowseRampupConfig)
    dry_run: bool = True  # 试运行模式：send_* 仅记录不发送（首次安装默认开启，FR3.9）
    ramp_up: RampUpConfig = Field(default_factory=RampUpConfig)


# ---- messaging 节 ----


class MessagingConfig(StrictModel):
    """消息自动化参数（设计文档 §4 messaging 节 / §8）。"""

    # 单会话自动发送消息上限（含招呼语，不含附件简历）；下限校验 >=1 [A2]
    auto_reply_limit: int = Field(default=2, ge=1)
    # 附件简历自动投递每会话上限（A2 硬性规则：0-1）[R6-1]
    resume_auto_send_limit_per_conv: int = Field(default=1, ge=0, le=1)
    passive_greeting_enabled: bool = False  # 被动招呼（FR3.7，默认关）
    positive_response_resume_enabled: bool = False  # 正向回应自动投简历（FR3.8，默认关）
    outbox_timeout_min: int = Field(default=60, ge=1)  # 待发箱超时提醒（FR6.4）


# ---- ai 节 ----


class PreferenceRule(StrictModel):
    """AI 偏好规则（V2-7 预留；v1 恒空 [N5]：空表行为与无字段一致）。"""

    text: str  # 偏好文本
    strength: Literal["hard", "soft"]  # 硬性=明确违反才 skip；软性=仅影响排序


class AIConfig(StrictModel):
    """AI 服务（OpenAI 兼容协议，FR9）。"""

    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""  # 明文存储，用户自担风险并妥善保管 [R2]
    model: str = "deepseek-chat"
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    proxy: str = ""  # 可选 HTTP 代理（默认留空，FR9.4）
    min_score: int = Field(default=60, ge=0, le=100)  # 投递判定阈值（L1 临时默认）
    neutral_invest: bool = True  # neutral 是否允许投递（FR4.2，默认允许）
    daily_budget_calls: int = Field(default=1000, ge=1)  # AI 日调用预算（B13）
    preferences: list[PreferenceRule] = Field(default_factory=list)  # v1 恒空 [V2-7 预留]


# ---- match 节 ----


class ExcludeGroup(StrictModel):
    """排除组（预置组+自定义组统一结构 [G9]；v1 仅加载 kind=preset）。"""

    id: str
    kind: Literal["preset", "custom"] = "preset"
    enabled: bool = False  # 预置组默认全关（FR3.2）
    keywords: list[str] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)  # company_name / jd_text / tags（V2-7 可扩展）


def _default_exclude_groups() -> list[ExcludeGroup]:
    """预置排除组（设计文档 §4 字面值；判定为启发式，存在误杀可能）。"""
    return [
        ExcludeGroup(
            id="outsourcing",
            keywords=["外包", "人力资源服务", "软件外包"],
            scope=["company_name", "jd_text", "tags"],
        ),
        ExcludeGroup(id="headhunter", keywords=["猎头"], scope=["tags", "jd_text"]),
        ExcludeGroup(
            id="trainee_intern_parttime",
            keywords=["实习", "兼职", "培训生"],
            scope=["jd_text", "tags"],
        ),
    ]


class MatchConfig(StrictModel):
    """匹配排除规则（FR3.2）。"""

    exclude_groups: list[ExcludeGroup] = Field(default_factory=_default_exclude_groups)
    company_blacklist: list[str] = Field(default_factory=list)


# ---- notify 节 ----


NotifyLevel = Literal["high", "medium", "low"]


def _default_feishu_levels() -> list[NotifyLevel]:
    return ["high", "medium"]


class FeishuConfig(StrictModel):
    """飞书群机器人 Webhook（主通知通道，FR7.1）。

    levels 必须包含 high——FR7.2 规定"高级告警邮件+飞书必推"，
    去掉 high 会让登录失效/风控熔断等关键告警静默 [R6-1]。
    """

    webhook: str = ""
    secret: str = ""  # 签名校验密钥
    levels: list[NotifyLevel] = Field(default_factory=_default_feishu_levels)

    @field_validator("levels")
    @classmethod
    def _levels_must_contain_high(cls, value: list[NotifyLevel]) -> list[NotifyLevel]:
        if "high" not in value:
            raise ValueError("feishu.levels 必须包含 high（FR7.2：高级告警飞书必推）")
        return value


class EmailConfig(StrictModel):
    """邮件 SMTP（仅推送 high 级，FR7.1）。"""

    smtp_host: str = ""
    smtp_port: Port = 465
    user: str = ""
    password: str = ""
    to: str = ""


class DndConfig(StrictModel):
    """免打扰时段（B11；可跨午夜，如 23:00-07:30）。"""

    enabled: bool = False
    start: HHMM = "23:00"
    end: HHMM = "07:30"
    high_bypass: bool = True  # 高级告警不受免打扰限制


class NotifyConfig(StrictModel):
    """通知渠道与免打扰（设计文档 §4 notify 节 / §11）。"""

    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    dnd: DndConfig = Field(default_factory=DndConfig)


# ---- console / data 节 ----


class ConsoleConfig(StrictModel):
    """本地 Web 控制台（FR11：仅本机监听）。

    host 是安全约束（S1 加固第 1 项），不开放配置——0.0.0.0 会把控制台
    暴露到局域网 [R6-1]。
    """

    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: Port = 8000


class DataConfig(StrictModel):
    """数据存储（SQLite 单库，NFR6）。"""

    db_path: Path = Path("data/agent.db")

    @field_serializer("db_path")
    def _serialize_db_path(self, value: Path) -> str:
        """写回 YAML 统一为 posix 分隔符（Windows 下 str(Path) 会得到反斜杠）。"""
        return value.as_posix()


# ---- 根模型 ----


class Config(StrictModel):
    """根配置模型（设计文档 §4 全字段树）。"""

    site: SiteConfig = Field(default_factory=SiteConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    messaging: MessagingConfig = Field(default_factory=MessagingConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    match: MatchConfig = Field(default_factory=MatchConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    console: ConsoleConfig = Field(default_factory=ConsoleConfig)
    data: DataConfig = Field(default_factory=DataConfig)


# ---- 读写 ----

# 实测验证过的安全节奏基线（X4 两轮实测：详情间隔 41-53s；H2 下限 30s）——低于时警告 [R6-1]
_VERIFIED_MIN_INTERVAL_S = 30
_VERIFIED_DELAY_MAX_S = 20


def safety_warnings(config: Config) -> list[str]:
    """安全相关参数低于实测验证值时的警告清单 [R6-1]。

    不拒绝（保留调参余地），但 __main__ 启动时逐条打印 WARNING，
    控制台配置页（S5b）同样展示。
    """
    warns: list[str] = []
    d = config.delivery
    if d.min_interval_s.detail < _VERIFIED_MIN_INTERVAL_S:
        warns.append(
            f"min_interval_s.detail={d.min_interval_s.detail}s 低于实测验证节奏 "
            f"({_VERIFIED_MIN_INTERVAL_S}s)，风控风险升高"
        )
    if d.min_interval_s.search < _VERIFIED_MIN_INTERVAL_S:
        warns.append(
            f"min_interval_s.search={d.min_interval_s.search}s 低于实测验证节奏 "
            f"({_VERIFIED_MIN_INTERVAL_S}s)，风控风险升高"
        )
    if d.action_delay_range_s[1] < _VERIFIED_DELAY_MAX_S:
        warns.append(
            f"action_delay_range_s 上限={d.action_delay_range_s[1]}s 低于默认拟人节奏 "
            f"({_VERIFIED_DELAY_MAX_S}s)"
        )
    return warns


def load_config(path: str | Path | None = None) -> Config:
    """从 YAML 加载配置并与内置默认值合并。

    - ``path`` 缺省：默认位置 ``<应用根>/config.yaml`` [R6-2]；
      显式相对路径同样基于应用根解析（绝对路径原样）；
    - 文件不存在：返回纯内置默认值（首次运行向导的起点，不报错）；
    - 文件存在：解析 YAML，未出现的字段/分节回落内置默认值；
    - 结构或取值非法：抛出 ``pydantic.ValidationError``；根节点非映射抛 ``ValueError``。
    """
    file = resolve_path(path) if path is not None else default_config_path()
    if not file.exists():
        return Config()
    with file.open("r", encoding="utf-8") as fh:
        raw: Any = yaml.safe_load(fh)
    if raw is None:  # 空文件等价于"全部默认值"
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"配置文件根节点必须是映射（key: value），实际为 {type(raw).__name__}: {file}"
        )
    return Config.model_validate(raw)


# 写回文件头提示（全量重写产物自带，非保留用户注释）
_GENERATED_NOTE = (
    "# 本文件由 job-agent 全量重写生成（控制台保存配置时）。\n"
    "# 注意：手写注释不会被保留——请以 config.example.yaml 为注释参照。\n\n"
)


def dump_config_yaml(config: Config) -> str:
    """序列化为完整 YAML 文本（全字段快照，含默认值；供写回与控制台预览）。"""
    data: dict[str, Any] = config.model_dump(mode="json")
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )


def write_back(config: Config, path: str | Path) -> None:
    """将配置全量重写回 YAML 文件 [B14]。

    - 不保留文件中已有的任何手写注释（页面提示"手改注释会被覆盖"）；
    - 先写同目录临时文件再原子替换，避免写一半损坏配置。
    """
    file = Path(path)
    text = _GENERATED_NOTE + dump_config_yaml(config)
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(file)
