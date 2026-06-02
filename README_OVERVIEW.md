# music-freedom: обзор проекта

## Что это

`music-freedom` — сервис для обработки музыкальных плейлистов. В пользовательском интерфейсе проект называется **Музыка Свободы**.

Главная идея: пользователь дает список треков, а система ищет подходящие mp3-источники, скачивает найденные файлы, проверяет качество совпадения, отбраковывает неподходящие версии и собирает итоговый архив.

Проект стремится находить не “любой файл с похожим названием”, а правильную версию трека: original, full, album, explicit или uncensored, если пользователь не попросил другую версию явно.

## Что важно

По умолчанию в итоговый архив не должны попадать:

- clean / censored / edited / safe version;
- radio edit / radio version;
- short / preview / snippet;
- remix / live / karaoke / instrumental / cover;
- slowed / reverb / nightcore / sped up;
- версии “без мата”, если пользователь не указал такую версию во входном плейлисте.

Если пользователь явно указал, например, `Radio Edit` или `Remix`, такая версия может считаться допустимой.

## Что проект не делает

Проект не реализует обход DRM, paywall, авторизации, региональных ограничений, антибот-защиты или технических ограничений сайтов.

Яндекс Музыка используется только как источник списка треков и metadata. Аудио из Яндекс Музыки проект не скачивает.

## Основные возможности

- Web-интерфейс с dashboard-дизайном `Музыка Свободы`.
- Импорт плейлиста из обычного текста.
- Загрузка готового `.txt` файла.
- Импорт ссылки или iframe Яндекс Музыки.
- Импорт сохраненного HTML-файла страницы плейлиста.
- Preview импортированного плейлиста.
- Редактирование preview перед запуском: удалить трек, убрать дубли, скопировать строку, открыть поиск для прослушивания.
- Отдельные окна `Настройки`, `Помощь` и `История архивов`, чтобы интерфейс не ощущался одной длинной страницей.
- Фоновая обработка через `job_id`.
- Polling статуса и live progress.
- Параллельное скачивание.
- Fallback-источники поиска: Hitmo/Hitmotop, LMusic и Zvyki.
- Quality-фильтр версий треков.
- Post-download validation.
- Отчеты качества и ручной проверки.
- Итоговый ZIP только из accepted/OK-файлов.
- CLI-режим и retry по проблемным статусам.

## Архитектура

Ключевые части проекта:

- `downloader_core.py` — поиск, кандидаты, скачивание, quality-валидация.
- `quality.py` — статусы качества, version flags, scoring, reports, validation.
- `playlist_importers/` — импорт plain text, `.txt`, iframe, URL и HTML Яндекс Музыки.
- `web/app.py` — backend API, фоновые задачи, импорт, отчеты, скачивание результата.
- `frontend/src/App.tsx` — основной frontend dashboard.
- `frontend/src/App.css` и `frontend/src/index.css` — визуальный слой.
- `archive/scripts.py` — CLI.
- `pack_downloads.py` — упаковка результата.
- `start.bat` — упрощенный запуск проекта на Windows одной командой.
- `prepare_release.bat` — сборка переносимого ZIP-архива для другого Windows-ПК.
- `tests/` — unit-тесты quality-слоя, импорта и упаковки.

## Pipeline

1. Пользователь вводит список треков или импортирует плейлист.
2. Импортер формирует внутренний `ImportedPlaylist`.
3. Список превращается в обычный текстовый playlist format.
4. Для каждого трека генерируются поисковые запросы.
5. Провайдеры возвращают кандидатов.
6. Используются основные и fallback-источники: Hitmo/Hitmotop, LMusic, Zvyki.
7. Кандидаты нормализуются, получают flags и score.
8. Clean/radio/censored/remix/live и похожие версии hard-reject-ятся, если не были запрошены.
9. Скачивается лучший допустимый кандидат.
10. После скачивания файл проходит validation.
11. OK-файлы попадают в `accepted/`.
12. Rejected/suspicious/manual-review файлы не попадают в итоговый ZIP.
13. Создаются отчеты.

## Статусы качества

Основные статусы:

- `OK`
- `NOT_FOUND`
- `DOWNLOAD_FAILED`
- `LOW_CONFIDENCE_MATCH`
- `REJECTED_CLEAN_VERSION`
- `REJECTED_CENSORED_VERSION`
- `REJECTED_RADIO_VERSION`
- `REJECTED_EDITED_VERSION`
- `REJECTED_SHORT_VERSION`
- `REJECTED_REMIX_VERSION`
- `REJECTED_LIVE_VERSION`
- `REJECTED_KARAOKE_VERSION`
- `VERSION_MISMATCH`
- `DURATION_MISMATCH`
- `SUSPICIOUS_FILE`
- `NEEDS_MANUAL_REVIEW`

В архив попадают только треки со статусом `OK`.

## Диагностические файлы прогона

Для прогонов создаются:

- `quality_report.json`
- `quality_report.csv`
- `manual_review.csv`
- `candidates.json`
- `downloaded.log`
- `run_info.txt`
- `accepted/`
- `rejected/`
- `result.zip`

`candidates.json` хранит найденных кандидатов, включая отклоненных. `manual_review.csv` нужен для треков, которые не удалось уверенно принять.

## Импорт плейлистов

Поддерживаются:

- обычный текст;
- `.txt`, `.m3u`, `.m3u8`;
- iframe Яндекс Музыки;
- ссылка Яндекс Музыки;
- сохраненный HTML страницы плейлиста.

Для iframe/ссылки система может распознать playlist metadata, но полный список треков не всегда доступен. Для полного импорта лучше открыть плейлист, прокрутить список до конца, сохранить страницу как HTML и загрузить файл.

Результаты импорта сохраняются в:

- `downloaded_music/imports/import_*/imported_playlist.txt`
- `downloaded_music/imports/import_*/imported_playlist.json`
- `downloaded_music/imports/import_*/imported_playlist.csv`
- `downloaded_music/imports/import_*/import_warnings.txt`

## Web UI

Текущий frontend — dark music dashboard под названием **Музыка Свободы**.

В интерфейсе есть:

- sidebar;
- hero-блок;
- Import card;
- Quality card;
- Playlist preview;
- Archive settings;
- Progress card;
- Results card;
- отдельное окно помощи с подсказками по TXT, ссылке, iframe и HTML;
- отдельное окно настроек с правилами качества и очисткой текущего preview-списка;
- отдельное окно истории архивов с последним запуском, статусом, счетчиками и отчетами.

`Шаблоны`, личный кабинет, тарифы и статистика пользователя пока не реализуются. `Поддержка` остается неактивной заглушкой, чтобы не обещать несуществующий сценарий.

## Запуск и упаковка

Добавлен упрощенный Windows-сценарий для обычного пользователя:

- `start.bat` запускает проект одной командой.
- Если `.env` отсутствует, `start.bat` копирует `.env.example` в `.env`.
- Если Python `3.10+` уже установлен, используется системный Python.
- Если Python не найден, `start.bat` скачивает официальный Python `3.11.9` с `python.org` и ставит его локально в `.runtime\python` без прав администратора.
- Python-зависимости ставятся в `.venv`.
- Приложение запускается через `uvicorn web.app:app --host 127.0.0.1 --port 8000`.
- Браузер открывается на `http://127.0.0.1:8000`.
- Google Chrome остается обязательным внешним требованием, потому что Selenium/ChromeDriver используют установленный Chrome.

Frontend уже поддерживает production-сценарий: после `npm run build` папка `frontend/dist` отдается самим FastAPI через `StaticFiles`. Поэтому для обычного запуска не обязательно держать два терминала и Vite.

Добавлен `prepare_release.bat`:

- запускается на машине разработчика или сборщика, где есть Node.js;
- выполняет `npm install` и `npm run build` в `frontend/`;
- создает архив `release\music-freedom-release.zip`;
- исключает из архива `.git`, `.idea`, `.venv`, `.runtime`, `node_modules`, `downloaded_music`, `backups`, `archive`, `release`, `.env`, `.pyc`, `.pyo`, `.log`;
- предполагаемый сценарий: собрать ZIP, перенести на другой Windows-ПК, распаковать, запустить `start.bat`.

`.runtime/` добавлен в `.gitignore`, чтобы локально скачанный Python не попадал в репозиторий.

## Документация

`README.md` переписан как короткая инструкция для обычного пользователя:

- что делает приложение;
- запуск для чайников через `start.bat`;
- перенос на другой ПК через `prepare_release.bat`;
- базовое использование;
- важные ограничения;
- ручной запуск для разработки.

Подробные технические детали, архитектура, контекст решений и рабочая память проекта должны оставаться в `README_OVERVIEW.md`.

## Источники поиска

Проект использует несколько публичных источников-кандидатов и не пытается обходить их технические ограничения:

- `Hitmo/Hitmotop` — основной поиск и зеркала mp3-ссылок.
- `LMusic` — fallback-поиск, если основной источник не дал подходящий результат.
- `Zvyki` — дополнительный HTTP fallback. Если сайт недоступен, отвечает `451` или не дает уверенного совпадения, источник тихо пропускается и не ломает прогон.

Переменные окружения для fallback-источников:

- `LMUSIC_ORIGIN`
- `LMUSIC_FALLBACK_ENABLED`
- `ZVYKI_ORIGIN`
- `ZVYKI_FALLBACK_ENABLED`

## API

Основные endpoint-ы:

- `GET /api/health`
- `GET /api/limits`
- `POST /api/import-playlist`
- `POST /api/jobs`
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/download`
- `GET /api/jobs/{job_id}/reports/{report_name}`
- `GET /api/imports/{import_id}/files/{file_name}`

`POST /api/import-playlist` принимает textarea content, `.txt`/HTML-файл, iframe или URL. При `run=true` endpoint сразу запускает обычный pipeline обработки.

## CLI

CLI находится в `archive/scripts.py`.

Поддерживаются:

- обычный запуск по плейлисту;
- `--import-playlist`;
- `--import-playlist ... --run`;
- `--retry-failed`;
- `--retry-not-found`;
- `--retry-suspicious`;
- `--retry-manual-review`;
- `--retry-rejected`.

## Текущий статус

Проект уже умеет:

- принимать и импортировать плейлисты;
- запускать фоновые web-задачи;
- показывать прогресс;
- собирать кандидатов;
- оценивать совпадения;
- отклонять неподходящие версии;
- валидировать скачанные файлы;
- собирать ZIP только из `accepted/`;
- создавать расширенные отчеты;
- запускаться из web и CLI;
- запускаться на Windows через `start.bat` с автоматической подготовкой Python/.venv;
- собираться в переносимый ZIP через `prepare_release.bat`.

## Ограничения

- Проект не гарантирует нахождение 100% треков.
- Источники могут быть неполными или нестабильными.
- Некоторые ссылки могут временно не работать.
- Редкие треки могут требовать ручной проверки.
- DOM fallback для HTML Яндекс Музыки может вернуть неполный список, если страница сохранена до полной прокрутки.
- `manual_review` означает, что файл не принят автоматически и требует проверки.

## Проверка

Последняя проверка после текущих изменений:

- `npm run build` — успешно.
- `python -m unittest tests.test_playlist_importers tests.test_quality` — успешно, 27 тестов.
- `python -m py_compile downloader_core.py download_service.py quality.py` — успешно.
- `POST /api/import-playlist` с `.txt` файлом и ссылкой Яндекс Музыки — успешно.

## Ключевая мысль

`music-freedom` — это не просто скачиватель найденных mp3. Это pipeline для сборки личного архива, который пытается выбрать подходящую версию каждого трека, отбраковать clean/radio/censored и спорные варианты, сохранить диагностику и положить в итоговый ZIP только принятые `OK`-файлы.
