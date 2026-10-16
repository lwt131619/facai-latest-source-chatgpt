"""Independent capability catalogs. Instances are copied per app, not shared globally."""

import ipaddress
import os
import re
from copy import deepcopy
from urllib.parse import urlparse

_SKILL_ID_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
_SKILL_ID_MAX_LEN = 64
_VALID_REVIEW_STATUSES = frozenset({"pending", "approved", "rejected"})
_FORBIDDEN_SKILL_FIELDS = frozenset({
    "raw", "content", "instruction", "instructions", "body", "scripts",
    "skill_md", "commands", "code", "executable", "path", "api_key",
})
_FULL_METADATA_SIGNALS = frozenset({"source_url", "license", "review_status"})

DEFAULTS = {
    "agents": {"opencode": {"name": "OpenCode", "adapter": "opencode", "status": "available_if_installed"},
               "goose": {"name": "Goose", "adapter": "goose", "status": "available_if_installed"}},
    "models": {
        # The local GLM takes the default seat ONLY where a local GLM endpoint
        # actually exists (REROOT_PLAYBOOK F4): dict order decides the shared
        # default because api.default_binding picks ``next(iter(models))``
        # when no client-registered user model wins (api.py:1631-1633). A
        # production VPS without Ollama/pivot must keep deepseek as default,
        # so the seat is gated on GLM_LOCAL_ENDPOINT being set explicitly.
        # deepseek-flash stays below either way — capability is swapped in
        # front of it, never removed.
        #
        # Endpoint-guard note: this seed path is NOT run through
        # secretbox.validate_public_endpoint. That guard only fires on the two
        # registration APIs — POST /api/v1/my/models (api.py:1092) and the
        # tenant PUT /api/v1/registries/{kind}/{key} (api.py:1337, admins are
        # exempt there per api.py:1334) — so a loopback/pivot endpoint here is
        # seeded as-is; every client-supplied endpoint still goes through the
        # full SSRF guard.
        **({
            "glm-local": {
                "name": "GLM Local (Ollama)", "provider": "Ollama",
                # GLM_LOCAL_ENDPOINT repoints the local seat; the Facai pivot
                # (9900/v1) fronts the Ollama GLM on machines that have it.
                "endpoint": os.environ["GLM_LOCAL_ENDPOINT"],
                "model": "glm-4.6",
                "api_key_env": "GLM_LOCAL_KEY",
                # opencode CLI ref, matching the playbook O1 provider
                # (OPENCODE_CONFIG_CONTENT: provider "facai-glm" at 9900/v1).
                "opencode_model": "facai-glm/glm-4.6",
            },
        } if os.getenv("GLM_LOCAL_ENDPOINT") else {}),
        "deepseek-flash": {
            "name": "DeepSeek Flash", "provider": "DeepSeek",
            "endpoint": "https://api.deepseek.com", "model": "deepseek-flash",
            "api_key_env": "DEEPSEEK_API_KEY", "opencode_model": "deepseek/deepseek-flash",
        },
    },
    "skills": {},
    # 咨询/情报案例库（2026-10-13 操作员指令：爬取外源案例导入 facai）。
    # 与 skills 的分野：skills 是可执行能力（SKILL.md），cases 是参考数据
    # （外源正文，不可执行、无远程权限可切断），但同样必须过边界四类筛查。
    "cases": {},
    "runtimes": {"local": {"name": "Local worker", "kind": "local", "status": "ready",
                           "provider": "facai", "location": "local"}},
}


def _is_full_skill_metadata(value):
    return bool(set(value.keys()) & _FULL_METADATA_SIGNALS)


def _is_private_or_loopback_hostname(hostname):
    if not hostname:
        return False
    if hostname.casefold() in ("localhost", "localtest"):
        return True
    try:
        addr = ipaddress.ip_address(hostname)
        return addr.is_loopback or addr.is_private or addr.is_reserved
    except ValueError:
        return False


def _validate_skill_id(key):
    if not isinstance(key, str) or not (1 <= len(key) <= _SKILL_ID_MAX_LEN):
        raise ValueError(f"Skill ID must be 1-{_SKILL_ID_MAX_LEN} characters")
    if not _SKILL_ID_RE.match(key):
        raise ValueError(
            "Skill ID must be lowercase alphanumeric with single hyphens; "
            "no leading/trailing/double hyphens"
        )


def _validate_skill_metadata(key, value):
    _validate_skill_id(key)

    name = value.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("Skill name must be a non-empty string")
    if name != key:
        raise ValueError(f"Skill name must match registry key: name={name!r}, key={key!r}")

    description = value.get("description")
    if not description or not isinstance(description, str) or not description.strip():
        raise ValueError("Skill description must be a non-empty string")
    if len(description) > 1024:
        raise ValueError("Skill description must be <= 1024 characters")

    source_url = value.get("source_url")
    if not isinstance(source_url, str):
        raise ValueError("Skill source_url must be an https URL string")
    parsed = urlparse(source_url)
    if parsed.scheme != "https":
        raise ValueError("Skill source_url must use https scheme")
    if not parsed.hostname:
        raise ValueError("Skill source_url must have a hostname")
    if parsed.username or parsed.password:
        raise ValueError("Skill source_url must not contain user info")
    if parsed.query:
        raise ValueError("Skill source_url must not contain a query string")
    if parsed.fragment:
        raise ValueError("Skill source_url must not contain a fragment")
    if _is_private_or_loopback_hostname(parsed.hostname):
        raise ValueError(
            "Skill source_url hostname must not be localhost, loopback, "
            "or private/reserved IP"
        )

    license_val = value.get("license")
    if not license_val or not isinstance(license_val, str) or not license_val.strip():
        raise ValueError("Skill license must be specified")

    # SKILL-001 ①: version is optional (legacy rows carry none and stay
    # version-less), but when present it must be a non-empty bounded string —
    # never an int, never whitespace, never a smuggled payload.
    version = value.get("version")
    if version is not None:
        if not isinstance(version, str) or not version.strip():
            raise ValueError("Skill version must be a non-empty string when present")
        if len(version) > 64:
            raise ValueError("Skill version must be <= 64 characters")

    review_status = value.get("review_status", "approved")
    if not isinstance(review_status, str):
        raise ValueError(f"Skill review_status must be a string, got {type(review_status).__name__}")
    if review_status not in _VALID_REVIEW_STATUSES:
        raise ValueError(f"Skill review_status must be one of {sorted(_VALID_REVIEW_STATUSES)}")

    # §十一.C.6: the pending-only gate is gone — a registered skill may carry
    # enabled:true with any review_status; the field is owner bookkeeping.

    value.setdefault("review_status", "approved")


def _check_forbidden_skill_fields(value):
    forbidden = {key for key in value
                 if isinstance(key, str) and key.casefold() in _FORBIDDEN_SKILL_FIELDS}
    if forbidden:
        raise ValueError(f"Forbidden fields in skill metadata: {sorted(forbidden)}")


def reject_raw_api_keys(value):
    """Reject raw credential fields at any depth in JSON registry data."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key.casefold() == "api_key":
                raise ValueError("Use api_key_env; raw API keys are not accepted")
            reject_raw_api_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            reject_raw_api_keys(nested)


def public_registry_value(value):
    """Return registry data with legacy credential fields removed recursively."""
    if isinstance(value, dict):
        return {key: public_registry_value(nested) for key, nested in value.items()
                if not isinstance(key, str) or key.casefold() != "api_key"}
    if isinstance(value, list):
        return [public_registry_value(nested) for nested in value]
    return deepcopy(value)


class Registries:
    KINDS = frozenset(DEFAULTS)

    def __init__(self, store=None):
        self.store = store
        # Preserve the legacy in-memory catalog API used by older integrations
        # and tests. It is scoped to the configured admin owner; tenant APIs
        # continue to read and write their own persisted rows explicitly.
        self.legacy_owner = os.getenv("FACAI_ADMIN_USER", "admin")
        self.catalogs = deepcopy(DEFAULTS)

    def list(self, kind, owner_id="admin"):
        defaults = (self.catalogs.get(kind, {}) if owner_id == self.legacy_owner
                    else deepcopy(DEFAULTS.get(kind, {})))
        if not self.store:
            return defaults
        persisted = self.store.registry_list(kind, owner_id=owner_id)
        # Ensure defaults are persisted for this owner if they aren't already.
        for key, value in defaults.items():
            if key not in persisted:
                self.store.registry_register(kind, key, value, owner_id=owner_id)
                persisted[key] = value
        return {**defaults, **persisted}

    def resolve(self, kind, key, owner_id="admin"):
        """Return a bound registry entry, or None when unbound/missing."""
        if not key:
            return None
        return self.list(kind, owner_id=owner_id).get(key)

    def delete(self, kind, key, owner_id="admin", deleted_by=None):
        """Soft-delete one owner-scoped entry (tombstone kept by the store).

        Returns the tombstone dict when a live row was removed, ``None``
        when nothing live existed. With no store (pure in-memory mode) the
        old boolean contract degrades to a pop-and-report.
        """
        if owner_id == self.legacy_owner:
            self.catalogs.get(kind, {}).pop(key, None)
        if self.store:
            return self.store.registry_delete(kind, key, owner_id=owner_id,
                                              deleted_by=deleted_by)
        return False

    def register(self, kind, key, value, owner_id="admin"):
        if kind not in self.KINDS or not key or not isinstance(value, dict):
            raise ValueError("A nonempty ID and object are required")
        reject_raw_api_keys(value)
        if kind == "skills":
            _check_forbidden_skill_fields(value)
            if "enabled" in value and not isinstance(value["enabled"], bool):
                raise ValueError(
                    "Skill enabled must be a JSON boolean (true/false), "
                    "not a truthy/falsy value"
                )
            if "review_status" in value:
                rs = value["review_status"]
                if not isinstance(rs, str) or rs not in _VALID_REVIEW_STATUSES:
                    raise ValueError(
                        f"Skill review_status must be one of {sorted(_VALID_REVIEW_STATUSES)}"
                    )
            if _is_full_skill_metadata(value):
                _validate_skill_metadata(key, value)
            value.setdefault("review_status", "approved")
        safe_value = deepcopy(value)
        if owner_id == self.legacy_owner:
            self.catalogs[kind][key] = safe_value
        if self.store:
            self.store.registry_register(kind, key, safe_value, owner_id=owner_id)
        return safe_value
