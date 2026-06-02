# music-freedom

Веб-приложение для сборки личного музыкального архива из плейлиста.

Проект принимает список треков, ищет подходящие mp3-источники, скачивает найденные файлы, проверяет качество совпадения и собирает итоговый архив. Интерфейс показывает прогресс обработки, а рядом с результатом сохраняются диагностические отчёты.

## Что умеет проект

- принимать обычный текстовый плейлист;
- импортировать плейлист из:
  - текста;
  - ссылки или iframe Яндекс Музыки;
  - сохранённого HTML-файла страницы плейлиста;
- искать несколько кандидатов для каждого трека;
- отфильтровывать неподходящие версии:
  - `clean`;
  - `censored`;
  - `radio edit`;
  - `remix`, `live`, `karaoke`, `instrumental` и другие альтернативные версии, если они не были явно запрошены;
- сохранять отчёты по качеству совпадений;
- собирать ZIP только из треков со статусом `OK`.

## Для чего это нужно

Проект полезен, когда у вас есть плейлист, но нет готового архива файлов. Он автоматизирует рутину:

1. получить список треков;
2. найти подходящие версии;
3. исключить неподходящие варианты;
4. скачать найденное;
5. собрать всё в один архив.

## Что проект не делает

Проект не предназначен для обхода ограничений платформ.

Он:

- не скачивает аудио из Яндекс Музыки;
- не обходит DRM;
- не обходит paywall;
- не выполняет автоматический логин;
- не обходит авторизацию, антибот-защиту или региональные ограничения.

Яндекс Музыка используется только как источник списка треков и metadata.

## Технологии

- Backend: `FastAPI`
- Frontend: `React + Vite`
- Поиск и скачивание: `selenium`, `chromedriver-autoinstaller`, HTTP fallback
- Проверка mp3 metadata: `mutagen`

## Требования

Перед запуском понадобятся:

- Python `3.10+`
- Node.js `18+`
- Google Chrome

ChromeDriver подтягивается автоматически под установленную версию Chrome, если не задан свой путь через переменные окружения.

## Быстрый старт

### 1. Клонирование

```bash
git clone <URL_ВАШЕГО_РЕПОЗИТОРИЯ>
cd music-freedom
```

### 2. Установка Python-зависимостей

Рекомендуется использовать виртуальное окружение.

Windows:

```bash
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

### 3. Подготовка `.env`

Скопируйте шаблон:

Windows:

```bash
copy .env.example .env
```

macOS / Linux:

```bash
cp .env.example .env
```

Минимально полезные переменные:

- `WEB_CORS_ORIGINS` — адрес фронтенда, например `http://localhost:5173`
- `WEB_SESSION_SECRET` — длинная случайная строка для cookie-сессии

Если вы просто запускаете проект локально для себя, часто достаточно оставить значения по умолчанию из `.env.example` и при необходимости только настроить `WEB_CORS_ORIGINS`.

### 4. Установка frontend-зависимостей

```bash
cd frontend
npm install
cd ..
```

## Запуск проекта

Нужно открыть два терминала: для API и для frontend.

### Терминал 1: backend

Из корня проекта:

```bash
py -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

Если у вас не Windows:

```bash
python3 -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

### Терминал 2: frontend

```bash
cd frontend
npm run dev
```

После этого откройте адрес, который покажет Vite. Обычно это:

```text
http://localhost:5173
```

## Продакшн-сборка frontend

```bash
cd frontend
npm run build
```

Собранные файлы появятся в `frontend/dist`.

## Как пользоваться

### Вариант 1: обычный текстовый плейлист

Вставьте список строк в формате:

```text
Artist - Title
Artist - Title
Artist - Title
```

Или загрузите `.txt`-файл с таким списком.

### Вариант 2: импорт из Яндекс Музыки

Поддерживается:

- ссылка на плейлист;
- iframe-код;
- сохранённый HTML-файл страницы плейлиста.

Лучший результат обычно даёт HTML-файл страницы после прокрутки плейлиста до конца.

Порядок действий:

1. Откройте плейлист в браузере.
2. Если нужен полный список треков, прокрутите страницу до конца.
3. Сохраните страницу как HTML.
4. Загрузите её через интерфейс.
5. Проверьте preview списка и запустите обработку.

## Как работает quality layer

Проект старается подобрать не просто любой mp3, а максимально подходящую версию трека.

По умолчанию приоритет получают:

- `original`
- `album version`
- `full version`
- `explicit`
- `uncensored`

По умолчанию отклоняются:

- `clean`
- `censored`
- `radio edit`
- `edited`
- `safe version`
- `short version`
- `preview`
- `remix`
- `live`
- `karaoke`
- `instrumental`
- `slowed`
- `nightcore`
- `sped up`
- `cover`

Исключение: если такая версия явно указана во входном треке, она может считаться допустимой.

## Что появляется после обработки

Для каждого прогона проект сохраняет диагностические артефакты, например:

- `quality_report.json`
- `quality_report.csv`
- `manual_review.csv`
- `candidates.json`
- `accepted/`
- `rejected/`
- `result.zip`

В итоговый архив попадают только файлы со статусом `OK`.

## Основные статусы

- `OK`
- `NOT_FOUND`
- `DOWNLOAD_FAILED`
- `LOW_CONFIDENCE_MATCH`
- `REJECTED_CLEAN_VERSION`
- `REJECTED_CENSORED_VERSION`
- `REJECTED_RADIO_VERSION`
- `REJECTED_REMIX_VERSION`
- `REJECTED_LIVE_VERSION`
- `VERSION_MISMATCH`
- `DURATION_MISMATCH`
- `SUSPICIOUS_FILE`
- `NEEDS_MANUAL_REVIEW`

## Переменные окружения

Основные настройки находятся в [.env.example](.env.example).

Наиболее важные:

- `WEB_CORS_ORIGINS`
- `WEB_SESSION_SECRET`
- `WEB_SESSION_HTTPS_ONLY`
- `WEB_REPORT_LIST_CAP`
- `WEB_MAX_UPLOAD_BYTES`
- `HEADLESS`
- `MAX_PLAYLIST_LINES`
- `MAX_CONCURRENT_JOBS`
- `DOWNLOAD_PARALLEL_WORKERS`
- `RESOLVE_PARALLEL_WORKERS`
- `LMUSIC_FALLBACK_ENABLED`
- `ZVYKI_ORIGIN`
- `ZVYKI_FALLBACK_ENABLED`
- `MP3_MIRROR_HOSTS`

## Структура проекта

Ключевые части:

- `web/app.py` — backend API
- `frontend/` — React-интерфейс
- `downloader_core.py` — поиск, выбор кандидатов, скачивание
- `quality.py` — quality layer, scoring, validation, отчёты
- `playlist_importers/` — импорт текста, iframe, URL и HTML
- `pack_downloads.py` — упаковка результата
- `tests/` — тесты

## Ограничения и важные замечания

- Проект не гарантирует нахождение 100% треков.
- Источники могут быть неполными или нестабильными.
- Некоторые треки будут уходить в `manual_review`.
- Сохранённый HTML плейлиста может содержать персональные данные профиля. Не публикуйте такие файлы.

## Лицензия и использование

Используйте проект ответственно и только в рамках законного доступа к источникам. Если вы публикуете репозиторий, отдельно проверьте, что не коммитите локальные данные, сохранённые HTML-страницы с персональной информацией и приватные настройки.
