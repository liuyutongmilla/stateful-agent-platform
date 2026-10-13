"""Application wiring: FastAPI app, lifespan-managed resources, REST endpoints."""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from mcp import Client
from pydantic import BaseModel

from .agent.graph import build_graph
from .agent.llm import LLM, make_llm
from .agent.runtime import AgentRuntime
from .agent.sessions import SessionStore
from .agent.state import STAGES
from .agent.tools import ToolGateway
from .auth import AuthError, CallerContext, Principal, issue_session_token, verify_session_token
from .authz import make_authorizer
from .authz.seed import DEMO_USERS
from .config import Settings, get_settings
from .gateway.hub import Hub
from .gateway.ws import router as ws_router
from .mcp_server import build_admin_server

log = logging.getLogger("app")


class LoginBody(BaseModel):
    user: str


def create_app(settings: Settings | None = None, *, llm: LLM | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            # --- persistence: one SQLite file for checkpoints and the session registry
            if settings.checkpoint_db != ":memory:":
                Path(settings.checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
            conn = await stack.enter_async_context(aiosqlite.connect(settings.checkpoint_db))
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            checkpointer = AsyncSqliteSaver(conn)
            await checkpointer.setup()
            sessions = SessionStore(conn)
            await sessions.setup()

            # --- tools: remote MCP server, or the same server in-process
            if settings.mcp_url:
                mcp_target: Any = settings.mcp_url
            else:
                authz = await make_authorizer(settings)
                stack.push_async_callback(authz.close)
                app.state.admin_server = build_admin_server(authz, None, settings.secret)
                mcp_target = app.state.admin_server.mcp
            client = await stack.enter_async_context(Client(mcp_target))
            tools = ToolGateway(client, settings.secret)

            # --- agent runtime
            hub = Hub(settings.replay_buffer)
            graph = build_graph(
                llm or make_llm(settings.llm_provider, settings.llm_model, settings.mock_token_delay_ms),
                tools,
                checkpointer,
                max_iterations=settings.max_iterations,
                prefilter_tools=os.environ.get("AGENT_PREFILTER_TOOLS", "1") == "1",
                require_approval=os.environ.get("AGENT_REQUIRE_APPROVAL", "1") == "1",
            )
            runtime = AgentRuntime(graph, hub, sessions, max_iterations=settings.max_iterations)
            stack.push_async_callback(runtime.shutdown)

            app.state.settings, app.state.hub, app.state.sessions = settings, hub, sessions
            app.state.runtime, app.state.tools = runtime, tools
            log.info(
                "ready: llm=%s authz=%s mcp=%s",
                settings.llm_provider,
                "spicedb" if settings.spicedb_endpoint else ("remote" if settings.mcp_url else "in-memory"),
                settings.mcp_url or "in-process",
            )
            yield

    app = FastAPI(title="Stateful AI Agent Platform", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ws_router)

    def principal(authorization: str = Header(default="")) -> Principal:
        try:
            return verify_session_token(settings.secret, authorization.removeprefix("Bearer ").strip())
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from None

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "stages": list(STAGES), "max_iterations": settings.max_iterations, "llm": settings.llm_provider}

    @app.get("/api/demo-users")
    async def demo_users() -> list[dict[str, str]]:
        if not settings.demo_login:
            raise HTTPException(404)
        return [{"id": uid, **u} for uid, u in DEMO_USERS.items()]

    @app.post("/api/login")
    async def login(body: LoginBody) -> dict[str, Any]:
        """Passwordless login for the seeded demo users. Disable with DEMO_LOGIN=0 and put real SSO in front."""
        if not settings.demo_login:
            raise HTTPException(404)
        user = DEMO_USERS.get(body.user)
        if user is None:
            raise HTTPException(401, "unknown user")
        p = Principal(body.user, user["org"])
        return {"token": issue_session_token(settings.secret, p), "user": {"id": body.user, **user}}

    @app.get("/api/sessions")
    async def list_sessions(p: Principal = Depends(principal)) -> list[dict[str, Any]]:
        return await app.state.sessions.list_for(p.user_id)

    @app.post("/api/sessions")
    async def create_session(p: Principal = Depends(principal)) -> dict[str, Any]:
        return await app.state.sessions.create(p.user_id, p.org_id)

    @app.get("/api/me")
    async def me(p: Principal = Depends(principal)) -> dict[str, Any]:
        """The caller's roles and permitted tools, as SpiceDB sees them right now."""
        outcome = await app.state.tools.call("whoami", {}, CallerContext(p.user_id, p.org_id, "", "whoami", ""))
        if outcome.status != "ok":
            return {"user": p.user_id, "organization": p.org_id, "roles": [], "allowed_tools": [], "operable_services": []}
        return outcome.data

    @app.get("/api/tools")
    async def list_tools(p: Principal = Depends(principal)) -> list[dict[str, Any]]:
        """The full tool catalog with the permission each tool requires (for display)."""
        return [
            {"name": s.name, "description": s.description, "permission": s.permission, "destructive": s.destructive, "read_only": s.read_only}
            for s in await app.state.tools.specs()
        ]

    # Serve the built console from the same origin when it is present (single-container deploys).
    static_dir = Path(os.environ.get("STATIC_DIR", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
    if static_dir.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=static_dir, html=True), name="console")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))
