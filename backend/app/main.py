import uuid

from fastapi import FastAPI, Request

from app.routers import admin, audit, auth, health, reviews


def create_app() -> FastAPI:
    # nginx strips the /api prefix; root_path keeps generated docs/links correct behind it.
    app = FastAPI(title="PR Review Agent", root_path="/api")

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = request.headers.get("x-request-id")
        if not rid or len(rid) > 64:
            rid = uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(audit.router)
    app.include_router(reviews.router)
    return app


app = create_app()
