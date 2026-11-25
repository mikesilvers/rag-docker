import sys

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings

# The LLM when LLM_MODEL isn't set (#647).
DEFAULT_LLM_MODEL = "llama3.1:8b"


class Settings(BaseSettings):
    weaviate_host: str = "localhost"
    weaviate_port: int = 8080
    ollama_host: str = "localhost"
    ollama_port: int = 11434
    # Two LLMs (#671): the build model prepares documents and writes gold
    # standard pairs; the answer model answers questions. Each is its own
    # setting when set and not empty, else LLM_MODEL, else DEFAULT_LLM_MODEL.
    # Code reads only the resolved `build_model` and `answer_model` below,
    # never these three fields directly.
    llm_model: str = DEFAULT_LLM_MODEL
    build_llm_model: str = ""
    answer_llm_model: str = ""
    embed_model: str = "nomic-embed-text"
    upload_dir: str = "/app/uploads"
    # Retained original documents, content-addressed per collection. Kept in
    # its own volume because it grows with the corpus, unlike upload_dir which
    # holds only small config and session files.
    sources_dir: str = "/app/sources"
    # Written export packages. A host bind mount, not a named volume: the
    # whole point is that the user can pick the file up and carry it away.
    exports_dir: str = "/app/exports"
    # How long a package this instance exported stays in exports_dir when nobody
    # downloads it (#677), in whole hours, 1 to 720. Counted from the export's
    # finish; the sweep (services/package_retention.py) deletes it after that.
    # Packages copied in to be imported are never expired.
    export_retention_hours: int = 24
    # Ollama's model store, mounted from the same volume the ollama service
    # uses. Only touched when a package bundles models.
    ollama_models_dir: str = "/ollama"
    # Reported by /health/details and compared against the memory Docker actually
    # provides. Raise it in docker-compose.yml; no rebuild required.
    recommended_memory_gb: float = 12.0
    # The most an upload's ZIPs may expand to, in bytes, summed over every ZIP
    # in one request, gzipped members' output included (#298). A small ZIP of
    # zeros can otherwise unpack to gigabytes and fill Docker's disk.
    max_zip_expanded_bytes: int = 2 * 1024 * 1024 * 1024
    # The most entries one ZIP may hold, folders included (#28), and the most a
    # member of 1 MiB or more may expand relative to its compressed size, as
    # declared and as actually read. Members under 1 MiB are exempt from the ratio.
    max_zip_members: int = 5000
    max_zip_member_ratio: int = 200
    # The caps on an imported package's TAR (#317): the most its members' sizes
    # may add up to, and the most entries it may hold, folders included. A
    # package carrying models is several GB, so these are not the ZIP caps.
    # Checked as each header is read, before the member is extracted.
    max_package_expanded_bytes: int = 16 * 1024 * 1024 * 1024
    max_package_members: int = 100000
    # Accounts, sessions and the login throttle (#25): one SQLite file in the
    # auth_data volume. Not /app/auth, which would hide the `auth` package that
    # `python -m auth` runs.
    auth_dir: str = "/app/auth_data"
    # Marks the session cookie Secure. Off for the loopback HTTP profile, where a
    # Secure cookie would never be sent back; LAN mode serves TLS and turns it on (#26).
    auth_cookie_secure: bool = False
    # LAN mode (#26), set only by docker-compose.lan.yml. When true the API
    # refuses to start unless the cookie is Secure and an enabled Engineer
    # exists (lan_mode.py).
    rag_lan_mode: bool = False
    # Query-only mode (#670), set only by docker-compose.query-only.yml. When
    # true the API refuses every route that creates, changes or deletes a
    # collection or its data (auth/policy.py QUERY_ONLY_REFUSED) with
    # 403 QUERY_ONLY; collections are loaded on the host with `python -m loader`.
    rag_query_only: bool = False
    # The audit log (#29): append-only JSON Lines in the ingest_uploads volume,
    # rotated by size (services/audit.py). The folder is its own setting so
    # tests that point upload_dir elsewhere don't find an audit/ folder there.
    audit_dir: str = "/app/uploads/audit"
    # Whether a query's question text is recorded. Off by default; an operator
    # setting only, no route or page changes it.
    audit_log_questions: bool = False
    # audit.jsonl is rotated once it reaches this size; this many rotated files
    # are kept (audit.jsonl.1 newest), the oldest beyond them deleted.
    audit_log_max_bytes: int = 10 * 1024 * 1024
    audit_log_keep: int = 10
    # The preparation area (#638): raw documents uploaded for preparation and
    # their prepared parts, in the rag_preparation volume, until deleted.
    preparation_dir: str = "/app/preparation"
    # The most the area's files may hold, raw and prepared together. A
    # best-effort cap: each upload and each prepared part is checked before it
    # is written, nothing is reserved, so writes running at once can pass it
    # briefly (maintainer's decision, #638).
    max_preparation_bytes: int = 20 * 1024 * 1024 * 1024

    @field_validator("auth_cookie_secure", "rag_lan_mode", "rag_query_only", "audit_log_questions", mode="before")
    @classmethod
    def _exact_boolean(cls, value, info):
        # pydantic would read "1", "yes" or "on" as true. A typo in a security
        # setting must stop the API instead of silently meaning something.
        if isinstance(value, bool):
            return value
        if value in ("true", "false"):
            return value == "true"
        raise ValueError(f"{info.field_name.upper()} must be exactly true or false")

    @field_validator("audit_log_max_bytes")
    @classmethod
    def _rotation_size(cls, value):
        # One record must fit; a smaller limit would rotate on every write.
        if value < 4096:
            raise ValueError("AUDIT_LOG_MAX_BYTES must be at least 4096")
        return value

    @field_validator("max_preparation_bytes")
    @classmethod
    def _preparation_cap(cls, value):
        if value < 1024 * 1024:
            raise ValueError("MAX_PREPARATION_BYTES must be at least 1048576")
        return value

    @field_validator("max_package_expanded_bytes", "max_package_members")
    @classmethod
    def _package_cap(cls, value, info):
        # 0 or less would refuse every package import (#599).
        if value < 1:
            raise ValueError(f"{info.field_name.upper()} must be at least 1")
        return value

    @field_validator("export_retention_hours")
    @classmethod
    def _retention_hours(cls, value):
        # No value turns expiry off (#677): a package must not stay for good.
        if not 1 <= value <= 720:
            raise ValueError("EXPORT_RETENTION_HOURS must be 1 to 720")
        return value

    @field_validator("audit_log_keep")
    @classmethod
    def _rotation_keep(cls, value):
        if not 1 <= value <= 1000:
            raise ValueError("AUDIT_LOG_KEEP must be 1 to 1000")
        return value

    @property
    def build_model(self) -> str:
        """The LLM for preparation and gold standard generation (#671)."""
        return self.build_llm_model or self.llm_model or DEFAULT_LLM_MODEL

    @property
    def answer_model(self) -> str:
        """The LLM that answers questions (#671)."""
        return self.answer_llm_model or self.llm_model or DEFAULT_LLM_MODEL

    class Config:
        env_file = ".env"


def refusal_lines(error: ValidationError) -> list[str]:
    """One `Settings refused:` line per invalid setting (#555), without the values given."""
    reasons: dict[str, list[str]] = {}
    for item in error.errors(include_url=False, include_input=False):
        name = ".".join(str(part) for part in item["loc"]).upper()
        own = str(item["ctx"]["error"]) if item["type"] == "value_error" and "error" in item.get("ctx", {}) else ""
        message = own or item["msg"]
        reason = message if not name or name in message else f"{name}: {message}"
        reasons.setdefault(name, []).append(reason)
    return ["Settings refused: " + "; ".join(parts) for parts in reasons.values()]


def _load() -> Settings:
    # An invalid setting stops the API, and anything else importing this
    # module, with one line per setting and no traceback (#555), like LAN
    # mode's refusal: SystemExit isn't printed as a traceback.
    try:
        return Settings()
    except ValidationError as error:
        for line in refusal_lines(error):
            print(line, file=sys.stderr, flush=True)
        raise SystemExit(1) from None


settings = _load()
