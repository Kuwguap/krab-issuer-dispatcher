from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class ApiConfig:
    """
    Configuration for the Admin API.

    Currently focuses on:
    - Admin password for protecting dashboard endpoints.
    """

    admin_password: str
    cors_origins: tuple[str, ...]
    cors_origin_regex: str | None
    supabase_url: str
    supabase_service_role_key: str

    @classmethod
    def from_env(cls) -> "ApiConfig":
        # Baseline admin origins: always included so a partial CORS_ORIGINS in Render
        # cannot accidentally lock out the production Vercel dashboard or local dev.
        baseline = [
            "https://krab-dispatch.vercel.app",
            "https://krabdispatch.vercel.app",
            # Local dev across common ports (uvicorn 8000/8080, Vite 5173, Flask 5000)
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:5000",
            "http://localhost:5000",
            "http://127.0.0.1:3000",
            "http://localhost:3000",
        ]
        raw_origins = (os.getenv("CORS_ORIGINS") or "").strip()
        env_list = (
            [o.strip() for o in raw_origins.split(",") if o.strip()] if raw_origins else []
        )
        seen: set[str] = set()
        merged: list[str] = []
        for o in env_list + baseline:
            if o not in seen:
                seen.add(o)
                merged.append(o)
        origins = tuple(merged)
        raw_regex = (os.getenv("CORS_ORIGIN_REGEX") or "").strip() or None
        # Default regex allows Vercel previews AND any localhost / 127.0.0.1 port.
        return cls(
            admin_password=os.getenv("ADMIN_PASSWORD", "AdminPassword123!"),
            cors_origins=origins,
            cors_origin_regex=raw_regex
            or r"^(https://.*\.vercel\.app|http://(localhost|127\.0\.0\.1)(:\d+)?)$",
            supabase_url=(os.getenv("SUPABASE_URL") or "").strip().rstrip("/"),
            supabase_service_role_key=(os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip(),
        )

    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)










