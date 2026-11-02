"""Working memory: the observation summary the model is allowed to see.

Two design choices are worth defending in an interview.

**It is derived, not stored.** The context is rebuilt from the observation log
every round rather than mutated in place. A model that could mutate memory could
rewrite history; a derived view cannot drift from the record.

**It is compressed, not raw.** Each observation is reduced to the fields a
decision actually needs. Passing full tool output forward is how a context window
fills with text nobody reads — and it also makes the next action depend on
phrasing rather than on facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from enum import Enum


class SourceTrust(str, Enum):
    """P0-2 观测来源可信度。缺省即不可信(fail-closed)。

    VERIFIED_TOOL      — 注册表内、宿主确定性执行的内部工具;
    AGENT_INFERRED     — 模型自身推理产物(非工具观测);
    EXTERNAL_UNTRUSTED — MCP/外部 HTTP/未知来源:内容进 prompt 前必须隔离。
    """

    VERIFIED_TOOL = "verified_tool"
    AGENT_INFERRED = "agent_inferred"
    EXTERNAL_UNTRUSTED = "external_untrusted"
from typing import Any, Callable

from ..core.contracts import AgentError
from ..tools.base import VALID_SUMMARY_POLICIES
from ..core.hashing import canonical_json
from ..tools.base import Observation

__all__ = [
    "DEFAULT_INSTRUCTION", "DEFAULT_SUMMARY_MAX_DEPTH", "DEFAULT_SUMMARY_HEAD",
    "DEFAULT_SUMMARY_TAIL", "RAW_MAX_CHARS", "ObservationRecord", "WorkingMemory",
    "default_summarize", "detailed_summarize", "raw_summarize", "summarizer_for",
]

# ---- P0-1 压缩常量(集中定义,不散落) -------------------------------------
#: 嵌套展开的最大深度:超过该层的 dict 折叠为键名列表。
DEFAULT_SUMMARY_MAX_DEPTH = 2
#: 长字符串保留头/尾的字符数;总长 ≤ HEAD+TAIL 时不截断。
DEFAULT_SUMMARY_HEAD = 200
DEFAULT_SUMMARY_TAIL = 200
#: "raw" 策略的单条截断上限:不做结构化压缩,只防失控。
RAW_MAX_CHARS = 4_000

#: The kernel's default protocol wording. Read-only by construction, which is
#: exactly what the finance reference workload is: the frozen v10 prompt was
#: built against this string, so it is a constant, not a suggestion. A domain
#: whose work is to *write* supplies its own wording instead — a real model
#: obeys what it is told, and a read-only instruction makes it refuse the very
#: steps its plan requires (observed on a real run).
DEFAULT_INSTRUCTION = (
    "Propose exactly one read-only action. You cannot execute it, "
    "cannot mark the task complete, and cannot pass a financial value. "
    "The runtime decides when the evidence is sufficient."
)


def _arguments_head(arguments: dict[str, Any], limit: int = 80) -> str:
    flat = " ".join(f"{key}={str(value)[:24]}" for key, value in arguments.items())
    return flat[:limit]


def _clip(value: str) -> str:
    """长字符串的头尾保留规则(P0-1):≤400 原样;否则头 200 + 标记 + 尾 200。"""
    if len(value) <= DEFAULT_SUMMARY_HEAD + DEFAULT_SUMMARY_TAIL:
        return value
    omitted = len(value) - DEFAULT_SUMMARY_HEAD - DEFAULT_SUMMARY_TAIL
    return (value[:DEFAULT_SUMMARY_HEAD]
            + f"...[truncated {omitted} chars]..."
            + value[-DEFAULT_SUMMARY_TAIL:])


def _compact(value: Any, depth: int) -> Any:
    """P0-1 的结构化压缩:深度内保留一层键值/列表首项,超出折叠为键名/计数。"""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _clip(value)
    if depth <= 0:
        if isinstance(value, list):
            return {"count": len(value)}
        if isinstance(value, dict):
            return {"keys": sorted(value)[:8]}
        return str(value)[:120]
    if isinstance(value, list):
        if len(value) <= 3:
            return [_compact(item, DEFAULT_SUMMARY_MAX_DEPTH) for item in value]
        first = _compact(value[0], DEFAULT_SUMMARY_MAX_DEPTH)
        return [first, {"count": len(value)}]
    if isinstance(value, dict):
        return {key: _compact(item, depth - 1) for key, item in value.items()}
    return str(value)[:120]


def default_summarize(result: dict[str, Any]) -> dict[str, Any]:
    """Shape-based fallback — **byte-frozen legacy behavior**(见下)。

    冻结原因:finance 参考负载的观测经此函数压缩,SFT 金色哈希与
    test_sft_export 的字节保真测试钉住了它的输出。改进版压缩见
    :func:`detailed_summarize`(策略 "detailed");两个"默认"的关系:
    "default" = 向后兼容的旧形状压缩,"detailed" = P0-1 的修复,
    由新工具/审计域显式选用。

    原始行为(保持不变):Scalars survive whole (long strings as head +
    true length); lists become a count plus a peek at the first scalar
    item; nested objects become their sorted key list.
    """
    out: dict[str, Any] = {}
    for key, value in result.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            if isinstance(value, str) and len(value) > 240:
                out[key] = {"head": value[:240], "chars": len(value)}
            else:
                out[key] = value
        elif isinstance(value, list):
            scalars = [item for item in value if isinstance(item, (str, int, float, bool))]
            entry: dict[str, Any] = {"count": len(value)}
            if scalars:
                entry["first"] = scalars[0]
            out[key] = entry
        elif isinstance(value, dict):
            out[key] = {"keys": sorted(value)[:8]}
        else:
            out[key] = str(value)[:120]
    return out


def detailed_summarize(result: dict[str, Any]) -> dict[str, Any]:
    """P0-1 改进版压缩(策略 "detailed"),修复"模型失明"。

    与旧 default 的差异:列表首项全量(深度内)、其余项保留计数;短列表
    (≤3)全部保留;嵌套 dict 保留一层键值;长字符串头尾保留并带显式
    截断标记。深度与预算常量集中在本模块头部。
    """
    return {key: _compact(value, DEFAULT_SUMMARY_MAX_DEPTH)
            for key, value in result.items()}


def raw_summarize(result: dict[str, Any]) -> dict[str, Any]:
    """"raw" 策略:不做结构化压缩,仅单条截断保护(RAW_MAX_CHARS)。"""
    out: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, str) and len(value) > RAW_MAX_CHARS:
            out[key] = {"head": value[:RAW_MAX_CHARS], "chars": len(value)}
        else:
            out[key] = value
    return out


def summarizer_for(spec) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """P0-1 策略分发:按 ToolSpec.summary_policy 返回压缩函数。

    duck-typed(不 import ToolSpec,避免 tools↔memory 反向耦合);
    未知策略 fail-closed。优先级:**已提供 summarize 实现 → 用实现**
    (向后兼容,finance 字节冻结依赖此条);显式策略覆盖实现
    ("raw"/"detailed" 压过自定义实现是有意的——作者改了主意)。
    """
    policy = getattr(spec, "summary_policy", "default")
    impl = getattr(spec, "summarize", None)
    if policy == "custom":
        if impl is None:
            raise AgentError(
                "TOOL_SUMMARY_POLICY_INVALID",
                f"{spec.name!r} declares summary_policy='custom' "
                "but provides no summarize implementation",
            )
        return impl
    if policy == "raw":
        return raw_summarize
    if policy == "detailed":
        return detailed_summarize
    if policy == "default":
        return impl if impl is not None else default_summarize
    raise AgentError(
        "TOOL_SUMMARY_POLICY_INVALID",
        f"{spec.name!r} has unknown summary_policy {policy!r}",
    )


@dataclass(frozen=True)
class PinPolicy:
    """P0-3 pin 集:pinned 观测在全量级保留,永不降为索引行。

    三个集合按"或"生效;event_kinds 由 loop 在构造观测行时传入事件类型
    (restore 重放时为链上事件类型)。全部结构化声明,不做模型判断。
    """

    tool_names: frozenset[str] = frozenset()
    event_kinds: frozenset[str] = frozenset()
    observation_ids: frozenset[str] = frozenset()

    def should_pin(self, record: "ObservationRecord",
                   event_kind: str | None = None) -> bool:
        if record.tool in self.tool_names:
            return True
        if event_kind is not None and event_kind in self.event_kinds:
            return True
        if record.result_hash and record.result_hash in self.observation_ids:
            return True
        return False


@dataclass(frozen=True)
class ObservationRecord:
    """One compressed observation, kept for the whole task."""

    round: int
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    result_hash: str
    #: P0-2 来源可信度。旧事件缺该字段时按 EXTERNAL_UNTRUSTED 读取
    #: (fail-closed:不声明即不可信),旧链校验不受影响。
    source_trust: SourceTrust = SourceTrust.EXTERNAL_UNTRUSTED
    #: P0-3 该观测来源事件的类型(restore 重放时由 loop 传入)——
    #: PinPolicy.event_kinds 的匹配依据;增量字段,缺省 None。
    event_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "tool": self.tool,
            "arguments": self.arguments,
            "result": self.result,
            "result_hash": self.result_hash,
            "trust": self.source_trust.value,
        }


@dataclass
class WorkingMemory:
    """Accumulates observations and derives the next context."""

    objective: str
    tool_catalog: list[dict[str, Any]]
    records: list[ObservationRecord] = field(default_factory=list)
    #: Frozen at delegation time and recorded in the chain. Re-ranking on every
    #: round would make the prompt depend on wall-clock time and on cases
    #: created by the run itself.
    prior_cases: list[dict[str, Any]] = field(default_factory=list)
    #: Per-tool compressors declared on their ToolSpecs (memory does not know
    #: tool names). Tools without one use :func:`default_summarize`.
    summarizers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = field(
        default_factory=dict
    )
    #: The domain's "what has been established so far" view. Registered on the
    #: DomainModule; without one a neutral fallback keeps the context honest.
    state_fn: Callable[[Any], dict[str, Any]] | None = None
    #: Character budget for the inlined compressed observations. Older rounds
    #: fold into ``observation_index`` rows — prompt-level forgetting while the
    #: store keeps everything (auditability is store-level, not prompt-level).
    observation_budget: int = 24_000
    #: Highest round folded into the index so far; the loop turns a fresh fold
    #: into a ``context_compacted`` chain event and clears the pending marker.
    folded_through: int = 0
    pending_compaction: dict[str, Any] | None = None
    #: P0-3 pin 集:pinned 观测保持全量且永不降为索引行。None = 无 pin
    #(语义与改动前等价:全部观测按非 pinned 处理)。
    pin_policy: "PinPolicy | None" = None
    #: P0-3 pinned 全量自身超预算时置位;loop 落 context_pin_overflow
    #: 事件(fail-open 仅限此情形:证据完整性优先于预算)。
    pending_pin_overflow: dict[str, Any] | None = None
    #: P1-1 增量 summary 缓存(SummaryCache|None)。None = 不缓存(默认,
    #: 语义与改动前完全一致)。缓存异常一律 fail-open 回退实时 summarize。
    summary_cache: Any = None
    #: P1-1 缓存指纹来源:tool → (summary_policy, summary_version)。由 loop
    #: 从 ToolSpec 装配;缺失时按 ("default", 1) 计(仍稳定)。
    summarizer_config: dict[str, tuple[str, int]] = field(default_factory=dict)
    #: The conversation a delegation was handed off from (chat → work bridge).
    #: Operator input, frozen in the task binding, sanitised by the console
    #: (role/text pairs, capped) — carried so temporal/place references in the
    #: objective ("周三", "当天", "到了后") resolve against what was actually
    #: said. Absent for direct delegations; the key then never appears in the
    #: context, so the finance v10 prompt bytes are untouched.
    conversation: list[dict[str, Any]] | None = None
    #: Mid-run operator notes ("顺便查一下 B"), refreshed from the chain by the
    #: loop once per round. Emitted only when non-empty, so a run without notes
    #: keeps byte-identical prompts — the finance v10 gate depends on that.
    operator_messages: list[dict[str, Any]] | None = None
    #: What this domain's planner is told each round. ``None`` keeps the
    #: read-only kernel wording (the v10 prompt was frozen against it); a
    #: domain that writes files or runs commands says so instead.
    instruction: str | None = None
    #: Which system-prompt profile this run is bound by (see
    #: :mod:`flowagent.providers.prompt`). ``None`` emits no key at all, so the
    #: contexts of a domain that did not select a profile are byte-identical to
    #: the ones the SFT golden hash was computed over. Only a domain whose
    #: behaviour contract differs from the default names one here.
    prompt_profile: str | None = None
    #: Proposals the gate refused for their *shape* (an argument that does not
    #: fit the tool's schema), with the error that refused them. Written by the
    #: loop so the next proposal can be corrected instead of the run ending —
    #: emitted only when non-empty, so runs that never hit one keep their bytes.
    rejected_proposals: list[dict[str, Any]] | None = None

    def record(self, round_no: int, observation: Observation,
               source_trust: "SourceTrust | None" = None,
               event_kind: str | None = None) -> ObservationRecord:
        record = ObservationRecord(
            round=round_no,
            tool=observation.tool,
            arguments=dict(observation.arguments),
            result=dict(observation.result),
            result_hash=observation.result_hash,
            source_trust=source_trust or SourceTrust.EXTERNAL_UNTRUSTED,
            event_kind=event_kind,
        )
        self.records.append(record)
        return record

    # ------------------------------------------------------------------- state

    def state(self) -> dict[str, Any]:
        """The domain's derived state; neutral fallback without a state_fn."""
        if self.state_fn is not None:
            return self.state_fn(self.records)
        return {
            "rounds_recorded": len(self.records),
            "tools_used": sorted({record.tool for record in self.records}),
        }

    # ----------------------------------------------------------------- context

    def last_evidence(self) -> dict[str, Any] | None:
        """One compressed card for the policy packet, not the whole history."""
        if not self.records:
            return None
        record = self.records[-1]
        return {
            "round": record.round,
            "tool": record.tool,
            "arguments": dict(record.arguments),
            "summary": self._summarise(record),
        }

    def build(
        self,
        round_no: int,
        budget_snapshot: dict[str, Any],
        plan: Any | None = None,
    ) -> dict[str, Any]:
        """Return the context object handed to the provider this round.

        ``plan`` is the open-todo view, not the whole plan: the model needs to
        know what is left, not the history of how it got there.

        P0-3 三级折叠(替代原纯 FIFO 单档折叠):

        - 第 1 级(全量):pinned 观测保持全量(仅受 RAW_MAX_CHARS 单条
          上限保护)——证据完整性优先于预算;
        - 第 2 级(摘要):非 pinned 观测呈 summarize 产物(策略分发见
          summarizer_for);超预算时最老的非 pinned 摘要先降为索引行;
        - 第 3 级(索引行):仅当第 2 级后仍超预算才发生,pinned 永不降入。

        若只剩 pinned 全量仍超预算:不报错、不丢 pinned,记
        ``pending_pin_overflow``(loop 落 context_pin_overflow 事件)——
        fail-open 仅限此情形,理由:证据完整性优先于预算。

        无 pin_policy 时语义与改动前等价:全部观测按非 pinned 处理,
        索引行内容一致(round/tool/arguments_head/result_hash)。
        """
        # P0-2:trust 字段只在选择了非默认 profile 的上下文中输出——
        # finance 参考负载的字节冻结(SFT 金色哈希)依赖缺省键的缺席语义;
        # 审计域(general 及后续 audit profile)则强制携带来源可信度。
        include_trust = self.prompt_profile is not None

        rows: list[dict[str, Any]] = []
        for record in self.records:
            pinned = bool(self.pin_policy and self.pin_policy.should_pin(record))
            summary = self._summarise(record)
            full = {
                "round": record.round,
                "tool": record.tool,
                "arguments": record.arguments,
                "result": raw_summarize({"result": record.result})["result"],
                "result_hash": record.result_hash,
                **({"trust": record.source_trust.value} if include_trust else {}),
            }
            summary_row = {
                "round": record.round,
                "tool": record.tool,
                "arguments": record.arguments,
                "summary": summary,
                **({"trust": record.source_trust.value} if include_trust else {}),
            }
            rows.append({"record": record, "pinned": pinned,
                         "full": full, "summary_row": summary_row})

        pinned_full = [row["full"] for row in rows if row["pinned"]]
        inline: list[dict[str, Any]] = []
        index_rows: list[dict[str, Any]] = []

        pinned_chars = sum(len(canonical_json(o)) for o in pinned_full)
        if pinned_chars > self.observation_budget:
            # fail-open(仅限此情形):证据完整性优先于预算。
            self.pending_pin_overflow = {
                "pinned_count": len(pinned_full),
                "pinned_chars": pinned_chars,
                "budget": self.observation_budget,
            }
            inline = pinned_full + [
                row["summary_row"] for row in rows if not row["pinned"]]
        else:
            non_pinned_summaries = [
                row["summary_row"] for row in rows if not row["pinned"]]
            budget_left = self.observation_budget - pinned_chars
            used = sum(len(canonical_json(o)) for o in non_pinned_summaries)
            if used <= budget_left or not non_pinned_summaries:
                inline = [row["full"] for row in rows if row["pinned"]] \
                    + non_pinned_summaries
            else:
                # 第 3 级:最老的非 pinned 摘要先降为索引行,直到预算内。
                keep_from = len(non_pinned_summaries)
                running = used
                for i, row in enumerate(non_pinned_summaries):
                    running -= len(canonical_json(row))
                    if running <= budget_left:
                        keep_from = i + 1
                        break
                folded = non_pinned_summaries[:keep_from]
                inline = ([row["full"] for row in rows if row["pinned"]]
                          + non_pinned_summaries[keep_from:])
                pinned_records = [row["record"] for row in rows if row["pinned"]]
                non_pinned_records = [row["record"] for row in rows
                                      if not row["pinned"]]
                index_rows = [
                    {
                        "round": row["round"],
                        "tool": row["tool"],
                        "arguments_head": _arguments_head(
                            record.arguments),
                        "result_hash": record.result_hash[:12],
                    }
                    for row, record in zip(folded, non_pinned_records[:keep_from])
                ]
                if index_rows:
                    last_folded = index_rows[-1]["round"]
                    if last_folded > self.folded_through:
                        self.pending_compaction = {
                            "from_round": index_rows[0]["round"],
                            "through_round": last_folded,
                            "inlined_from_round": inline[-1].get("round",
                                                                 round_no),
                            "folded_count": len(index_rows),
                            "pinned_count": len(pinned_full),
                            "levels": {"full": len(pinned_full),
                                       "summary": len(inline) - len(pinned_full),
                                       "index": len(index_rows)},
                        }
                        self.folded_through = last_folded

        context = {
            "objective": self.objective,
            "round": round_no,
            "tools": self.tool_catalog,
            "plan": plan.to_context() if plan is not None else [],
            "state": self.state(),
            "observations": inline,
            "budget": budget_snapshot,
            "instruction": self.instruction or DEFAULT_INSTRUCTION,
        }
        if self.prompt_profile:
            # Emitted only when a domain selected one. See the field docstring:
            # the absent key is what keeps every finance context — and the SFT
            # golden hash computed over them — unchanged.
            context["prompt_profile"] = self.prompt_profile
        if index_rows:
            context["observation_index"] = index_rows
        if self.conversation:
            context["conversation"] = [
                {"role": item.get("role"), "text": item.get("text")}
                for item in self.conversation
                if item.get("role") in ("user", "assistant") and item.get("text")
            ]
        notes = [
            {
                "round": int(item.get("round") or 0),
                "text": str(item.get("text") or "")[:600],
                **({"steered": True} if item.get("steered") else {}),
            }
            for item in (self.operator_messages or [])
            if str(item.get("text") or "").strip()
        ]
        if notes:
            context["operator_messages"] = notes
        if self.rejected_proposals:
            context["rejected_proposals"] = [
                {
                    "tool": str(item.get("tool") or ""),
                    "code": str(item.get("code") or ""),
                    "message": str(item.get("message") or "")[:300],
                }
                for item in self.rejected_proposals[-3:]
            ]
        if self.prior_cases:
            context["prior_cases"] = [
                {
                    "run_id": hit.get("run_id"),
                    "objective": hit.get("objective"),
                    "outcome": hit.get("outcome"),
                    "summary": hit.get("summary"),
                    "chain_head": hit.get("chain_head"),
                    "score_bp": hit.get("score_bp"),
                }
                for hit in self.prior_cases[:3]
            ]
        return context

    def _summarise(self, record: ObservationRecord) -> dict[str, Any]:
        """Compress one observation to the fields a decision needs.

        The tool's own ``summarize`` contract wins; the shape-based default
        keeps unknown tools honest without the memory layer knowing tool names.

        P1-1:若配置了 summary_cache,先按五元组查缓存;未命中才实时
        summarize 并写回。缓存读写异常一律 fail-open(回退实时计算),
        缓存永远不能让 loop 中断。
        """
        summarizer = self.summarizers.get(record.tool)
        key = None
        if self.summary_cache is not None:
            policy, version = self.summarizer_config.get(
                record.tool, ("default", 1))
            args_hash = hashlib.sha256(
                canonical_json(record.arguments).encode("utf-8")).hexdigest()
            key = (record.tool, args_hash, record.result_hash,
                   policy, int(version))
            cached = self.summary_cache.get(key)
            if cached is not None:
                return cached
        if summarizer is not None:
            product = summarizer(record.result)
        else:
            product = default_summarize(record.result)
        if key is not None:
            self.summary_cache.put(key, product)
        return product
