"""Reader-only Notion library, isolated from bot knowledge and AI retrieval."""
import json
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
from dashboard.auth import current_permissions, has_permission
from utils.notion_library import articles


def install_library_routes(app, templates, template_context):
    async def visible(request, query=""):
        if not has_permission(request, "library.access"):
            raise HTTPException(403, "Knowledge Base access required.")
        return await run_in_threadpool(articles, current_permissions(request), query)

    @app.get("/library", response_class=HTMLResponse, name="library_index")
    async def library_index(request: Request, q: str = ""):
        q = q.strip()[:200]
        rows = await visible(request, q)
        return templates.TemplateResponse(request=request, name="library.html",
            context=template_context(request, page_title="Knowledge Base", articles=rows, query=q, article=None),
            headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"})

    @app.get("/library/{page_id}", response_class=HTMLResponse, name="library_article")
    async def library_article(request: Request, page_id: str):
        rows = await visible(request)
        article = next((r for r in rows if r["page_id"] == page_id), None)
        if article is None:
            raise HTTPException(404, "Article not found.")
        article["blocks"] = json.loads(article["content"])
        return templates.TemplateResponse(request=request, name="library.html",
            context=template_context(request, page_title=article["title"], articles=[], query="", article=article),
            headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"})
