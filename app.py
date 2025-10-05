import os, tempfile, subprocess, glob, threading, queue, sys, json
from typing import Optional, List, Dict
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from langdetect import detect_langs

app = FastAPI(title="Doc pipeline utils")

# ---------- small utils ----------
def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:800])
    return p.stdout

def pdf_to_text(path: str) -> str:
    return run(["pdftotext","-layout",path,"-"])

def pdf_pages(path: str) -> int:
    out = run(["pdfinfo", path])
    for line in out.splitlines():
        if line.lower().startswith("pages:"):
            return int(line.split(":")[1].strip())
    return None

def ocr_pdf(path: str, langs: str="deu+eng+rus", dpi: str="300") -> str:
    # pdftoppm -> OCR по страницам
    base = path + "_p"
    run(["pdftoppm","-r",dpi,path,base,"-png"])
    parts = []
    for img in sorted(glob.glob(base + "-*.png")):
        txt_path = img + ".txt"
        subprocess.run(["tesseract", img, img, "-l", langs], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
            parts.append(f.read())
        try: os.remove(txt_path)
        except: pass
        try: os.remove(img)
        except: pass
    return "\n\n".join(parts)

# ---------- API: extract-text ----------
@app.post("/extract-text")
async def extract_text(file: UploadFile = File(...),
                       ocr_langs: str = Form("deu+eng+rus")):
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix != ".pdf":
        return JSONResponse({"error":"only .pdf accepted"}, status_code=400)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        data = await file.read()
        tmp.write(data)
        tmp_path = tmp.name

    try:
        pages = pdf_pages(tmp_path)
        # сначала пробуем текстовый слой
        try_text = ""
        try:
            try_text = pdf_to_text(tmp_path)
        except Exception:
            try_text = ""
        has_text_layer = bool(try_text.strip())
        used_ocr = False

        if has_text_layer:
            text = try_text
        else:
            used_ocr = True
            text = ocr_pdf(tmp_path, langs=ocr_langs)

        return {
            "text": text,
            "has_text_layer": has_text_layer,
            "used_ocr": used_ocr,
            "pages": pages,
            "size_bytes": len(data)
        }
    finally:
        try: os.remove(tmp_path)
        except: pass

# ---------- API: language detection ----------
class LangIn(BaseModel):
    text: str

@app.post("/lang")
def lang_detect(body: LangIn):
    text = (body.text or "").strip()
    if not text:
        return {"detected_lang": None, "prob": 0.0}
    try:
        langs = detect_langs(text)  # e.g. [de:0.99]
        best = max(langs, key=lambda x: x.prob)
        return {"detected_lang": str(best.lang), "prob": float(best.prob)}
    except Exception:
        return {"detected_lang": None, "prob": 0.0}

# ---------- Console decisions (n8n Wait webhook handshake) ----------
DECISIONS = queue.Queue()

class DecisionInit(BaseModel):
    request_id: str
    resume_url: str
    folder_endpoints: List[str]
    suggested_path: Optional[str] = None
    preview_text: Optional[str] = None  # до 1000 символов

@app.post("/decisions/init")
def decisions_init(d: DecisionInit):
    DECISIONS.put(d.dict())
    print(f"\n[docrouter] DECISION queued: {d.request_id} (waiting in console)")
    return {"ok": True}

def _console_loop():
    while True:
        d = DECISIONS.get()
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

        print("\nChoose: enter number 1..N to use existing folder,")
        print("or 'c' to create new (will ask path, default is suggested).")
        choice = input("> ").strip()

        if choice.lower() == "c":
            new_path = input(f"New path [{sugg}]: ").strip() or sugg
            body = {"request_id": d["request_id"], "selected_path": None,
                    "suggested_path": new_path, "create": True}
        else:
            try:
                idx = int(choice)
                sel = d["folder_endpoints"][idx-1]
                body = {"request_id": d["request_id"], "selected_path": sel,
                        "suggested_path": None, "create": False}
            except Exception:
                print("Invalid choice. Skipping; re-run init if needed.", file=sys.stderr)
                continue

        try:
            import httpx
            with httpx.Client(timeout=30) as client:
                client.post(d["resume_url"], json=body)
            print("[docrouter] decision sent to n8n, workflow resumed.")
        except Exception as e:
            print(f"[docrouter] failed to POST resume: {e}", file=sys.stderr)

@app.on_event("startup")
def _boot():
    t = threading.Thread(target=_console_loop, daemon=True)
    t.start()

# ---------- API: print final report ----------
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

    print("\n========== DOC PIPELINE REPORT ==========")
    print(f"status: {fr.get('status')}")
    print(f"file:   {file.get('original_name')} | pages={file.get('pages')} | size={file.get('size_bytes')} | lang={file.get('detected_lang')} OCR={file.get('used_ocr')}")
    if routing.get("matched"):
        print(f"path:   {routing.get('selected_path')}  (conf={routing.get('confidence')})")
    else:
        if routing.get("needs_new_folder"):
            sug = routing.get("suggestion") or {}
            sug_path = sug if isinstance(sug, str) else "/".join([sug.get(k,"") for k in ("category","subcategory","issuer","person")])
            print(f"path:   NEEDS NEW → {sug_path}  (conf={routing.get('confidence')})")
        if routing.get("reason"): print(f"reason: {routing.get('reason')}")
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
