"""Minimal stdlib HTTP server for the sparse matrix repository.

Run:  python3 -m app.server --port 8000 [--root data]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import posixpath
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .sparse import (
    KINDS,
    MatrixError,
    Conflict,
    UnknownLabel,
    LayoutNotReady,
    Repository,
    VerificationFailed,
    parse_triples_text,
)

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class Api:
    def __init__(self, repo: Repository):
        self.repo = repo

    # -- dispatch ----------------------------------------------------------
    def call(self, method: str, path: str, query: dict[str, list[str]],
             body: Any) -> tuple[int, Any]:
        try:
            return self._call(method, path, query, body)
        except (UnknownLabel, LayoutNotReady) as exc:
            return 404 if isinstance(exc, UnknownLabel) else 409, {"error": str(exc)}
        except (Conflict, VerificationFailed) as exc:
            return 409, {"error": str(exc)}
        except MatrixError as exc:
            return 400, {"error": str(exc)}

    def _call(self, method: str, path: str, query: dict[str, list[str]],
              body: Any) -> tuple[int, Any]:
        repo = self.repo
        body = body or {}

        def q(name: str, default: str | None = None) -> str | None:
            vals = query.get(name)
            return vals[0] if vals else default

        def q_int(name: str, default: int) -> int:
            try:
                return int(q(name, str(default)))
            except ValueError:
                raise MatrixError(f"查询参数 {name} 必须是整数")

        def q_range(name: str) -> tuple[int, int] | None:
            raw = q(name)
            if not raw:
                return None
            m = re.fullmatch(r"\s*(-?\d+)\s*[:-]\s*(-?\d+)\s*", raw)
            if not m:
                raise MatrixError(f"查询参数 {name} 应形如 lo:hi")
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            return lo, hi

        if path == "/api/health":
            return 200, {"ok": True}

        if path == "/api/state" and method == "GET":
            return 200, repo.summary()

        if path == "/api/reset" and method == "POST":
            repo.clear()
            return 200, repo.summary()

        if path == "/api/import" and method == "POST":
            policy = str(body.get("policy") or "sum")
            layout_hint = str(body.get("layout") or "csr")
            label = str(body.get("label") or "")
            source = body.get("source") or None
            default_version = int(body.get("source_version") or 1)
            if isinstance(body.get("text"), str):
                records = parse_triples_text(body["text"])
            else:
                records = body.get("triples")
                if not isinstance(records, list):
                    raise MatrixError("请提供 text（CSV/TSV）或 triples（列表）")
                for rec in records:
                    if not isinstance(rec, dict):
                        raise MatrixError("每条三元组必须是对象")
            view = repo.ingest(records, policy=policy, label=label, source=source,
                               default_source_version=default_version,
                               layout_hint=layout_hint)
            return 201, view

        m = re.fullmatch(r"/api/versions/(\d+)/triples", path)
        if m and method == "GET":
            vid = int(m.group(1))
            kinds_raw = q("kinds")
            kinds = set(kinds_raw.split(",")) if kinds_raw else None
            rows = [x for x in (q("rows") or "").split(",") if x]
            cols = [x for x in (q("cols") or "").split(",") if x]
            return 200, {
                "version_id": vid,
                "triples": repo.logical_triples(
                    vid, limit=q_int("limit", 1000), row_names=rows or None,
                    col_names=cols or None, kinds=kinds,
                    row_positions=q_range("rowpos"), col_positions=q_range("colpos")),
            }

        m = re.fullmatch(r"/api/versions/(\d+)/query", path)
        if m and method == "GET":
            vid = int(m.group(1))
            layout = (q("layout") or "csr").lower()
            kinds_raw = q("kinds")
            kinds = set(kinds_raw.split(",")) if kinds_raw else None
            rows = [x for x in (q("rows") or "").split(",") if x]
            cols = [x for x in (q("cols") or "").split(",") if x]
            result = repo.query(
                vid, layout, row_names=rows or None, col_names=cols or None,
                row_positions=q_range("rowpos"), col_positions=q_range("colpos"),
                kinds=kinds, limit=q_int("limit", 5000))
            return 200, result

        m = re.fullmatch(r"/api/versions/(\d+)/point", path)
        if m and method == "GET":
            vid = int(m.group(1))
            row, col = q("row"), q("col")
            if not row or not col:
                raise MatrixError("需要 row 和 col")
            return 200, repo.point(vid, (q("layout") or "csr").lower(), row, col)

        m = re.fullmatch(r"/api/versions/(\d+)/layouts/(csr|csc)/arrays", path)
        if m and method == "GET":
            return 200, repo.arrays_preview(int(m.group(1)), m.group(2),
                                            limit=q_int("limit", 200))

        m = re.fullmatch(r"/api/versions/(\d+)/compare", path)
        if m and method == "GET":
            return 200, repo.compare(int(m.group(1)), (q("layout") or "csr").lower())

        m = re.fullmatch(r"/api/versions/(\d+)/layouts/(csr|csc)/(start|pause|resume|verify)", path)
        if m and method == "POST":
            vid, layout, action = int(m.group(1)), m.group(2), m.group(3)
            if action == "start":
                budget = int(body.get("budget_bytes") or q_int("budget", 4096))
                if budget < 256:
                    raise MatrixError("内存预算至少 256 字节")
                return 200, repo.start_conversion(
                    vid, layout, budget_bytes=budget,
                    pace_ms=int(body.get("pace_ms") or 0),
                    restart=bool(body.get("restart")))
            if action == "pause":
                return 200, repo.pause_conversion(vid, layout)
            if action == "resume":
                return 200, repo.resume_conversion(vid, layout)
            return 200, {"verification": repo.verify_layout(vid, layout)}

        m = re.fullmatch(r"/api/versions/(\d+)/layouts/(csr|csc)/export", path)
        if m and method == "POST":
            vid, layout = int(m.group(1)), m.group(2)
            return 200, {"export": repo.export(vid, layout)}

        m = re.fullmatch(r"/api/versions/(\d+)/element", path)
        if m and method == "POST":
            vid = int(m.group(1))
            row, col = body.get("row"), body.get("col")
            if not row or not col:
                raise MatrixError("需要 row 和 col")
            kind = body.get("kind")
            if kind is None:
                kind = "value" if body.get("value") is not None else "missing"
            return 200, repo.correct_element(
                vid, str(row), str(col), kind=str(kind), value=body.get("value"))

        if path == "/api/labels" and method == "POST":
            return 200, repo.add_label(str(body.get("axis") or ""), str(body.get("name") or ""))

        m = re.fullmatch(r"/api/labels/(row|col)/(\d+)", path)
        if m and method == "POST":
            axis, lid = m.group(1), int(m.group(2))
            return 200, repo.rename_label(axis, lid, str(body.get("name") or ""))

        m = re.fullmatch(r"/api/labels/(row|col)/order", path)
        if m and method == "POST":
            return 200, repo.reorder_labels(m.group(1), body.get("order") or [])

        if path == "/api/sources" and method == "POST":
            return 200, {"source": repo.add_source(str(body.get("name") or ""))}

        m = re.fullmatch(r"/api/sources/(\d+)", path)
        if m and method == "DELETE":
            return 200, repo.delete_source(int(m.group(1)))

        if path == "/api/exports" and method == "GET":
            return 200, {"exports": repo.exports()}

        m = re.fullmatch(r"/api/exports/([A-Za-z0-9._-]+)", path)
        if m and method == "GET":
            name = m.group(1)
            found = [e for e in repo.exports() if e["name"] == name]
            if not found:
                raise UnknownLabel(f"没有名为 {name} 的导出文件")
            return 200, {"export": found[0], "download": f"/files/{name}"}

        raise UnknownLabel(f"未知接口 {method} {path}")


class Handler(BaseHTTPRequestHandler):
    server_version = "SparseRepo/1.0"
    api: Api

    def log_message(self, fmt, *args):  # quieter logs
        pass

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, payload: Any, *, content_type: str = "application/json",
              headers: dict[str, str] | None = None) -> None:
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            data = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise MatrixError(f"请求体不是合法 JSON：{exc}") from exc

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        if path.startswith("/api/"):
            try:
                body = self._json_body() if method in ("POST", "PUT", "PATCH") else None
                status, payload = self.api.call(method, path, query, body)
                self._send(status, payload)
            except MatrixError as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover - last resort
                self._send(500, {"error": f"内部错误：{exc}"})
            return
        self._static(path)

    def _static(self, path: str) -> None:
        root = self.api.repo.root
        if path.startswith("/files/"):
            name = posixpath.basename(path[len("/files/"):])
            target = os.path.join(self.api.repo.export_dir, name)
            if not os.path.isfile(target):
                self._send(404, {"error": "文件不存在"})
                return
            with open(target, "rb") as fh:
                data = fh.read()
            self._send(200, data, content_type="application/octet-stream",
                       headers={"Content-Disposition": f'attachment; filename="{name}"'})
            return
        rel = path.lstrip("/") or "index.html"
        rel = posixpath.normpath(rel)
        if rel.startswith("..") or rel.startswith("/"):
            self._send(403, {"error": "禁止访问"})
            return
        target = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(target):
            self._send(404, {"error": "页面不存在"})
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            data = fh.read()
        self._send(200, data, content_type=ctype)


def make_server(root: str, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    repo = Repository(root)
    api = Api(repo)
    handler = type("BoundHandler", (Handler,), {"api": api})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.path.join(here, "data"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    httpd = make_server(args.root, args.host, args.port)
    print(f"Sparse matrix repository serving on http://{args.host}:{args.port} (data: {os.path.abspath(args.root)})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
