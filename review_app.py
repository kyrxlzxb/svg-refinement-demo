"""Launch the local human SVG layout editor."""

from __future__ import annotations

import argparse
from pathlib import Path

from review_core import ReviewSession


def create_app(session: ReviewSession):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, HTMLResponse, Response
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt to run the review interface") from exc

    app = FastAPI(title="SVG Layout Review", docs_url=None, redoc_url=None)
    assets = Path(__file__).with_name("review_web")

    @app.get("/", response_class=HTMLResponse)
    def page():
        return (assets / "index.html").read_text(encoding="utf-8")

    @app.get("/app.js")
    def javascript():
        return Response((assets / "app.js").read_text(encoding="utf-8"), media_type="text/javascript")

    @app.get("/style.css")
    def stylesheet():
        return Response((assets / "style.css").read_text(encoding="utf-8"), media_type="text/css")

    @app.get("/api/reference")
    def reference():
        return FileResponse(session.reference, media_type="image/png")

    @app.get("/api/state")
    def state():
        return session.state()

    @app.post("/api/action")
    def action(payload: dict):
        try:
            return session.apply(payload)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/undo")
    def undo():
        try:
            return session.undo()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/redo")
    def redo():
        try:
            return session.redo()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/save-draft")
    def save_draft():
        return {"draft_svg": str(session.save_draft())}

    @app.post("/api/prepare")
    def prepare():
        try:
            return session.prepare()
        except (ValueError, ModuleNotFoundError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/prepared/edited.png")
    def prepared_render():
        if session.prepared_revision != session.revision or session.prepared_dir is None:
            raise HTTPException(status_code=404, detail="No current prepared render")
        return FileResponse(session.prepared_dir / "edited.png", media_type="image/png")

    @app.get("/api/prepared/directions/{direction_id}")
    def prepared_direction(direction_id: str):
        if session.prepared_revision != session.revision:
            raise HTTPException(status_code=404, detail="Direction outputs are stale")
        for path in session.prepared_files:
            if path.stem == direction_id:
                return FileResponse(path, media_type="image/svg+xml")
        raise HTTPException(status_code=404, detail="Unknown direction")

    @app.post("/api/finalize")
    def finalize():
        try:
            return session.finalize()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Review and adjust an SVG layout")
    parser.add_argument("--config", type=Path, required=True, help="Review task JSON file")
    parser.add_argument("--punchout", help="Python adapter as module:function; overrides punchout_entry in config")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    session = ReviewSession(args.config, punchout_entry=args.punchout)
    app = create_app(session)
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt to run the review interface") from exc
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
