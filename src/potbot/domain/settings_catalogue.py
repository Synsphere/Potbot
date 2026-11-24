"""Versioned settings descriptors; descriptions never confer runtime authority."""

from __future__ import annotations

import json
from typing import Any

from potbot.domain.configuration import DEFAULTS, LAYERS

# Keep category identity stable across the CLI/API and the settings shell.
CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("workspace", "Workspace and profiles", "/"),
    ("appearance", "Appearance and accessibility", "/settings/appearance"),
    ("models", "Models and providers", "/settings/models"),
    ("tools", "Tools, MCP and skills", "/settings/tools"),
    ("swarm", "Workspace swarm", "/settings/swarm"),
    ("execution", "Tasks and execution", "/tools"),
    ("context", "Context and memory", "/context"),
    ("research", "Research and sources", "/research"),
    ("applications", "Applications and browser", "/"),
    ("storage", "Storage and transfers", "/"),
    ("security", "Security and credentials", "/setup/apis"),
    ("services", "Services and network", "/setup/operator"),
    ("diagnostics", "Monitoring and audit", "/setup/operator"),
    ("updates", "Updates and recovery", "/setup/tools"),
)

FEATURES: tuple[tuple[str, str, str, str], ...] = (
    (
        "CFG01",
        "Workspace and profiles",
        "workspace",
        "Workspace identity, roots and layer provenance.",
    ),
    ("CFG02", "Saved appearance", "appearance", "Seven revisioned user preferences."),
    (
        "CFG03",
        "Local layout",
        "appearance",
        "Dockview coordinates and compact density stay browser-local.",
    ),
    (
        "CFG04",
        "Installed model library",
        "models",
        "Observe selected local roots without loading weights.",
    ),
    (
        "CFG05",
        "Inference deployments",
        "models",
        "Exact model, tokenizer, template and runtime identity.",
    ),
    ("CFG06", "Model routing", "models", "Reasoning, coding and specialist deployment selections."),
    (
        "CFG07",
        "Hosted providers",
        "models",
        "Provider status, native credentials and disclosure policy.",
    ),
    (
        "CFG08",
        "Generation and prompts",
        "models",
        "Supported generation and prompt-adapter limits.",
    ),
    (
        "CFG09",
        "Installed tools",
        "tools",
        "Installed metadata, release records and execution authority.",
    ),
    ("CFG10", "MCP and adapters", "tools", "Explicit discovery and installed qualification."),
    ("CFG11", "Skills and workflows", "tools", "Reviewed package identity and enabled state."),
    (
        "CFG12",
        "Agents and reasoning",
        "execution",
        "Versioned search policy, rounds and task revisions.",
    ),
    (
        "CFG13",
        "Budgets and ceilings",
        "execution",
        "Calls, tokens, time, money and resource reservations.",
    ),
    (
        "CFG14",
        "Scheduling and placement",
        "execution",
        "Current jobs; distributed placement remains planned.",
    ),
    (
        "CFG15",
        "Context and instructions",
        "context",
        "MODEL.md snapshots, ACLs, history and restore.",
    ),
    (
        "CFG16",
        "Embeddings and retrieval",
        "context",
        "Versioned embedding profiles and reindex dependencies.",
    ),
    (
        "CFG17",
        "Research and sources",
        "research",
        "Source access, disclosure, fetch and provider limits.",
    ),
    (
        "CFG18",
        "Applications and Xpra",
        "applications",
        "Launch, view, input and terminate remain separate rights.",
    ),
    ("CFG19", "Browser and egress", "applications", "Approved browser recipes and network policy."),
    (
        "CFG20",
        "Storage and downloads",
        "storage",
        "Project-local model caches, artifacts and transfer receipts.",
    ),
    (
        "CFG21",
        "Node identity and lifecycle",
        "swarm",
        "Registered hosts and actual setup records; no inferred liveness.",
    ),
    (
        "CFG22",
        "Membership and control",
        "swarm",
        "Owner-reviewed requests to revoke direct control grants.",
    ),
    (
        "CFG23",
        "Swarm resources",
        "swarm",
        "Deployment relationships; live cross-host capacity remains unavailable.",
    ),
    (
        "CFG24",
        "API and native services",
        "services",
        "Operator attachment and scoped server connections.",
    ),
    (
        "CFG25",
        "Credentials and trust",
        "security",
        "Native custody and credential status, never secret values.",
    ),
    (
        "CFG26",
        "Monitoring and audit",
        "diagnostics",
        "Actual events and qualified operator telemetry.",
    ),
    (
        "CFG27",
        "Updates and recovery",
        "updates",
        "Existing upgrade, history and reconciliation owners.",
    ),
    (
        "CFG28",
        "Feature availability",
        "diagnostics",
        "Distinguish supported, operator-managed and planned features.",
    ),
    (
        "CFG29",
        "Repositories and Types",
        "workspace",
        "Exact source snapshots, verification and Syn releases.",
    ),
)

_CONFIG = {
    "policy_versions": ("Versioned policies", "execution"),
    "reasoning_models": ("Reasoning models", "models"),
    "coding_models": ("Coding models", "models"),
    "allowed_tools": ("Allowed tool selections", "tools"),
    "source_access": ("Source access", "research"),
    "physical_inference_concurrency": ("Physical inference concurrency", "execution"),
    "max_packet_tokens": ("Maximum context packet tokens", "execution"),
    "verification_policy": ("Verification policy", "execution"),
    "embedding_profile": ("Embedding profile", "context"),
    "secret_refs": ("Credential reference count", "security"),
}
PREFERENCE_DESCRIPTORS: tuple[tuple[str, str, str, object], ...] = (
    ("theme", "Theme", "choice", ["system", "dark", "light"]),
    ("graphDisplay", "Graph display", "choice", ["3d", "list"]),
    ("reducedMotion", "Reduce motion", "boolean", None),
    ("showGraphDiagnostics", "Graph diagnostics", "boolean", None),
    ("orbAutoHide", "Automatically hide the orb", "boolean", None),
    ("downloadNotifications", "Download notifications", "boolean", None),
    ("xpraMaxFps", "Xpra client frame limit", "choice", [10, 15, 24, 30, 60]),
)


def display_value(name: str, value: Any) -> str:
    """Do not expose credential references or arbitrary private source locations."""
    if name in {"secret_refs", "source_access"}:
        return f"{len(value)} configured" if isinstance(value, (dict, list)) else "Not recorded"
    text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    return (
        text
        if len(text) <= 1024
        else "Value exceeds the bounded display; inspect its native owner."
    )


def catalogue() -> dict[str, Any]:
    if set(_CONFIG) != set(DEFAULTS):
        raise RuntimeError("Every computational setting requires a reviewed descriptor")
    descriptors: list[dict[str, Any]] = [
        {
            "id": "configuration." + name,
            "label": label,
            "category": category,
            "kind": "integer" if type(DEFAULTS[name]) is int else "snapshot",
            "scopes": list(LAYERS),
            "defaultValue": display_value(name, DEFAULTS[name]),
            "editable": False,
            "apply": "next_admission",
            "owner": "domain.configuration",
            "reason": "Layer editing needs the reviewed configuration-change service.",
            "options": [],
        }
        for name, (label, category) in _CONFIG.items()
    ]
    descriptors.extend(
        {
            "id": "preferences." + name,
            "label": label,
            "category": "appearance",
            "kind": kind,
            "scopes": ["principal_workspace"],
            "defaultValue": "Uses the current client default when unset",
            "editable": True,
            "apply": "client",
            "owner": "ui_resources",
            "reason": "Saved through the existing revisioned preferences API.",
            "options": options or [],
        }
        for name, label, kind, options in PREFERENCE_DESCRIPTORS
    )
    return {
        "categories": [
            {"id": identity, "label": label, "path": path} for identity, label, path in CATEGORIES
        ],
        "features": [
            {"id": identity, "label": label, "category": category, "description": description}
            for identity, label, category, description in FEATURES
        ],
        "descriptors": descriptors,
        "layers": list(LAYERS),
        "layerEditing": False,
    }
