# music-freedom

Локальное Windows-приложение для сборки музыкального архива по плейлисту.

Проект принимает список треков, ищет подходящие mp3-кандидаты, скачивает найденные файлы, проверяет качество совпадения и собирает итоговый архив только из принятых треков.

## Что умеет

- импорт обычного текстового списка треков;
- загрузка `.txt`, `.m3u`, `.m3u8`;
- импорт ссылки или iframe Яндекс Музыки;
- импорт сохранённой HTML-страницы плейлиста;
- предпросмотр и очистка плейлиста перед запуском;
- несколько режимов поиска: `fast`, `balanced`, `deep`;
- отчёты по качеству, ручной проверке и проблемным трекам;
- сборка итогового ZIP-архива.

Важно: Яндекс Музыка используется только как источник списка треков и метаданных. Аудио из Яндекс Музыки проект не скачивает.

## Быстрый старт

Самый простой запуск на Windows:

```bat
start.bat
```

Что делает `start.bat`:

1. создаёт `.env` из `.env.example`, если файла ещё нет;
2. ищет Python `3.10+`;
3. если Python не найден, скачивает portable Python `3.11.9` в `.runtime\python`;
4. создаёт `.venv`;
5. ставит зависимости из `requirements.txt`;
6. при необходимости собирает `frontend/dist`;
7. запускает приложение на `http://127.0.0.1:8000`.

Если `frontend/dist` уже существует, Node.js для обычного запуска не нужен.

## Как пользоваться

Можно передать проекту:

- список строк в формате `Artist - Title`;
- `.txt`, `.m3u`, `.m3u8`;
- ссылку на плейлист Яндекс Музыки;
- iframe Яндекс Музыки;
- сохранённую HTML-страницу плейлиста.

Рекомендуемый сценарий для большого плейлиста Яндекс Музыки:

1. открыть плейлист в браузере;
2. прокрутить страницу до конца;
3. сохранить страницу как HTML;
4. загрузить HTML в приложение;
5. проверить preview перед запуском.

## Режимы поиска

- `fast` — быстрый HTTP-first поиск без Selenium.
- `balanced` — основной режим по умолчанию, HTTP-поиск плюс кэш.
- `deep` — более медленный режим с разрешённым Selenium fallback.

Google Chrome нужен только для `deep` или если явно включён `ENABLE_SELENIUM_FALLBACK=true`.

## Что лежит в результатах

После прогона проект сохраняет диагностические файлы и отчёты. В зависимости от сценария среди них могут быть:

- `quality_report.json`
- `quality_report.csv`
- `manual_review.csv`
- `candidates.json`
- `downloaded.log`
- `run_info.txt`
- `accepted/`
- `rejected/`
- `result.zip`

В итоговый архив попадают только треки со статусом `OK`.

## Сборка релиза для другого ПК

Чтобы подготовить переносимую сборку:

```bat
prepare_release.bat
```

Скрипт:

1. ставит frontend-зависимости;
2. собирает `frontend/dist`;
3. копирует проект без локальных служебных файлов;
4. создаёт архив:

```text
release\music-freedom-release.zip
```

На другом Windows-ПК обычно достаточно распаковать архив и запустить `start.bat`.

## Ручной запуск для разработки

Требования:

- Python `3.10+`
- Node.js `18+`

Установка backend-зависимостей:

```bat
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
```

Установка frontend-зависимостей:

```bat
cd frontend
npm install
cd ..
```

Запуск backend:

```bat
.venv\Scripts\python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

Запуск frontend dev-сервера:

```bat
cd frontend
npm run dev
```

Frontend dev по умолчанию открывается через Vite, обычно на `http://localhost:5173`.

## Основные настройки

Пример настроек лежит в [`.env.example`](E:/Music_Download/muzik_maks/.env.example).

Чаще всего важны:

- `SEARCH_MODE`
- `ENABLE_SELENIUM_FALLBACK`
- `SEARCH_CACHE_ENABLED`
- `SEARCH_CACHE_TTL_DAYS`
- `HTTP_SEARCH_CONCURRENCY`
- `DOWNLOAD_CONCURRENCY`
- `VALIDATION_CONCURRENCY`
- `SELENIUM_MAX_WORKERS`
- `PROVIDER_TIMEOUT_SEC`
- `MAX_CONCURRENT_JOBS`

## Структура проекта

- [web/app.py](E:/Music_Download/muzik_maks/web/app.py) — FastAPI backend и web API.
- [frontend/src/App.tsx](E:/Music_Download/muzik_maks/frontend/src/App.tsx) — основной React-интерфейс.
- [downloader_core.py](E:/Music_Download/muzik_maks/downloader_core.py) — основной pipeline поиска и скачивания.
- [quality.py](E:/Music_Download/muzik_maks/quality.py) — quality layer, статусы и валидация.
- [search/](E:/Music_Download/muzik_maks/search) — HTTP/Selenium search layer и кэш.
- [playlist_importers/](E:/Music_Download/muzik_maks/playlist_importers) — импорт плейлистов из текста, URL, iframe и HTML.
- [prepare_release.bat](E:/Music_Download/muzik_maks/prepare_release.bat) — сборка переносимого релиза.
- [README_OVERVIEW.md](E:/Music_Download/muzik_maks/README_OVERVIEW.md) — расширенный технический обзор.
- [CODEX_PROJECT_CONTEXT.md](E:/Music_Download/muzik_maks/CODEX_PROJECT_CONTEXT.md) — рабочий контекст и заметки по проекту.

## Проверка

Frontend:

```bat
cd frontend
npm run build
```

Python-тесты:

```bat
python -m unittest
```

## Ограничения и аккуратное использование

- Проект не гарантирует нахождение всех треков.
- Публичные mp3-источники могут быть нестабильными.
- Некоторые треки могут уйти в `manual_review`.
- Сохранённые HTML-страницы плейлистов могут содержать личные данные профиля.
- Проект не предназначен для обхода DRM, paywall, авторизации, антибот-защиты или региональных ограничений.
