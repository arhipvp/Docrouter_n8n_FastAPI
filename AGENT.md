Докроутер н8н — спецификация проекта (актуальная)

Ниже — цельная спецификация, с которой любая ИИ-модель/разработчик сможет продолжать писать код и расширять систему без контекста переписки. Всё заточено под Windows и локальный ран.

1) Цель и сценарий

Цель: Автоконвейер обрабатывает 1 PDF за запуск:

Читает файл из C:\Data\inbox.

Извлекает текст (OCR при необходимости).

Определяет язык (статистически).

ИИ выбирает конечную папку в архиве или предлагает новую.

Формирует итоговое имя и путь и перемещает PDF в C:\Data\archive.

Генерирует описания (summary RU/DE) и полные тексты (RU/DE).

Пишет сайдкары: metadata.json, content.ru.txt, content.de.txt.

Печатает финальный отчёт в консоль FastAPI.

Структура архива (4 уровня):
<category>/<subcategory>/<issuer>/<person>
(В текущей итерации верхний уровень — люди, но код/LLM рассчитаны на общую схему.)

2) Технологии и окружение

ОС: Windows.

Оркестратор: n8n (локально).

Сервис-утилиты: FastAPI (uvicorn), локально 127.0.0.1:8081.

Библиотеки:

PDF: PyMuPDF (fitz) для текста; ocrmypdf (Tesseract) для OCR.

Язык: langdetect (детектор инициализируется и «прогревается»).

HTTP клиент (в консольном потоке решений): httpx.

Внешний LLM-провайдер: напр. OpenRouter (или другая совместимая нода n8n).

Инструменты в PATH: Tesseract (deu, eng, rus), (опционально) Poppler не требуется для текущего пути.

Пути:

Inbox: C:\Data\inbox

Archive (root): C:\Data\archive

3) FastAPI — эндпоинты и контракты

Базовый запуск:

uvicorn app:app --host 127.0.0.1 --port 8081 --reload

3.1 /health (GET)

Проверка живости: { "ok": true }.

3.2 /extract-text-by-path (POST)

In: { "file_path": "C:\\Data\\inbox\\<file>.pdf", "ocr_langs": "deu+eng+rus" }
Out:

{
  "text": "<полный текст>",
  "has_text_layer": true|false,       // true только если это был "родной" текст, не OCR
  "used_ocr": true|false,
  "pages": 1,
  "size_bytes": 12345
}


Логика: Сначала PyMuPDF, если пусто — OCR через ocrmypdf. Логи ведутся на каждом шаге.

3.3 /extract-text (POST, multipart)

Вспомогательный (ручной тест).

3.4 /lang (POST)

In: { "text": "..." }
Out: { "detected_lang": "ru|de|en|...", "prob": 0.0..1.0 }
Важно: есть warm-up и lock, чтобы избежать Need to load profiles.

3.5 /list-archive-tree (GET)

Out: { "tree": {...} } — рекурсивное дерево всех папок в C:\Data\archive.
(Ранее возвращался ещё плоский список — сейчас не нужен.)

3.6 /folder-endpoints (GET)

Out: { "folder_endpoints": ["a/b/c/d", ...] } — только готовые конечные точки 4-го уровня.

3.7 /route-apply (POST)

In: { "inbox_name": "original.pdf", "selected_path": "A/B/C/D" }
Out:

{
  "final_rel_path": "A/B/C/D",
  "final_path": "C:\\Data\\archive\\A\\B\\C\\D",
  "final_name": "YYYY-MM-DD__original.pdf"
}


(Используется, если хотим стандартизировать имя на стороне FastAPI. Сейчас можно и без него — из ответа LLM.)

3.8 /fs-mkdir (POST)

In: { "rel_path": "A/B/C/D" }
Создаёт папку внутри архива. Возвращает { "ok": true, "dest_dir": "..." }.

3.9 /fs-move (POST)

In: { "src_path": "C:\\Data\\inbox\\x.pdf", "dest_dir": "C:\\Data\\archive\\A\\B\\C\\D", "dest_name": "Name.pdf" }
Out: { "ok": true, "dest_path": "..." }

Предохранители (уже реализованы):

dest_dir должен быть абсолютным, не оканчиваться на .pdf, и находиться внутри C:\Data\archive.

Убираем случайный префикс = в путях (если прилетело из n8n).

Иначе возвращаем 400, файл не «теряется».

3.10 /decisions/init (POST)

In:

{
  "request_id": "C:\\Data\\inbox\\x.pdf",
  "resume_url": "<n8n wait url>",
  "folder_endpoints": ["A/B/C/D", ...],
  "suggested_path": "A/B/C/D",
  "preview_text": "<<=1000 chars>"
}


Печатает в консоль меню выбора, ждёт ввод и POST’ит в resume_url одно из:

// выбрана существующая:
{ "request_id": "...", "selected_path": "A/B/C/D", "suggested_path": null, "create": false }

// выбрана новая:
{ "request_id": "...", "selected_path": null, "suggested_path": "A/B/C/D", "create": true }

3.11 /print-report (POST)

In: { "final_report": { ... } } — печатает «человеческий» отчёт (см. формат ниже).

4) Форматы данных (контракты с LLM и отчётом)
4.1 LLM — Routing (выбор папки)

System (строго):

Выбери ОДНУ папку из folder_endpoints по TEXT.
Верни строго JSON:
{"matched":bool,"selected_path":string|null,"confidence":0..1,"reason":string,"needs_new_folder":bool,"suggested_path":string|null}
Без текста вне JSON.


User:

FOLDER_ENDPOINTS:
<JSON-массив путей 4-го уровня>

TEXT:
---
<полный текст>
---


Ожидаемый JSON:

{
  "matched": true,
  "selected_path": "Вера Архипова/Госуслуги и ведомства/Регистрация",
  "confidence": 0.9,
  "reason": "краткая причина",
  "needs_new_folder": false,
  "suggested_path": null
}


Если нет подходящей — "matched": false, "needs_new_folder": true, "suggested_path": "...".

4.2 LLM — Texts (summary + RU/DE full)

System (строго):

Верни СТРОГО JSON:
{
  "summaries":{"ru":string,"de":string},
  "content":{
    "ru":{"text":string,"source":"original|machine_translation"},
    "de":{"text":string,"source":"original|machine_translation"},
    "truncated":false
  }
}
Для языка-оригинала ставь source:"original" и копируй текст; второй язык — переведи.
Никакого текста вне JSON.


User (с подстановками):

DETECTED_LANG={{ $('Lang').item.json.detected_lang }}
TEXT:
---
{{ $('Text').item.json.text }}
---

4.3 Финальный отчёт

Формат:

{
  "status": "routed",
  "file": {
    "original_name": "<inbox_name>",
    "pages": 1,
    "size_bytes": 87426,
    "detected_lang": "ru",
    "used_ocr": false
  },
  "routing": {
    "matched": true,
    "selected_path": "A/B/C/D",
    "confidence": 0.9,
    "needs_new_folder": false,
    "reason": "..."
  },
  "summaries": { "ru": "...", "de": "..." },
  "content_preview": { "ru_short": "<=1000>", "de_short": "<=1000>" }
}

5) План работы приложения

Шаг 1. Запуск FastAPI-сервиса. На Windows-машине в корне проекта поднимается uvicorn (`uvicorn app:app --host 127.0.0.1 --port 8081 --reload`). FastAPI предоставляет контракты, описанные выше, и остаётся доступным для запросов из n8n и CLI-утилиты решений. При старте важно убедиться, что доступ к файловой системе (C:\Data\inbox и C:\Data\archive) открыт, а зависимости PyMuPDF, ocrmypdf и langdetect готовы к работе.

Шаг 2. Запуск n8n-воркфлоу. Воркфлоу запускается локально и инициирует обработку входящего PDF-файла из `C:\Data\inbox`. На первом этапе узел чтения файлов получает список файлов и по одному передаёт их дальше.

Шаг 3. Извлечение текста. Узел HTTP-запроса вызывает эндпоинт FastAPI `/extract-text-by-path`, передавая путь к PDF и список поддерживаемых языков OCR. FastAPI сначала пробует PyMuPDF и при необходимости запускает OCR через ocrmypdf. Результат (текст, флаги `has_text_layer` и `used_ocr`, метаданные страниц и размера) возвращается в n8n.

Шаг 4. Определение языка. Следующий узел n8n вызывает `/lang`, передавая полученный текст. FastAPI выполняет «прогрев» детектора и возвращает JSON с `detected_lang` и `prob`. Эти данные используются для дальнейшей маршрутизации и подсказок LLM.

Шаг 5. Маршрутизация с участием LLM. n8n получает полный список доступных конечных папок через `/folder-endpoints` (при необходимости также `/list-archive-tree` для визуализации). Узел LLM маршрутизации формирует промпт согласно разделу 4.1, подставляя `folder_endpoints` и извлечённый текст. Ответ LLM анализируется: если предложен существующий путь — используется он; если требуется новая папка — формируется предложение пути. Для стандартизации имени файла n8n при необходимости вызывает `/route-apply` с выбранными параметрами.

Шаг 6. Создание папок и перемещение файлов. Если LLM запросил новую структуру, n8n вызывает `/fs-mkdir`, передавая относительный путь, чтобы FastAPI создал директории внутри архива. Затем нода перемещения вызывает `/fs-move`, передавая исходный путь, целевую директорию и итоговое имя (из `/route-apply` либо сформированное LLM). FastAPI проверяет предохранители, перемещает файл в архив и возвращает конечный путь.

Шаг 7. Генерация текстов и отчёта. Узел LLM текстов вызывает модель по контракту 4.2, получая JSON с summary и полными текстами на двух языках. n8n формирует структуру финального отчёта (раздел 4.3) и вызывает `/print-report`, передавая собранные данные. FastAPI выводит отчёт в консоль. При необходимости воркфлоу также создаёт сайдкары (metadata.json, content.ru.txt, content.de.txt) и сохраняет их рядом с перемещённым PDF.

Шаг 8. Завершение и ожидание следующего файла. Воркфлоу уведомляет оператора (например, через CLI-утилиту решений `/decisions/init`, если требовалось вмешательство) и возвращается к ожиданию следующего документа в `C:\Data\inbox`.
