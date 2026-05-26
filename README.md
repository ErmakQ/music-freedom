# muzik_maks

Веб-приложение: загрузка текстового плейлиста (строки вида «исполнитель - название»), поиск треков и скачивание архива. **Backend:** FastAPI. **Frontend:** React (Vite).

Старый Telegram-бот и консольный скрипт лежат в папке **[archive/](archive/)** (см. [archive/README.md](archive/README.md)).

## Требования

- **Python** 3.10+
- **Google Chrome** и **ChromeDriver** (драйвер подтягивается автоматически под версию Chrome; см. `CHROMEDRIVER_*` в [.env.example](.env.example))
- **Node.js** 18+ — для сборки и dev-режима фронта

## Установка

```bash
cd путь\к\muzik_maks
py -m pip install -r requirements.txt
copy .env.example .env
```

В `.env` задайте при необходимости `WEB_SESSION_SECRET` и `WEB_CORS_ORIGINS` (пример в `.env.example`).

## Запуск

**API** (из корня репозитория):

```bash
py -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

**Фронт** (отдельный терминал):

```bash
cd frontend
npm install
npm run dev
```

Открой URL из Vite (обычно `http://localhost:5173`). Прокси на API — порт **8000**.

Во время обработки web-интерфейс показывает живой статус задачи: очередь, фазу поиска ссылок, фазу скачивания, текущий трек и итоговую сводку. Когда архив готов, браузер начинает скачивание автоматически.

После `npm run build` статика из `frontend/dist` может отдаваться тем же uvicorn на одном порту — открой `http://127.0.0.1:8000`.

Если API на другом origin, в `frontend/.env` задайте `VITE_API_BASE` (см. `frontend/.env.example`).

## Формат плейлиста

Описание строк и кодировок: **[archive/playlist_format.txt](archive/playlist_format.txt)**.

## Структура проекта

| Путь | Роль |
|------|------|
| `web/app.py` | FastAPI: `/api/process`, лимиты, CORS, сессия |
| `frontend/` | React UI |
| `downloader_core.py` | Поиск ссылок и скачивание |
| `download_service.py` | `DownloadConfig` из `.env` |
| `playlist_utils.py` | Разбор `.txt` плейлиста |
| `archive/` | Бот, CLI, примеры (не используются вебом) |

## Переменные окружения

| Переменная | Назначение |
|------------|------------|
| `WEB_CORS_ORIGINS` | Origins фронта через запятую |
| `WEB_SESSION_SECRET` | Cookie-сессия с `anon_id` (пусто — без неё) |
| `WEB_SESSION_HTTPS_ONLY` | `1` — cookie только по HTTPS |
| `WEB_REPORT_LIST_CAP` | Сколько failed/skipped строк в отчёте (10–500) |
| `WEB_MAX_UPLOAD_BYTES` | Лимит размера загружаемого .txt (в байтах) |
| `CHROMEDRIVER_PATH` / `CHROMEDRIVER_CACHE_DIR` / … | См. `.env.example` |
| `HEADLESS`, `WAIT_SEC`, `DELAY`, `MAX_PLAYLIST_LINES` | Поведение Chrome и плейлиста |
| `SITE_ORIGIN` | Базовый домен сайта для поиска и скачивания (по умолчанию `https://eu.hitmoz.com`) |
| `LMUSIC_ORIGIN` | Домен fallback-провайдера для ненайденных треков (по умолчанию `https://lmusic.kz`) |
| `LMUSIC_FALLBACK_ENABLED` | Включить fallback-поиск на `lmusic.kz` для строк, не найденных на основном сайте |
| `MP3_MIRROR_HOSTS` | Дополнительные зеркала mp3 для fallback во 2 фазе, через запятую |
| `MAX_CONCURRENT_JOBS` | Одновременные задания на API |
| `MAX_DOWNLOAD_BYTES` | Лимит размера одного mp3 (0 = без лимита) |
| `DOWNLOAD_PARALLEL_WORKERS`, `RESOLVE_PARALLEL_WORKERS` | Параллелизм HTTP и Chrome |
| `SEARCH_TRY_HTTP_FIRST`, `SEARCH_TRANSLIT`, … | См. `.env.example` |

Полный список с комментариями — в [.env.example](.env.example).

## Логи

- **`playlist_web`** — HTTP API.
- **`playlist_dl`** — загрузка плейлиста (фазы поиска и скачивания).
- Для CLI каждый прогон пишет `downloaded_music/run_*/run_info.txt` со сводкой по запуску и `downloaded_music/run_*/downloaded.log` с успешно скачанными треками.
- Для web/API сохраняются только неуспешные прогоны: `downloaded_music/web_runs/run_*/run_info.txt` и `downloaded_music/web_runs/run_*/job.log`.

## Прервать Фазу 2 И Собрать ZIP

Если во время **фазы 2** (HTTP-скачивание файлов) нужно остановить процесс, самый безопасный вариант:

```bash
Ctrl+C
py pack_downloads.py
```

По умолчанию будет собран архив **`playlist_partial.zip`** из файлов последнего прогона в папке **`downloaded_music/run_*`**.

Можно указать свои пути:

```bash
py pack_downloads.py --source downloaded_music --output my_tracks.zip
```

Сообщение **`Could not find platform independent libraries <prefix>`** на Windows часто связано с установкой Python; при проблемах поставьте Python с [python.org](https://www.python.org/downloads/) или вызывайте явную версию: `py -3.12 -m uvicorn ...`.
## Quality layer

`muzik_maks` теперь проверяет не только сам факт скачивания mp3, но и качество совпадения версии трека. Цель: в итоговый архив должны попадать только подходящие варианты: original, album, full, explicit или uncensored, если пользователь не попросил другую версию явно.

По умолчанию отклоняются clean, censored, edited, safe, radio edit/radio version, short/preview/snippet, remix, live, karaoke, instrumental, slowed, reverb, nightcore, sped up, cover, acoustic и demo-версии. Если такая версия указана во входной строке плейлиста, она считается допустимой для этого трека.

Ключевые статусы качества: `OK`, `NOT_FOUND`, `DOWNLOAD_FAILED`, `LOW_CONFIDENCE_MATCH`, `REJECTED_CLEAN_VERSION`, `REJECTED_CENSORED_VERSION`, `REJECTED_RADIO_VERSION`, `REJECTED_EDITED_VERSION`, `REJECTED_SHORT_VERSION`, `REJECTED_REMIX_VERSION`, `REJECTED_LIVE_VERSION`, `REJECTED_KARAOKE_VERSION`, `VERSION_MISMATCH`, `DURATION_MISMATCH`, `SUSPICIOUS_FILE`, `NEEDS_MANUAL_REVIEW`.

Каждый прогон пишет диагностические файлы рядом с результатом: `quality_report.json`, `quality_report.csv`, `manual_review.csv`, `candidates.json`, а скачанные файлы делятся на `accepted/` и `rejected/`. Итоговый ZIP собирается только из `accepted/`, то есть только из файлов со статусом `OK`.

CLI retry-режимы:

```bash
py archive/scripts.py --retry-failed downloaded_music/run_YYYYMMDD_HHMMSS
py archive/scripts.py --retry-not-found downloaded_music/run_YYYYMMDD_HHMMSS
py archive/scripts.py --retry-suspicious downloaded_music/run_YYYYMMDD_HHMMSS
py archive/scripts.py --retry-manual-review downloaded_music/run_YYYYMMDD_HHMMSS
py archive/scripts.py --retry-rejected downloaded_music/run_YYYYMMDD_HHMMSS
```

Ограничения: проект не гарантирует нахождение 100% треков, источники могут быть неполными или отдавать нестабильные ссылки. Логика проекта не обходит DRM, paywall, авторизацию, региональные блокировки или техническую защиту сайтов. `rejected`, `suspicious` и `manual review` означают, что файл не подходит для итогового архива или требует ручной проверки.

## Импорт плейлистов

`muzik_maks` умеет формировать входной плейлист из:

- обычного текстового списка;
- iframe Яндекс Музыки;
- ссылки на плейлист Яндекс Музыки;
- сохраненного HTML-файла страницы плейлиста.

Яндекс Музыка используется только как источник списка треков и metadata. Проект не скачивает аудио из Яндекс Музыки, не выполняет автоматический вход в аккаунт и не обходит DRM, paywall, авторизацию, региональные ограничения или техническую защиту сайтов.

### Как импортировать плейлист Яндекс Музыки

1. Откройте плейлист в браузере.
2. Если используете iframe, скопируйте iframe-код и вставьте его в поле импорта.
3. Если нужен полный список треков:
   - откройте страницу плейлиста;
   - прокрутите список до конца;
   - сохраните страницу полностью как HTML;
   - загрузите HTML-файл в `muzik_maks`.
4. Проверьте найденный список и preview первых треков.
5. Запустите обработку.

После импорта создаются:

- `downloaded_music/imports/import_*/imported_playlist.txt`
- `downloaded_music/imports/import_*/imported_playlist.json`
- `downloaded_music/imports/import_*/imported_playlist.csv`
- `downloaded_music/imports/import_*/import_warnings.txt`

CLI:

```bash
py archive/scripts.py --import-playlist playlist.html
py archive/scripts.py --import-playlist playlist.html --run
py archive/scripts.py --import-playlist iframe.txt
py archive/scripts.py --import-playlist yandex_url.txt
```

Web API:

```text
POST /api/import-playlist
```

Endpoint принимает `multipart/form-data` (`file`, `content`, `run`, `delivery`) или JSON с `content`. Если `run=true`, после импорта создается обычная web-задача обработки.

Сохраненный HTML может содержать персональные данные аккаунта или профиля. Не передавайте такие файлы третьим лицам.
