"""Servidor local (127.0.0.1) com a interface e a API. Sem serviços externos."""
from __future__ import annotations

import copy
import json
import logging
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .common import PrepError, VERSION, now_tag, sha256
from .converters import available_converters
from .modules import module_for
from .office import import_roa, transmittal_ready
from .paths import app_root
from .pipeline import generate_edition
from .store import Store

UPLOAD_LIMIT = 1024 ** 3
JSON_LIMIT = 4 * 1024 ** 2
UI_FILE = app_root() / "ep_toolbox" / "ui" / "index.html"
MAX_SOURCES = 8


class Application:
    """Estado da sessão: origens carregadas, trabalhos em curso e token de acesso."""

    def __init__(self, store: Store):
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.sources: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.guard = threading.RLock()
        self.busy: str | None = None

    def start_job(self, work) -> dict:
        with self.guard:
            if self.busy:
                raise PrepError("Há um processamento em curso. Aguarda a conclusão.")
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"status": "running", "message": "A iniciar…", "percent": 0}
            self.busy = job_id

        def progress(message, percent):
            with self.guard:
                self.jobs[job_id].update(message=message, percent=int(percent))

        def run():
            try:
                value = work(progress)
                with self.guard:
                    self.jobs[job_id].update(status="done", percent=100, result=value)
            except PrepError as error:
                with self.guard:
                    self.jobs[job_id].update(status="error", message=str(error), percent=0,
                                             recovery=error.recovery)
            except Exception as error:  # erro inesperado: regista e mostra uma mensagem geral
                logging.exception("Falha inesperada no processamento")
                with self.guard:
                    self.jobs[job_id].update(status="error", percent=0,
                                             message=f"Erro inesperado: {error}. Consulta aplicacao.log.")
            finally:
                with self.guard:
                    self.busy = None

        threading.Thread(target=run, daemon=True).start()
        return {"job_id": job_id}

    def remember_source(self, source_id: str, source: dict) -> None:
        with self.guard:
            while len(self.sources) >= MAX_SOURCES:
                oldest = next(iter(self.sources))
                _discard_source(self.sources.pop(oldest))
            self.sources[source_id] = source

    def discard_all(self) -> None:
        with self.guard:
            for source in self.sources.values():
                _discard_source(source)
            self.sources.clear()


def _discard_source(source: dict) -> None:
    for key in ("path", "upload"):
        if source.get(key):
            Path(source[key]).unlink(missing_ok=True)
    if source.get("work_dir"):
        shutil.rmtree(source["work_dir"], ignore_errors=True)


def _safe_name(value: str) -> str:
    if not value or value in (".", "..") or Path(value).name != value or "/" in value or "\\" in value:
        raise PrepError("Nome de ficheiro inválido.")
    return value


def make_handler(app: Application):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EPToolbox"

        def log_message(self, *args):
            pass

        def _headers(self, status=200, content_type="application/json; charset=utf-8", length=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "frame-ancestors 'none'; connect-src 'self'")

        def _reply(self, value, status=200):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self._headers(status, length=len(data))
            self.end_headers()
            self.wfile.write(data)

        def _check(self, query):
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                raise PrepError("Pedido recusado: endereço local inválido.")
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + expected:
                raise PrepError("Origem do pedido inválida.")
            token = self.headers.get("X-EP-Token", query.get("token", [""])[0])
            if not secrets.compare_digest(token, app.token):
                raise PrepError("Sessão inválida. Abre novamente a aplicação.")

        def _read_json(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length < 0 or length > JSON_LIMIT:
                raise PrepError("Pedido demasiado grande.")
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            try:
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if url.path == "/":
                    if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                        raise PrepError("Endereço inválido.")
                    data = UI_FILE.read_bytes()
                    self._headers(200, "text/html; charset=utf-8", len(data))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if url.path == "/favicon.ico":
                    self._headers(204, length=0)
                    self.end_headers()
                    return
                self._check(query)
                if url.path == "/api/state":
                    self._reply({
                        "version": VERSION, "profiles": app.store.summary(), "forms": app.store.forms(),
                        "converters": available_converters(), "transmittal_ready": transmittal_ready(),
                        "data_folder": str(app.store.base), "busy": app.busy,
                    })
                elif url.path == "/api/editions":
                    self._reply({"editions": app.store.editions()})
                elif url.path == "/api/job":
                    with app.guard:
                        job = copy.deepcopy(app.jobs.get(query.get("id", [""])[0]))
                    if job is None:
                        raise PrepError("Processamento desconhecido.")
                    self._reply(job)
                elif url.path == "/download":
                    self._download(query)
                else:
                    self._reply({"error": "Recurso desconhecido."}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except PrepError as error:
                self._reply({"error": str(error)}, 400)
            except Exception as error:
                logging.exception("Pedido GET recusado")
                self._reply({"error": f"Erro inesperado: {error}"}, 500)

        def _download(self, query):
            folder = _safe_name(query.get("folder", [""])[0])
            name = _safe_name(query.get("file", [""])[0])
            base = (app.store.base / "saidas").resolve()
            path = (base / folder / name).resolve()
            if not path.is_relative_to(base) or not path.is_file():
                raise PrepError("Ficheiro não encontrado.")
            self._headers(200, mimetypes.guess_type(name)[0] or "application/octet-stream", path.stat().st_size)
            disposition = "inline" if path.suffix in (".pdf", ".html") else "attachment"
            self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{quote(name)}")
            self.end_headers()
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile)

        def do_POST(self):
            try:
                url = urlparse(self.path)
                query = parse_qs(url.query)
                self._check(query)
                if url.path == "/api/upload":
                    self._upload(query)
                    return
                data = self._read_json()
                if url.path == "/api/analyze":
                    self._reply(self._analyze(data))
                elif url.path == "/api/generate":
                    self._reply(self._generate(data))
                elif url.path == "/api/history":
                    with app.guard:
                        if app.busy:
                            raise PrepError("Há um processamento em curso. Aguarda a conclusão.")
                        app.store.set_history(data["key"], data.get("history"))
                    self._reply({"ok": True})
                elif url.path == "/api/forms":
                    with app.guard:
                        if app.busy:
                            raise PrepError("Há um processamento em curso. Aguarda a conclusão.")
                        app.store.save_forms(data.get("forms"))
                    self._reply({"ok": True})
                elif url.path == "/api/open-folder":
                    self._open_folder(data.get("folder"))
                elif url.path == "/api/close":
                    with app.guard:
                        if app.busy:
                            raise PrepError("Há um processamento em curso. Aguarda a conclusão.")
                    self._reply({"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self._reply({"error": "Operação desconhecida."}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except PrepError as error:
                self._reply({"error": str(error)}, 400)
            except Exception as error:
                logging.exception("Pedido POST recusado")
                self._reply({"error": f"Erro inesperado: {error}"}, 500)

        def _upload(self, query):
            kind = query.get("kind", [""])[0]
            key = query.get("key", [""])[0]
            if kind not in ("source", "cover", "history"):
                raise PrepError("Tipo de ficheiro inválido.")
            with app.guard:
                if app.busy:
                    raise PrepError("Há um processamento em curso. Aguarda a conclusão.")
            if kind != "source":
                app.store.get(key)  # valida o perfil antes de receber o ficheiro
            suffix = {"history": ".docx", "cover": ".pdf"}.get(kind)
            if kind == "source":
                suffix = query.get("ext", [".pdf"])[0]
                if suffix not in (".pdf", ".zip"):
                    raise PrepError("Tipo de origem não suportado. Usa PDF ou ZIP.")
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not 1 <= length <= UPLOAD_LIMIT:
                raise PrepError("O ficheiro está vazio ou excede 1 GB.")
            upload_id = uuid.uuid4().hex
            path = app.store.base / "temporarios" / (upload_id + suffix)
            registered = False
            try:
                with path.open("wb") as stream:
                    left = length
                    while left:
                        block = self.rfile.read(min(left, 1024 ** 2))
                        if not block:
                            raise PrepError("Transferência incompleta.")
                        stream.write(block)
                        left -= len(block)
                if kind == "source":
                    self._check_source(path, suffix)
                    app.remember_source(upload_id, {"path": path, "upload": path, "suffix": suffix,
                                                    "name": _safe_name(query.get("name", ["origem"])[0])[:120]})
                    registered = True
                    self._reply({"source_id": upload_id})
                elif kind == "cover":
                    app.store.set_cover(key, path)
                    self._reply({"ok": True})
                else:
                    rows = import_roa(path)
                    profile = app.store.get(key)
                    profile["history"] = rows
                    app.store.save_profile(profile)
                    self._reply({"ok": True, "rows": len(rows)})
            finally:
                if not registered:
                    path.unlink(missing_ok=True)

        def _check_source(self, path: Path, suffix: str):
            with path.open("rb") as stream:
                signature = stream.read(5)
            if suffix == ".pdf" and signature != b"%PDF-":
                raise PrepError("O ficheiro não é um PDF.")
            if suffix == ".zip" and not zipfile.is_zipfile(path):
                raise PrepError("O ZIP está incompleto ou não é válido.")

        def _analyze(self, data):
            source_id = data.get("source_id")
            key = data.get("key")
            profile = app.store.get(key)
            module = module_for(profile)
            if source_id not in app.sources:
                raise PrepError("Seleciona primeiro o ficheiro de origem.")
            source = app.sources[source_id]
            if (source["suffix"] == ".zip") != (module.source_suffix == ".zip"):
                raise PrepError(f"Para este módulo usa {module.source_suffix} ({module.source_label}).")

            def work(progress):
                work_dir = app.store.base / "temporarios" / ("analise_" + source_id)
                # Analisa sempre a origem original; o PDF compilado de um ZIP é guardado à parte.
                path, analysis, effective = module.analyze(
                    source["upload"], profile, work_dir, progress, resolve_profile=app.store.get)
                source.update(
                    path=path, work_dir=work_dir if module.source_suffix == ".zip" else None,
                    analysis=analysis, key=analysis.get("profile_key") or key,
                    profile_snapshot=app.store.get(analysis.get("profile_key") or key),
                    module_id=module.id,
                )
                return analysis

            return app.start_job(work)

        def _generate(self, data):
            source = app.sources.get(data.get("source_id"))
            if not source or "analysis" not in source:
                raise PrepError("Analisa o ficheiro antes de gerar.")
            if app.store.get(source["key"]) != source["profile_snapshot"]:
                raise PrepError("O perfil mudou. Volta a analisar o ficheiro.")
            options = data.get("options") or {}

            def work(progress):
                if source["suffix"] == ".zip" and sha256(source["upload"]) != source["analysis"]["zip_sha256"]:
                    raise PrepError("O ZIP original mudou. Volta a analisá-lo.")
                result = generate_edition(app.store, source["key"], source["path"], source["analysis"],
                                          options, progress)
                source["profile_snapshot"] = app.store.get(source["key"])
                return result

            return app.start_job(work)

        def _open_folder(self, name):
            base = (app.store.base / "saidas").resolve()
            path = base if not name else (base / _safe_name(name)).resolve()
            if not path.is_relative_to(base):
                raise PrepError("Pasta inválida.")
            if os.name == "nt":
                os.startfile(str(path))  # noqa: S606 - pasta local validada acima
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            self._reply({"path": str(path)})

    return Handler


def run(data_dir, open_browser: bool = True) -> None:
    """Arranca o servidor e abre a interface no navegador predefinido."""
    store = Store(data_dir)
    logging.basicConfig(filename=store.base / "aplicacao.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    app = Application(store)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    server.daemon_threads = True
    address = f"http://127.0.0.1:{server.server_port}/#{app.token}"
    logging.info("Aplicação %s iniciada na porta %s (%s)", VERSION, server.server_port, now_tag())
    print(address, flush=True)
    if open_browser:
        webbrowser.open(address)
    try:
        server.serve_forever(poll_interval=.3)
    finally:
        server.server_close()
        app.discard_all()
