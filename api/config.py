from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    weaviate_host: str = "localhost"
    weaviate_port: int = 8080
    ollama_host: str = "localhost"
    ollama_port: int = 11434
    llm_model: str = "phi3.5"
    embed_model: str = "nomic-embed-text"
    upload_dir: str = "/app/uploads"
    # Retained original documents, content-addressed per collection. Kept in
    # its own volume because it grows with the corpus, unlike upload_dir which
    # holds only small config and session files.
    sources_dir: str = "/app/sources"
    # Written export packages. A host bind mount, not a named volume: the
    # whole point is that the user can pick the file up and carry it away.
    exports_dir: str = "/app/exports"
    # Ollama's model store, mounted from the same volume the ollama service
    # uses. Only touched when a package bundles models.
    ollama_models_dir: str = "/ollama"
    # Reported by /health and compared against the memory Docker actually
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

    @field_validator("auth_cookie_secure", "rag_lan_mode", "audit_log_questions", mode="before")
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

    @field_validator("audit_log_keep")
    @classmethod
    def _rotation_keep(cls, value):
        if not 1 <= value <= 1000:
            raise ValueError("AUDIT_LOG_KEEP must be 1 to 1000")
        return value

    class Config:
        env_file = ".env"


settings = Settings()
