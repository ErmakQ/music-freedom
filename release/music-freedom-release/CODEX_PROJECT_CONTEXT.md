# Codex project context: music-freedom

Этот файл нужен как рабочая память для будущих сессий Codex. Он не заменяет `README.md` и `README_OVERVIEW.md`: здесь собраны технический контекст, договоренности, текущая логика и краткий лог решений по проекту.

## Название

- Репозиторий/публичное имя: `music-freedom`.
- Название в интерфейсе: `Музыка Свободы`.
- Старое имя `muzik_maks` может оставаться во внутренних путях локальной папки, но в пользовательской документации его лучше не использовать.

## Главная идея

Проект принимает плейлист, ищет подходящие mp3-кандидаты, скачивает найденные файлы, проверяет качество совпадения и собирает итоговый архив.

Важное правило: проект должен стремиться скачать правильную версию трека, а не любой похожий файл. По умолчанию предпочтительны:

- original;
- full;
- album version;
- explicit;
- uncensored.

По умолчанию должны отбраковываться, если пользователь явно не указал такую версию во входном треке:

- clean;
- censored;
- edited;
- safe;
- radio edit / radio version;
- short / preview / snippet;
- remix;
- live;
- karaoke / instrumental;
- slowed / reverb / nightcore / sped up;
- cover;
- версии "без мата".

## Ограничения безопасности и законности

Не реализовывать:

- обход DRM;
- обход paywall;
- обход авторизации;
- обход региональных ограничений;
- обход антибот-защиты;
- автоматический логин;
- хранение логинов/паролей;
- скачивание аудио из Яндекс Музыки.

Яндекс Музыка используется только как источник списка треков и metadata.

## Основные файлы

- `downloader_core.py` — основной pipeline поиска, кандидатов, скачивания, accepted/rejected, отчеты.
- `download_service.py` — загрузка `DownloadConfig` из переменных окружения для web/backend.
- `quality.py` — quality layer: version flags, hard reject, scoring, validation, reports.
- `web/app.py` — FastAPI backend, jobs, import endpoint, reports/download endpoints.
- `frontend/src/App.tsx` — основной React UI.
- `frontend/src/App.css` — стили dashboard UI.
- `frontend/src/index.css` — глобальные стили.
- `playlist_importers/` — импорт plain text, txt/m3u, Yandex URL/iframe/saved HTML.
- `pack_downloads.py` — сборка zip только из `accepted/`, если папка есть.
- `archive/scripts.py` — CLI и retry-сценарии.
- `tests/` — unit-тесты.
- `README.md` — пользовательская установка/запуск.
- `README_OVERVIEW.md` — обзор архитектуры.
- `.env.example` — безопасный пример настроек без секретов.

## Текущие источники поиска

Основной источник:

- Hitmo/Hitmotop через HTTP и Selenium.

Fallback:

- LMusic (`LMUSIC_ORIGIN`, `LMUSIC_FALLBACK_ENABLED`).
- Zvyki (`ZVYKI_ORIGIN`, `ZVYKI_FALLBACK_ENABLED`).

Zvyki подключен как обычный публичный HTTP fallback. Если сайт отвечает `451`, недоступен или не дает уверенного совпадения, источник пропускается и не ломает прогон. Никаких обходов ограничений не добавлено.

## Import pipeline

Поддерживаемые входы:

- обычный текст;
- `.txt`;
- `.m3u`;
- `.m3u8`;
- ссылка Яндекс Музыки;
- iframe Яндекс Музыки;
- сохраненный HTML страницы плейлиста.

Endpoint:

- `POST /api/import-playlist`

Результат импорта:

- `ImportedPlaylist`;
- `imported_playlist.txt`;
- `imported_playlist.json`;
- `imported_playlist.csv`;
- `import_warnings.txt`.

Для ссылки Яндекс Музыки сейчас используется публичная metadata-логика. На тестовом плейлисте пользователя ссылка возвращала 630 треков после исправления пагинации.

## Quality pipeline

Ключевые функции в `quality.py`:

- `normalize_track_text`
- `detect_requested_version_flags`
- `detect_candidate_version_flags`
- `should_hard_reject_candidate`
- `generate_search_queries`
- `score_candidate`
- `rank_candidates`
- `select_best_candidate`
- `validate_downloaded_file`
- report writers

Статусы:

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

В итоговый zip должны попадать только `OK`-файлы. Не складывать rejected/suspicious/manual review в итоговый архив.

## Reports

Для прогона должны сохраняться:

- `quality_report.json`
- `quality_report.csv`
- `manual_review.csv`
- `candidates.json`
- `downloaded.log`
- `run_info.txt`
- `accepted/`
- `rejected/`
- `result.zip`

`candidates.json` должен хранить всех кандидатов, включая rejected.

## Frontend UI

UI — dark music dashboard с названием `Музыка Свободы`.

Основные элементы:

- sidebar;
- hero;
- import card;
- quality card;
- playlist preview;
- archive settings;
- progress card;
- results card;
- отдельное окно настроек;
- отдельное окно помощи;
- отдельное окно истории архивов.

Важные UX-решения:

- Загрузка `.txt` должна всегда оставаться рабочей.
- Вкладка `Ссылка / iframe` вызывает `/api/import-playlist` и заполняет preview.
- Preview показывает весь текущий список с вертикальным scroll.
- В preview есть меню трека: послушать через поиск Яндекс Музыки, копировать, редактировать, убрать из списка.
- Есть действия: вернуть удаленный трек, убрать дубли.
- `Настройки` открывает отдельное окно, где можно менять quality toggles и чистить текущий preview-список.
- `Помощь` открывает отдельное окно с объяснением TXT, ссылки, iframe, HTML-файла и ограничений.
- `История архивов` открывает отдельное окно с последним запуском текущей сессии. Отдельная база истории пока не добавлялась.
- `Шаблоны`, личный кабинет, тарифы и статистику пользователя не добавлять без отдельного ТЗ.

## GitHub safety

Не коммитить:

- `.env`
- `.env.*`, кроме `.env.example`
- `.idea/`
- `Lib/`
- `site-packages/`
- `__pycache__/`
- `downloaded_music/`
- `backups/`
- `archive/`
- `node_modules/`
- `dist/`
- `build/`

См. `.gitignore`.

Telegram-бот удален из README как неиспользуемый сценарий.

## Проверочные команды

Frontend:

```bash
cd frontend
npm run build
```

Python tests:

```bash
python -m unittest tests.test_playlist_importers tests.test_quality
```

Python syntax:

```bash
python -m py_compile downloader_core.py download_service.py quality.py
```

Последняя проверка после изменений:

- `npm run build` — успешно.
- `python -m unittest tests.test_playlist_importers tests.test_quality` — успешно, 27 тестов.
- `python -m py_compile downloader_core.py download_service.py quality.py` — успешно.

В этой среде `git` может быть не в PATH. Если нужен `git status`, сначала проверить доступность `git`.

## Краткий лог нашей работы

1. Обновили цель проекта: скачивать не просто найденный mp3, а подходящую версию трека.
2. Добавили/расширили quality layer: version flags, hard reject, scoring, reports, validation, accepted/rejected.
3. Добавили импорт плейлистов из Яндекс Музыки как источника списка и metadata, без скачивания аудио.
4. Исправили импорт ссылки Яндекс Музыки: плейлист пользователя стал распознаваться полностью, 630 треков вместо неполного результата.
5. Переделали frontend в dark music dashboard `Музыка Свободы`.
6. Вернули и сохранили загрузку `.txt`.
7. Подключили `/api/import-playlist` во frontend для вкладки `Ссылка / iframe`.
8. Улучшили preview: scroll, счетчик, удаление, редактирование, копирование, поиск для прослушивания, удаление дублей.
9. Убрали бессмысленные элементы sidebar: `Шаблоны`, тарифы, лишние переходы.
10. Добавили отдельные окна `Настройки`, `Помощь`, `История архивов`.
11. Подготовили проект к GitHub: `.gitignore`, `.env.example`, README.
12. Убрали Telegram-бота из README и заменили публичное имя на `music-freedom`.
13. Добавили fallback-источник `zvyki.com` без обходов ограничений.
14. Обновили `README_OVERVIEW.md`.

## Что важно помнить будущему Codex

- Не переписывать проект с нуля.
- Не ломать старый web-сценарий.
- Не ломать CLI без необходимости.
- Не удалять локальные рабочие данные пользователя.
- Не трогать `.env` и runtime-папки без явного подтверждения.
- В frontend внимательно относиться к кодировке: в некоторых старых строках файла встречался mojibake, но новые пользовательские строки лучше добавлять нормальным UTF-8.
- Если добавляется новый источник, он должен быть fallback, безопасно отключаемый через env и не должен обходить ограничения сайтов.
- Если меняется логика zip, итоговый архив должен включать только accepted/OK.

