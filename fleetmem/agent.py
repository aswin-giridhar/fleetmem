"""The robot agent.

Each robot runs one of these. The agent's competence comes entirely from the memory layer:
it recalls what the fleet has learned before acting, it claims physical resources through
CockroachDB, and it checkpoints so it can die and resume.

Reasoning uses Amazon Bedrock when credentials exist. Without them it uses a deterministic
policy so the project remains runnable and the safety properties stay demonstrable — the
race and the recall are database properties, not model properties.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .config import CONFIG
from .errors import ReasoningUnavailableError, ResourceHeldError
from .memory import FleetMemory

log = logging.getLogger("fleetmem.agent")

SYSTEM_PROMPT = """You are the onboard planner for one warehouse robot in a shared fleet.
You are given: your task, the fleet's recalled memories relevant to it, and which physical
resources are currently held by other robots.

Rules:
- Never plan to use a resource another robot holds. Choose an alternative.
- If a recalled memory warns about a location, adapt the plan and say which memory you used.
Reply ONLY with JSON: {"target": "<resource id>", "speed": "slow|normal",
"reason": "<one sentence>", "memory_used": "<lesson or null>"}"""


@dataclass
class Decision:
    target: str
    speed: str = "normal"
    reason: str = ""
    memory_used: str | None = None
    provider: str = "local-policy"
    raw: dict[str, Any] = field(default_factory=dict)


class BedrockReasoner:
    """Bedrock planning via the **Converse API**.

    Converse is provider-agnostic: one request shape for Amazon Nova, Anthropic Claude,
    Meta Llama and Mistral alike. The earlier implementation used invoke_model with the
    Anthropic-specific body format, which meant the model choice was welded into the
    request. With Converse, switching providers is a config change — which mattered here,
    because Anthropic inference profiles were not available in this account while Nova,
    Llama and Mistral all were.

    FALLBACK_MODELS is tried in order when the configured model is unavailable, so a model
    that is missing in one account or region degrades to another rather than to no agent.
    """

    name = "bedrock"

    FALLBACK_MODELS = [
        "us.amazon.nova-lite-v1:0",
        "us.amazon.nova-pro-v1:0",
        "us.meta.llama3-3-70b-instruct-v1:0",
        "mistral.mistral-large-2407-v1:0",
    ]

    def __init__(self):
        import boto3
        self._client = boto3.client("bedrock-runtime", region_name=CONFIG.aws_region)
        self.model_id: str | None = None

    def _candidates(self) -> list[str]:
        ordered = [CONFIG.chat_model] + [m for m in self.FALLBACK_MODELS
                                         if m != CONFIG.chat_model]
        return ordered

    def _converse(self, model_id: str, prompt: str, max_tokens: int = 400) -> str:
        response = self._client.converse(
            modelId=model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
        )
        return response["output"]["message"]["content"][0]["text"]

    def resolve(self) -> str:
        """Find the first usable model. Caches the winner on the instance."""
        if self.model_id:
            return self.model_id
        last: Exception | None = None
        for model_id in self._candidates():
            try:
                self._converse(model_id, "Reply with the single word: ready", max_tokens=8)
                self.model_id = model_id
                return model_id
            except Exception as exc:
                last = exc
                continue
        raise ReasoningUnavailableError(f"no Bedrock model usable: {last}")

    def decide(self, prompt: str) -> dict:
        model_id = self.resolve()
        text = self._converse(model_id, prompt).strip()
        # Validate the CONTENT. A 200 that is not JSON is not an answer.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ReasoningUnavailableError(f"model returned no JSON object: {text[:120]!r}")
        return json.loads(text[start:end + 1])


class LocalPolicy:
    """Deterministic fallback planner. Encodes the same rules the prompt states."""

    name = "local-policy"

    def decide_structured(self, task: str, memories: list[dict], held: dict[str, str],
                          candidates: list[str]) -> dict:
        free = [c for c in candidates if c not in held]
        target = free[0] if free else (candidates[0] if candidates else "none")
        warn = next((m for m in memories
                     if m.get("location") and m["location"] in target), None)
        return {
            "target": target,
            "speed": "slow" if warn else "normal",
            "reason": (f"{target} is free; approaching slowly because of a prior report"
                       if warn else f"{target} is free and nearest"),
            "memory_used": warn["lesson"] if warn else None,
        }


class RobotAgent:
    def __init__(self, robot_id: str, memory: FleetMemory):
        self.robot_id = robot_id
        self.memory = memory
        self._reasoner = None
        self._local = LocalPolicy()

    def _get_reasoner(self):
        if self._reasoner is not None:
            return self._reasoner
        try:
            self._reasoner = BedrockReasoner()
            log.info("agent %s: reasoning via Bedrock %s", self.robot_id, CONFIG.chat_model)
        except Exception as exc:
            if CONFIG.strict:
                raise ReasoningUnavailableError(str(exc))
            log.warning("agent %s: Bedrock unavailable (%s) -- deterministic local policy",
                        self.robot_id, type(exc).__name__)
            self._reasoner = self._local
        return self._reasoner

    def plan(self, task: str, candidates: list[str]) -> Decision:
        """Recall first, then decide. Memory precedes action — that is the whole point."""
        memories = self.memory.recall(task, limit=3)
        held = {c["resource_id"]: c["robot_id"] for c in self.memory.live_claims()}

        reasoner = self._get_reasoner()
        if isinstance(reasoner, LocalPolicy):
            data = reasoner.decide_structured(task, memories, held, candidates)
            provider = reasoner.name
        else:
            prompt = json.dumps({
                "task": task,
                "candidate_resources": candidates,
                "held_by_others": held,
                "recalled_memories": [
                    {"lesson": m["lesson"], "location": m["location"],
                     "distance": round(float(m["distance"]), 4)} for m in memories],
            }, indent=2)
            try:
                data = reasoner.decide(prompt)
                provider = reasoner.name
            except Exception as exc:
                log.warning("agent %s: Bedrock call failed (%s) -- local policy for this step",
                            self.robot_id, exc)
                data = self._local.decide_structured(task, memories, held, candidates)
                provider = f"{self._local.name} (bedrock failed)"

        decision = Decision(
            target=data.get("target", "none"),
            speed=data.get("speed", "normal"),
            reason=data.get("reason", ""),
            memory_used=data.get("memory_used"),
            provider=provider,
            raw=data,
        )
        self.memory.record_event(self.robot_id, "decision", {
            "task": task, "target": decision.target, "speed": decision.speed,
            "reason": decision.reason, "memory_used": decision.memory_used,
            "provider": provider,
            "recalled": [m["lesson"] for m in memories],
        })
        return decision

    def execute(self, task: str, candidates: list[str]) -> dict:
        """Plan, claim, act — with a durable run so death is survivable."""
        run_id = self.memory.start_run(self.robot_id, task)
        decision = self.plan(task, candidates)
        self.memory.checkpoint(run_id, 1, {"phase": "planned", "target": decision.target})

        attempted: list[str] = []
        for target in [decision.target] + [c for c in candidates if c != decision.target]:
            if target in attempted or target == "none":
                continue
            attempted.append(target)
            try:
                self.memory.claim(target, self.robot_id, purpose=task)
            except ResourceHeldError as held:
                # Deterministic rejection: re-route rather than retry into the same wall.
                log.info("agent %s: %s held by %s, re-routing", self.robot_id, target, held.holder)
                continue
            self.memory.checkpoint(run_id, 2, {"phase": "claimed", "target": target})
            self.memory.finish_run(run_id, "done")
            return {"robot_id": self.robot_id, "granted": target, "attempted": attempted,
                    "decision": decision.__dict__}

        self.memory.finish_run(run_id, "blocked")
        return {"robot_id": self.robot_id, "granted": None, "attempted": attempted,
                "decision": decision.__dict__}


def active_reasoner_name() -> str:
    """Which reasoner is ACTUALLY usable right now, established by a real call.

    An earlier version checked only that a boto3 client could be built and that STS
    returned an identity. Both succeed with credentials that have no Bedrock access at all,
    so the UI displayed "bedrock" while every inference failed. Valid credentials are not
    model access, and a configured model id is not an available one. Probe the real thing.
    """
    global _PROBED
    try:
        return _PROBED
    except NameError:
        pass
    try:
        reasoner = BedrockReasoner()
        model_id = reasoner.resolve()
        _PROBED = f"bedrock ({model_id})"
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        reason = {"AccessDeniedException": "no Bedrock model access",
                  "NoCredentialsError": "no AWS credentials",
                  "ResourceNotFoundException": "configured model not available in region",
                  }.get(code, code)
        _PROBED = f"local-policy ({reason})"
    return _PROBED
