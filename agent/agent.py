"""Agent端：独立观测循环 + 决策循环"""
from openai import OpenAI, BadRequestError
import base64
import copy
import hashlib
import random
import shlex
import time
import re
import uuid
import threading
import json
import os
import shutil
import signal
import subprocess
import html
import urllib.request
from pathlib import Path
from datetime import datetime
from typing import Optional, Literal, Callable, List, Any, Tuple
from dataclasses import dataclass, asdict

from agent.frame_filter import (
    ExactFrameDeduper,
    FrameFilterConfig,
    summarize_frame_filter_telemetry_file,
)
from agent.state_record import compute_state_delta as _compute_state_delta_impl
from agent.grading_prompt import GRADER_SYSTEM_PROMPT, build_prompt as _build_self_reward_prompt
from agent.navigation_completion import NavigationClaimClient
from agent.llm_request_gate import LLMRequestGateConfig, SharedLLMRequestGate


@dataclass
class Action:
    """动作指令"""
    type: Literal[
        "exec",
        "skip",
        "stop_execute",
        "stop_observe",
        "start_observe",
        "claim_done",
    ]
    content: Optional[str] = None
    observe_after_sec: Optional[float] = None
    action_id: Optional[str] = None
    target_action_id: Optional[str] = None


ACTION_TOOL_NAME = "minecraft_action"
ACTION_PROTOCOLS = {"xml", "tool_calls"}


class _AssistantMessage:
    """Small protocol-neutral assistant message wrapper."""

    def __init__(self, payload: dict):
        self._payload = copy.deepcopy(payload)
        for key, value in self._payload.items():
            setattr(self, key, value)

    def model_dump(self) -> dict:
        payload = copy.deepcopy(self._payload)
        for key, value in vars(self).items():
            if key != "_payload":
                payload[key] = copy.deepcopy(value)
        return payload


def _is_context_overflow_error(exc: BaseException) -> bool:
    """Detect whether an LLM exception was caused by context-length overflow.

    Recognizes both real-OpenAI BadRequestError shapes and ROLL's
    McbotsEnvManager 400 response (which sets error.code = "context_length_exceeded").
    Falls back to substring matching for providers that only encode the reason
    in the error message.
    """
    if getattr(exc, "code", None) == "context_length_exceeded":
        return True
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error_obj = body.get("error")
        if isinstance(error_obj, dict) and error_obj.get("code") == "context_length_exceeded":
            return True
    msg = str(exc)
    if "context_length_exceeded" in msg or "Prompt too long" in msg:
        return True
    return False


class Agent:
    """
    Agent端：观测环境 + VLM决策
    
    两个独立线程：
    1. 观测线程：拉取state → 插入messages（可控制）
    2. 决策线程：处理messages → VLM → 发送动作
    """
    
    def __init__(
        self,
        env_getter: Callable[[int], List[Any]],  # 获取新states的函数，传入after_index
        action_sender: Callable[[Action], None],
        api_key: str,
        base_url: str,
        model: str,
        servername: str,
        agentname: str,
        api_protocol: str = "chat_completions",
        action_protocol: str = "xml",
        observe_interval: float = 1.0,
        system_prompt: str = "",
        sampling_config_path: Optional[str] = None,
        max_conversation_rounds: int = 100,
        max_images_in_context: int = 0,
        request_extra_body: Optional[dict] = None,
        initial_user_message: str = "",
        eval_mode: bool = False,
        display: str = ":0",
        enable_video_recording: bool = True,
        video_fps: int = 24,
        video_resolution: str = "",
        video_filename: str = "session.mp4",
        video_codec: str = "libx264",
        video_crf: int = 23,
        video_preset: str = "veryfast",
        enable_frame_dedup: bool = True,
        frame_dedup_force_keep_after_event: int = 1,
        enable_approx_frame_dedup: bool = False,
        approx_frame_diff_threshold: float = 1.5,
        approx_frame_peak_tile_guard_threshold: float = 6.0,
        approx_frame_signature_width: int = 32,
        approx_frame_signature_height: int = 18,
        approx_frame_tile_cols: int = 4,
        approx_frame_tile_rows: int = 3,
        enable_approx_center_roi_weighting: bool = False,
        approx_center_roi_width_ratio: float = 0.5,
        approx_center_roi_height_ratio: float = 0.5,
        approx_center_roi_weight: float = 2.0,
        enable_adaptive_frame_budget: bool = False,
        adaptive_frame_idle_min_keep_interval_sec: float = 4.0,
        adaptive_frame_active_min_keep_interval_sec: float = 1.0,
        adaptive_frame_event_boost_window_sec: float = 4.0,
        adaptive_frame_bypass_local_peak_change: bool = True,
        adaptive_frame_boost_on_visual_change: bool = True,
        frame_keepalive_sec: float = 8.0,
        enable_frame_filter_telemetry: bool = True,
        max_llm_request_successes: int = 0,
        max_llm_request_failures: int = 0,
        max_consecutive_llm_failures: int = 0,
        capture_now_getter: Optional[Callable[[], Any]] = None,  # 主动即时抓图
        runtime_state_getter: Optional[Callable[[], Optional[dict]]] = None,  # 拉一次 mcapi state（用于全景 gating）
        panorama_capturer: Optional[Callable[[], Optional[bytes]]] = None,  # 触发一次全景截图，返回 JPEG bytes
        enable_panorama: bool = False,  # 全景观测总开关，关闭时 panorama 路径整段被跳过
        roll_notify_url: Optional[str] = None,  # ROLL McbotsEnvManager URL for /window_complete, /episode_done
        llm_request_timeout_sec: float = 120.0,  # OpenAI client 每次请求超时
        llm_max_retries: int = 2,  # OpenAI client 自动重试次数
        llm_watchdog_interval_sec: float = 30.0,  # inflight 日志心跳间隔（<=0 关闭）
        llm_gate_dir: Optional[str] = None,
        llm_gate_max_inflight: int = 0,
        llm_gate_starts_per_minute: float = 0.0,
        llm_gate_initial_burst: int = 0,
        llm_429_max_retries: int = 0,
        llm_429_backoff_base_sec: float = 5.0,
        llm_429_backoff_max_sec: float = 60.0,
        llm_429_backoff_jitter: float = 0.2,
        default_observe_enabled: bool = True,  # 启动时是否默认持续观测截图流
        allow_model_observe_toggle: bool = True,  # 是否允许模型自己 start_observe / stop_observe 切换流模式
        auto_summarize_token_threshold: int = 0,  # <=0 关闭；>0 时按 total_tokens 触发自动总结
        auto_summarize_turn_threshold: int = 0,  # <=0 关闭；>0 时按窗口内 assistant turn 数触发总结
        auto_summary_profile: str = "general",
        enable_self_reward: bool = False,  # rollout 结束前让 agent 给自己每次 response 打分
        self_reward_share_system_prompt: bool = True,  # True: 复用 agent system prompt (KV cache 友好); False: 独立 GRADER_SYSTEM_PROMPT
        state_snapshot_provider: Optional[Callable[[], Optional[dict]]] = None,  # 可选: 评分时拉取的额外 state 快照
        navigation_claim_client: Optional[NavigationClaimClient] = None,
    ):
        """
        Args:
            env_getter: 获取环境新states的函数，接受after_index参数
            action_sender: 发送动作到环境的函数
            api_key: OpenAI API key
            base_url: API base URL
            model: 模型名称
            servername: MC服务器容器名
            agentname: Agent容器名
            api_protocol: provider API transport (chat_completions or responses)
            action_protocol: action wire format (xml or native tool_calls)
            observe_interval: 观测间隔（秒）
            system_prompt: 系统提示词
            sampling_config_path: 采样参数配置文件路径（JSON），默认为agent/sampling_config.json
            max_conversation_rounds: 最大对话轮数，超过后触发 auto-summarize
            initial_user_message: 启动时注入的首条 user 消息（任务指令）
            eval_mode: 是否运行在评测模式（影响上下文保留策略）
            display: 录屏使用的 DISPLAY（例如 :1）
            enable_video_recording: 是否启用录屏
            video_fps: 录屏帧率
            video_resolution: 录屏分辨率（如 1280x720，空字符串表示使用显示默认）
            video_filename: 录屏文件名
            video_codec: ffmpeg 视频编码器
            video_crf: libx264 质量参数（越小质量越高）
            video_preset: ffmpeg 编码预设
            enable_frame_dedup: 是否启用截图完全相同帧去重（仅影响发给模型的截图）
            frame_dedup_force_keep_after_event: 重要事件后强制保留的后续截图数量
            enable_approx_frame_dedup: 是否启用近似重复帧去重（需要 Pillow，默认关闭）
            approx_frame_diff_threshold: 低分辨率灰度图平均差异阈值（越低越保守）
            approx_frame_peak_tile_guard_threshold: 局部 tile 差异保底阈值（保小物体/局部变化）
            approx_frame_signature_width: 近似去重灰度签名宽度
            approx_frame_signature_height: 近似去重灰度签名高度
            approx_frame_tile_cols: 局部峰值保底 tile 列数
            approx_frame_tile_rows: 局部峰值保底 tile 行数
            enable_approx_center_roi_weighting: 是否启用中心视野 ROI 加权（近似去重）
            approx_center_roi_width_ratio: 中心 ROI 宽度占比（相对签名宽度）
            approx_center_roi_height_ratio: 中心 ROI 高度占比（相对签名高度）
            approx_center_roi_weight: 中心 ROI 权重（边缘权重固定为1）
            enable_adaptive_frame_budget: 是否启用自适应截图预算（时间冷却，默认关闭）
            adaptive_frame_idle_min_keep_interval_sec: 空闲阶段最小保留间隔
            adaptive_frame_active_min_keep_interval_sec: 活跃阶段（事件窗口）最小保留间隔
            adaptive_frame_event_boost_window_sec: 事件后活跃窗口时长
            adaptive_frame_bypass_local_peak_change: 局部峰值变化是否绕过预算冷却
            adaptive_frame_boost_on_visual_change: 视觉变化是否触发后续 active 窗口
            enable_frame_filter_telemetry: 是否记录截图过滤 telemetry（JSONL）
            max_llm_request_successes: rollout 内 LLM 请求成功次数上限（<=0 表示不限制）
            max_llm_request_failures: rollout 内 LLM 请求失败次数上限（<=0 表示不限制）
        """
        self.env_getter = env_getter
        self.capture_now_getter = capture_now_getter
        self.runtime_state_getter = runtime_state_getter
        self.panorama_capturer = panorama_capturer
        self.enable_panorama = bool(enable_panorama)
        self.roll_notify_url = roll_notify_url
        self.action_sender = action_sender
        self.navigation_claim_client = navigation_claim_client
        self.stop_reason: Optional[str] = None
        self.llm_request_timeout_sec = max(float(llm_request_timeout_sec), 1.0)
        self.llm_max_retries = max(int(llm_max_retries), 0)
        self.llm_watchdog_interval_sec = float(llm_watchdog_interval_sec)
        self.llm_429_max_retries = max(int(llm_429_max_retries), 0)
        self.llm_429_backoff_base_sec = max(float(llm_429_backoff_base_sec), 0.0)
        self.llm_429_backoff_max_sec = max(
            float(llm_429_backoff_max_sec), self.llm_429_backoff_base_sec
        )
        self.llm_429_backoff_jitter = min(
            max(float(llm_429_backoff_jitter), 0.0), 1.0
        )
        if self.llm_429_max_retries > 0 and self.llm_max_retries > 0:
            raise ValueError(
                "llm_max_retries must be 0 when explicit 429 retries are enabled"
            )
        self.llm_request_gate: Optional[SharedLLMRequestGate] = None
        if llm_gate_dir:
            self.llm_request_gate = SharedLLMRequestGate(
                LLMRequestGateConfig(
                    directory=Path(llm_gate_dir),
                    max_inflight=int(llm_gate_max_inflight),
                    starts_per_minute=float(llm_gate_starts_per_minute),
                    initial_burst=int(llm_gate_initial_burst),
                )
            )
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=self.llm_request_timeout_sec,
            max_retries=self.llm_max_retries,
        )
        self.model = model
        normalized_protocol = (api_protocol or "chat_completions").strip().lower()
        if normalized_protocol not in {"chat_completions", "responses"}:
            raise ValueError(
                "api_protocol must be 'chat_completions' or 'responses'"
            )
        self.api_protocol = normalized_protocol
        normalized_action_protocol = (action_protocol or "xml").strip().lower()
        if normalized_action_protocol not in ACTION_PROTOCOLS:
            raise ValueError("action_protocol must be 'xml' or 'tool_calls'")
        self.action_protocol = normalized_action_protocol
        self.previous_response_id: Optional[str] = None
        self.responses_previous_response_id_supported = True
        self.servername = servername
        self.agentname = agentname
        self.observe_interval = observe_interval
        self.system_prompt = system_prompt
        self.request_extra_body = request_extra_body or {}
        self.initial_user_message = (initial_user_message or "").strip()
        self.eval_mode = bool(eval_mode)
        self._initial_user_message_id = uuid.uuid4().hex if self.initial_user_message else ""
        self.display = (display or ":0").strip() or ":0"
        self.enable_video_recording = bool(enable_video_recording)
        self.video_fps = max(int(video_fps), 1)
        self.video_resolution = self._normalize_video_resolution(video_resolution)
        self.video_filename = (video_filename or "session.mp4").strip() or "session.mp4"
        self.video_codec = (video_codec or "libx264").strip() or "libx264"
        self.video_crf = int(video_crf)
        self.video_preset = (video_preset or "veryfast").strip() or "veryfast"
        self.enable_frame_filter_telemetry = bool(enable_frame_filter_telemetry)
        self.max_llm_request_successes = int(max_llm_request_successes)
        self.max_llm_request_failures = int(max_llm_request_failures)
        self.max_consecutive_llm_failures = int(max_consecutive_llm_failures)
        self.llm_request_total = 0
        self.llm_request_success = 0
        self.llm_request_failed = 0
        self.consecutive_llm_failures = 0
        self.frame_filter = ExactFrameDeduper(
            FrameFilterConfig(
                enabled=bool(enable_frame_dedup),
                force_keep_after_event_count=max(int(frame_dedup_force_keep_after_event), 0),
                enable_approx_dedup=bool(enable_approx_frame_dedup),
                approx_signature_width=max(int(approx_frame_signature_width), 1),
                approx_signature_height=max(int(approx_frame_signature_height), 1),
                approx_diff_threshold=float(approx_frame_diff_threshold),
                approx_peak_tile_guard_threshold=float(approx_frame_peak_tile_guard_threshold),
                approx_tile_cols=max(int(approx_frame_tile_cols), 1),
                approx_tile_rows=max(int(approx_frame_tile_rows), 1),
                enable_approx_center_roi_weighting=bool(enable_approx_center_roi_weighting),
                approx_center_roi_width_ratio=float(approx_center_roi_width_ratio),
                approx_center_roi_height_ratio=float(approx_center_roi_height_ratio),
                approx_center_roi_weight=float(approx_center_roi_weight),
                enable_adaptive_budget=bool(enable_adaptive_frame_budget),
                adaptive_idle_min_keep_interval_sec=float(adaptive_frame_idle_min_keep_interval_sec),
                adaptive_active_min_keep_interval_sec=float(adaptive_frame_active_min_keep_interval_sec),
                adaptive_event_boost_window_sec=float(adaptive_frame_event_boost_window_sec),
                adaptive_bypass_local_peak_change=bool(adaptive_frame_bypass_local_peak_change),
                adaptive_boost_on_visual_change=bool(adaptive_frame_boost_on_visual_change),
                keepalive_max_interval_sec=float(frame_keepalive_sec),
            )
        )

        # 加载采样参数
        if sampling_config_path is None:
            # 默认使用agent目录下的sampling_config.json
            agent_dir = Path(__file__).parent
            sampling_config_path = str(agent_dir / "sampling_config.json")

        self.sampling_params = self._load_sampling_config(sampling_config_path)
        print(f"📋 Loaded sampling config: {self.sampling_params}")
        print(
            "🧠 Frame filter: "
            f"exact_dedup={'on' if enable_frame_dedup else 'off'} "
            f"approx_dedup={'on' if enable_approx_frame_dedup else 'off'} "
            f"approx_sig={max(int(approx_frame_signature_width), 1)}x{max(int(approx_frame_signature_height), 1)} "
            f"approx_tiles={max(int(approx_frame_tile_cols), 1)}x{max(int(approx_frame_tile_rows), 1)} "
            f"center_roi={'on' if enable_approx_center_roi_weighting else 'off'} "
            f"roi_box={float(approx_center_roi_width_ratio):.2f}x{float(approx_center_roi_height_ratio):.2f} "
            f"roi_weight={float(approx_center_roi_weight):.2f} "
            f"approx_diff_threshold={float(approx_frame_diff_threshold):.2f} "
            f"peak_tile_guard={float(approx_frame_peak_tile_guard_threshold):.2f} "
            f"adaptive_budget={'on' if enable_adaptive_frame_budget else 'off'} "
            f"budget_idle={float(adaptive_frame_idle_min_keep_interval_sec):.2f}s "
            f"budget_active={float(adaptive_frame_active_min_keep_interval_sec):.2f}s "
            f"boost_window={float(adaptive_frame_event_boost_window_sec):.2f}s "
            f"visual_boost={'on' if adaptive_frame_boost_on_visual_change else 'off'} "
            f"force_keep_after_event={max(int(frame_dedup_force_keep_after_event), 0)} "
            f"keepalive={float(frame_keepalive_sec):.1f}s "
            f"telemetry={'on' if self.enable_frame_filter_telemetry else 'off'}"
        )
        print(
            "🧮 LLM request limits: "
            f"max_successes={self.max_llm_request_successes} "
            f"max_failures={self.max_llm_request_failures} "
            f"max_consecutive_failures={self.max_consecutive_llm_failures}"
        )
        print(f"🧭 Context mode: eval_mode={'on' if self.eval_mode else 'off'}")
        print(f"🔌 Model API protocol: {self.api_protocol}")

        # 上下文管理
        self.max_conversation_rounds = max_conversation_rounds
        self.max_images_in_context = max_images_in_context  # 0 = 不限制

        # 对话历史：
        # - conversation_history: 当前发给LLM的活动窗口（可裁剪/重置）
        # - full_message_history: 完整记录（运行中追加到 messages.jsonl，不裁剪）
        self.conversation_history = []
        self.full_message_history = []
        self.messages_lock = threading.RLock()
        self.message_revision = 0  # 每次conversation发生变更都自增
        self.context_reset_count = 0

        # 自动总结（token 或 turn 阈值任一命中即触发）
        self.auto_summarize_token_threshold = max(int(auto_summarize_token_threshold), 0)
        self.auto_summarize_turn_threshold = max(int(auto_summarize_turn_threshold), 0)
        self.auto_summary_profile = (auto_summary_profile or "general").strip().lower()
        self.awaiting_summary = False
        # Reason that triggered the current pending auto-summarize:
        #   "token_threshold" — recent response's total_tokens exceeded the threshold
        #   "image_limit"     — active context image count exceeded max_images_in_context
        self.pending_auto_summarize_reason: Optional[str] = None
        self.last_prompt_tokens = 0
        self.last_total_tokens = 0
        self.summarize_count = 0
        self.pending_auto_summarize_total_tokens: Optional[int] = None

        # Self-rewarding: per-window grading. Fires synchronously at each window
        # boundary (summary reset, hard reset, end-of-rollout) as an out-of-band
        # one-shot LLM call over a snapshot of the current window. Main
        # conversation is never polluted; each completed window is saved to its
        # own self_reward_{N}.json file next to messages.json.
        self.enable_self_reward = bool(enable_self_reward)
        self.self_reward_share_system_prompt = bool(self_reward_share_system_prompt)
        self.self_reward_in_flight = False  # guard against reentrancy during a reward call
        self.self_reward_window_count = 0  # number of windows graded so far (next file index)
        self.self_reward_history: list[dict] = []  # in-memory list of completed window rewards
        # Per-window spec_criteria evolution. Each entry: {"window": N, "emitted": str|None, "effective": str|None}.
        # `emitted` is what the grader returned in this window; `effective` is the active spec after inheritance
        # (= emitted if not empty, else carried over from the most recent prior `effective`).
        self.spec_criteria_history: list[dict] = []
        self.state_snapshot_provider = state_snapshot_provider  # callable () -> raw state dict (or None)
        # Per-window state record. Built up by _reset_window_state_record (window_initial)
        # + _record_turn_state (one pre-action snapshot per assistant turn). Per-turn deltas
        # are computed in turn order at grading time. Consumed by _run_out_of_band_self_reward.
        self._window_state_record: Optional[dict] = None  # {"window_initial":..., "turns": [{"action","snapshot"}]}

        # 观测控制
        self.observe_enabled = bool(default_observe_enabled)
        self.allow_model_observe_toggle = bool(allow_model_observe_toggle)
        self.observe_lock = threading.Lock()

        self.running = False
        self._frame_filter_summary_written = False

        # 记录相关
        self.record_dir = None  # 记录目录路径
        self.screenshot_dir = None  # 截图目录路径
        self.messages_file = None  # 退出后生成的兼容 messages.json 文件路径
        self.messages_journal_file = None  # 运行中增量 messages.jsonl 文件路径
        self._messages_journal_handle = None
        self._messages_journal_next_seq = 0
        self.frame_filter_telemetry_file = None  # 帧过滤 telemetry 文件路径（JSONL）
        self.frame_filter_summary_file = None  # 帧过滤 summary 文件路径（JSON）
        self.screenshot_counter = 0  # 截图计数器
        self.record_lock = threading.Lock()  # 保护文件写入
        self.video_lock = threading.Lock()  # 保护录屏进程管理
        self.video_file = None  # 录屏文件路径
        self.video_capture_file = None  # ffmpeg 实际写入路径（可能是中间容器）
        self.video_proc = None  # ffmpeg 录屏进程
        self.llm_inflight = False
        self.staging_buffer = []  # in-flight期间暂存的消息: (arrive_ts, seq, msg)
        self._staging_seq = 0
        self.kept_image_seq = 0  # 发给模型的图片序号（仅保留帧）
        self.committed_image_seq = 0  # 已真正写入conversation_history的图片序号
        self.pending_exec_result = False
        self.pending_exec_observe_deadline = 0.0
        self.pending_paused_observe_after_deadline = 0.0
        self.pending_paused_observe_after_action_id: Optional[str] = None
        self.pending_post_exec_image = False
        self.post_exec_result_ts = 0.0
        self.post_exec_seen_frames = 0
        self.post_exec_kept_frames = 0
        self.post_exec_last_dropped_bytes = None
        self.post_exec_last_dropped_ts = 0.0
        self.last_llm_request_sent_ts = 0.0
        self.last_async_notice_end_ts = 0.0
        self.last_async_notice_start_ts = 0.0
        self.current_exec_action = None
        self.last_exec_action_content: Optional[str] = None
        self.last_exec_action_ts: float = 0.0
        self.default_observe_after_sec = 2.0
        self.eval_success_force_keep_marker = "__MCBOTS_EVAL_SUCCESS_FORCE_KEEP_5__"
        self.eval_success_force_keep_count = 5
        self._state_cursor = 0
        self._state_cursor_lock = threading.Lock()

    def _arm_post_action_image_gate(self, since_ts: float):
        """动作后图片门槛：至少一张后续图可用；若全被过滤则后续补最后一张。"""
        self.pending_post_exec_image = True
        self.post_exec_result_ts = float(since_ts)
        self.post_exec_seen_frames = 0
        self.post_exec_kept_frames = 0
        self.post_exec_last_dropped_bytes = None
        self.post_exec_last_dropped_ts = 0.0

    @staticmethod
    def _message_has_image(msg: dict) -> bool:
        content = msg.get("content")
        if not isinstance(content, list):
            return False
        return any(isinstance(x, dict) and x.get("type") == "image_url" for x in content)

    @staticmethod
    def _count_images_in_message(msg: dict) -> int:
        content = msg.get("content")
        if not isinstance(content, list):
            return 0
        return sum(1 for x in content if isinstance(x, dict) and x.get("type") == "image_url")

    @staticmethod
    def _tail_content_for_image_limit(content_list, images_to_keep: int) -> list:
        """从 content 列表尾部截取恰好包含 images_to_keep 张图片的子列表。
        对于每张保留的图片，若其紧邻前一项是 text，则一并保留（通常是 caption）。
        """
        if images_to_keep <= 0 or not isinstance(content_list, list) or not content_list:
            return []
        n = len(content_list)
        imgs = 0
        keep_start = n
        for j in range(n - 1, -1, -1):
            item = content_list[j]
            if isinstance(item, dict) and item.get("type") == "image_url":
                imgs += 1
                keep_start = j
                if imgs >= images_to_keep:
                    if j - 1 >= 0:
                        prev = content_list[j - 1]
                        if isinstance(prev, dict) and prev.get("type") == "text":
                            keep_start = j - 1
                    break
        return list(content_list[keep_start:])

    def _on_image_committed(self, frame_ts: Optional[float]):
        self.committed_image_seq += 1

    def _new_action_id(self) -> str:
        return uuid.uuid4().hex[:8]

    @staticmethod
    def _action_reference(action_id: str) -> str:
        """Bound display-only IDs without changing provider IDs or signatures.

        Gemini compatibility gateways can embed an opaque thought signature in
        the ID. Hash the full ID, not its prefix: prefixes may be reused. A
        stateless reference also stays stable across summary resets and late
        results. Native tool-call pairing and execution still use the raw ID.
        """
        if len(action_id) <= 128 and "__thought__" not in action_id:
            return action_id
        return "action_" + hashlib.sha256(action_id.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def _action_reference_line(cls, action_id: str, *, related: bool = False) -> str:
        reference = cls._action_reference(action_id)
        if reference == action_id:
            label = "Related tool call ID" if related else "Tool call ID"
        else:
            label = "Related action reference" if related else "Action reference"
        return f"{label}: {reference}"

    def _append_action_event(
        self,
        text: str,
        event_ts: Optional[float] = None,
        *,
        action_id: Optional[str] = None,
        related_action_id: Optional[str] = None,
    ):
        ts_text = self._format_timestamp(event_ts if event_ts is not None else time.time())
        if action_id and not self._uses_native_action_tools():
            if text.startswith("Action Interrupted") and related_action_id:
                text = f"Action {action_id} Interrupted (by Action {related_action_id})"
            elif text.startswith("Action "):
                text = f"Action {action_id} {text[len('Action '):]}"
        event_lines = [f"[Action Event at {ts_text}] {text}"]
        if action_id and self._uses_native_action_tools():
            # DeepSeek V4's DSML encoder does not render OpenAI tool-call IDs
            # into the assistant/tool token stream. Put the correlation key in
            # user-visible lifecycle events so the model can bind asynchronous
            # Start/End/Interrupted observations to the exact action.
            event_lines.append(self._action_reference_line(action_id))
            if related_action_id:
                event_lines.append(self._action_reference_line(related_action_id, related=True))
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "\n".join(event_lines) + "\n"}
            ],
            # Keep the same identifiers as machine-readable local metadata for
            # exact audit joins. _sanitize_message_for_llm removes these copies;
            # the explicit lifecycle text above is the model-visible binding.
            "mcbots_action_id": action_id,
            "mcbots_related_action_id": related_action_id,
        }
        self._append_or_stage_observation(msg, event_ts if event_ts is not None else time.time())

    def _mark_action_observation(self, event_ts: float):
        if self.current_exec_action is None:
            return
        if self.current_exec_action.get("finished", False):
            return
        if not self.current_exec_action.get("window_has_observation", False):
            self.current_exec_action["window_has_observation"] = True
        if not self.current_exec_action.get("start_event_emitted", False):
            self._append_action_event(
                "Action Start",
                self.current_exec_action.get("start_ts", event_ts),
                action_id=self.current_exec_action["id"],
            )
            self.current_exec_action["start_event_emitted"] = True

    def _load_sampling_config(self, config_path: str) -> dict:
        """加载采样参数配置文件"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            return config
        except Exception as e:
            print(f"⚠️  Failed to load sampling config from {config_path}: {e}")
            print("⚠️  Using default parameters: temperature=0.7")
            return {"temperature": 0.7}

    @staticmethod
    def _normalize_video_resolution(raw_value: str) -> str:
        """将分辨率统一成 ffmpeg 可接受的 WIDTHxHEIGHT 格式。"""
        value = (raw_value or "").strip().lower()
        if not value:
            return ""
        parts = value.split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return f"{parts[0]}x{parts[1]}"
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
            return f"{parts[0]}x{parts[1]}"
        print(f"⚠️  Invalid video resolution '{raw_value}', fallback to display default")
        return ""

    def start(self):
        """启动Agent（两个独立线程）"""
        self.running = True

        # 1. 初始化记录目录
        self._setup_record_directory()
        self._initialize_message_journal()

        # 2. 初始化对话历史
        if self.system_prompt:
            self._smart_append_msg({
                "role": "system",
                "content": self.system_prompt
            })
        if self.initial_user_message:
            self._smart_append_msg({
                "role": "user",
                "content": [
                    {"type": "text", "text": f"{self.initial_user_message}\n"},
                ],
                "mcbots_initial_user_message": True,
                "mcbots_initial_user_message_id": self._initial_user_message_id,
            })

        # 3. 保存初始messages
        self._save_messages()

        # 4. 启动线程
        # 线程1: 观测循环
        observe_thread = threading.Thread(
            target=self._observe_loop,
            daemon=True
        )

        # 线程2: 决策循环
        decision_thread = threading.Thread(
            target=self._decision_loop,
            daemon=True
        )

        observe_thread.start()
        decision_thread.start()

        # 5. 主动抓一张初始帧（新 trajectory 必有视觉锚点，绕过 stop_observe）
        self._capture_initial_trajectory_snapshot()

        print(f"✓ Agent started")
        print(f"📁 Record directory: {self.record_dir}\n")

    def _get_debug_reward(self) -> float:
        """Return random 0/1 reward when MCBOTS_DEBUG_RANDOM_REWARD=1, else 0.0."""
        if os.environ.get("MCBOTS_DEBUG_RANDOM_REWARD", "0") == "1":
            return float(random.randint(0, 1))
        return 0.0

    @staticmethod
    def _print_reasoning_content(message: Any, *, label: str = "Assistant reasoning_content"):
        """Print provider reasoning_content in dim gray when present."""
        raw = getattr(message, "reasoning_content", None)
        if raw is None:
            return
        text = raw if isinstance(raw, str) else str(raw)
        if not text.strip():
            return
        print(label)
        print(f"\033[90m{text}\n\033[0m")

    @staticmethod
    def _maybe_recover_reasoning_from_content(message: Any) -> None:
        """When the provider doesn't surface a separate `reasoning_content` but the
        content embeds DeepSeek-R1-style `<think>...</think>` markers, split on the
        first `</think>`: text before becomes reasoning_content (stripping any
        leading `<think>`), text after becomes the visible content. In-place;
        no-op if reasoning_content is already populated or `</think>` is absent.
        """
        if message is None:
            return
        existing = getattr(message, "reasoning_content", None)
        if isinstance(existing, str) and existing.strip():
            return
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            return
        if content.lstrip().startswith("</think>"):
            print(
                "\033[93m[reasoning] model output starts directly with `</think>` "
                "(empty reasoning, action follows immediately). \033[0m"
            )
        elif "</think>" not in content:
            print(
                "\033[93m[reasoning] model output does not contain `</think>` "
                "(no reasoning, only action, but this is not that expected as we still need a </think> to end the reasoning even it's empty). \033[0m"
            )
            return
        else:
            print(
                "\033[93m[reasoning] model output contains `</think>` "
                "(normal reasoning, action follows after). \033[0m"
            )
        head, _, tail = content.partition("</think>")
        head_stripped = head.lstrip()
        if head_stripped.startswith("<think>"):
            head_stripped = head_stripped[len("<think>"):]
        head_stripped = head_stripped.strip()
        new_content = tail.lstrip()
        try:
            message.reasoning_content = head_stripped or None
            message.content = new_content
        except Exception:
            pass

    def _print_token_usage(self, usage: Any):
        """Print prompt/completion/total token usage when the provider reports it."""
        if usage is None:
            return
        try:
            if isinstance(usage, dict):
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
                total_tokens = usage.get("total_tokens")
            else:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                completion_tokens = getattr(usage, "completion_tokens", None)
                total_tokens = getattr(usage, "total_tokens", None)
            if total_tokens is None:
                total_tokens = self._extract_total_tokens(usage)
            parts = []
            if prompt_tokens is not None:
                parts.append(f"prompt={prompt_tokens}")
            if completion_tokens is not None:
                parts.append(f"completion={completion_tokens}")
            if total_tokens is not None:
                parts.append(f"total={total_tokens}")
            if parts:
                print(f"📊 Token usage: {', '.join(parts)}")
        except Exception:
            return

    def stop(self):
        """停止Agent"""
        self._notify_roll_episode_done(reward=self._get_debug_reward())
        self.running = False
        self._stop_video_recording()
        self._write_frame_filter_summary()

    def _notify_roll_window_complete(self, reward: float = 0.0):
        """Notify ROLL's McbotsEnvManager that the current context window is complete."""
        if not self.roll_notify_url:
            return
        try:
            body = json.dumps({"reward": reward}).encode()
            req = urllib.request.Request(
                f"{self.roll_notify_url}/window_complete",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"⚠️  Failed to notify ROLL /window_complete: {e}")

    def _notify_roll_episode_done(self, reward: float = 0.0):
        """Notify ROLL's McbotsEnvManager that the episode is done."""
        if not self.roll_notify_url:
            return
        try:
            body = json.dumps({"reward": reward}).encode()
            req = urllib.request.Request(
                f"{self.roll_notify_url}/episode_done",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"⚠️  Failed to notify ROLL /episode_done: {e}")

    def _append_full_history_locked(self, msg: dict):
        """将消息写入完整历史（调用方需持有 messages_lock）。"""
        record = copy.deepcopy(msg)
        journal_handle = getattr(self, "_messages_journal_handle", None)
        if journal_handle is not None:
            sequence = len(self.full_message_history)
            expected_sequence = getattr(self, "_messages_journal_next_seq", 0)
            if sequence != expected_sequence:
                raise RuntimeError(
                    "message journal sequence mismatch: "
                    f"history={sequence}, journal={expected_sequence}"
                )
            envelope = {
                "schema_version": 1,
                "seq": sequence,
                "message": record,
            }
            try:
                journal_handle.write(
                    json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                journal_handle.flush()
            except Exception as error:
                raise RuntimeError(
                    f"failed to append message journal entry {sequence}: {error}"
                ) from error
            self._messages_journal_next_seq = sequence + 1
        self.full_message_history.append(record)

    def _handle_navigation_claim(self) -> None:
        """Ask the evaluator to arbitrate claim_done without exposing its target."""
        claim_client = getattr(self, "navigation_claim_client", None)
        if claim_client is None:
            raise RuntimeError("navigation completion is not enabled")
        result = claim_client.claim()
        feedback = result.get("feedback")
        if result.get("accepted") is True:
            print("🏁 Navigation evaluator accepted claim_done")
            self.stop_reason = "claim_done_accepted"
            self.running = False
            return
        if result.get("terminal") is True:
            print(
                "🛑 Navigation evaluator rejected the final claim_done: "
                f"{result.get('reason', 'unknown')}"
            )
            self.stop_reason = str(
                result.get("reason") or "claim_done_terminal_rejection"
            )
            self.running = False
            return
        if isinstance(feedback, dict):
            remaining = " -> ".join(
                f'"{name}"' for name in feedback.get("remaining_locations", [])
            )
            message = (
                "[Navigation evaluator] claim_done rejected. The following locations "
                "have not yet been visited and recorded in the required order: "
                f"{remaining}. Remaining attempts: "
                f"{feedback.get('remaining_attempts')}."
            )
        else:
            message = (
                "[Navigation evaluator] claim_done was rejected because the external "
                "arrival rule is not yet satisfied. Continue navigating."
            )
        feedback_msg = {
            "role": "user",
            "content": [{"type": "text", "text": message}],
            "mcbots_navigation_claim_feedback": True,
        }
        with self.messages_lock:
            self.conversation_history.append(feedback_msg)
            self._append_full_history_locked(feedback_msg)
            self.message_revision += 1
        self._save_messages()

    def _drain_navigation_events(self) -> int:
        client = getattr(self, "navigation_claim_client", None)
        if client is None:
            return 0
        messages = client.read_events()
        if not messages:
            return 0
        with self.messages_lock:
            for message in messages:
                event_msg = {
                    "role": "user",
                    "content": [{"type": "text", "text": message}],
                    "mcbots_navigation_evaluator_event": True,
                }
                if self.llm_inflight or self.awaiting_summary:
                    self._append_or_stage_observation(event_msg, time.time())
                else:
                    self.conversation_history.append(event_msg)
                    self._append_full_history_locked(event_msg)
                    self.message_revision += 1
        self._save_messages()
        return len(messages)

    def _find_initial_user_message_locked(self) -> Optional[dict]:
        """定位首条任务描述 user 消息（调用方需持有 messages_lock）。"""
        if not self._initial_user_message_id:
            return None
        for msg in self.conversation_history:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            if msg.get("mcbots_initial_user_message_id") == self._initial_user_message_id:
                return msg
        return None

    @staticmethod
    def _normalize_assistant_tool_call_history(msg: dict) -> dict:
        """Give native tool-call assistant messages provider-safe content."""
        normalized = copy.deepcopy(msg)
        if (
            isinstance(normalized, dict)
            and normalized.get("role") == "assistant"
            and normalized.get("tool_calls")
            and normalized.get("content") is None
        ):
            normalized["content"] = ""
        return normalized

    @staticmethod
    def _sanitize_message_for_llm(msg: dict) -> dict:
        """Remove local metadata while retaining provider reasoning state."""
        if not isinstance(msg, dict):
            return msg
        allowed_keys = (
            "role",
            "content",
            "name",
            "tool_calls",
            "tool_call_id",
            "function_call",
            "refusal",
            # Provider-owned opaque reasoning state must be returned unchanged
            # on subsequent Chat Completions requests.
            "reasoning_content",
            "reasoning",
            "reasoning_details",
            "thinking_blocks",
            "provider_specific_fields",
            "thought_signature",
            "thought_signatures",
        )
        sanitized = {}
        for key in allowed_keys:
            if key in msg:
                sanitized[key] = copy.deepcopy(msg[key])
        # Backfill provider-safe content for histories created before native
        # tool-call messages were normalized on insertion.
        return Agent._normalize_assistant_tool_call_history(sanitized)

    def _smart_append_msg(self, msg, *, archive: bool = True):
        """把同 role 的连续消息尽量合并，无法安全合并时直接追加。"""
        with self.messages_lock:
            if archive:
                self._append_full_history_locked(msg)
            if len(self.conversation_history) == 0:
                self.conversation_history.append(msg)
                self.message_revision += 1
                return

            last_msg = self.conversation_history[-1]
            if last_msg.get("role") != msg.get("role"):
                self.conversation_history.append(msg)
                self.message_revision += 1
                return

            last_content = last_msg.get("content")
            raw_content = msg.get("content")

            if isinstance(last_content, list) and isinstance(raw_content, list):
                last_content.extend(raw_content)
                self.message_revision += 1
                return
            if isinstance(last_content, list) and isinstance(raw_content, str):
                last_content.append({
                    "type": "text",
                    "text": raw_content
                })
                self.message_revision += 1
                return
            if isinstance(last_content, list) and isinstance(raw_content, dict):
                last_content.append(raw_content)
                self.message_revision += 1
                return

            self.conversation_history.append(msg)
            self.message_revision += 1

    def _append_or_stage_observation(self, msg: dict, event_ts: Optional[float]) -> bool:
        """
        观测消息入口：
        - LLM in-flight 或 awaiting_summary 时进入 staging_buffer
        - 否则直接进入 conversation_history
        Returns:
            True: 已直接写入conversation_history
            False: 已暂存
        """
        with self.messages_lock:
            if self.llm_inflight or self.awaiting_summary:
                self._staging_seq += 1
                arrival_ts = float(event_ts) if event_ts is not None else time.time()
                self.staging_buffer.append((arrival_ts, self._staging_seq, msg))
                return False
            self._smart_append_msg(msg)
            return True

    def _flush_staging_buffer_locked(
        self,
        with_async_window_markers: bool,
        async_start_ts: Optional[float] = None,
        async_end_ts: Optional[float] = None,
    ) -> int:
        """
        将 staging_buffer 按时间顺序写入 conversation_history。
        调用方需持有 messages_lock。
        """
        # Record async window timestamps even when there is no staged content.
        if async_start_ts is not None:
            self.last_async_notice_start_ts = float(async_start_ts)
        if async_end_ts is not None:
            self.last_async_notice_end_ts = float(async_end_ts)

        if not self.staging_buffer:
            return 0
        staged = sorted(self.staging_buffer, key=lambda x: (x[0], x[1]))
        self.staging_buffer.clear()
        staged_start_ts = staged[0][0]
        staged_end_ts = staged[-1][0]
        resolved_async_start_ts = async_start_ts if async_start_ts is not None else staged_start_ts
        resolved_async_end_ts = async_end_ts if async_end_ts is not None else staged_end_ts
        if with_async_window_markers:
            ts_start = self._format_timestamp(resolved_async_start_ts)
            self._smart_append_msg(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"[Async Notice Start at {ts_start}] The following observations were captured while the previous "
                                "LLM request was in-flight. They were not seen by that request and are not "
                                "direct results of its action.\n"
                            ),
                        }
                    ],
                }
            )
        for arrival_ts, _, msg in staged:
            self._smart_append_msg(msg)
            if self._message_has_image(msg):
                self._on_image_committed(arrival_ts)
        if with_async_window_markers:
            ts_end = self._format_timestamp(resolved_async_end_ts)
            self._smart_append_msg(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"[Async Notice End at {ts_end}] The in-flight observation segment ended. "
                                "Observations between Async Notice and this line belong to that thinking window.\n"
                            ),
                        }
                    ],
                }
            )
        self.last_async_notice_start_ts = float(resolved_async_start_ts)
        self.last_async_notice_end_ts = float(resolved_async_end_ts)
        return len(staged)


    # ===== 记录管理 =====

    def _setup_record_directory(self):
        """创建记录目录结构"""
        project_root = Path(__file__).parent.parent.absolute()

        record_dir_env = os.getenv("MCBOTS_RECORD_DIR", "").strip()
        if record_dir_env:
            record_dir = Path(record_dir_env).expanduser()
            if not record_dir.is_absolute():
                record_dir = project_root / record_dir
            self.record_dir = record_dir
            self.screenshot_dir = self.record_dir / "screenshots"
            self.messages_file = self.record_dir / "messages.json"
            self.messages_journal_file = self.record_dir / "messages.jsonl"
            self.frame_filter_telemetry_file = self.record_dir / "frame_filter_telemetry.jsonl"
            self.frame_filter_summary_file = self.record_dir / "frame_filter_summary.json"

            self.record_dir.mkdir(parents=True, exist_ok=True)
            self.screenshot_dir.mkdir(exist_ok=True)
            print(f"📁 Created record directory (fixed): {self.record_dir}")
            self._start_video_recording()
            return

        # 1. 获取服务器容器启动时间
        server_launch_time = self._get_container_start_time(self.servername)

        # 2. 获取Agent容器（Bot）启动时间
        agent_launch_time = self._get_container_start_time(self.agentname)

        # 3. 生成目录名
        dir_name = f"{self.servername}_{server_launch_time}_{self.agentname}_{agent_launch_time}"

        # 4. 创建目录结构
        record_root_env = os.getenv("MCBOTS_RECORD_ROOT", "").strip()
        if record_root_env:
            record_root = Path(record_root_env).expanduser()
            if not record_root.is_absolute():
                record_root = project_root / record_root
        else:
            record_root = project_root / "agent_records"

        self.record_dir = record_root / dir_name
        self.screenshot_dir = self.record_dir / "screenshots"
        self.messages_file = self.record_dir / "messages.json"
        self.messages_journal_file = self.record_dir / "messages.jsonl"
        self.frame_filter_telemetry_file = self.record_dir / "frame_filter_telemetry.jsonl"
        self.frame_filter_summary_file = self.record_dir / "frame_filter_summary.json"

        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(exist_ok=True)

        print(f"📁 Created record directory: {self.record_dir}")
        self._start_video_recording()

    def _start_video_recording(self):
        """启动高帧率录屏（独立于截图观测循环）。"""
        if not self.enable_video_recording:
            return

        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin is None:
            print("⚠️  ffmpeg not found, skip video recording")
            return

        with self.video_lock:
            if self.video_proc is not None and self.video_proc.poll() is None:
                return

            self._ensure_record_dirs(ensure_screenshots=False)
            self.video_file = self.record_dir / self.video_filename
            self.video_capture_file = self.video_file
            cmd = [
                ffmpeg_bin,
                "-y",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "x11grab",
                "-framerate",
                str(self.video_fps),
            ]
            if self.video_resolution:
                cmd.extend(["-video_size", self.video_resolution])
            cmd.extend(
                [
                    "-draw_mouse",
                    "0",
                    "-i",
                    self.display,
                    "-an",
                    "-c:v",
                    self.video_codec,
                ]
            )
            if self.video_codec == "libx264":
                cmd.extend(["-preset", self.video_preset, "-crf", str(self.video_crf)])
            # Fragmented MP4 is more resilient when process exits unexpectedly.
            if self.video_capture_file.suffix.lower() == ".mp4":
                cmd.extend(["-movflags", "+frag_keyframe+empty_moov+default_base_moof"])
            cmd.extend(["-pix_fmt", "yuv420p", str(self.video_capture_file)])

            try:
                self.video_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except Exception as e:
                self.video_proc = None
                print(f"⚠️  Failed to start video recording: {e}")
                return

            time.sleep(0.3)
            if self.video_proc.poll() is not None:
                err = ""
                if self.video_proc.stderr:
                    try:
                        err = self.video_proc.stderr.read().decode("utf-8", errors="replace").strip()
                    except Exception:
                        err = ""
                print("⚠️  Video recording exited immediately.")
                if err:
                    print(f"    ffmpeg: {err.splitlines()[-1]}")
                self.video_proc = None
                return

            print(
                f"🎥 Video recording started (display={self.display}, fps={self.video_fps}) -> "
                f"{self.video_capture_file}"
            )

    def _stop_video_recording(self):
        """停止录屏并关闭 ffmpeg 进程。"""
        with self.video_lock:
            proc = self.video_proc
            video_file = self.video_file
            capture_file = self.video_capture_file or video_file
            self.video_proc = None
            self.video_capture_file = None

        if proc is None:
            return

        stderr_bytes = b""
        try:
            if proc.poll() is None:
                # SIGINT usually lets ffmpeg flush trailer more reliably than SIGTERM.
                proc.send_signal(signal.SIGINT)
            _, stderr_bytes = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                _, stderr_bytes = proc.communicate(timeout=3)
            except Exception:
                stderr_bytes = b""
        except Exception:
            pass

        if proc.returncode not in (0, None):
            err = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
            print(f"⚠️  Video recording exited with code {proc.returncode}")
            if err:
                print(f"    ffmpeg: {err.splitlines()[-1]}")

        saved_file = capture_file
        if (
            capture_file
            and video_file
            and capture_file != video_file
            and capture_file.exists()
        ):
            saved_file = self._finalize_recording_file(capture_file, video_file)

        if saved_file and saved_file.exists():
            try:
                size_mb = saved_file.stat().st_size / (1024 * 1024)
                print(f"🎬 Video saved: {saved_file} ({size_mb:.2f} MB)")
            except Exception:
                print(f"🎬 Video saved: {saved_file}")
            if video_file and saved_file != video_file:
                print(f"⚠️  MP4 remux failed, keeping fallback recording: {saved_file}")
            probe_ok, probe_msg = self._probe_video_file(saved_file)
            if not probe_ok:
                print(f"⚠️  Video may be unreadable: {probe_msg}")

    def _finalize_recording_file(self, capture_file: Path, target_file: Path) -> Path:
        """Try to produce target video file from capture container; fallback to raw capture."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin is None:
            return capture_file

        remux_cmd = [
            ffmpeg_bin,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(capture_file),
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            str(target_file),
        ]
        try:
            remux = subprocess.run(remux_cmd, capture_output=True, text=True, timeout=30)
            if remux.returncode == 0 and target_file.exists():
                try:
                    capture_file.unlink(missing_ok=True)
                except Exception:
                    pass
                return target_file
            err = (remux.stderr or remux.stdout or "").strip()
            if err:
                print(f"⚠️  Video remux failed: {err.splitlines()[-1]}")
        except Exception as e:
            print(f"⚠️  Video remux failed: {e}")

        transcode_cmd = [
            ffmpeg_bin,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(capture_file),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            self.video_preset,
            "-crf",
            str(self.video_crf),
            "-pix_fmt",
            "yuv420p",
            str(target_file),
        ]
        try:
            transcode = subprocess.run(transcode_cmd, capture_output=True, text=True, timeout=60)
            if transcode.returncode == 0 and target_file.exists():
                try:
                    capture_file.unlink(missing_ok=True)
                except Exception:
                    pass
                return target_file
            err = (transcode.stderr or transcode.stdout or "").strip()
            if err:
                print(f"⚠️  Video transcode fallback failed: {err.splitlines()[-1]}")
        except Exception as e:
            print(f"⚠️  Video transcode fallback failed: {e}")

        return capture_file

    def _get_container_start_time(self, container_name: str) -> str:
        """获取容器启动时间（无容器环境会退化为当前时间）"""
        fallback = datetime.now().strftime("%Y%m%d_%H%M%S")
        if not container_name:
            return fallback
        if shutil.which("podman") is None:
            return fallback
        try:
            cmd = ["podman", "inspect", container_name, "--format", "{{.State.StartedAt}}"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                # 解析时间：2026-01-05T12:00:00.123456789Z
                time_str = result.stdout.strip()
                if not time_str:
                    return fallback
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                return dt.strftime("%Y%m%d_%H%M%S")
            else:
                return fallback
        except Exception as e:
            print(f"⚠️  Failed to get container start time for {container_name}: {e}")
            return fallback

    def _probe_video_file(self, video_file: Path) -> tuple[bool, str]:
        """检查视频文件是否可被 ffprobe 正常解析。"""
        ffprobe_bin = shutil.which("ffprobe")
        if ffprobe_bin is None:
            return True, ""
        try:
            result = subprocess.run(
                [
                    ffprobe_bin,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=format_name,duration,size,bit_rate",
                    str(video_file),
                ],
                capture_output=True,
                text=True,
                timeout=6,
            )
        except Exception as e:
            return False, f"ffprobe check failed: {e}"
        if result.returncode == 0:
            return True, ""
        msg = (result.stderr or result.stdout or "").strip()
        return False, (msg.splitlines()[-1] if msg else "unknown ffprobe error")

    def _initialize_message_journal(self):
        """为本次运行创建只追加的完整消息 journal。"""
        with self.messages_lock:
            self._ensure_record_dirs(ensure_screenshots=False)
            if not self.record_dir:
                raise RuntimeError("record directory is not configured")
            self.messages_file = self.record_dir / "messages.json"
            self.messages_journal_file = self.record_dir / "messages.jsonl"
            previous_handle = getattr(self, "_messages_journal_handle", None)
            if previous_handle is not None:
                previous_handle.close()
            try:
                self.messages_file.unlink(missing_ok=True)
                self._messages_journal_handle = self.messages_journal_file.open(
                    "w",
                    encoding="utf-8",
                    buffering=1,
                    newline="\n",
                )
                self._messages_journal_next_seq = 0
                existing_history = list(self.full_message_history)
                self.full_message_history.clear()
                for message in existing_history:
                    self._append_full_history_locked(message)
            except Exception:
                self._messages_journal_handle = None
                raise

    def _save_messages(self):
        """刷新增量 messages.jsonl；最终 messages.json 由任务 wrapper 生成。"""
        with self.messages_lock:
            journal_handle = getattr(self, "_messages_journal_handle", None)
            if journal_handle is None:
                return
            try:
                journal_handle.flush()
            except Exception as error:
                raise RuntimeError(f"failed to flush message journal: {error}") from error

    def _append_frame_filter_telemetry(self, payload: dict):
        """记录截图过滤结果，便于离线调试/回放分析。"""
        if not self.enable_frame_filter_telemetry:
            return
        if not self.frame_filter_telemetry_file:
            return
        try:
            with self.record_lock:
                self._ensure_record_dirs(ensure_screenshots=False)
                with open(self.frame_filter_telemetry_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except FileNotFoundError:
            try:
                with self.record_lock:
                    self._ensure_record_dirs(ensure_screenshots=False)
                    with open(self.frame_filter_telemetry_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"⚠️  Failed to write frame filter telemetry: {e}")
        except Exception as e:
            print(f"⚠️  Failed to write frame filter telemetry: {e}")

    def _write_frame_filter_summary(self):
        """写出帧过滤会话摘要，便于快速评估压缩效果。"""
        if self._frame_filter_summary_written:
            return
        if not self.record_dir or not self.frame_filter_summary_file:
            return

        payload = {
            "generated_at": time.time(),
            "frame_filter_config": asdict(self.frame_filter.config),
            "frame_filter_stats": self.frame_filter.stats(),
            "telemetry_enabled": bool(self.enable_frame_filter_telemetry),
        }
        if self.frame_filter_telemetry_file:
            payload["telemetry"] = summarize_frame_filter_telemetry_file(self.frame_filter_telemetry_file)

        try:
            with self.record_lock:
                self._ensure_record_dirs(ensure_screenshots=False)
                with open(self.frame_filter_summary_file, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
            self._frame_filter_summary_written = True

            telemetry_summary = (payload.get("telemetry") or {}).get("summary", {})
            total_frames = int(telemetry_summary.get("screenshot_frames_total", 0) or 0)
            dropped_frames = int(telemetry_summary.get("screenshot_frames_dropped", 0) or 0)
            drop_ratio = float(telemetry_summary.get("drop_ratio", 0.0) or 0.0)
            print(
                "📊 Frame filter summary saved: "
                f"{self.frame_filter_summary_file} "
                f"(frames={total_frames}, dropped={dropped_frames}, drop_ratio={drop_ratio:.1%})"
            )
        except Exception as e:
            print(f"⚠️  Failed to write frame filter summary: {e}")

    def _ensure_record_dirs(self, ensure_screenshots: bool = False):
        """确保记录目录存在，避免并发/外部清理导致后续写入失败。"""
        if not self.record_dir:
            return
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            if ensure_screenshots and self.screenshot_dir:
                self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"⚠️  Failed to ensure record dirs: {e}")

    def _trigger_screenshot_force_keep(self, source: str, timestamp: Optional[float] = None):
        """重要事件触发后，确保下一张（或几张）截图不会被去重掉。"""
        self.frame_filter.notify_event(source, timestamp)
        stats = self.frame_filter.stats()
        self._append_frame_filter_telemetry(
            {
                "type": "screenshot_force_keep_trigger",
                "timestamp": float(timestamp if timestamp is not None else time.time()),
                "source": source,
                "force_budget": self.frame_filter.pending_force_keep_budget(),
                "stats": stats,
            }
        )
        print(
            "🪝 Screenshot keep-trigger armed "
            f"(source={source}, force_budget={self.frame_filter.pending_force_keep_budget()}, total={stats['total']})"
        )

    _LAST_EXEC_INLINE_MAX = 500  # chars; above this we spill to disk

    def _observe_mode_text(self) -> str:
        """Human-readable tag for the current observation mode."""
        return "streaming" if self.observe_enabled else "event_only"

    def _emit_observe_mode_notice(self, reason: str):
        """Append a short user-role notice announcing the current observation mode.
        Goes to both the active window and the full archive so the model sees it
        after toggling and later reviewers can trace the state transitions."""
        mode = self._observe_mode_text()
        ts_text = self._format_timestamp(time.time())
        notice = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"[Observation status at {ts_text}] Mode is now `{mode}` "
                        f"(reason: {reason}). "
                        + (
                            "Continuous screenshots will be inserted automatically."
                            if self.observe_enabled
                            else "The continuous screenshot stream is suppressed; "
                                 "only command results, chat, guaranteed captures "
                                 "(post-exec / post-skip / post-stop_execute / post-reset / initial), "
                                 "and event_only observe_after one-shot captures that survive dedup will appear. "
                                 "Note: pre-action frames are NOT captured in event_only mode."
                        )
                    ),
                }
            ],
            "mcbots_observe_status": True,
            "mcbots_observe_mode": mode,
            "mcbots_observe_reason": reason,
        }
        with self.messages_lock:
            self.conversation_history.append(notice)
            self._append_full_history_locked(notice)
            self.message_revision += 1
        self._save_messages()

    def _dump_long_exec_content(self, content: str) -> Optional[str]:
        """Persist a long exec content string under <record_dir>/tmp/ and return
        the absolute path, or None on failure."""
        try:
            if not self.record_dir:
                return None
            tmp_dir = Path(self.record_dir) / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            fname = f"last_exec_action_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.txt"
            path = tmp_dir / fname
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return str(path)
        except Exception as e:
            print(f"⚠️  Failed to dump long exec content: {e}")
            return None

    def _format_last_exec_hint(self) -> str:
        """Return a reminder string about the most recent exec action (if any),
        intended to be appended to trim/reset markers so the model doesn't lose
        track of long-running side-effects it initiated before the window reset."""
        if not self.last_exec_action_content:
            return ""
        ts_text = (
            self._format_timestamp(self.last_exec_action_ts)
            if self.last_exec_action_ts > 0
            else "unknown time"
        )
        content = str(self.last_exec_action_content)
        dumped_path = None
        if len(content) > self._LAST_EXEC_INLINE_MAX:
            dumped_path = self._dump_long_exec_content(content)
            if dumped_path:
                excerpt = content[: self._LAST_EXEC_INLINE_MAX].rstrip() + " ...(truncated)"
                content_block = (
                    f"`{excerpt}` "
                    f"(full content saved at `{dumped_path}` — read it with a shell command if you need the complete version)"
                )
            else:
                content_block = f"`{content[: self._LAST_EXEC_INLINE_MAX].rstrip()} ...(truncated, full content unavailable)`"
        else:
            content_block = f"`{content.strip()}`"
        return (
            " Your most recent `exec` action was: "
            f"{content_block} (issued at {ts_text}). "
            "If it was a long-running directive that keeps controlling the bot in the background, "
            "it may still be running — the observations in the recent window above could be produced by it. "
            "Inspect the scene and issue an explicit stop command "
            "(for example, for Baritone: `mcapi chat_command \"#stop\"` or `mcapi chat_command \"#forcecancel\"`) "
            "or send a new exec to regain control, "
            "otherwise continue the task if it is still appropriate."
        )

    # ===== 观测循环 =====

    def _drain_new_states(self) -> int:
        """尽可能处理当前已产生的所有状态；返回处理条数。"""
        processed = 0
        with self._state_cursor_lock:
            new_states = self.env_getter(self._state_cursor)
            if not new_states:
                return 0

            with self.observe_lock:
                screenshot_enabled = self.observe_enabled

            for state in new_states:
                if state.type == "screenshot":
                    if screenshot_enabled:
                        self._insert_screenshot_message(state)
                elif state.type == "command_result":
                    self._insert_command_result_message(state)
                elif state.type == "chat_message":
                    self._insert_chat_message(state)
                self._state_cursor += 1
                processed += 1
        return processed

    def _observe_loop(self):
        """独立线程：有新状态就立即处理，无新状态短暂等待。"""
        while self.running:
            try:
                paused_observe_capture_attempted = self._capture_paused_observe_after_snapshot_if_due()
                processed = self._drain_new_states() + self._drain_navigation_events()
                if processed <= 0 and not paused_observe_capture_attempted:
                    time.sleep(0.05)

            except Exception as e:
                print(f"⚠️  Observe loop error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(1)

    def _insert_screenshot_message(
        self,
        state,
        *,
        override_ts: Optional[float] = None,
        label: str = "",
    ):
        """插入截图消息"""
        frame_bytes = state.screenshot or b""
        frame_ts = float(override_ts if override_ts is not None else state.timestamp)

        # 1. 保存截图到文件
        self.screenshot_counter += 1
        screenshot_filename = f"screenshot_{self.screenshot_counter:03d}.jpg"
        screenshot_path = self.screenshot_dir / screenshot_filename
        screenshot_abs_path = str(screenshot_path.absolute())

        try:
            self._ensure_record_dirs(ensure_screenshots=True)
            with open(screenshot_path, "wb") as f:
                f.write(frame_bytes)
        except FileNotFoundError:
            try:
                self._ensure_record_dirs(ensure_screenshots=True)
                with open(screenshot_path, "wb") as f:
                    f.write(frame_bytes)
            except Exception as e:
                print(f"⚠️  Failed to save screenshot: {e}")
                return
        except Exception as e:
            print(f"⚠️  Failed to save screenshot: {e}")
            return

        decision = self.frame_filter.decide(frame_bytes, frame_ts)
        telemetry = {
            "type": "screenshot_frame",
            "timestamp": frame_ts,
            "screenshot_index": self.screenshot_counter,
            "kept": decision.kept,
            "reason": decision.reason,
            "force_event": decision.force_event,
            "sha1": decision.sha1,
            "size_bytes": len(frame_bytes),
            "path": screenshot_abs_path,
            "metrics": decision.metrics or {},
            "stats": decision.stats or {},
        }
        self._append_frame_filter_telemetry(telemetry)

        if not decision.kept:
            if self.pending_post_exec_image and frame_ts >= float(self.post_exec_result_ts):
                self.post_exec_seen_frames += 1
                self.post_exec_last_dropped_bytes = frame_bytes
                self.post_exec_last_dropped_ts = frame_ts
            return

        # 保留帧序号（用于“action后至少一张新图”门槛）
        self.kept_image_seq += 1
        if self.pending_post_exec_image and frame_ts >= float(self.post_exec_result_ts):
            self.post_exec_seen_frames += 1
            self.post_exec_kept_frames += 1

        # event_only 模式下，bot 没有动作在执行且没打开 GUI 时尝试发全景；任何失败都回退到单帧。
        image_bytes_for_vlm = frame_bytes
        if (
            self.enable_panorama
            and not self.observe_enabled
            and self.runtime_state_getter is not None
            and self.panorama_capturer is not None
        ):
            # 用 current_exec_action 判定空闲：某些指令速度看起来为 0 但仍在后台执行。
            idle = self.current_exec_action is None
            bot_state = self.runtime_state_getter() if idle else None
            if bot_state:
                # container_id 在打开玩家背包时仍为 0，不能区分有无 GUI；
                # 用 AgentBridge 新增的 gui_open（Minecraft.screen != null）判断。
                # 字段缺失时按"GUI 打开"处理，避免在 GUI 状态下误发全景。
                no_gui = bot_state.get("gui_open") is False
                if idle and no_gui:
                    panorama_bytes = self.panorama_capturer()
                    if panorama_bytes:
                        panorama_filename = f"panorama_{self.screenshot_counter:03d}.jpg"
                        panorama_path = self.screenshot_dir / panorama_filename
                        try:
                            with open(panorama_path, "wb") as f:
                                f.write(panorama_bytes)
                            image_bytes_for_vlm = panorama_bytes
                            print(f"🌐 Panorama sent to VLM -> {panorama_path.absolute()}")
                        except Exception as e:
                            print(f"⚠️  Failed to save panorama: {e}")

        # 2. 生成base64编码
        image_b64 = base64.b64encode(image_bytes_for_vlm).decode()

        # 3. 构建消息（包含base64和文件路径）
        ts_text = self._format_timestamp(frame_ts)
        screenshot_text = f"[Screenshot at {ts_text}]"
        if label:
            screenshot_text = f"{screenshot_text} [{label}]"
        screenshot_text = f"{screenshot_text}\n"
        observation_msg = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": screenshot_text
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_b64}"
                    }
                }
            ]
        }

        self._mark_action_observation(frame_ts)
        inserted = self._append_or_stage_observation(observation_msg, frame_ts)
        if inserted:
            self._on_image_committed(frame_ts)
            self._save_messages()
        else:
            print(f"⏳ Screenshot staged (in-flight) [{ts_text}]")

        keep_reason = decision.reason
        if decision.force_event:
            keep_reason = f"{keep_reason}:{decision.force_event}"
        print(
            f"👁️  Screenshot inserted [{keep_reason}] "
            f"({len(frame_bytes)/1024:.1f}KB) -> {screenshot_abs_path}"
        )

    def _insert_command_result_message(self, state):
        """插入命令结果消息"""
        command_text = (state.command or "").strip()
        result_action_id = getattr(state, "action_id", None)
        result_task_id = getattr(state, "task_id", None)
        current_action_id = (
            self.current_exec_action.get("id")
            if self.current_exec_action is not None
            else None
        )
        matches_current_action = bool(
            current_action_id
            and isinstance(result_action_id, str)
            and result_action_id == current_action_id
        )
        is_earlier_action_result = bool(
            isinstance(result_action_id, str)
            and result_action_id
            and not matches_current_action
        )
        requests_mcapi_state = self._command_requests_mcapi_state(command_text)
        suppress_success_mcapi_result = (
            command_text.startswith("mcapi ")
            and not requests_mcapi_state
            and int(state.exit_code or 0) == 0
            # The CLI already silences successful action acknowledgements.
            # Preserve query/help/verbose output and any compound-shell output.
            and not state.stdout
            and not state.stderr
        )

        # 如果这是当前执行动作的完成信号，先更新动作状态（不依赖是否有stdout/stderr）
        if (
            matches_current_action
            and self.current_exec_action is not None
            and not self.current_exec_action.get("finished", False)
        ):
            self.current_exec_action["finished"] = True
            self.current_exec_action["finish_ts"] = float(state.timestamp)
            self.current_exec_action["exit_code"] = int(state.exit_code or 0)
            self._arm_post_action_image_gate(float(state.timestamp))

            # Always keep start/end events, even if no observations in between.
            # Emit the events before capturing the post-exec snapshot so that the
            # direct-append path (LLM not in-flight) preserves chronological order:
            # Action End (state.timestamp) precedes the new frame's own grab time.
            if not self.current_exec_action.get("start_event_emitted", False):
                self._append_action_event(
                    "Action Start",
                    self.current_exec_action.get("start_ts", state.timestamp),
                    action_id=self.current_exec_action["id"],
                )
                self.current_exec_action["start_event_emitted"] = True
            action_status = (
                "Timeout"
                if getattr(state, "command_status", None) == "timed_out"
                else f"End (exit_code={int(state.exit_code or 0)})"
            )
            self._append_action_event(
                f"Action {action_status}",
                float(state.timestamp),
                action_id=self.current_exec_action["id"],
            )
            self.current_exec_action = None

            # A post-exec snapshot supersedes a scheduled observe_after frame
            # when the command finishes before that deadline.
            self._clear_paused_observe_after_snapshot()

            # 主动抓一张 post-exec 快照（绕过 stop_observe，保证动作结果总有视觉锚点）
            self._capture_post_exec_snapshot(anchor_ts=float(state.timestamp))

        if suppress_success_mcapi_result:
            if matches_current_action and self.current_exec_action is None:
                self.pending_exec_result = False
            self._inject_pending_summary_request()
            return

        # 构建消息文本
        ts_text = self._format_timestamp(state.timestamp)
        if is_earlier_action_result:
            lines = [f"[Earlier Interrupted Command Result at {ts_text}]"]
            lines.append(
                "This result belongs to an earlier command and does not finish "
                "the current command."
            )
        else:
            lines = [f"[Command Result at {ts_text}]"]
        if self._uses_native_action_tools() and result_action_id:
            # The command already exists in the assistant tool-call arguments.
            # Repeat only the correlation key so the model can join this
            # asynchronous result to the exact structured call without paying
            # for the Bash payload twice.
            lines.append(self._action_reference_line(result_action_id))
        else:
            lines.append(f"Command: {state.command}")

        if state.stdout:
            lines.append(f"Stdout:\n{state.stdout}")

        if state.stderr:
            lines.append(f"Stderr:\n{state.stderr}")

        lines.append(f"Exit code: {state.exit_code}")

        message_text = "\n".join(lines) + "\n"

        observation_msg = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": message_text
                }
            ],
            "mcbots_action_id": result_action_id,
            "mcbots_remote_task_id": result_task_id,
            "mcbots_matches_current_action": matches_current_action,
        }

        if matches_current_action:
            self._mark_action_observation(float(state.timestamp))
        inserted = self._append_or_stage_observation(observation_msg, state.timestamp)
        if inserted:
            self._save_messages()
        else:
            print(f"⏳ Command result staged (in-flight) [{ts_text}]")
        print(f"📋 Command result inserted (exit: {state.exit_code})")
        if matches_current_action and self.current_exec_action is None:
            self.pending_exec_result = False
        self._inject_pending_summary_request()

    @staticmethod
    def _command_requests_mcapi_state(command_text: str) -> bool:
        """Return whether a shell command contains an actual ``mcapi state`` command.

        Successful non-state ``mcapi`` output is normally suppressed to keep the
        observation history small.  Tokenize the full shell expression so a
        trailing state read in commands such as ``mcapi look ... && mcapi state``
        remains visible, without treating quoted chat text as a state command.
        If tokenization is ambiguous, keep the result rather than risk dropping
        state output.
        """
        if "mcapi state" not in command_text:
            return False
        try:
            lexer = shlex.shlex(
                command_text,
                posix=True,
                punctuation_chars=";&|()",
            )
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            return True

        command_separators = {"&&", "||", ";", "|", "&", "("}
        for index in range(len(tokens) - 1):
            if tokens[index] != "mcapi" or tokens[index + 1] != "state":
                continue
            if index == 0 or tokens[index - 1] in command_separators:
                return True
        return False

    def _insert_chat_message(self, state):
        """插入聊天消息"""
        chat_content = str(state.chat_content or "")
        if self.eval_success_force_keep_marker in chat_content:
            self.frame_filter.force_keep_next(
                self.eval_success_force_keep_count,
                event_type="eval_success",
                timestamp=state.timestamp,
            )
            stats = self.frame_filter.stats()
            self._append_frame_filter_telemetry(
                {
                    "type": "screenshot_force_keep_trigger",
                    "timestamp": float(state.timestamp),
                    "source": "eval_success_marker",
                    "force_budget": self.frame_filter.pending_force_keep_budget(),
                    "stats": stats,
                }
            )
            print(
                "✅ Eval success force-keep armed "
                f"(count={self.eval_success_force_keep_count}, force_budget={self.frame_filter.pending_force_keep_budget()})"
            )
            # Marker仅用于控制截图保留，不暴露给模型上下文。
            return

        # 格式化聊天消息
        ts_text = self._format_timestamp(state.timestamp)
        if state.chat_type == "player":
            message_text = f"[Chat at {ts_text}] {chat_content}\n"
        else:
            message_text = f"[System at {ts_text}] {chat_content}\n"

        observation_msg = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": message_text
                }
            ]
        }

        self._mark_action_observation(float(state.timestamp))
        inserted = self._append_or_stage_observation(observation_msg, state.timestamp)
        if inserted:
            self._save_messages()
        else:
            print(f"⏳ Chat message staged (in-flight) [{ts_text}]")
        self._trigger_screenshot_force_keep(f"chat_message:{state.chat_type or 'unknown'}", state.timestamp)

        print(f"💬 Chat message inserted [{state.chat_type}]")

    def _append_fallback_post_exec_screenshot(self) -> bool:
        """若exec后截图全被过滤，补入最后一张被过滤截图。调用方需在非in-flight阶段使用。"""
        if not self.pending_post_exec_image:
            return True
        if self.post_exec_kept_frames > 0:
            self.pending_post_exec_image = False
            return True
        if self.post_exec_seen_frames <= 0 or not self.post_exec_last_dropped_bytes:
            return False

        ts_text = self._format_timestamp(self.post_exec_last_dropped_ts or time.time())
        image_b64 = base64.b64encode(self.post_exec_last_dropped_bytes).decode()
        observation_msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": f"[Screenshot at {ts_text}] [post-exec fallback kept]\n"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }
        with self.messages_lock:
            self._smart_append_msg(observation_msg)
        self.kept_image_seq += 1
        self._on_image_committed(float(self.post_exec_last_dropped_ts or time.time()))
        self.pending_post_exec_image = False
        self.post_exec_last_dropped_bytes = None
        self.post_exec_last_dropped_ts = 0.0
        self._save_messages()
        print("📌 Kept fallback screenshot after exec (all post-exec frames were filtered)")
        return True

    def _capture_pre_action_snapshot(self, anchor_ts: Optional[float] = None):
        """在发送 exec 前主动抓一帧，确保位于 Async Notice End 之后、Action Start 之前。
        仅在 streaming 模式下生效；paused 模式下跳过以避免打乱 quiet 视野。"""
        with self.observe_lock:
            if not self.observe_enabled:
                return
        if self.capture_now_getter is None:
            return
        try:
            state = self.capture_now_getter()
        except Exception as e:
            print(f"⚠️  Pre-action snapshot failed: {e}")
            return
        if state is None:
            return
        if getattr(state, "type", None) != "screenshot":
            return
        # Keep this frame forcibly, but use the real capture timestamp for ordering.
        self.frame_filter.force_keep_next(1, event_type="pre_action_snapshot", timestamp=anchor_ts)
        effective_ts = float(state.timestamp)
        self._insert_screenshot_message(
            state,
            override_ts=effective_ts,
            label="pre-start snapshot",
        )

    def _schedule_paused_observe_after_snapshot(self, *, action_id: Optional[str], deadline_ts: float):
        """在 paused 模式下，为 exec 安排一个 observe_after_sec 的单次抓图。"""
        with self.observe_lock:
            if self.observe_enabled:
                self.pending_paused_observe_after_deadline = 0.0
                self.pending_paused_observe_after_action_id = None
                return
            self.pending_paused_observe_after_deadline = max(float(deadline_ts), 0.0)
            self.pending_paused_observe_after_action_id = action_id

    def _clear_paused_observe_after_snapshot(self):
        with self.observe_lock:
            self.pending_paused_observe_after_deadline = 0.0
            self.pending_paused_observe_after_action_id = None

    def _pending_paused_observe_after_deadline(self) -> float:
        with self.observe_lock:
            if self.observe_enabled:
                return 0.0
            deadline = float(self.pending_paused_observe_after_deadline)
            return deadline if deadline > 0.0 else 0.0

    def _capture_paused_observe_after_snapshot_if_due(self, now_ts: Optional[float] = None) -> bool:
        """在 paused 模式下，到达 observe_after_sec 目标时主动抓一帧，但不强制保留。"""
        now = float(now_ts if now_ts is not None else time.time())
        with self.observe_lock:
            if self.observe_enabled:
                self.pending_paused_observe_after_deadline = 0.0
                self.pending_paused_observe_after_action_id = None
                return False
            deadline = float(self.pending_paused_observe_after_deadline)
            action_id = self.pending_paused_observe_after_action_id
            if deadline <= 0.0 or now < deadline:
                return False
            self.pending_paused_observe_after_deadline = 0.0
            self.pending_paused_observe_after_action_id = None

        if self.capture_now_getter is None:
            print("⚠️  Paused observe_after snapshot skipped: capture_now_getter is unavailable")
            return True
        try:
            state = self.capture_now_getter()
        except Exception as e:
            print(f"⚠️  Paused observe_after snapshot failed: {e}")
            return True
        if state is None or getattr(state, "type", None) != "screenshot":
            print("⚠️  Paused observe_after snapshot skipped: invalid state")
            return True

        effective_ts = float(state.timestamp)
        self._insert_screenshot_message(
            state,
            override_ts=effective_ts,
            label="observe_after snapshot",
        )
        action_suffix = f" for action {self._action_reference(action_id)}" if action_id else ""
        print(f"⏱️  Paused observe_after snapshot attempted{action_suffix}")
        return True

    def _capture_post_reset_snapshot(self, anchor_ts: Optional[float] = None):
        """上下文重置后主动抓一帧并强制保留，确保LLM拥有最新视觉锚点。"""
        if self.capture_now_getter is None:
            print("⚠️  Post-reset snapshot skipped: capture_now_getter is unavailable")
            return
        try:
            state = self.capture_now_getter()
        except Exception as e:
            print(f"⚠️  Post-reset snapshot failed: {e}")
            return
        if state is None or getattr(state, "type", None) != "screenshot":
            print("⚠️  Post-reset snapshot skipped: invalid state")
            return
        self.frame_filter.force_keep_next(1, event_type="post_reset_snapshot", timestamp=anchor_ts)
        effective_ts = float(state.timestamp)
        self._insert_screenshot_message(
            state,
            override_ts=effective_ts,
            label="post-reset snapshot",
        )
        # State record: 上下文 reset = 新 window 起点
        self._reset_window_state_record(reason="post_reset")

    def _capture_post_exec_snapshot(self, anchor_ts: Optional[float] = None):
        """exec 完成后主动抓一帧并强制保留；绕过 stop_observe，保证动作结果一定有视觉锚点。"""
        if self.capture_now_getter is None:
            return
        try:
            state = self.capture_now_getter()
        except Exception as e:
            print(f"⚠️  Post-exec snapshot failed: {e}")
            return
        if state is None or getattr(state, "type", None) != "screenshot":
            return
        self.frame_filter.force_keep_next(1, event_type="post_exec_snapshot", timestamp=anchor_ts)
        effective_ts = float(state.timestamp)
        self._insert_screenshot_message(
            state,
            override_ts=effective_ts,
            label="post-exec snapshot",
        )

    def _capture_post_skip_snapshot(self, anchor_ts: Optional[float] = None):
        """skip 动作后主动抓一帧并强制保留；绕过 stop_observe，保证 paused 模式下 skip 也有视觉反馈。"""
        if self.capture_now_getter is None:
            return
        try:
            state = self.capture_now_getter()
        except Exception as e:
            print(f"⚠️  Post-skip snapshot failed: {e}")
            return
        if state is None or getattr(state, "type", None) != "screenshot":
            return
        self.frame_filter.force_keep_next(1, event_type="post_skip_snapshot", timestamp=anchor_ts)
        effective_ts = float(state.timestamp)
        self._insert_screenshot_message(
            state,
            override_ts=effective_ts,
            label="post-skip snapshot",
        )

    def _capture_post_stop_execute_snapshot(self, anchor_ts: Optional[float] = None):
        """stop_execute 后主动抓一帧并强制保留；绕过 stop_observe，让模型看到取消后的状态。"""
        if self.capture_now_getter is None:
            return
        try:
            state = self.capture_now_getter()
        except Exception as e:
            print(f"⚠️  Post-stop_execute snapshot failed: {e}")
            return
        if state is None or getattr(state, "type", None) != "screenshot":
            return
        self.frame_filter.force_keep_next(1, event_type="post_stop_execute_snapshot", timestamp=anchor_ts)
        effective_ts = float(state.timestamp)
        self._insert_screenshot_message(
            state,
            override_ts=effective_ts,
            label="post-stop_execute snapshot",
        )

    def _capture_initial_trajectory_snapshot(self):
        """新 trajectory 启动时主动抓一帧并强制保留；绕过 stop_observe。"""
        if self.capture_now_getter is None:
            print("⚠️  Initial trajectory snapshot skipped: capture_now_getter is unavailable")
            return
        try:
            state = self.capture_now_getter()
        except Exception as e:
            print(f"⚠️  Initial trajectory snapshot failed: {e}")
            return
        if state is None or getattr(state, "type", None) != "screenshot":
            print("⚠️  Initial trajectory snapshot skipped: invalid state")
            return
        self.frame_filter.force_keep_next(1, event_type="initial_trajectory_snapshot", timestamp=float(state.timestamp))
        effective_ts = float(state.timestamp)
        self._insert_screenshot_message(
            state,
            override_ts=effective_ts,
            label="initial trajectory snapshot",
        )
        # State record: rollout 起点 = window 0 initial
        self._reset_window_state_record(reason="initial_trajectory")

    # ============== State record (per-window initial + per-turn deltas) ==============

    def _reset_window_state_record(self, reason: str = ""):
        """Reset state record at a window boundary. Stores the current full state as window_initial."""
        if self.state_snapshot_provider is None:
            return
        try:
            snap = self.state_snapshot_provider()
        except Exception as e:
            print(f"⚠️  state record snapshot failed at {reason}: {e}")
            return
        if not snap:
            return
        self._window_state_record = {
            "window_initial": snap,
            "per_turn_deltas": [],
            "_last_full_state": snap,  # for delta computation, stripped before grading
        }

    def _record_post_exec_state(self):
        """Capture state after an action and append a delta entry to the current window record."""
        if self.state_snapshot_provider is None or self._window_state_record is None:
            return
        try:
            cur = self.state_snapshot_provider()
        except Exception as e:
            print(f"⚠️  state record snapshot failed at post-exec: {e}")
            return
        if not cur:
            return
        prev = self._window_state_record.get("_last_full_state")
        delta = self._compute_state_delta(prev, cur)
        action_summary = (self.last_exec_action_content or "").strip().splitlines()[0][:80] if self.last_exec_action_content else ""
        turn_idx = len(self._window_state_record["per_turn_deltas"])
        self._window_state_record["per_turn_deltas"].append({
            "turn": turn_idx,
            "action": action_summary,
            "delta": delta,
        })
        self._window_state_record["_last_full_state"] = cur

    @staticmethod
    def _compute_state_delta(prev: Optional[dict], cur: Optional[dict]) -> dict:
        """Compute a structured delta between two state snapshots.
        See agent/state_record.py:compute_state_delta for the full diff schema."""
        return _compute_state_delta_impl(prev, cur)

    def _build_window_state_record_for_grading(self) -> Optional[dict]:
        """Snapshot the current state (= window_final), build the record JSON to ship to grader."""
        if self._window_state_record is None or self.state_snapshot_provider is None:
            return None
        try:
            final = self.state_snapshot_provider()
        except Exception as e:
            print(f"⚠️  state record snapshot failed at grading: {e}")
            final = None
        out = {
            "window_initial": self._window_state_record.get("window_initial"),
            "per_turn_deltas": list(self._window_state_record.get("per_turn_deltas", [])),
            "window_final": final,
        }
        return out

    def _reset_context_after_llm_request_failure(self, error: Exception):
        """在上下文超限且LLM请求失败时重置活动上下文，并保留重置标记与新截图。"""
        # Try self-reward out-of-band before the hard reset. The main LLM call
        # just failed on context size; the reward call uses the same context plus
        # a short grading prompt, so it may also fail — that's OK, we log and
        # proceed to the reset.
        if self.enable_self_reward and not self.self_reward_in_flight:
            self._run_out_of_band_self_reward("hard_reset")
        self._notify_roll_window_complete()
        reset_ts = time.time()
        error_text = str(error).strip() or error.__class__.__name__
        if len(error_text) > 500:
            error_text = error_text[:500] + "...(truncated)"

        with self.messages_lock:
            # These observations were never read by the failed request. Keep
            # them for the rebuilt context, including a failed summary turn.
            staged_count = len(self.staging_buffer)

            system_messages = [
                msg
                for msg in self.conversation_history
                if isinstance(msg, dict) and msg.get("role") == "system"
            ]
            rebuilt_history = list(system_messages)

            if self.eval_mode:
                pinned_initial_user = self._find_initial_user_message_locked()
                if pinned_initial_user is not None:
                    rebuilt_history.append(pinned_initial_user)

            self.context_reset_count += 1
            reset_message = {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"[Context Reset at {self._format_timestamp(reset_ts)}] "
                            "LLM request failed after retries while conversation exceeded round limit. "
                            "Context was reset to recover. "
                            f"staged_buffer_recovered={staged_count}. "
                            f"error={error_text}\n"
                            f"Observation mode is currently `{self._observe_mode_text()}`."
                            + self._format_last_exec_hint()
                        ),
                    }
                ],
                "context_reset_first_message": True,
                "context_reset_reason": "llm_request_failed_after_round_overflow",
                "context_reset_count": self.context_reset_count,
                "mcbots_observe_mode": self._observe_mode_text(),
            }

            rebuilt_history.append(reset_message)
            self.conversation_history = rebuilt_history
            self._append_full_history_locked(reset_message)
            self.message_revision += 1
            self._replay_staged_observations_after_reset_locked("context")

        self._save_messages()
        self._capture_post_reset_snapshot(anchor_ts=reset_ts)
        # 硬重置若撞上 summarize turn，应清掉等待标记，避免卡在 awaiting_summary 永远等不到回复
        self.awaiting_summary = False
        self.pending_auto_summarize_total_tokens = None
        self.pending_auto_summarize_reason = None
        self.previous_response_id = None
        print(
            "🧹 Context reset completed "
            f"(count={self.context_reset_count}, eval_mode={'on' if self.eval_mode else 'off'})"
        )

    # ===== 自动总结（token 阈值驱动） =====

    def _extract_total_tokens(self, usage) -> Optional[int]:
        """从 usage 对象里提取 total_tokens；兼容 OpenAI CompletionUsage 对象与 dict 两种形态。"""
        if usage is None:
            return None
        try:
            if isinstance(usage, dict):
                total = usage.get("total_tokens")
                if isinstance(total, int) and total > 0:
                    return total
                p = usage.get("prompt_tokens") or 0
                c = usage.get("completion_tokens") or 0
                total = int(p) + int(c)
                return total if total > 0 else None
            total = getattr(usage, "total_tokens", None)
            if isinstance(total, int) and total > 0:
                return total
            p = getattr(usage, "prompt_tokens", 0) or 0
            c = getattr(usage, "completion_tokens", 0) or 0
            total = int(p) + int(c)
            return total if total > 0 else None
        except Exception:
            return None

    def _should_trigger_summarize(self, usage=None) -> Optional[Tuple[str, int]]:
        """检查所有 summarize 红线，返回命中的 (reason, count) 或 None。

        四条独立红线，任一命中即返回（按下列优先级）：
          - round_limit:     conversation_history 非 system 消息数 > max_conversation_rounds
          - image_limit:     活动上下文图片数 > max_images_in_context
          - token_threshold: 上一轮 total_tokens > auto_summarize_token_threshold（需 usage）
          - turn_threshold:  当前窗口 assistant 轮数 >= auto_summarize_turn_threshold
        usage 为 None（pre-call 场景）时跳过 token 检查。
        已在等摘要 / 已 queue 时返回 None。
        """
        if self.awaiting_summary:
            return None
        if self.pending_auto_summarize_total_tokens is not None:
            return None

        with self.messages_lock:
            conversation_messages = [
                m for m in self.conversation_history
                if isinstance(m, dict) and m.get("role") != "system"
            ]
            n_rounds = len(conversation_messages)
            n_images = sum(self._count_images_in_message(m) for m in conversation_messages)
            window_turns = self._count_assistant_responses_in_current_window_locked()

        max_conversation_rounds = int(getattr(self, "max_conversation_rounds", 0) or 0)
        if max_conversation_rounds > 0 and n_rounds > max_conversation_rounds:
            return ("round_limit", n_rounds)
        if self.max_images_in_context > 0 and n_images > self.max_images_in_context:
            return ("image_limit", n_images)
        if usage is not None and self.auto_summarize_token_threshold > 0:
            total = self._extract_total_tokens(usage)
            if total is not None and total > self.auto_summarize_token_threshold:
                return ("token_threshold", total)
        turn_threshold = int(getattr(self, "auto_summarize_turn_threshold", 0) or 0)
        if turn_threshold > 0 and window_turns >= turn_threshold:
            return ("turn_threshold", window_turns)
        return None

    def _queue_summary_request(self, trigger_count: int, *, reason: str = "token_threshold"):
        """Record that the next safe action boundary should receive a summary request.

        `trigger_count` is the measurement that tripped the trigger:
          - reason="token_threshold": total_tokens on the most recent response
          - reason="image_limit":     number of images currently in active context
        """
        total = int(trigger_count)
        if total <= 0:
            return
        self.pending_auto_summarize_total_tokens = total
        self.pending_auto_summarize_reason = reason
        if reason == "image_limit":
            print(
                f"📝 Auto-summarize queued (reason=image_limit, "
                f"images={total}, threshold={self.max_images_in_context})"
            )
        elif reason == "round_limit":
            print(
                f"📝 Auto-summarize queued (reason=round_limit, "
                f"rounds={total} > max={self.max_conversation_rounds})"
            )
        elif reason == "turn_threshold":
            print(
                f"📝 Auto-summarize queued (reason=turn_threshold, "
                f"window_turns={total} >= {self.auto_summarize_turn_threshold})"
            )
        else:
            print(
                f"📝 Auto-summarize queued "
                f"(total_tokens={total} > threshold={self.auto_summarize_token_threshold})"
            )

    def _inject_summary_request(self, trigger_count: int, *, reason: str = "token_threshold"):
        """插入一条 user message 请求模型做总结，下一轮 LLM 自动被 message_revision 触发。"""
        ts = time.time()
        if reason == "image_limit":
            trigger_line = (
                f"Your active context has accumulated **{trigger_count}** images, "
                f"exceeding the configured maximum (`{self.max_images_in_context}`). "
            )
        elif reason == "round_limit":
            trigger_line = (
                f"Your active conversation has reached **{trigger_count}** messages, "
                f"exceeding the configured round limit (`{self.max_conversation_rounds}`). "
            )
        elif reason == "turn_threshold":
            trigger_line = (
                f"You have taken **{trigger_count}** turns in the current window "
                f"(threshold: {self.auto_summarize_turn_threshold}). "
            )
        else:
            trigger_line = (
                f"Your combined context+response tokens on the previous turn totalled "
                f"**{trigger_count}** (threshold: {self.auto_summarize_token_threshold}). "
            )
        if self.auto_summary_profile == "navigation":
            summary_instructions = (
                "Before continuing, write a concise summary of your navigation progress. "
                "This summary will be the only previous context carried into the next turn. "
                "When useful, consider mentioning your current location or orientation, route progress, "
                "relevant observations, obstacles or failed approaches, and a sensible next step. "
                "Include only what is relevant to continuing the task effectively. "
                "Keep the original navigation task unchanged, and do not invent progress or waypoint information. "
                "At the end of your response, put the final summary inside exactly one "
                "`<summary>...</summary>` block. Only the content of the last complete `<summary>` block "
                "will be retained. Do not put an executable `<action>` block inside the summary."
            )
        else:
            summary_instructions = (
                "Before continuing, write a concise summary that will be the ONLY context carried into the next turn "
                "(everything else is dropped). Keep it compact but structured enough for the next turn to act intelligently. "
                "Include: long-term goal, medium-term objective, immediate next step, what you have accomplished, "
                "relevant bot/world state (position, inventory, notable locations, visible opportunities or dangers, "
                "anything that just failed or where you got stuck), and any assumptions or constraints that matter. "
                "Maintain goal stability across windows: the long-term goal should change rarely and only when it is no longer viable or no longer matches the task; "
                "the medium-term objective may update when the current phase changes; the immediate next step should be the most frequently updated item. "
                "Reassess as you write: if the original goal is still reasonable given your actual situation, continue it. "
                "If the situation has made the original plan impractical "
                "(e.g. you are stuck in a hole you cannot climb out of, you have respawned far from the target, "
                "you have lost required items, the environment has changed in a way that blocks the plan), "
                "you may revise the goal or change strategy — state the new plan clearly in the summary so the next "
                "turn resumes on the right track. "
                "Respond **with the summary text only** — do NOT emit an `<action>...</action>` block this turn. "
                "The reply will be treated as the summary and your context will be reset immediately after."
            )
        request_msg = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"[Summary Request at {self._format_timestamp(ts)}] "
                        + trigger_line
                        + summary_instructions
                    ),
                }
            ],
            "mcbots_summary_request": True,
            "mcbots_summary_reason": reason,
            "mcbots_summary_trigger_count": int(trigger_count),
            "mcbots_summary_threshold": int(self.auto_summarize_token_threshold),
            "mcbots_summary_image_limit": int(self.max_images_in_context),
            "mcbots_summary_profile": self.auto_summary_profile,
        }
        with self.messages_lock:
            self.conversation_history.append(request_msg)
            self._append_full_history_locked(request_msg)
            self.message_revision += 1
            self.awaiting_summary = True
            self.pending_auto_summarize_total_tokens = None
            self.pending_auto_summarize_reason = None
        self._save_messages()
        if reason == "image_limit":
            print(
                f"📝 Auto-summarize requested (reason=image_limit, "
                f"images={trigger_count}, threshold={self.max_images_in_context})"
            )
        elif reason == "round_limit":
            print(
                f"📝 Auto-summarize requested (reason=round_limit, "
                f"rounds={trigger_count} > max={self.max_conversation_rounds})"
            )
        elif reason == "turn_threshold":
            print(
                f"📝 Auto-summarize requested (reason=turn_threshold, "
                f"window_turns={trigger_count} >= {self.auto_summarize_turn_threshold})"
            )
        else:
            print(
                f"📝 Auto-summarize requested "
                f"(total_tokens={trigger_count} > threshold={self.auto_summarize_token_threshold})"
            )

    def _inject_pending_summary_request(self) -> bool:
        """Stop once, collect the terminal result, then freeze summary input."""
        stop_action = None
        with self.messages_lock:
            total = self.pending_auto_summarize_total_tokens
            if total is None or self.awaiting_summary or self.llm_inflight:
                return False
            active = getattr(self, "current_exec_action", None)
            if active is not None and not active.get("finished", False):
                if active.get("summary_stop_requested"):
                    return False
                active["summary_stop_requested"] = True
                stop_action = Action(type="stop_execute", target_action_id=active["id"])
            elif getattr(self, "pending_exec_result", False):
                # End/snapshot handling can clear current_exec_action before
                # stdout/stderr are appended. Do not freeze in that interval.
                return False
            reason = self.pending_auto_summarize_reason or "token_threshold"

        if stop_action is not None:
            self._clear_paused_observe_after_snapshot()
            try:
                self.action_sender(stop_action)
            except Exception:
                with self.messages_lock:
                    active.pop("summary_stop_requested", None)
                raise
            return False

        if self.enable_self_reward and not self.self_reward_in_flight:
            self._run_out_of_band_self_reward(reason)
        with self.messages_lock:
            # Recheck after the out-of-band call and serialize competing
            # observation/decision callbacks that can both reach this boundary.
            if (self.pending_auto_summarize_total_tokens is None
                    or self.awaiting_summary or self.llm_inflight
                    or getattr(self, "pending_exec_result", False)):
                return False
            active = getattr(self, "current_exec_action", None)
            if active is not None and not active.get("finished", False):
                return False
            self._flush_staging_buffer_locked(with_async_window_markers=False)
            self._inject_summary_request(total, reason=reason)
        return True

    @staticmethod
    def _is_valid_summary_text(summary_text: str) -> bool:
        """Return whether a summary response is safe to use as reset context."""
        text = (summary_text or "").strip()
        if not text:
            return False
        # A summary turn must never be allowed to smuggle an executable action
        # through the reset boundary. Models occasionally answer the stale
        # navigation request when summary injection races with an in-flight call.
        return re.search(r"<\s*action(?:\s|>)", text, flags=re.IGNORECASE) is None

    def _extract_summary_text(self, response_text: str) -> Optional[str]:
        """Extract reset memory according to the active summary profile."""
        text = response_text or ""
        if self.auto_summary_profile != "navigation":
            stripped = text.strip()
            return stripped if self._is_valid_summary_text(stripped) else None

        openings = list(re.finditer(r"<\s*summary\s*>", text, flags=re.IGNORECASE))
        if not openings:
            return None
        start = openings[-1].end()
        closing = re.search(r"</\s*summary\s*>", text[start:], flags=re.IGNORECASE)
        if closing is None:
            return None
        summary = text[start : start + closing.start()].strip()
        if not summary or re.search(r"<\s*action(?:\s|>)", summary, flags=re.IGNORECASE):
            return None
        return summary

    def _reject_invalid_summary_response(self, assistant_msg, summary_text: str) -> None:
        """Keep the old context and request a real summary after a bad response."""
        ts = time.time()
        response_record = assistant_msg.model_dump()
        response_record["mcbots_invalid_summary_response"] = True
        if self.auto_summary_profile == "navigation":
            retry_instruction = (
                "Put the final navigation summary inside a complete "
                "`<summary>...</summary>` block. Do not put an executable "
                "`<action>` block inside the summary."
            )
        else:
            retry_instruction = (
                "Do not emit an `<action>...</action>` block. Respond now with "
                "summary text only, preserving the task goal, completed progress, "
                "current state, obstacles, and immediate next step."
            )
        feedback_msg = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"[Invalid summary response at {self._format_timestamp(ts)}] "
                        "Your previous reply did not contain a usable summary and has NOT been executed. "
                        + retry_instruction
                    ),
                }
            ],
            "mcbots_invalid_summary_feedback": True,
        }
        with self.messages_lock:
            self.conversation_history.append(response_record)
            self._append_full_history_locked(response_record)
            self.conversation_history.append(feedback_msg)
            self._append_full_history_locked(feedback_msg)
            self.message_revision += 2
            self.llm_inflight = False
            # Keep awaiting_summary true. The next decision must retry the
            # summary instead of accepting this response or running its action.
            self.awaiting_summary = True
        self._save_messages()
        preview = (summary_text or "")[:200]
        print(f"⚠️  Invalid summary response rejected; retrying summary: {preview}")

    def _enter_forced_summary_barrier(
        self,
        trigger_count: int,
        *,
        reason: str,
        withheld_action: Optional[Action],
    ) -> None:
        """Stop model control and force a summary before any further action."""
        self._queue_summary_request(trigger_count, reason=reason)
        ts = time.time()
        action_type = withheld_action.type if withheld_action is not None else "none"
        barrier_msg = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"[Summary barrier at {self._format_timestamp(ts)}] "
                        f"The previous `{action_type}` response was not executed because "
                        "the context reached an automatic-summary boundary. Any earlier "
                        "running command is being stopped. Summarize the state after the "
                        "stop; do not assume the withheld action occurred."
                    ),
                }
            ],
            "mcbots_summary_barrier": True,
            "mcbots_withheld_action_type": action_type,
        }
        with self.messages_lock:
            self.conversation_history.append(barrier_msg)
            self._append_full_history_locked(barrier_msg)
            self.message_revision += 1
            active_exec = bool(
                self.current_exec_action is not None
                and not self.current_exec_action.get("finished", False)
            )

        if active_exec:
            # Use the same stop-once / terminal-result gate as pre-call image
            # limits. A stop request alone is not a completed action boundary.
            self._inject_pending_summary_request()
            return

        # Even without an active Remote Bash task, capture a final visual anchor
        # before freezing the old context.
        self._capture_post_stop_execute_snapshot(anchor_ts=ts)
        self._inject_pending_summary_request()

    def _count_assistant_responses_in_current_window_locked(self) -> int:
        """Count assistant responses in the current window."""
        n = 0
        for m in self.conversation_history:
            if not isinstance(m, dict):
                continue
            if m.get("role") != "assistant":
                continue
            n += 1
        return n

    def _self_reward_file_for_window(self, window_index: int) -> Optional[Path]:
        """Return Path for self_reward_{N}.json in record_dir (None if no record_dir yet)."""
        if not self.record_dir:
            return None
        return self.record_dir / f"self_reward_{window_index}.json"

    def _build_self_reward_prompt_text(
        self,
        *,
        window_index: int,
        n_responses: int,
        trigger: str,
        state_snippet: str,
        ts: float,
    ) -> str:
        """Build the grading prompt text. Thin wrapper over agent.grading_prompt.build_prompt —
        full template lives there for editability."""
        return _build_self_reward_prompt(
            window_index=window_index,
            n_responses=n_responses,
            trigger=trigger,
            state_snippet=state_snippet,
            timestamp=self._format_timestamp(ts),
            specific_criteria_block=self._render_specific_criteria_blocks(),
            share_system_prompt=getattr(
                self,
                "self_reward_share_system_prompt",
                False,
            ),
        )

    def _active_specific_criteria(self) -> Optional[str]:
        """Most recent non-empty `effective` spec from history, or None."""
        for entry in reversed(self.spec_criteria_history):
            eff = entry.get("effective")
            if eff:
                return eff
        return None

    def _render_specific_criteria_blocks(self) -> str:
        """Render the currently active specific_criteria with provenance attrs.
        Earlier per-window specs remain in spec_criteria_history.json on disk for
        post-hoc analysis, but the grader sees only the active rule (Markov-style)
        — annotated with which window emitted it and how many windows it has carried
        over for, so the grader can judge staleness."""
        active = self._active_specific_criteria()
        if not active:
            return (
                "<specific_criteria>\n"
                "(none yet — no spec has been emitted in any prior window)\n"
                "</specific_criteria>\n\n"
            )
        origin = None
        for entry in reversed(self.spec_criteria_history):
            if entry.get("emitted") == active:
                origin = entry.get("window")
                break
        attrs = f' emitted_by_window="{origin}"' if origin is not None else ""
        return (
            f"<specific_criteria{attrs}>\n"
            f"{active}\n"
            "</specific_criteria>\n\n"
        )

    def _spec_criteria_history_file(self) -> Optional[Path]:
        if self.record_dir is None:
            return None
        return self.record_dir / "spec_criteria_history.json"

    def _run_out_of_band_self_reward(self, trigger: str) -> bool:
        """Fire a one-shot out-of-band LLM call to grade the current window's
        assistant responses. Saves the result to self_reward_{N}.json. The main
        conversation is never modified. Returns True if a reward entry was saved.

        Called synchronously at each window boundary (summary reset, hard reset,
        end-of-rollout). Blocks the caller for the duration of one LLM call."""
        if not self.enable_self_reward:
            return False
        if self.self_reward_in_flight:
            return False

        ts_start = time.time()
        with self.messages_lock:
            n_responses = self._count_assistant_responses_in_current_window_locked()
            if n_responses <= 0:
                return False
            if getattr(self, "self_reward_share_system_prompt", False):
                # Mirror the main LLM call (agent.py:2664-2668) exactly so the prefix
                # matches the agent's prior request and the server's KV cache stays warm.
                # Role switch into grader mode happens inside the trailing user message.
                snapshot_messages = [
                    self._sanitize_message_for_llm(msg)
                    for msg in self.conversation_history
                    if isinstance(msg, dict)
                ]
            else:
                snapshot_messages = [{"role": "system", "content": GRADER_SYSTEM_PROMPT}]
                for msg in self.conversation_history:
                    if not isinstance(msg, dict):
                        continue
                    sanitized = self._sanitize_message_for_llm(msg)
                    if sanitized.get("role") == "system":
                        continue
                    snapshot_messages.append(sanitized)

        # Inject the per-window state record (window_initial + per_turn_deltas + window_final)
        # into the grading prompt so the grader can compute true per-turn progress from world state.
        state_snippet = ""
        record = self._build_window_state_record_for_grading()
        if record:
            state_snippet = (
                "**Per-window state record** — `window_initial` is the state at the start of THIS window; "
                "`per_turn_deltas[i]` is the structured diff produced by zero-based turn i's action "
                "and should match `rewards[i].turn == i` (only changed fields are listed; "
                "`{\"no_change\": true}` means nothing measurable changed); `window_final` is the state right now. "
                "Use this as authoritative ground truth — the agent's prose may exaggerate or misremember progress, "
                "but these state diffs come directly from the world.\n"
                "```json\n"
                f"{json.dumps(record, indent=2, ensure_ascii=False)}\n"
                "```\n\n"
            )

        window_index = self.self_reward_window_count + 1
        prompt_text = self._build_self_reward_prompt_text(
            window_index=window_index,
            n_responses=n_responses,
            trigger=trigger,
            state_snippet=state_snippet,
            ts=ts_start,
        )
        snapshot_messages.append({
            "role": "user",
            "content": [{"type": "text", "text": prompt_text}],
        })

        request_kwargs = {
            "model": self.model,
            "messages": snapshot_messages,
            **self.sampling_params,
        }
        if self.request_extra_body:
            request_kwargs["extra_body"] = self.request_extra_body

        print(f"🎯 Self-reward window {window_index} starting (trigger={trigger}, n={n_responses})")
        self.self_reward_in_flight = True
        response_text: Optional[str] = None
        reasoning_content: Optional[str] = None
        # Retry on transient failures (timeout / connection error) — large grading prompts
        # for big windows (e.g. N=100) routinely overrun the OpenAI client default timeout.
        max_attempts = 3
        last_err: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._create_model_completion(
                    request_kwargs,
                    continue_response_chain=False,
                )
                response_text = ""
                msg = self._assistant_message_from_response(response)
                if msg is not None:
                    self._maybe_recover_reasoning_from_content(msg)
                    # Kimi K2.5 quirk: when the whole answer is treated as "thinking",
                    # the JSON grading object can land in `reasoning_content` and `.content`
                    # comes back empty. Fall back to reasoning_content so we don't lose the grade.
                    content = (getattr(msg, "content", None) or "").strip()
                    rc = (getattr(msg, "reasoning_content", None) or "").strip()
                    reasoning_content = rc or None
                    if content:
                        response_text = content
                    elif rc:
                        response_text = rc
                        print(f"ℹ️  Self-reward window {window_index}: content empty, recovered grade from reasoning_content ({len(rc)} chars)")
                last_err = None
                break  # success (even if response_text is empty string, that's a "successful" empty completion)
            except Exception as e:
                last_err = e
                if attempt < max_attempts:
                    print(f"⚠️  Self-reward out-of-band call failed (attempt {attempt}/{max_attempts}, trigger={trigger}): {e} — retrying")
                    time.sleep(2.0)
                    continue
                # Final failure
                print(f"⚠️  Self-reward out-of-band call failed (attempt {attempt}/{max_attempts}, trigger={trigger}): {e} — giving up")
                response_text = None
        ts_end = time.time()

        saved = False
        if response_text is not None:
            self._save_self_reward_window(
                response_text=response_text,
                reasoning_content=reasoning_content,
                trigger=trigger,
                n_responses=n_responses,
                window_index=window_index,
                ts_start=ts_start,
                ts_end=ts_end,
            )
            self.self_reward_window_count = window_index
            saved = True

        # Reset per-window state record so the next window starts with a fresh
        # `window_initial` snapshot and an empty per_turn_deltas list. Without this,
        # deltas accumulate across windows and the grader sees more turns than
        # n_responses claims (e.g. v9 w2 had n_responses=41 but kimi graded 85).
        self._reset_window_state_record(reason=f"post_grading_{trigger}")
        self.self_reward_in_flight = False
        return saved

    def _save_self_reward_window(
        self,
        *,
        response_text: str,
        reasoning_content: Optional[str],
        trigger: str,
        n_responses: int,
        window_index: int,
        ts_start: float,
        ts_end: float,
    ):
        """Parse the grading response and write it to self_reward_{window_index}.json."""
        parsed = None
        text = (response_text or "").strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            blob = m.group(0)
            try:
                parsed = json.loads(blob)
            except Exception:
                # Lenient retry: strip leading `+` from numeric literals OUTSIDE strings.
                # Kimi often emits a leading plus on positive ternary scores
                # (invalid JSON per spec).
                # Walk char-by-char so `+9` inside `"reason"` strings is preserved.
                fixed_chars: list = []
                in_str = False
                escape = False
                i = 0
                while i < len(blob):
                    c = blob[i]
                    if in_str:
                        fixed_chars.append(c)
                        if escape:
                            escape = False
                        elif c == '\\':
                            escape = True
                        elif c == '"':
                            in_str = False
                        i += 1
                        continue
                    if c == '"':
                        in_str = True
                        fixed_chars.append(c)
                    elif c == '+' and i + 1 < len(blob) and blob[i + 1].isdigit():
                        # skip the '+' (do not append)
                        pass
                    else:
                        fixed_chars.append(c)
                    i += 1
                blob_fixed = ''.join(fixed_chars)
                try:
                    parsed = json.loads(blob_fixed)
                except Exception as e:
                    print(f"⚠️  Self-reward window {window_index}: failed to parse JSON ({e}); saving raw text only")

        # Extract specific_criteria emitted this window. Treat empty/whitespace/"null"/"none" as no-update.
        emitted: Optional[str] = None
        if isinstance(parsed, dict):
            raw_emitted = parsed.get("specific_criteria")
            if isinstance(raw_emitted, str):
                s = raw_emitted.strip()
                if s and s.lower() not in ("null", "none"):
                    emitted = s
        prior_active = self._active_specific_criteria()
        effective = emitted if emitted else prior_active
        hist_entry = {
            "window": window_index,
            "emitted": emitted,
            "effective": effective,
        }
        self.spec_criteria_history.append(hist_entry)
        if emitted:
            print(f"🧭 Self-reward window {window_index}: new specific_criteria emitted (active starting next window)")
        elif effective:
            print(f"🧭 Self-reward window {window_index}: specific_criteria inherited from prior window")
        spec_path = self._spec_criteria_history_file()
        if spec_path is not None:
            try:
                with open(spec_path, "w", encoding="utf-8") as f:
                    json.dump(self.spec_criteria_history, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"⚠️  Failed to save spec_criteria history: {e}")

        entry = {
            "window_index": window_index,
            "trigger": trigger,
            "n_responses": n_responses,
            "raw_response": response_text,
            "reasoning_content": reasoning_content,
            "parsed": parsed,
            "spec_criteria_emitted": emitted,
            "spec_criteria_effective_for_this_window": effective,
            "spec_criteria_prior_active": prior_active,
            "ts_start": self._format_timestamp(ts_start),
            "ts_end": self._format_timestamp(ts_end),
            "elapsed_sec": round(ts_end - ts_start, 3),
        }
        self.self_reward_history.append(entry)
        path = self._self_reward_file_for_window(window_index)
        if path is None:
            print(f"⚠️  Self-reward window {window_index}: record_dir not set; entry not persisted")
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entry, f, indent=2, ensure_ascii=False)
            print(f"🎯 Self-reward window {window_index} saved: {path}")
        except Exception as e:
            print(f"⚠️  Failed to save self-reward window {window_index}: {e}")

    def _replay_staged_observations_after_reset_locked(self, reset_kind: str) -> int:
        """Deliver unread observations after the reset marker, once, in order.

        The caller holds messages_lock. Archive-only recovery loses stdout and
        stderr that arrived after the request's frozen input. Append separate
        messages so replay metadata does not get merged into the summary.
        """
        if not self.staging_buffer:
            return 0
        staged = sorted(self.staging_buffer, key=lambda item: (item[0], item[1]))
        self.staging_buffer.clear()
        notice = {
            "role": "user",
            "content": [{
                "type": "text",
                "text": (
                    f"[Unread observations after {reset_kind} reset] "
                    "The following observations arrived after the previous request's "
                    "input was frozen and were not seen by that request. Use them to "
                    "update the state described above before choosing the next action."
                ),
            }],
            "mcbots_unread_observation_notice": True,
        }
        self.conversation_history.append(notice)
        self._append_full_history_locked(notice)
        self.message_revision += 1
        for arrival_ts, _, message in staged:
            replay = copy.deepcopy(message)
            replay[f"mcbots_staged_replayed_after_{reset_kind}_reset"] = True
            self.conversation_history.append(replay)
            self._append_full_history_locked(replay)
            self.message_revision += 1
            if self._message_has_image(replay):
                self._on_image_committed(arrival_ts)
        return len(staged)

    def _perform_summary_reset(self, summary_text: str):
        """拿到模型的总结回复后，用 summary 替换活动上下文；保留 system + 可选的 pinned initial user。"""
        # Summary reset is a window boundary; notify ROLL so it can emit the
        # current window (matching the hard-reset path). The per-window
        # self-reward for this window has already been graded before the summary
        # request was injected (see _inject_pending_summary_request).
        self._notify_roll_window_complete()
        reset_ts = time.time()
        summary = (summary_text or "").strip() or "(empty summary — model returned no content)"

        with self.messages_lock:
            # The summary cannot include observations that arrived after its
            # input was frozen. Replay them after the new memory instead.
            staged_count = len(self.staging_buffer)

            system_messages = [
                msg
                for msg in self.conversation_history
                if isinstance(msg, dict) and msg.get("role") == "system"
            ]
            rebuilt_history = list(system_messages)

            if self.eval_mode:
                pinned_initial_user = self._find_initial_user_message_locked()
                if pinned_initial_user is not None:
                    rebuilt_history.append(pinned_initial_user)

            self.summarize_count += 1
            summary_reset_msg = {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"[Context Summarized at {self._format_timestamp(reset_ts)}] "
                            "Your previous context is too long and you produced the summary below. "
                            "Treat the summary as your authoritative memory of everything before this point; "
                            "the older messages are no longer visible. "
                            f"Observation mode is currently `{self._observe_mode_text()}`."
                            + self._format_last_exec_hint()
                            + "\n\n**Summary:**\n"
                            + summary
                            + "\n\nContinue your task from here."
                        ),
                    }
                ],
                "mcbots_summary_reset": True,
                "mcbots_summary_count": self.summarize_count,
                "mcbots_summary_staged_recovered": staged_count,
                "mcbots_observe_mode": self._observe_mode_text(),
            }
            rebuilt_history.append(summary_reset_msg)
            self.conversation_history = rebuilt_history
            self._append_full_history_locked(summary_reset_msg)
            self.message_revision += 1
            self._replay_staged_observations_after_reset_locked("summary")
            self.awaiting_summary = False
            self.pending_auto_summarize_total_tokens = None
            self.pending_auto_summarize_reason = None

        # The summary becomes the authoritative start of a new Responses chain.
        self.previous_response_id = None

        self._save_messages()
        self._capture_post_reset_snapshot(anchor_ts=reset_ts)
        print(
            f"🗜️  Context summarized and reset (count={self.summarize_count}, "
            f"eval_mode={'on' if self.eval_mode else 'off'})"
        )

    def _format_timestamp(self, value: float) -> str:
        """将 Unix 时间戳格式化成人类可读时间。"""
        try:
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S.%f")[:-4]
        except Exception:
            return f"{value:.2f}"

    def _call_chat_completion_with_watchdog(self, req: dict):
        """Invoke client.chat.completions.create() with start/end timing logs
        and a heartbeat watchdog that reports if the request is still pending."""
        attempt_id = self.llm_request_total
        start_ts = time.time()
        watchdog_interval = self.llm_watchdog_interval_sec
        stop_event = threading.Event()

        def _heartbeat():
            while not stop_event.is_set():
                if stop_event.wait(watchdog_interval):
                    return
                elapsed = time.time() - start_ts
                print(
                    f"⏱️  LLM request still pending after {elapsed:.1f}s "
                    f"(attempt #{attempt_id}, timeout={self.llm_request_timeout_sec}s, "
                    f"max_retries={self.llm_max_retries})"
                )

        watchdog_thread = None
        if watchdog_interval > 0:
            watchdog_thread = threading.Thread(
                target=_heartbeat, name="llm-watchdog", daemon=True
            )
            watchdog_thread.start()

        print(
            f"🚀 LLM request start (attempt #{attempt_id}, model={self.model}, "
            f"timeout={self.llm_request_timeout_sec}s, max_retries={self.llm_max_retries}, "
            f"rate_limit_retries={self.llm_429_max_retries})"
        )
        try:
            resp = self._run_provider_request(
                lambda: self.client.chat.completions.create(**req),
                attempt_id=attempt_id,
            )
            elapsed = time.time() - start_ts
            print(f"✅ LLM request done in {elapsed:.2f}s (attempt #{attempt_id})")
            return resp
        except Exception as e:
            elapsed = time.time() - start_ts
            print(
                f"❌ LLM request raised after {elapsed:.2f}s "
                f"(attempt #{attempt_id}): {type(e).__name__}: {e}"
            )
            raise
        finally:
            stop_event.set()
            if watchdog_thread is not None:
                watchdog_thread.join(timeout=1.0)

    @staticmethod
    def _is_rate_limit_error(error: BaseException) -> bool:
        return getattr(error, "status_code", None) == 429

    @staticmethod
    def _is_transient_image_parse_error(error: BaseException) -> bool:
        """Recognize GLM's intermittent image decoder failure, not generic 400s."""
        if getattr(error, "status_code", None) != 400:
            return False
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            detail = body.get("error")
            if isinstance(detail, dict) and str(detail.get("code")) == "1210":
                return True
        message = str(error)
        return "1210" in message and "图片输入格式/解析错误" in message

    @staticmethod
    def _retry_after_seconds(error: BaseException) -> Optional[float]:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        raw_value = headers.get("retry-after")
        if raw_value is None:
            return None
        try:
            return max(float(raw_value), 0.0)
        except (TypeError, ValueError):
            return None

    def _rate_limit_retry_delay(self, error: BaseException, retry_number: int) -> float:
        exponent = max(int(retry_number) - 1, 0)
        backoff = min(
            self.llm_429_backoff_base_sec * (2**exponent),
            self.llm_429_backoff_max_sec,
        )
        if self.llm_429_backoff_jitter > 0:
            backoff *= random.uniform(
                1.0 - self.llm_429_backoff_jitter,
                1.0 + self.llm_429_backoff_jitter,
            )
        retry_after = self._retry_after_seconds(error)
        return max(backoff, retry_after or 0.0)

    def _run_provider_request(self, request: Callable[[], Any], *, attempt_id: int) -> Any:
        """Run one logical request through the gate with bounded transient retries."""
        rate_limit_retries = 0
        image_parse_retries = 0
        while True:
            try:
                if self.llm_request_gate is None:
                    return request()
                with self.llm_request_gate.request() as lease:
                    waited = lease.rate_wait_sec + lease.inflight_wait_sec
                    if waited >= 0.05:
                        print(
                            "🚦 LLM gate admitted request "
                            f"after {waited:.2f}s "
                            f"(attempt #{attempt_id}, slot={lease.slot_index}, "
                            f"rate_wait={lease.rate_wait_sec:.2f}s, "
                            f"inflight_wait={lease.inflight_wait_sec:.2f}s)"
                        )
                    return request()
            except Exception as error:
                if self._is_rate_limit_error(error):
                    if rate_limit_retries >= self.llm_429_max_retries:
                        raise
                    rate_limit_retries += 1
                    delay = self._rate_limit_retry_delay(error, rate_limit_retries)
                    print(
                        "⚠️  Provider returned HTTP 429; "
                        f"retry {rate_limit_retries}/{self.llm_429_max_retries} "
                        f"after {delay:.2f}s (attempt #{attempt_id})"
                    )
                    if self.llm_request_gate is not None:
                        self.llm_request_gate.defer_after_rate_limit(delay)
                    else:
                        time.sleep(delay)
                    continue
                if self._is_transient_image_parse_error(error):
                    if image_parse_retries >= 3:
                        raise
                    image_parse_retries += 1
                    delay = min(10.0 * (2 ** (image_parse_retries - 1)), 30.0)
                    print(
                        "⚠️  Provider returned transient image parse error 1210; "
                        f"retry {image_parse_retries}/3 after {delay:.2f}s "
                        f"(attempt #{attempt_id})"
                    )
                    time.sleep(delay)
                    continue
                raise

    def _create_chat_completion(self, request_kwargs: dict):
        """创建聊天补全请求，并处理已知 provider 兼容性问题。"""
        req = dict(request_kwargs)
        for _ in range(2):
            try:
                return self._call_chat_completion_with_watchdog(req)
            except BadRequestError as e:
                message = str(e)
                match = re.search(r"invalid ([a-zA-Z_]+): only ([^ ]+) is allowed", message)
                if not match:
                    raise
                param_name = match.group(1)
                raw_allowed = match.group(2).strip().rstrip(".,")
                if param_name not in req:
                    raise
                current_value = req.get(param_name)
                if current_value is None:
                    raise
                if raw_allowed.lower() in {"true", "false"}:
                    allowed_value = raw_allowed.lower() == "true"
                else:
                    try:
                        allowed_value = int(raw_allowed) if re.fullmatch(r"-?\d+", raw_allowed) else float(raw_allowed)
                    except ValueError:
                        allowed_value = raw_allowed
                if current_value == allowed_value:
                    raise

                print(
                    "⚠️  Provider rejected "
                    f"{param_name}={current_value}; retry with {param_name}={allowed_value} "
                    f"for model {self.model}"
                )
                req[param_name] = allowed_value
                # 持久化修正，避免后续每轮都先失败一次
                self.sampling_params[param_name] = allowed_value
        return self._call_chat_completion_with_watchdog(req)

    @staticmethod
    def _responses_content(content: Any) -> Any:
        """Translate Chat-style multimodal content into Responses input parts."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return content
        converted = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"text", "input_text"}:
                converted.append({"type": "input_text", "text": part.get("text", "")})
            elif part_type in {"image_url", "input_image"}:
                image_value = part.get("image_url")
                if isinstance(image_value, dict):
                    image_value = image_value.get("url")
                if image_value:
                    image_part = {"type": "input_image", "image_url": image_value}
                    if part.get("detail") is not None:
                        image_part["detail"] = part["detail"]
                    converted.append(image_part)
        return converted

    @staticmethod
    def _without_none_values(value: Any) -> Any:
        """Remove provider-emitted null fields before replaying Responses items."""
        if isinstance(value, dict):
            return {
                key: Agent._without_none_values(item)
                for key, item in value.items()
                if item is not None
            }
        if isinstance(value, list):
            return [Agent._without_none_values(item) for item in value]
        return copy.deepcopy(value)

    def _responses_tool_schema(self, tools: Any) -> Any:
        """Translate Chat Completions function schemas to Responses schemas."""
        if not isinstance(tools, list):
            return tools
        translated = []
        for tool in tools:
            if not isinstance(tool, dict):
                translated.append(tool)
                continue
            function = tool.get("function")
            if tool.get("type") != "function" or not isinstance(function, dict):
                translated.append(copy.deepcopy(tool))
                continue
            response_tool = {"type": "function"}
            for key in ("name", "description", "parameters", "strict"):
                if key in function:
                    response_tool[key] = copy.deepcopy(function[key])
            translated.append(response_tool)
        return translated

    def _responses_assistant_items(self, message: dict) -> list[dict]:
        """Recover native Responses output items, with a Chat-history fallback."""
        raw_response = message.get("responses_api_response")
        if isinstance(raw_response, dict) and isinstance(raw_response.get("output"), list):
            return [
                self._without_none_values(item)
                for item in raw_response["output"]
                if isinstance(item, dict)
            ]

        result = []
        content = message.get("content")
        if not self._is_empty_message_content(content):
            result.append(
                {
                    "role": "assistant",
                    "content": self._responses_content(content),
                }
            )
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                call_id = tool_call.get("id")
                if not isinstance(function, dict) or not isinstance(call_id, str):
                    continue
                result.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", ""),
                    }
                )
        return result

    @staticmethod
    def _is_empty_message_content(content: Any) -> bool:
        if content is None or content == "":
            return True
        if isinstance(content, list):
            return len(content) == 0
        return False

    def _prepare_responses_input(
        self,
        messages: list[dict],
        *,
        use_stored_boundary: bool = True,
    ) -> list[dict]:
        """Build native Responses input items from local conversation history."""
        selected = messages
        if use_stored_boundary and self.previous_response_id:
            boundary = -1
            for index in range(len(messages) - 1, -1, -1):
                if messages[index].get("mcbots_response_id") == self.previous_response_id:
                    boundary = index
                    break
            if boundary >= 0:
                selected = messages[boundary + 1 :]

        result = []
        for message in selected:
            role = message.get("role")
            if role == "assistant":
                result.extend(self._responses_assistant_items(message))
                continue
            if role == "tool":
                call_id = message.get("tool_call_id")
                if isinstance(call_id, str) and call_id:
                    result.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": message.get("content", ""),
                        }
                    )
                continue
            if role not in {"system", "developer", "user"}:
                continue
            result.append(
                {
                    "role": role,
                    "content": self._responses_content(message.get("content", "")),
                }
            )
        return result

    @staticmethod
    def _is_zdr_previous_response_error(error: BaseException) -> bool:
        """Detect NVIDIA/OpenAI-compatible ZDR rejection of response chaining."""
        status_code = getattr(error, "status_code", None)
        if status_code is not None and status_code != 400:
            return False
        message = str(error).lower()
        return (
            "previous_response_id" in message
            and (
                "zero data retention" in message
                or "cannot be used for this organization" in message
                or "unsupported_parameter" in message
            )
        )

    def _create_responses_completion(
        self,
        request_kwargs: dict,
        *,
        continue_chain: bool,
    ):
        req = dict(request_kwargs)
        messages = req.pop("messages")
        chain_supported = getattr(
            self,
            "responses_previous_response_id_supported",
            True,
        )
        use_stored_response = bool(
            continue_chain and chain_supported and self.previous_response_id
        )
        req["input"] = self._prepare_responses_input(
            messages,
            use_stored_boundary=use_stored_response,
        )
        req["store"] = chain_supported
        if use_stored_response:
            req["previous_response_id"] = self.previous_response_id
        if "tools" in req:
            req["tools"] = self._responses_tool_schema(req["tools"])
        extra_body = req.get("extra_body")
        if isinstance(extra_body, dict):
            extra_body = dict(extra_body)
            extra_body.pop("store", None)
            extra_body.pop("previous_response_id", None)
            req["extra_body"] = extra_body
        if "max_tokens" in req and "max_output_tokens" not in req:
            req["max_output_tokens"] = req.pop("max_tokens")

        attempt_id = self.llm_request_total
        start_ts = time.time()
        print(
            f"🚀 Responses request start (attempt #{attempt_id}, model={self.model}, "
            f"previous_response_id={req.get('previous_response_id', '<none>')})"
        )
        try:
            response = self._run_provider_request(
                lambda: self.client.responses.create(**req),
                attempt_id=attempt_id,
            )
        except Exception as error:
            if not (
                "previous_response_id" in req
                and self._is_zdr_previous_response_error(error)
            ):
                raise
            self.responses_previous_response_id_supported = False
            self.previous_response_id = None
            req.pop("previous_response_id", None)
            req["store"] = False
            req["input"] = self._prepare_responses_input(
                messages,
                use_stored_boundary=False,
            )
            print(
                "ℹ️  Provider Zero Data Retention disables previous_response_id; "
                "retrying with stateless full-history replay"
            )
            response = self._run_provider_request(
                lambda: self.client.responses.create(**req),
                attempt_id=attempt_id,
            )
        print(
            f"✅ Responses request done in {time.time() - start_ts:.2f}s "
            f"(attempt #{attempt_id}, response_id={getattr(response, 'id', '<none>')})"
        )
        if continue_chain and getattr(
            self,
            "responses_previous_response_id_supported",
            True,
        ):
            response_id = getattr(response, "id", None)
            if response_id:
                self.previous_response_id = str(response_id)
        return response

    def _prepare_gemini_signature_request(self, request_kwargs: dict) -> dict:
        """Keep Gemini state in its native field, separate from tool IDs.

        Transform only the outgoing copy. Local execution and archived records
        retain the provider IDs. Remove message-level signatures only when the
        identical bytes are already preserved on this message's tool calls;
        text-only or otherwise unmatched signatures must survive unchanged.
        """
        model = str(request_kwargs.get("model", getattr(self, "model", "")))
        if "gemini" not in model.lower() or "messages" not in request_kwargs:
            return request_kwargs
        prepared = copy.deepcopy(request_kwargs)
        aliases: dict[str, str] = {}
        owners: dict[str, str] = {}
        for message in prepared["messages"]:
            signatures = set()
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                if not isinstance(call_id, str):
                    continue
                fields = call.get("provider_specific_fields") or {}
                signature = fields.get("thought_signature")
                if "__thought__" in call_id:
                    suffix = call_id.split("__thought__", 1)[1]
                    if not suffix or (signature is not None and signature != suffix):
                        raise ValueError("Gemini tool-call signature is empty or inconsistent")
                    # Older compatibility clients may preserve only the ID copy.
                    fields["thought_signature"] = suffix
                    call["provider_specific_fields"] = fields
                    signature = suffix
                    aliases[call_id] = self._action_reference(call_id)
                    call["id"] = aliases[call_id]
                wire_id = call["id"]
                if wire_id in owners and owners[wire_id] != call_id:
                    raise ValueError("Gemini tool-call ID collision after normalization")
                owners[wire_id] = call_id
                if isinstance(signature, str) and signature:
                    signatures.add(signature)
            fields = message.get("provider_specific_fields")
            if isinstance(fields, dict):
                copies = fields.get("thought_signatures")
                if (isinstance(copies, list) and copies and signatures
                        and all(isinstance(s, str) and s in signatures for s in copies)):
                    fields.pop("thought_signatures")
                    if not fields:
                        message.pop("provider_specific_fields")
        for message in prepared["messages"]:
            call_id = message.get("tool_call_id")
            if call_id in aliases:
                message["tool_call_id"] = aliases[call_id]
        return prepared

    def _create_model_completion(
        self,
        request_kwargs: dict,
        *,
        continue_response_chain: bool = True,
    ):
        if self.api_protocol == "responses":
            return self._create_responses_completion(
                request_kwargs,
                continue_chain=continue_response_chain,
            )
        return self._create_chat_completion(
            self._prepare_gemini_signature_request(request_kwargs)
        )

    def _assistant_message_from_response(self, response: Any) -> Optional[Any]:
        if self.api_protocol == "chat_completions":
            choices = getattr(response, "choices", None)
            return choices[0].message if choices else None

        response_dump = (
            response.model_dump(mode="json")
            if hasattr(response, "model_dump")
            else dict(response)
        )
        content = getattr(response, "output_text", None)
        if not content:
            text_parts = []
            for item in response_dump.get("output", []):
                if item.get("type") != "message":
                    continue
                for part in item.get("content", []):
                    if part.get("type") in {"output_text", "text"} and part.get("text"):
                        text_parts.append(str(part["text"]))
            content = "\n".join(text_parts)
        reasoning_items = [
            item
            for item in response_dump.get("output", [])
            if item.get("type") == "reasoning"
        ]
        tool_calls = []
        for item in response_dump.get("output", []):
            if item.get("type") != "function_call":
                continue
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    },
                    "mcbots_responses_item_id": item.get("id"),
                }
            )
        return _AssistantMessage(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls or None,
                "thinking_blocks": reasoning_items,
                "mcbots_response_id": response_dump.get("id"),
                "responses_api_response": response_dump,
            }
        )

    # ===== 决策循环 =====

    def _decision_loop(self):
        """独立线程：处理messages → VLM → 发送动作"""
        last_processed_rev = -1  # 已处理到的修订号

        while self.running:
            try:
                is_summary_request = False
                llm_limit_reached_after_success = False
                with self.messages_lock:
                    if self.llm_inflight:
                        pass
                    else:
                        # 仅在非in-flight时刷新暂存到messages（无异步窗口标记）
                        flushed = 0
                        if not self.awaiting_summary:
                            flushed = self._flush_staging_buffer_locked(with_async_window_markers=False)
                        if flushed > 0:
                            self._save_messages()
                # 发请求前，先尽可能把已产生状态全部入消息，避免时序滞后。
                # Exception: once a summary request has been inserted, it becomes a
                # hard cutoff for the summary turn. New observations are staged and
                # must not be merged back into the active context before that turn.
                if not self.awaiting_summary:
                    while self._drain_new_states() > 0:
                        pass

                # 1. 检查是否有新消息需要处理
                with self.messages_lock:
                    if self.llm_inflight:
                        time.sleep(0.2)
                        continue
                    current_rev = self.message_revision
                    # 首轮请求前必须至少有一张保留图，避免“纯文本首发”
                    if last_processed_rev < 0 and self.committed_image_seq <= 0:
                        time.sleep(0.2)
                        continue
                    if self.pending_exec_result:
                        now_ts = time.time()
                        if now_ts < float(self.pending_exec_observe_deadline):
                            time.sleep(0.2)
                            continue
                    paused_observe_deadline = self._pending_paused_observe_after_deadline()
                    if paused_observe_deadline > 0.0:
                        now_ts = time.time()
                        if now_ts < paused_observe_deadline:
                            time.sleep(0.2)
                            continue

                if self._capture_paused_observe_after_snapshot_if_due():
                    while self._drain_new_states() > 0:
                        pass
                    time.sleep(0.05)
                    continue

                if self.pending_post_exec_image:
                    if not self._append_fallback_post_exec_screenshot():
                        time.sleep(0.2)
                        continue
                if self.pending_exec_result and self.pending_post_exec_image:
                    time.sleep(0.2)
                    continue

                if self._has_reached_llm_limit():
                    self._reached_llm_limit()
                    break

                # 需要有新的用户消息（observation）才触发
                if current_rev <= last_processed_rev:
                    time.sleep(0.5)
                    continue

                print(f"\n{'='*60}")
                print(f"🤔 Decision triggered (messages: {len(self.conversation_history)}, rev: {current_rev})")
                print(f"{'='*60}")

                # 2. Pre-call: 上下文红线检查（轮数/图片/turn），命中则路由到 auto-summarize
                overflow_trimmed = False
                # A post-call trigger may have queued a summary at the previous
                # action boundary. Inject it synchronously before snapshotting
                # messages for this request. Asynchronous observation callbacks
                # are forbidden from injecting while llm_inflight is true.
                self._inject_pending_summary_request()
                precall_trigger = self._should_trigger_summarize()
                if precall_trigger is not None:
                    _reason, _count = precall_trigger
                    self._queue_summary_request(_count, reason=_reason)
                    self._inject_pending_summary_request()
                    overflow_trimmed = True

                if self.pending_auto_summarize_total_tokens is not None and not self.awaiting_summary:
                    # Waiting for the stopped command's result. Do not send
                    # another navigation request through the summary barrier.
                    time.sleep(0.05)
                    continue

                # 3. 调用VLM
                with self.messages_lock:
                    is_summary_request = bool(self.awaiting_summary)
                    if self.api_protocol == "responses":
                        messages_to_send = [
                            copy.deepcopy(msg)
                            for msg in self.conversation_history
                            if isinstance(msg, dict)
                        ]
                    else:
                        messages_to_send = [
                            self._sanitize_message_for_llm(msg)
                            for msg in self.conversation_history
                            if isinstance(msg, dict)
                        ]
                    self.last_llm_request_sent_ts = time.time()
                    self.llm_inflight = True

                request_kwargs = {
                    "model": self.model,
                    "messages": messages_to_send,
                    **self.sampling_params,
                }
                request_kwargs = self._apply_action_request_protocol(
                    request_kwargs,
                    is_summary_request=is_summary_request,
                )
                if self.request_extra_body:
                    request_kwargs["extra_body"] = self.request_extra_body

                self.llm_request_total += 1
                try:
                    response = self._create_model_completion(request_kwargs)
                except Exception as e:
                    if not is_summary_request:
                        self.llm_request_failed += 1
                    self.consecutive_llm_failures += 1
                    with self.messages_lock:
                        self.llm_inflight = False
                    print(f"⚠️  LLM request failed: {e}")
                    # Hard-reset triggers (either is enough):
                    #   1. We just routed a round/image overflow to auto-summarize but the
                    #      LLM still rejected the request — preserves the original recovery
                    #      behavior (the injected summary turn itself overflowed).
                    #   2. The provider explicitly signaled context_length_exceeded
                    #      (e.g. ROLL's McbotsEnvManager returning HTTP 400). In that case
                    #      our local count-based heuristics didn't catch the overflow
                    #      because they don't tokenize, so we still need to drop history.
                    if overflow_trimmed or _is_context_overflow_error(e):
                        self._reset_context_after_llm_request_failure(e)
                        last_processed_rev = -1
                    if self._reached_llm_limit():
                        break
                    time.sleep(2.0)
                    continue

                assistant_msg = self._assistant_message_from_response(response)
                if assistant_msg is None:
                    if not is_summary_request:
                        self.llm_request_failed += 1
                    self.consecutive_llm_failures += 1
                    with self.messages_lock:
                        self.llm_inflight = False
                    print("⚠️  Decision loop warning: model response has no assistant output")
                    if self._reached_llm_limit():
                        break
                    time.sleep(1.0)
                    continue

                self._maybe_recover_reasoning_from_content(assistant_msg)

                # Empty content guard: kimi (and several other providers) strictly
                # reject any subsequent request that has a zero-length assistant
                # message in history with `position N with role 'assistant' must
                # not be empty`. So we treat None / "" / whitespace-only / empty
                # multimodal lists as a soft failure: do NOT append to history.
                def _is_empty_assistant_content(c) -> bool:
                    if c is None:
                        return True
                    if isinstance(c, str):
                        return c.strip() == ""
                    if isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict):
                                t = part.get("text")
                                if isinstance(t, str) and t.strip():
                                    return False
                                if part.get("type") not in ("text", None):
                                    return False
                            elif part:
                                return False
                        return True
                    return False

                has_native_tool_calls = bool(
                    self._tool_field(assistant_msg, "tool_calls", None)
                )
                valid_empty_tool_response = (
                    self._uses_native_action_tools()
                    and not is_summary_request
                    and has_native_tool_calls
                )
                if (
                    assistant_msg is None
                    or (
                        _is_empty_assistant_content(assistant_msg.content)
                        and not valid_empty_tool_response
                    )
                ):
                    if not is_summary_request:
                        self.llm_request_failed += 1
                    self.consecutive_llm_failures += 1
                    with self.messages_lock:
                        if self.api_protocol == "responses" and assistant_msg is not None:
                            # Keep a local chain boundary even when there is no
                            # executable text. The stored response itself is
                            # already represented by previous_response_id.
                            empty_record = assistant_msg.model_dump()
                            empty_record["mcbots_empty_response_output"] = True
                            self.conversation_history.append(empty_record)
                            self._append_full_history_locked(empty_record)
                            self.message_revision += 1
                        self.llm_inflight = False
                    if assistant_msg is not None:
                        self._print_reasoning_content(assistant_msg)
                    print("⚠️  Decision loop warning: empty assistant message content (dropped, not appended to history)")
                    self._print_token_usage(getattr(response, "usage", None))
                    if self._reached_llm_limit():
                        break
                    time.sleep(1.0)
                    continue

                # get usage
                if hasattr(response, "usage") and response.usage is not None:
                    usage = response.usage
                else:
                    usage = None

                # save via msg
                assistant_msg.usage = usage.model_dump() if usage is not None else None

                # 记录最近一次 token 使用量（供 auto-summarize 判定与日志用）
                if usage is not None:
                    _tot = self._extract_total_tokens(usage)
                    if _tot is not None:
                        self.last_total_tokens = _tot
                    try:
                        _pt = getattr(usage, "prompt_tokens", None)
                        if isinstance(_pt, int) and _pt > 0:
                            self.last_prompt_tokens = _pt
                    except Exception:
                        pass

                assistant_content = assistant_msg.content
                assistant_text = assistant_content if isinstance(assistant_content, str) else ""
                self.consecutive_llm_failures = 0
                if not is_summary_request:
                    self.llm_request_success += 1
                    llm_limit_reached_after_success = self._has_reached_llm_limit()

                # Summary 回合：如果我们正在等 summary，整条回复直接当作 summary 处理；跳过 action 解析与分发
                if is_summary_request:
                    response_received_ts = time.time()
                    extracted_summary = self._extract_summary_text(assistant_text)
                    if extracted_summary is None:
                        self._reject_invalid_summary_response(
                            assistant_msg=assistant_msg,
                            summary_text=assistant_text,
                        )
                        # The feedback message bumped the revision. Keep the
                        # revision from the request we just handled so the retry
                        # is triggered immediately.
                        last_processed_rev = current_rev
                        self._print_reasoning_content(
                            assistant_msg,
                            label="Assistant reasoning_content (invalid summary)",
                        )
                        self._print_token_usage(usage)
                        time.sleep(0.5)
                        continue
                    with self.messages_lock:
                        self._smart_append_msg(assistant_msg.model_dump())
                        # Keep any observations that arrived after the summary
                        # request out of the frozen summary input. The reset
                        # replays them after the summary in the next context.
                        self.last_async_notice_start_ts = float(self.last_llm_request_sent_ts)
                        self.last_async_notice_end_ts = float(response_received_ts)
                        self.llm_inflight = False
                        last_processed_rev = self.message_revision
                    self._save_messages()
                    self._print_reasoning_content(assistant_msg, label="Assistant reasoning_content (summary)")
                    preview = extracted_summary[:300] + "..." if len(extracted_summary) > 300 else extracted_summary
                    print(f"\n💬 Assistant (summary):\n{preview}\n")
                    self._print_token_usage(usage)
                    self._perform_summary_reset(summary_text=extracted_summary)
                    # Keep last_processed_rev at the pre-reset revision so the
                    # freshly rebuilt summary context triggers one immediate
                    # follow-up decision turn instead of stalling here.
                    time.sleep(0.5)
                    continue

                # 4. Parse the action before archiving the assistant response.
                action_parse_error = ""
                if self._uses_native_action_tools():
                    action, action_parse_error = self._parse_action_tool_calls(assistant_msg)
                    if action_parse_error:
                        print(f"⚠️  Action tool-call validation failed: {action_parse_error}")
                else:
                    action = self._parse_action(assistant_text)
                action_id = None
                if action is not None and action.type == "exec":
                    action_id = action.action_id or self._new_action_id()
                    action.action_id = action_id

                # 3. 添加assistant回复到历史，再刷新in-flight期间暂存
                response_received_ts = time.time()
                with self.messages_lock:
                    assistant_record = self._normalize_assistant_tool_call_history(
                        assistant_msg.model_dump()
                    )
                    self._smart_append_msg(assistant_record)
                    if self._uses_native_action_tools():
                        self._append_action_tool_results(
                            assistant_msg,
                            accepted=action is not None,
                            error=action_parse_error,
                        )
                    flushed = self._flush_staging_buffer_locked(
                        with_async_window_markers=True,
                        async_start_ts=self.last_llm_request_sent_ts,
                        async_end_ts=response_received_ts,
                    )
                    if flushed > 0:
                        print(f"🧵 Flushed in-flight staged observations: {flushed}")
                    self.llm_inflight = False
                    last_processed_rev = self.message_revision

                # 保存对话历史
                self._save_messages()

                # 打印回复
                self._print_reasoning_content(assistant_msg)
                if self._uses_native_action_tools():
                    preview = "<native minecraft_action tool call>" if action else "<invalid native tool call>"
                else:
                    preview = assistant_text[:300] + "..." if len(assistant_text) > 300 else assistant_text
                print(f"\n💬 Assistant:\n{preview}\n")
                self._print_token_usage(usage)

                summarize_total = None
                summarize_reason = None
                postcall_trigger = None if llm_limit_reached_after_success else self._should_trigger_summarize(usage)
                if postcall_trigger is not None:
                    summarize_reason, summarize_total = postcall_trigger

                # A summary is a hard control barrier. Do not execute the action
                # from the threshold-crossing response. Stop any older action,
                # collect its terminal state, and summarize before resuming.
                if summarize_total is not None:
                    self._enter_forced_summary_barrier(
                        summarize_total,
                        reason=summarize_reason or "token_threshold",
                        withheld_action=action,
                    )
                    time.sleep(0.5)
                    continue

                if action is None:
                    if llm_limit_reached_after_success:
                        pass
                    else:
                        # 没解析到 action：回注一条 user feedback 强制下一轮 LLM 调用，避免卡死
                        feedback_ts = time.time()
                        feedback_msg = {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        f"[Invalid response at {self._format_timestamp(feedback_ts)}] "
                                        + (
                                            f"Your previous reply did not contain exactly one valid `{ACTION_TOOL_NAME}` tool call. "
                                            f"Validation error: {action_parse_error}. "
                                            if self._uses_native_action_tools()
                                            else (
                                                "Your previous reply did not contain a parseable `<action>...</action>` block "
                                                "(or the type/content was invalid, or you put it in the thinking part but not in response part). "
                                            )
                                        )
                                        + "So no action was executed. "
                                        + (
                                            f"Please call `{ACTION_TOOL_NAME}` exactly once now "
                                            if self._uses_native_action_tools()
                                            else "Please emit exactly one valid action block now, following the schema in the system prompt "
                                        )
                                        + self._allowed_action_types_feedback()
                                    ),
                                }
                            ],
                            "mcbots_invalid_action_feedback": True,
                        }
                        with self.messages_lock:
                            self.conversation_history.append(feedback_msg)
                            self._append_full_history_locked(feedback_msg)
                            self.message_revision += 1
                        self._save_messages()
                        print("⚠️  No action parsed from response; injected feedback to force retry")

                if action:
                    # 5. 处理observe控制
                    if action.type == "claim_done":
                        self._handle_navigation_claim()

                    elif action.type == "stop_observe":
                        if not self.allow_model_observe_toggle:
                            print("⚠️  Model emitted stop_observe, but observe toggle is disabled — ignoring")
                            if summarize_total is not None:
                                self._queue_summary_request(summarize_total, reason=summarize_reason)
                                self._inject_pending_summary_request()
                        else:
                            with self.observe_lock:
                                self.observe_enabled = False
                            print("⏸️  Screenshot observation stopped (command results and chat still inserted)")
                            self._emit_observe_mode_notice("model invoked stop_observe")
                            if summarize_total is not None:
                                self._queue_summary_request(summarize_total, reason=summarize_reason)
                                self._inject_pending_summary_request()

                    elif action.type == "start_observe":
                        if not self.allow_model_observe_toggle:
                            print("⚠️  Model emitted start_observe, but observe toggle is disabled — ignoring")
                            if summarize_total is not None:
                                self._queue_summary_request(summarize_total, reason=summarize_reason)
                                self._inject_pending_summary_request()
                        else:
                            with self.observe_lock:
                                self.observe_enabled = True
                            self._clear_paused_observe_after_snapshot()
                            print("▶️  Screenshot observation resumed")
                            self._arm_post_action_image_gate(time.time())
                            self._emit_observe_mode_notice("model invoked start_observe")
                            if summarize_total is not None:
                                self._queue_summary_request(summarize_total, reason=summarize_reason)
                                self._inject_pending_summary_request()

                    # 6. 发送动作到环境
                    else:
                        print(f"⚡ Sending action: type={action.type}, content={action.content}")
                        if action.type == "exec":
                            # single-flight action: interrupt previous running action before starting next one
                            if self.current_exec_action is not None and not self.current_exec_action.get("finished", False):
                                self._clear_paused_observe_after_snapshot()
                                self.action_sender(
                                    Action(
                                        type="stop_execute",
                                        target_action_id=self.current_exec_action["id"],
                                    )
                                )
                                self._append_action_event(
                                    "Action Interrupted because a new `exec` action is starting",
                                    time.time(),
                                    action_id=self.current_exec_action["id"],
                                    related_action_id=action_id,
                                )
                            async_end_anchor_ts = (
                                float(self.last_async_notice_end_ts)
                                if self.last_async_notice_end_ts > 0
                                else response_received_ts
                            )
                            # Pre-start snapshot is anchored to Async Notice End and inserted just before Action Start.
                            self._capture_pre_action_snapshot(anchor_ts=async_end_anchor_ts)
                        if action.type == "exec":
                            observe_after = (
                                float(action.observe_after_sec)
                                if action.observe_after_sec is not None
                                else float(self.default_observe_after_sec)
                            )
                            observe_after = max(1.0, observe_after)
                            action_start_ts = time.time()
                            self.current_exec_action = {
                                "id": action_id,
                                "start_ts": action_start_ts,
                                "observe_after_sec": observe_after,
                                "window_has_observation": False,
                                "start_event_emitted": False,
                                "finished": False,
                            }
                            self._append_action_event(
                                "Action Start",
                                action_start_ts,
                                action_id=action_id,
                            )
                            self.current_exec_action["start_event_emitted"] = True
                            self.pending_exec_observe_deadline = action_start_ts + observe_after
                            self._schedule_paused_observe_after_snapshot(
                                action_id=action_id,
                                deadline_ts=self.pending_exec_observe_deadline,
                            )
                            self.pending_exec_result = True
                            self.last_exec_action_content = action.content
                            self.last_exec_action_ts = action_start_ts
                            self._arm_post_action_image_gate(action_start_ts)
                            # Publish the pending gate before the environment can
                            # return a very short command result.
                            self.action_sender(action)
                            if summarize_total is not None:
                                self._queue_summary_request(summarize_total, reason=summarize_reason)
                        elif action.type in {"skip", "stop_execute"}:
                            if (
                                action.type == "stop_execute"
                                and self.current_exec_action is not None
                                and not self.current_exec_action.get("finished", False)
                            ):
                                action.target_action_id = self.current_exec_action["id"]
                            self.action_sender(action)
                            if action.type == "stop_execute":
                                self._clear_paused_observe_after_snapshot()
                                self.last_exec_action_content = None
                                self.last_exec_action_ts = 0.0
                            action_ts = time.time()
                            self._arm_post_action_image_gate(action_ts)
                            if action.type == "skip":
                                self._capture_post_skip_snapshot(anchor_ts=action_ts)
                            elif action.type == "stop_execute":
                                self._capture_post_stop_execute_snapshot(anchor_ts=action_ts)
                            if summarize_total is not None:
                                self._queue_summary_request(summarize_total, reason=summarize_reason)
                                self._inject_pending_summary_request()

                # Rollout-end self-reward is fired at the next safe window boundary
                # once the current assistant turn and any required post-action
                # observation have landed in the window.
                time.sleep(0.5)

            except Exception as e:
                if not is_summary_request:
                    self.llm_request_failed += 1
                with self.messages_lock:
                    self.llm_inflight = False
                print(f"⚠️  Decision loop error: {e}")
                import traceback
                traceback.print_exc()
                if self._reached_llm_limit():
                    break
                time.sleep(2)

    def _llm_limit_hits(self) -> tuple[bool, bool, bool]:
        """Return limit hits for successes, total failures, and consecutive failures."""
        max_successes = int(getattr(self, "max_llm_request_successes", 0) or 0)
        max_failures = int(getattr(self, "max_llm_request_failures", 0) or 0)
        max_consecutive = int(
            getattr(self, "max_consecutive_llm_failures", 0) or 0
        )
        hit_success = (
            max_successes > 0
            and self.llm_request_success >= max_successes
        )
        hit_failure = (
            max_failures > 0
            and self.llm_request_failed >= max_failures
        )
        hit_consecutive = (
            max_consecutive > 0
            and int(getattr(self, "consecutive_llm_failures", 0)) >= max_consecutive
        )
        return hit_success, hit_failure, hit_consecutive

    def _has_reached_llm_limit(self) -> bool:
        hit_success, hit_failure, hit_consecutive = self._llm_limit_hits()
        return hit_success or hit_failure or hit_consecutive

    def _reached_llm_limit(self) -> bool:
        # When self-reward is enabled, fire the rollout-end out-of-band grading
        # synchronously at a safe window boundary. Then set self.running = False.
        hit_success, hit_failure, hit_consecutive = self._llm_limit_hits()
        if not (hit_success or hit_failure or hit_consecutive):
            return False
        if hit_consecutive:
            self.stop_reason = "max_consecutive_llm_failures"
            print(
                "🛑 Consecutive LLM failure limit reached: "
                f"{self.consecutive_llm_failures}/{self.max_consecutive_llm_failures}"
            )
        elif hit_success:
            self.stop_reason = "max_llm_request_successes"
            print(
                "🛑 LLM success limit reached: "
                f"{self.llm_request_success}/{self.max_llm_request_successes} "
                f"(total={self.llm_request_total}, failed={self.llm_request_failed})"
            )
        elif hit_failure:
            self.stop_reason = "max_llm_request_failures"
            print(
                "🛑 LLM failure limit reached: "
                f"{self.llm_request_failed}/{self.max_llm_request_failures} "
                f"(total={self.llm_request_total}, success={self.llm_request_success})"
            )
        if self.enable_self_reward and not self.self_reward_in_flight:
            self._run_out_of_band_self_reward("rollout_end")
        self.running = False
        return True

    def _allowed_action_types_feedback(self) -> str:
        action_types = self._valid_action_types()
        return f"(allowed types: {', '.join(action_types)})."

    def _uses_native_action_tools(self) -> bool:
        return getattr(self, "action_protocol", "xml") == "tool_calls"

    def _valid_action_types(self) -> list[str]:
        action_types = ["exec", "skip", "stop_execute"]
        if self.allow_model_observe_toggle:
            action_types.extend(["start_observe", "stop_observe"])
        if getattr(self, "navigation_claim_client", None) is not None:
            action_types.append("claim_done")
        return action_types

    def _action_tools(self) -> list[dict]:
        """Return the native function schema used for one agent decision."""
        return [
            {
                "type": "function",
                "function": {
                    "name": ACTION_TOOL_NAME,
                    "description": (
                        "Submit exactly one Minecraft controller action. Use exec to run a "
                        "Bash command; use another action type for control-only decisions."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": self._valid_action_types(),
                                "description": "The controller action to perform.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Required non-empty Bash command for exec; omit otherwise.",
                            },
                            "observe_after_sec": {
                                "type": "number",
                                "minimum": 1.0,
                                "description": (
                                    "Optional target delay before observation for exec; defaults to 2 seconds."
                                ),
                            },
                        },
                        "required": ["type"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    def _apply_action_request_protocol(
        self,
        request_kwargs: dict,
        *,
        is_summary_request: bool,
    ) -> dict:
        """Expose native actions only on decision turns.

        DeepSeek thinking mode supports native tools but rejects the
        ``tool_choice`` request parameter.  The decision prompt asks for one
        call and the response parser enforces exactly one call fail-closed.
        Summary turns normally omit tools altogether so they cannot dispatch
        actions.  Bedrock's OpenAI compatibility layer is stricter: once the
        replayed history contains tool calls it rejects a later request unless
        ``tools`` is present, even when that turn only asks for text.  Keep the
        schema attached for Bedrock summary turns; the local summary barrier
        still prevents any returned tool call from being dispatched.
        """
        request = dict(request_kwargs)
        model_name = str(request.get("model") or "").strip().lower()
        bedrock_summary = is_summary_request and "bedrock" in model_name
        if self._uses_native_action_tools() and (
            not is_summary_request or bedrock_summary
        ):
            request["tools"] = self._action_tools()
        return request

    @staticmethod
    def _tool_field(value: Any, name: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    def _parse_action_tool_calls(self, message: Any) -> tuple[Optional[Action], str]:
        """Validate one OpenAI-compatible native tool call and convert it to Action."""
        tool_calls = self._tool_field(message, "tool_calls", None)
        if not isinstance(tool_calls, (list, tuple)) or len(tool_calls) != 1:
            count = len(tool_calls) if isinstance(tool_calls, (list, tuple)) else 0
            return None, f"expected exactly one tool call, received {count}"

        tool_call = tool_calls[0]
        tool_call_id = self._tool_field(tool_call, "id", None)
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            return None, "tool call id must be a non-empty string"
        if self._tool_field(tool_call, "type", "function") != "function":
            return None, "tool call type must be function"
        function = self._tool_field(tool_call, "function", None)
        if self._tool_field(function, "name", "") != ACTION_TOOL_NAME:
            return None, f"tool call name must be {ACTION_TOOL_NAME!r}"
        raw_arguments = self._tool_field(function, "arguments", "")
        if not isinstance(raw_arguments, str):
            return None, "tool call arguments must be a JSON string"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            return None, f"tool call arguments are invalid JSON: {error.msg}"
        if not isinstance(arguments, dict):
            return None, "tool call arguments must decode to an object"

        unexpected = sorted(set(arguments) - {"type", "content", "observe_after_sec"})
        if unexpected:
            return None, "unexpected tool arguments: " + ", ".join(unexpected)
        action_type = arguments.get("type")
        if action_type not in self._valid_action_types():
            return None, f"invalid action type: {action_type!r}"

        content = arguments.get("content")
        if content is not None and not isinstance(content, str):
            return None, "content must be a string"
        content = content.strip() if isinstance(content, str) else None
        if not content:
            content = None
        if action_type == "exec" and content is None:
            return None, "exec requires non-empty content"

        observe_after_sec = arguments.get("observe_after_sec")
        if observe_after_sec is not None:
            if isinstance(observe_after_sec, bool) or not isinstance(observe_after_sec, (int, float)):
                return None, "observe_after_sec must be a number"
            observe_after_sec = max(1.0, float(observe_after_sec))

        return (
            Action(
                type=action_type,
                content=content,
                observe_after_sec=observe_after_sec,
                action_id=tool_call_id,
            ),
            "",
        )

    def _append_action_tool_results(self, message: Any, *, accepted: bool, error: str = "") -> None:
        """Close every native tool call in provider history before adding observations."""
        tool_calls = self._tool_field(message, "tool_calls", None)
        if not isinstance(tool_calls, (list, tuple)):
            return
        for tool_call in tool_calls:
            tool_call_id = self._tool_field(tool_call, "id", None)
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            payload = {"status": "received" if accepted else "rejected"}
            reference = self._action_reference(tool_call_id)
            if reference != tool_call_id:
                # Bind the short event label to this native tool result; keep
                # tool_call_id itself byte-for-byte intact for the provider.
                payload["action_ref"] = reference
            if error:
                payload["error"] = error
            self._smart_append_msg(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                }
            )

    def _parse_action(self, text: str) -> Optional[Action]:
        """
        从LLM响应中解析XML-like格式的动作（用正则提取，避免命令内容中的特殊字符导致 XML 解析失败）
        
        格式：
        <action>
          <type>exec</type>
          <content>command here</content>
        </action>
        """
        # 提取第一个 <action>...</action> 块
        match = re.search(r"<action>\s*(.*?)\s*</action>", text, re.DOTALL | re.IGNORECASE)
        if not match:
            return None

        action_block = match.group(1)

        type_match = re.search(r"<type>\s*(.*?)\s*</type>", action_block, re.DOTALL | re.IGNORECASE)
        if not type_match:
            return None

        action_type = type_match.group(1).strip()

        # 验证type是否合法
        valid_types = self._valid_action_types()
        if action_type not in valid_types:
            return None

        content = None
        content_match = re.search(r"<content>\s*(.*?)\s*</content>", action_block, re.DOTALL | re.IGNORECASE)
        if content_match:
            raw_content = content_match.group(1)
            # 兼容模型偶尔输出已转义实体，同时允许裸 && 等字符
            content = html.unescape(raw_content).strip()
            if content == "":
                content = None

        if action_type == "exec" and not content:
            print("⚠️  Action parse warning: exec action missing <content>")
            return None

        observe_after_sec = None
        observe_match = re.search(r"<observe_after_sec>\s*(.*?)\s*</observe_after_sec>", action_block, re.DOTALL | re.IGNORECASE)
        if observe_match:
            raw = observe_match.group(1).strip()
            try:
                observe_after_sec = max(1.0, float(raw))
            except Exception:
                observe_after_sec = None

        return Action(type=action_type, content=content, observe_after_sec=observe_after_sec)

    def wait(self):
        """等待Agent运行"""
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n⚠️  Interrupted")
            self.stop()
