from pathlib import Path
import os
import sys
import json
import glob
import shutil
import tempfile
import threading
import queue
import logging
import time
from typing import Optional, List, Dict

from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langdetect import detect_langs
import httpx

# === PDF stack: PyMuPDF for text, OCRmyPDF for OCR (internally uses Tesseract) ===
import fitz  # PyMuPDF
import ocrmypdf

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s"
)
logger = logging.getLogger("docrouter.fastapi")

# --------------------------------------------------------------------------------------
# Langdetect globals
# --------------------------------------------------------------------------------------
LANGDETECT_LOCK = threading.Lock()
LANGDETECT_READY = False

# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------
class RouteApplyIn(BaseModel):
    inbox_name: str                  # исходное имя файла из инбокса (с расширением)
    selected_path: str               # относительный путь с '/' (как отдаёт ИИ)

class MoveIn(BaseModel):
    src_path: str
    dest_dir: str
    dest_name: str

class MkdirIn(BaseModel):
    rel_path: str  # относительный путь от C:\Data\archive (со слэшами '/')

class ExtractByPathIn(BaseModel):
    file_path: str
    ocr_langs: Optional[str] = "deu+eng+rus"

class LangIn(BaseModel):
    text: str

class DecisionInit(BaseModel):
    request_id: str
    resume_url: str
    folder_endpoints: List[str]
    suggested_path: Optional[str] = None
    preview_text: Optional[str] = None

# --------------------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------------------

app = FastAPI(title="Doc pipeline utils")

# CORS (на всякий случай — удобно для локальных тестов/браузера)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Глобальный HTTP-middleware для логов всех запросов
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    path = request.url.path
    method = request.method
    try:
        logger.info(f"➡ {method} {path}")
        response = await call_next(request)
        dur_ms = int((time.time() - start) * 1000)
        logger.info(f"⬅ {method} {path} → {response.status_code} ({dur_ms} ms)")
        return response
    except Exception as e:
        dur_ms = int((time.time() - start) * 1000)
        logger.exception(f"✖ {method} {path} crashed after {dur_ms} ms: {e}")
        raise

# --------------------------------------------------------------------------------------
# Helpers: text extraction
# --------------------------------------------------------------------------------------
def safe(s: str) -> str:
    return (s or "").strip().replace("/", "_").replace("\\", "_").replace(":", "_")\
        .replace("*", "_").replace("?", "_").replace('"', "_").replace("<", "_")\
        .replace(">", "_").replace("|", "_")[:80]


def _read_pdf_text(path: str) -> str:
    """Извлекает текстовый слой через PyMuPDF. Без OCR."""
    parts: List[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            parts.append(page.get_text("text"))
    return "\n".join(parts).strip("\n")

def _pdf_pages(path: str) -> int:
    with fitz.open(path) as doc:
        return len(doc)

def _needs_ocr(text: str) -> bool:
    return not bool((text or "").strip())

def _ocr_via_ocrmypdf(src_pdf: str, langs: str = "deu+eng+rus", max_pages_ocr: Optional[int] = None) -> str:
    """
    Прогоняет PDF через ocrmypdf.force_ocr и возвращает ИЗВЛЕЧЁННЫЙ после этого текст.
    Не требует Poppler. Нужен установленный Tesseract-OCR (в PATH или стандартной папке).
    """
    if max_pages_ocr is not None:
        pages = _pdf_pages(src_pdf)
        if pages > max_pages_ocr:
            logger.warning(f"OCR skipped due to pages>{max_pages_ocr}: {pages}")
            # Можно реализовать частичный OCR — сейчас пропускаем.
            return ""

    tmp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp_out.close()
    try:
        logger.info(f"OCR start: langs={langs} file={src_pdf}")
        ocrmypdf.ocr(
            src_pdf,
            tmp_out.name,
            language=langs or "deu+eng+rus",
            force_ocr=True,
            progress_bar=False,
            quiet=True,
        )
        text_after = _read_pdf_text(tmp_out.name)
        logger.info(f"OCR done: {len(text_after)} chars")
        return text_after
    finally:
        try:
            os.remove(tmp_out.name)
        except Exception:
            pass

def extract_text_core(path: str, ocr_langs: Optional[str]) -> Dict:
    """Единая логика: попытка текста -> при необходимости OCR -> итоговый JSON."""
    pages = _pdf_pages(path)
    text = ""
    used_ocr = False

    logger.info(f"Extract start: {path} | pages={pages}")
    # 1) пробуем обычное извлечение
    try:
        text = _read_pdf_text(path)
        logger.info(f"Extract via text layer: {len(text)} chars")
    except Exception as e:
        logger.exception(f"PyMuPDF extract failed, will try OCR: {e}")
        text = ""

    # 2) если текста нет — OCR
    if _needs_ocr(text):
        if not ocr_langs:
            used_ocr = False
            logger.info("No text & OCR disabled → returning empty text")
        else:
            try:
                text = _ocr_via_ocrmypdf(path, ocr_langs, max_pages_ocr=None)
                used_ocr = True
            except Exception as e:
                logger.exception("OCR failed")
                raise HTTPException(
                    status_code=500,
                    detail=f"OCR failed: {e}. Make sure Tesseract is installed (Windows: C:\\Program Files\\Tesseract-OCR)."
                )

    has_text_layer = bool(text.strip())
    size_bytes = os.path.getsize(path)

    logger.info(f"Extract done: chars={len(text)} has_text_layer={has_text_layer and not used_ocr} used_ocr={used_ocr} size={size_bytes}")
    return {
        "text": text or "",
        "has_text_layer": has_text_layer and not used_ocr,  # если сделали OCR — слой уже «наш», отмечаем как не-оригинал
        "used_ocr": used_ocr,
        "pages": pages,
        "size_bytes": size_bytes,
    }

# --------------------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------------------

@app.get("/health")
def health():
    logger.info("Health check")
    return {"ok": True}

@app.post("/extract-text")
async def extract_text(file: UploadFile = File(...),
                       ocr_langs: str = Form("deu+eng+rus")):
    """
    Опциональный приём файла (multipart) — полезно для ручных тестов.
    Основной путь в конвейере — /extract-text-by-path.
    """
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix != ".pdf":
        logger.warning(f"/extract-text: wrong suffix {suffix}")
        return JSONResponse({"error": "only .pdf accepted"}, status_code=400)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        data = await file.read()
        tmp.write(data)
        tmp_path = tmp.name

    logger.info(f"/extract-text: uploaded bytes={len(data)}, ocr_langs={ocr_langs}")
    try:
        result = extract_text_core(tmp_path, ocr_langs)
        result["size_bytes"] = len(data)
        return result
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

@app.post("/extract-text-by-path")
def extract_text_by_path(body: ExtractByPathIn):
    path = os.path.normpath(body.file_path)
    if not path.lower().endswith(".pdf"):
        logger.warning(f"/extract-text-by-path wrong extension: {path}")
        return JSONResponse({"error": "only .pdf accepted"}, status_code=400)
    if not os.path.exists(path):
        logger.warning(f"/extract-text-by-path not found: {path}")
        return JSONResponse({"error": "file not found"}, status_code=404)

    logger.info(f"/extract-text-by-path: {path} ocr_langs={body.ocr_langs}")
    try:
        result = extract_text_core(path, body.ocr_langs or "deu+eng+rus")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("extract_text_by_path unexpected failure")
        return JSONResponse({"error": "extract_failed", "detail": str(e)}, status_code=500)

def _ensure_langdetect_ready():
    """Гарантирует единоразовый прогрев профилей langdetect."""
    global LANGDETECT_READY
    if LANGDETECT_READY:
        return
    with LANGDETECT_LOCK:
        if LANGDETECT_READY:
            return
        try:
            detect_langs("This is a warmup text for langdetect.")
            LANGDETECT_READY = True
            logger.info("langdetect warmed up")
        except Exception as e:
            logger.warning(f"langdetect warmup failed: {e}")


@app.post("/lang")
def lang_detect(body: LangIn):
    text = (body.text or "").strip()
    n_chars = len(text)
    logger.info(f"/lang: {n_chars} chars")
    if not text:
        return {"detected_lang": None, "prob": 0.0}
    try:
        _ensure_langdetect_ready()
        with LANGDETECT_LOCK:
            langs = detect_langs(text)
        best = max(langs, key=lambda x: x.prob)
        result = {"detected_lang": str(best.lang), "prob": float(best.prob)}
        logger.info(f"/lang result: {result}")
        return result
    except Exception as e:
        logger.exception("/lang failed")
        return {"detected_lang": None, "prob": 0.0}

# --------------------------------------------------------------------------------------
# Console decisions (ручное подтверждение маршрута)
# --------------------------------------------------------------------------------------

DECISIONS = queue.Queue()

@app.post("/decisions/init")
def decisions_init(d: DecisionInit):
    DECISIONS.put(d.dict())
    logger.info(f"[decisions] queued: {d.request_id}, endpoints={len(d.folder_endpoints)}, suggested={bool(d.suggested_path)}")
    print(f"\n[docrouter] DECISION queued: {d.request_id} (waiting in console)")
    return {"ok": True}

def console_loop():
    while True:
        d = DECISIONS.get()
        # Печатаем «человеческое» меню в консоль (сохраняем UX), но ещё и логируем
        logger.info(f"[console] decision required for {d['request_id']}")
        print("\n================= DECISION REQUIRED =================")
        print(f"request_id: {d['request_id']}")
        print("Existing endpoints:")
        for i, p in enumerate(d["folder_endpoints"], 1):
            print(f"  [{i}] {p}")
        sugg = d.get("suggested_path") or ""
        if sugg:
            print(f"Suggested NEW path: {sugg}")
        prev = (d.get("preview_text") or "")[:1000]
        if prev:
            print("\n[TEXT PREVIEW <=1000]:")
            print(prev)
        print("\nChoose: number 1..N, or 'c' to create new (then enter path).")
        choice = input("> ").strip()
        if choice.lower() == "c":
            new_path = input(f"New path [{sugg}]: ").strip() or sugg
            body = {"request_id": d["request_id"], "selected_path": None,
                    "suggested_path": new_path, "create": True}
            logger.info(f"[console] new path chosen: {new_path}")
        else:
            try:
                idx = int(choice); sel = d["folder_endpoints"][idx-1]
                body = {"request_id": d["request_id"], "selected_path": sel,
                        "suggested_path": None, "create": False}
                logger.info(f"[console] selected existing path: {sel}")
            except Exception:
                print("Invalid choice. Re-run init if needed.", file=sys.stderr)
                logger.warning("[console] invalid choice")
                continue
        try:
            with httpx.Client(timeout=30) as client:
                client.post(d["resume_url"], json=body)
            print("[docrouter] decision sent to n8n, workflow resumed.")
            logger.info("[console] resume POSTed to n8n")
        except Exception as e:
            print(f"[docrouter] failed to POST resume: {e}", file=sys.stderr)
            logger.exception("[console] resume POST failed")

@app.on_event("startup")
def boot():
    _ensure_langdetect_ready()
    t = threading.Thread(target=console_loop, daemon=True)
    t.start()
    logger.info("Console decision thread started")

# --------------------------------------------------------------------------------------
# Final report printer
# --------------------------------------------------------------------------------------

@app.post("/print-report")
async def print_report(payload: Dict):
    fr = payload.get("final_report", payload)

    def short(s, n=1000):
        if not s: return ""
        s = str(s)
        return s if len(s) <= n else s[:n].rsplit(' ',1)[0] + "…"

    file = fr.get("file", {})
    routing = fr.get("routing", {})
    sums = fr.get("summaries", {})
    cont = fr.get("content_preview", {})

    logger.info(f"[report] status={fr.get('status')} file={file.get('original_name')} pages={file.get('pages')} lang={file.get('detected_lang')} OCR={file.get('used_ocr')}")
    print("\n========== DOC PIPELINE REPORT ==========")
    print(f"status: {fr.get('status')}")
    print(f"file:   {file.get('original_name')} | pages={file.get('pages')} | size={file.get('size_bytes')} | lang={file.get('detected_lang')} OCR={file.get('used_ocr')}")
    if routing.get("matched"):
        print(f"path:   {routing.get('selected_path')}  (conf={routing.get('confidence')})")
    else:
        if routing.get("needs_new_folder"):
            sug_path = routing.get("selected_path") or routing.get("suggested_path") or ""
            print(f"path:   NEEDS NEW → {sug_path}  (conf={routing.get('confidence')})")
        if routing.get("reason"):
            print(f"reason: {routing.get("reason")}")
    print("\n-- SUMMARY (RU) --")
    print(sums.get('ru',""))
    print("\n-- SUMMARY (DE) --")
    print(sums.get('de',""))
    print("\n-- FULL TEXT PREVIEW (RU, 1000) --")
    print(short(cont.get('ru_short') or '', 1000))
    print("\n-- FULL TEXT PREVIEW (DE, 1000) --")
    print(short(cont.get('de_short') or '', 1000))
    print("=========================================\n")
    return {"ok": True}

@app.get("/folder-endpoints")
def list_folder_endpoints():
    root = r"C:\Data\archive"
    result = []
    logger.info(f"/folder-endpoints scan: root={root}")
    if os.path.exists(root):
        try:
            for a in os.scandir(root):
                if not a.is_dir():
                    continue
                pa = os.path.join(root, a.name)
                for b in os.scandir(pa):
                    if not b.is_dir():
                        continue
                    pb = os.path.join(pa, b.name)
                    for c in os.scandir(pb):
                        if not c.is_dir():
                            continue
                        pc = os.path.join(pb, c.name)
                        for d in os.scandir(pc):
                            if d.is_dir():
                                result.append(f"{a.name}/{b.name}/{c.name}/{d.name}")
        except Exception as e:
            logger.exception("/folder-endpoints scan_failed")
            return JSONResponse({"error": "scan_failed", "detail": str(e)}, status_code=500)
    logger.info(f"/folder-endpoints: {len(result)} endpoints")
    return {"folder_endpoints": result}

def _build_tree(node: Path, base: Path) -> dict:
    out = {
        "name": node.name,
        "path_rel": str(node.relative_to(base)).replace("\\", "/") if node != base else "",
        "children": []
    }
    for d in sorted([p for p in node.iterdir() if p.is_dir()], key=lambda x: x.name.lower()):
        out["children"].append(_build_tree(d, base))
    return out

@app.get("/list-archive-tree")
def list_archive_tree(root: str = r"C:\Data\archive"):
    base = Path(root)
    logger.info(f"/list-archive-tree: root={root}")
    if not base.exists():
        return {"tree": None, "all_paths": []}

    tree = _build_tree(base, base)

    # плоский список всех относительных путей (любой глубины, не только 4)
    all_paths: list[str] = []
    def _collect(n: dict):
        if n.get("path_rel"):
            all_paths.append(n["path_rel"])
        for c in n.get("children", []):
            _collect(c)
    _collect(tree)

    # Возвращаем только tree (как сейчас ожидает n8n)
    return {"tree": tree}

@app.post("/route-apply")
def route_apply(body: RouteApplyIn):
    rel = (body.selected_path or "").strip().strip("/").replace("\\", "/")
    if not rel:
        logger.warning("/route-apply: selected_path missing")
        raise HTTPException(status_code=400, detail="selected_path is required")

    final_path = os.path.join(r"C:\Data\archive", *rel.split("/"))
    date_prefix = __import__("datetime").date.today().isoformat()
    base_name = os.path.splitext(body.inbox_name or "document.pdf")[0]
    final_name = f"{date_prefix}__{safe(base_name)}.pdf"

    logger.info(f"/route-apply: rel={rel} → final_dir={final_path}, final_name={final_name}")
    return {
        "final_rel_path": rel,
        "final_path": final_path,
        "final_name": final_name
    }

@app.post("/fs-move")
def fs_move(body: MoveIn):
    src = os.path.normpath(body.src_path)
    dest_dir = os.path.normpath(body.dest_dir)
    dest_name = body.dest_name

    for bad in '\\/:*?"<>|':
        dest_name = dest_name.replace(bad, "_")

    logger.info(f"/fs-move: {src} → {dest_dir}\\{dest_name}")
    if not os.path.exists(src):
        logger.warning(f"/fs-move: src missing {src}")
        return JSONResponse({"error": "src_missing", "path": src}, status_code=404)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, dest_name)
        shutil.move(src, dest)
        logger.info(f"/fs-move: moved to {dest}")
        return {"ok": True, "dest_path": dest}
    except Exception as e:
        logger.exception("/fs-move failed")
        return JSONResponse({"error": "move_failed", "detail": str(e)}, status_code=500)

@app.post("/fs-mkdir")
def fs_mkdir(body: MkdirIn):
    rel = (body.rel_path or "").strip().strip("/").replace("\\", "/")
    if not rel:
        logger.warning("/fs-mkdir: rel_path missing")
        raise HTTPException(status_code=400, detail="rel_path is required")

    dest_dir = os.path.join(r"C:\Data\archive", *rel.split("/"))
    try:
        os.makedirs(dest_dir, exist_ok=True)
        logger.info(f"/fs-mkdir: created {dest_dir}")
        return {"ok": True, "dest_dir": dest_dir}
    except Exception as e:
        logger.exception("/fs-mkdir failed")
        return JSONResponse({"error": "mkdir_failed", "detail": str(e)}, status_code=500)
