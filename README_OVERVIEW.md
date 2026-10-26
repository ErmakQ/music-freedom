# Музыкальная полка: обзор проекта

## Что это

`music-shelf` — сервис для обработки музыкальных плейлистов. В пользовательском интерфейсе проект называется **Музыкальная полка**.

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

- Web-интерфейс с dashboard-дизайном `Музыкальная полка`.
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

## Архитектура

Ключевые части проекта:

- `downloader_core.py` — поиск, кандидаты, скачивание, quality-валидация.
- `quality.py` — статусы качества, version flags, scoring, reports, validation.
- `playlist_importers/` — импорт plain text, `.txt`, iframe, URL и HTML Яндекс Музыки.
- `web/app.py` — backend API, фоновые задачи, импорт, отчеты, скачивание результата.
- `frontend/src/App.tsx` — основной frontend dashboard.
- `frontend/src/App.css` и `frontend/src/index.css` — визуальный слой.
- `pack_downloads.py` — упаковка результата.
- `desktop.py` — EXE-лаунчер с системным треем.
- `build_exe.py` — сборка Windows EXE-пакета.
- `installer.iss` — сценарий установщика Inno Setup.
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

Текущий frontend — dark music dashboard под названием **Музыкальная полка**.

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

## Windows EXE

Пользовательский релиз собирается в EXE-пакет без `.bat`-запуска. Установщик Inno Setup предлагает папку установки, создаёт ярлык и запускает `MusicShelf.exe`.

EXE поднимает локальный FastAPI-сервер на свободном `127.0.0.1`-порту и открывает браузер автоматически. Работу можно завершить через значок в системном трее.

Ресурсы приложения находятся в папке установки, а кэш, логи, импортированные плейлисты и диагностика — в `%LOCALAPPDATA%\MusicShelf`. Это не требует прав администратора и не затрагивает установленные файлы при работе приложения.

Готовые ZIP-архивы не дублируются в служебной папке: браузер сохраняет их в выбранную пользователем папку загрузок. Google Chrome не является обязательным для fast/balanced режима; он нужен только для глубокого поиска с Selenium fallback.

Сборщик `build_exe.py` создаёт `release\music-shelf-windows.zip` и папку `dist\MusicShelf`. После установки Inno Setup команда `iscc installer.iss` создаёт `release\music-shelf-setup.exe`.

## Search layer

Поиск кандидатов вынесен в отдельный пакет `search/`, без переписывания остального pipeline:

- `search/models.py` — `TrackQuery`, `SearchCandidate`, `ProviderResult`, `SearchDecision`;
- `search/base.py` — `ProviderBase` с async API `search(track: TrackQuery) -> ProviderResult`;
- `search/providers.py` — provider-адаптеры `HitmoHTTPProvider`, `LMusicHTTPProvider`, `ZvykiHTTPProvider`, `SeleniumFallbackProvider`;
- `search/orchestrator.py` — `SearchOrchestrator`;
- `search/cache.py` — SQLite search cache.

Текущий принцип:

- основной поиск идет через HTTP providers;
- HTTP providers запускаются async через `asyncio`/`httpx`;
- кандидаты дедуплицируются перед quality scoring;
- quality scoring остается старый (`quality.py`);
- `downloader_core.py` получает тот же список `quality.TrackCandidate` и дальше сохраняет старый pipeline: validation, reports, accepted-only ZIP, web flow;
- Selenium fallback выключен по умолчанию и используется только при `ENABLE_SELENIUM_FALLBACK=true`, `SEARCH_MODE=deep` или UI-режиме `Глубокий поиск`;
- если HTTP дал хороший/high-confidence результат, Selenium не запускается;
- `candidates.json` теперь содержит `decision_reason`, provider summary и `from_cache`.

Новые env-настройки:

- `ENABLE_SELENIUM_FALLBACK=false`
- `SEARCH_CACHE_ENABLED=true`
- `SEARCH_CACHE_TTL_DAYS=14`
- `HTTP_SEARCH_CONCURRENCY=30`
- `DOWNLOAD_CONCURRENCY=5`
- `VALIDATION_CONCURRENCY=4`
- `SELENIUM_MAX_WORKERS=1`
- `PROVIDER_TIMEOUT_SEC=8`
- `SEARCH_MODE=balanced`

Concurrency разделена концептуально:

- HTTP search concurrency — `HTTP_SEARCH_CONCURRENCY`;
- download concurrency — `DOWNLOAD_CONCURRENCY`;
- selenium concurrency — `SELENIUM_MAX_WORKERS`;
- validation concurrency — `VALIDATION_CONCURRENCY`; validation выполняется внутри download path, но ограничивается отдельным semaphore.

UI режимы поиска:

- `Быстро` — HTTP без Chrome;
- `Баланс` — HTTP + кэш;
- `Глубокий поиск` — медленнее, разрешает Selenium fallback и может требовать установленный Google Chrome.

Selenium-контракт:

- Selenium/ChromeDriver не удаляется из проекта;
- в fast/balanced Selenium не используется и отсутствие Chrome не должно ломать обычный запуск;
- в deep, `SEARCH_MODE=deep` или при `ENABLE_SELENIUM_FALLBACK=true` ChromeDriver поднимается только если HTTP providers не дали high-confidence candidate или достаточное количество candidates;
- `SELENIUM_MAX_WORKERS` отдельный от `DOWNLOAD_CONCURRENCY`.

## Диагностика долгих запусков

После проверки на другом Windows-ПК с плейлистом на 630 треков найден важный UX/diagnostic issue:

- при `resolve_parallel_workers > 1` первая фаза поиска выполнялась параллельно, но job progress обновлялся только после завершения всех потоков phase1;
- из-за этого UI мог 20+ минут показывать `0%`, `Текущий трек: —` и фазу `Поиск`, хотя Selenium-потоки могли работать;
- поле `Найдено` во время phase1 было misleading: пока `not_found = 0`, frontend показывал `total - not_found`, то есть все 630 как найденные.

Исправления:

- `_phase1_resolve_worker` теперь принимает `on_progress` и вызывает его после каждого обработанного трека;
- для параллельного поиска в progress передается количество уже обработанных phase1-задач (`len(agg)`), а не исходный номер трека;
- frontend во время `phase1` показывает `Проверено X / total`;
- `Найдено` во время `phase1` не считается как `total - not_found`, чтобы не создавать ложное ощущение, что все треки уже найдены.

Дополнительная оптимизация после зависания при ручном выборе `Параллельные задачи = 10`:

- ручной параметр `parallel_tasks` больше не меняет `resolve_parallel_workers`; теперь он управляет `parallel_download_workers`;
- Selenium-поиск выключен в fast/balanced и разрешается только в deep / `ENABLE_SELENIUM_FALLBACK=true`;
- fast/balanced используют HTTP-first search layer и не требуют Chrome;
- deep ограничивает Selenium через `SELENIUM_MAX_WORKERS`;
- в пресеты добавлен `wait_sec` для Selenium fallback;
- `create_driver` выставляет `page_load_timeout` и `script_timeout` по `wait_sec`;
- если HTTP providers уже дали high-confidence candidate или достаточно candidates, Selenium/fallback-поиск для этого трека не запускается.

Для анализа зависаний на чужом ПК просить:

- файл `%LOCALAPPDATA%\MusicShelf\logs\music-shelf.log`;
- JSON `GET /api/jobs/{job_id}`; job_id виден в консольных строках polling, например `/api/jobs/eca63a0a6e27476997f703768c39b60b`;
- содержимое `downloaded_music\web_runs`, если папка появилась после failed/partial_success. Во время running-задачи этой папки может не быть, потому что текущий work_root временный.

## Документация

`README.md` переписан как короткая инструкция для обычного пользователя:

- что делает приложение;
- установка через `music-shelf-setup.exe`;
- переносимая EXE-сборка `music-shelf-windows.zip`;
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

## 2026-06-02 search/quality notes

- Playlist duration is parsed from trailing `[m:ss]` / `(m:ss)` and kept as `expected_duration_sec`; it is not included in the HTTP search query.
- Search uses normalized artist/title text, while duration is used later by downloaded-file validation.
- `POST_DOWNLOAD_OK_SCORE_THRESHOLD=0.80` is intentionally lower than `OK_SCORE_THRESHOLD=0.85`: after a file is downloaded and passes version/duration/file checks, near-exact matches such as `0.83-0.85` should be accepted instead of moved to `rejected/`.
- UI clears stale failed error messages when a job finishes as `completed` or `partial_success`, and backend clears `current_track` on final job states.

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
- запускаться из web;
- запускаться на Windows через `MusicShelf.exe` без установленного Python;
- собираться в EXE-пакет и установщик Inno Setup.

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

`music-shelf` — это не просто скачиватель найденных mp3. Это pipeline для сборки личного архива, который пытается выбрать подходящую версию каждого трека, отбраковать clean/radio/censored и спорные варианты, сохранить диагностику и положить в итоговый ZIP только принятые `OK`-файлы.
