"""Qdrant memory provider using a Qdrant MCP REST endpoint.

Provides semantic long-term memory backed by Qdrant, accessed via
qdrant_upsert / qdrant_search / qdrant_delete exposed by an MCP server.

Requirements:
  - MCP_API_KEY environment variable set
  - QDRANT_MCP_URL accessible (defaults to http://10.10.0.62:3100)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from agent.redact import redact_sensitive_text
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://10.10.0.62:3100"
_DEFAULT_COLLECTION = "memory-chief"
_DEFAULT_TIMEOUT = 10.0
_MIN_CONTENT_LENGTH = 5
_MAX_STORAGE_LENGTH = 2000


class QdrantMemoryProvider(MemoryProvider):
    name: str = "qdrant"

    def __init__(self):
        self._url: str = _DEFAULT_URL
        self._api_key: str = ""
        self._collection: str = _DEFAULT_COLLECTION
        self._session_id: str = ""
        self._redis_client: Any = None
        self._sync_thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()

    def is_available(self) -> bool:
        api_key = get_secret("MCP_API_KEY", "").strip()
        if not api_key:
            return False
        url = (get_secret("QDRANT_MCP_URL", "") or _DEFAULT_URL).rstrip("/")
        return bool(url) and url.startswith(("http://", "https://"))

    def initialize(self, session_id: str, **kwargs) -> None:
        self._api_key = get_secret("MCP_API_KEY", "").strip()
        self._url = (get_secret("QDRANT_MCP_URL", "") or _DEFAULT_URL).rstrip("/")
        self._collection = get_secret("QDRANT_MEMORY_COLLECTION", _DEFAULT_COLLECTION)
        self._session_id = session_id
        self._init_redis()

    def _init_redis(self) -> None:
        redis_password = get_secret("INFRA_REDIS_PASSWORD", "").strip()
        redis_url = get_secret("REDIS_URL", "").strip()
        redis_host = get_secret("REDIS_HOST", "").strip()
        if not redis_password or (not redis_url and not redis_host):
            return
        try:
            import redis as redis_lib
            if redis_url:
                client = redis_lib.from_url(
                    redis_url,
                    password=redis_password,
                    decode_responses=True,
                    socket_connect_timeout=1,
                )
            else:
                port = int(get_secret("REDIS_PORT", "6379"))
                client = redis_lib.Redis(
                    host=redis_host,
                    port=port,
                    password=redis_password,
                    decode_responses=True,
                    socket_connect_timeout=1,
                )
            client.ping()
            self._redis_client = client
            logger.debug("Qdrant provider connected to Redis for cache/health")
        except Exception:
            logger.debug("Qdrant provider Redis optional connection failed, continuing without cache")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self.is_available():
            return ""
        try:
            raw = self._call_mcp_tool(
                "qdrant_search",
                {
                    "collection": self._collection,
                    "query": query,
                    "limit": 8,
                },
            )
            return self._format_prefetch(raw, session_id=session_id or self._session_id)
        except Exception as e:
            logger.debug("Qdrant prefetch failed: %s", e, exc_info=True)
            return ""

    @staticmethod
    def _hit_session_id(item: Dict[str, Any]) -> str:
        """Extract the originating session_id from a search hit's payload."""
        try:
            payload = item.get("payload", {}) if isinstance(item.get("payload"), dict) else {}
            meta = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
            sid = meta.get("session_id", payload.get("session_id", ""))
            return str(sid or "")
        except Exception:
            return ""

    @staticmethod
    def _hit_date(item: Dict[str, Any]) -> str:
        """Extract a YYYY-MM-DD date from a search hit's payload timestamp."""
        try:
            payload = item.get("payload", {}) if isinstance(item.get("payload"), dict) else {}
            meta = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
            ts = str(meta.get("timestamp", payload.get("timestamp", "")) or "")
            return ts[:10] if len(ts) >= 10 else ""
        except Exception:
            return ""

    def _format_prefetch(self, raw: Any, *, session_id: str = "") -> str:
        items: List[dict] = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("result", raw.get("results", raw.get("points", [])))
        if not items:
            return ""
        items = [it for it in items if isinstance(it, dict)]
        current = str(session_id or "")
        # Prefer hits from the current session, then by score (stable sort).
        if current:
            items = sorted(
                items,
                key=lambda it: 0 if self._hit_session_id(it) == current else 1,
            )
        lines = [
            f"## Qdrant Memory ({self._collection})",
            "NOTE: Items marked [other session] are PAST conversations from "
            "DIFFERENT threads/sessions. Use them as background only — do NOT "
            "treat them as part of the current conversation.",
        ]
        count = 0
        for item in items:
            payload = item.get("payload", {}) if isinstance(item.get("payload"), dict) else {}
            text = payload.get("text", payload.get("content", ""))
            if not text:
                continue
            score = item.get("score", item.get("similarity", item.get("distance", 0)))
            try:
                score_str = f"[score: {float(score):.2f}] "
            except Exception:
                score_str = ""
            hit_sid = self._hit_session_id(item)
            if hit_sid and current and hit_sid == current:
                origin = "[this thread] "
            elif hit_sid:
                date = self._hit_date(item)
                short_id = hit_sid.split("_")[-1][:8] if "_" in hit_sid else hit_sid[:8]
                origin = f"[other session {short_id}{', ' + date if date else ''}] "
            else:
                origin = ""
            count += 1
            lines.append(f"- [{count}] {origin}{score_str}{text}")
            if count >= 5:
                break
        if count == 0:
            return ""
        return "\n".join(lines)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        # Raw turns can contain credentials, tool output, and temporary work.
        # Only explicit qdrant_memory_remember calls may create durable entries.
        return

    def _queue_upsert(self, point_id: str, content: str, metadata: Dict[str, Any]) -> None:
        content = self._sanitize_for_storage(content)
        if not content:
            return
        metadata = self._sanitize_metadata_for_storage(metadata)

        def _do() -> None:
            try:
                self._call_mcp_tool(
                    "qdrant_upsert",
                    {
                        "collection": self._collection,
                        "text": content,
                        "id": point_id,
                        "metadata": metadata,
                    },
                )
            except Exception as e:
                logger.debug("Qdrant sync_turn upsert failed: %s", e, exc_info=True)
        t = threading.Thread(target=_do, daemon=True)
        t.start()
        with self._write_lock:
            self._sync_thread = t

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "qdrant_memory_remember",
                "description": "Store a piece of information in long-term Qdrant memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Text to remember."},
                        "metadata": {
                            "type": "object",
                            "description": "Optional key-value metadata.",
                        },
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "qdrant_memory_search",
                "description": "Search Qdrant memory by semantic similarity.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query."},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "qdrant_memory_delete",
                "description": "Delete Qdrant memory points by ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "UUID point IDs to delete.",
                        },
                    },
                    "required": ["ids"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "qdrant_memory_remember":
                content = args.get("content", "")
                if not content:
                    return json.dumps({"error": "Missing required field: content"})
                sanitized = self._sanitize_for_storage(content)
                if not sanitized:
                    return json.dumps({"saved": False, "reason": "Content too short after sanitization"})
                point_id = str(uuid.uuid4())
                metadata = args.get("metadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata.setdefault("type", "explicit")
                metadata = self._sanitize_metadata_for_storage(metadata)
                self._call_mcp_tool(
                    "qdrant_upsert",
                    {
                        "collection": self._collection,
                        "text": sanitized,
                        "id": point_id,
                        "metadata": metadata,
                    },
                )
                return json.dumps({"saved": True, "id": point_id})
            elif tool_name == "qdrant_memory_search":
                query = args.get("query", "")
                limit = args.get("limit", 5)
                results = self._call_mcp_tool(
                    "qdrant_search",
                    {
                        "collection": self._collection,
                        "query": query,
                        "limit": limit,
                    },
                )
                return json.dumps({"results": results})
            elif tool_name == "qdrant_memory_delete":
                ids = args.get("ids", [])
                if not ids:
                    return json.dumps({"error": "Missing required field: ids"})
                for point_id in ids:
                    self._call_mcp_tool(
                        "qdrant_delete",
                        {
                            "collection": self._collection,
                            "id": point_id,
                        },
                    )
                return json.dumps({"deleted": True, "ids": ids})
            else:
                return json.dumps({"error": f"Unknown tool {tool_name}"})
        except Exception as e:
            logger.debug("Qdrant tool call %s failed: %s", tool_name, e, exc_info=True)
            return json.dumps({"error": str(e)})

    def _call_mcp_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        url = f"{self._url}/tools/call"
        payload = json.dumps({"name": name, "arguments": arguments}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(data["error"])
            return data.get("result", data.get("content", data))

    def _clean_content(self, text: str) -> str:
        if not text or len(text.strip()) < _MIN_CONTENT_LENGTH:
            return ""
        trivial = re.compile(
            r"^(ok|okay|thanks|thank you|got it|sure|yes|no|yep|nope|k|ty|thx|np)\.?$",
            re.IGNORECASE,
        )
        if trivial.match(text.strip()):
            return ""
        return text.strip()

    def _sanitize_for_storage(self, text: str) -> str:
        if not text:
            return ""
        # Strip bracketed System note blocks/lines
        text = re.sub(r'\[[Ss]ystem note:[^\]]*\]', '', text)
        text = re.sub(r'^[ \t]*[Ss]ystem note:.*$', '', text, flags=re.MULTILINE)
        # Strip PRIVATE_*_DO_NOT_QUOTE XML-ish blocks
        text = re.sub(
            r'<PRIVATE_[^>]+_DO_NOT_QUOTE>.*?</PRIVATE_[^>]+_DO_NOT_QUOTE>',
            '',
            text,
            flags=re.DOTALL,
        )
        text = re.sub(r'<PRIVATE_[^>]+_DO_NOT_QUOTE\s*/>', '', text)
        # Strip memory-context blocks
        text = re.sub(r'<memory-context>.*?</memory-context>', '', text, flags=re.DOTALL)
        # Strip excessive tool/log style content
        text = re.sub(r'```[\s\S]*?```', '', text)
        text = re.sub(r'^[ \t]*>.*$', '', text, flags=re.MULTILINE)
        text = re.sub(
            r'^[ \t]*(?:\[\d{2}:\d{2}:\d{2}\]|LOG|ERROR|DEBUG|INFO|WARN|tool_call).*$',
            '',
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        # Strip empty role placeholders left after content removal
        text = re.sub(r'\[role: user\]\s*(?=\[role: assistant\]|$)', '', text)
        text = re.sub(r'\[role: assistant\]\s*$', '', text)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        # Persistent-memory boundaries must redact regardless of the runtime
        # display/log preference. file_read=True produces non-reusable markers.
        text = redact_sensitive_text(
            text,
            force=True,
            file_read=True,
            redact_url_credentials=True,
        )
        # Cap stored text to safe length
        if len(text) > _MAX_STORAGE_LENGTH:
            text = text[:_MAX_STORAGE_LENGTH].rstrip() + '...'
        # Skip if too short
        if len(text) < _MIN_CONTENT_LENGTH:
            return ""
        return text

    @staticmethod
    def _sanitize_metadata_for_storage(metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Redact all metadata values before they cross the durable boundary."""
        def sanitize(value: Any) -> Any:
            if isinstance(value, dict):
                return {str(key): sanitize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [sanitize(item) for item in value]
            if isinstance(value, tuple):
                return [sanitize(item) for item in value]
            if isinstance(value, str):
                return redact_sensitive_text(
                    value,
                    force=True,
                    file_read=True,
                    redact_url_credentials=True,
                )
            return value

        return sanitize(metadata)

    def shutdown(self) -> None:
        with self._write_lock:
            if self._sync_thread and self._sync_thread.is_alive():
                self._sync_thread.join(timeout=2.0)
        if self._redis_client:
            try:
                self._redis_client.close()
            except Exception:
                pass


def register(ctx):
    ctx.register_memory_provider(QdrantMemoryProvider())
