# Muzik Maks / Музыка Свободы: обзор проекта

## Что это

`muzik_maks` — сервис для обработки музыкальных плейлистов. В пользовательском интерфейсе проект называется **Музыка Свободы**.

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
- Фоновая обработка через `job_id`.
- Polling статуса и live progress.
- Параллельное скачивание.
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
- `tests/` — unit-тесты quality-слоя, импорта и упаковки.

## Pipeline

1. Пользователь вводит список треков или импортирует плейлист.
2. Импортер формирует внутренний `ImportedPlaylist`.
3. Список превращается в обычный текстовый playlist format.
4. Для каждого трека генерируются поисковые запросы.
5. Провайдеры возвращают кандидатов.
6. Кандидаты нормализуются, получают flags и score.
7. Clean/radio/censored/remix/live и похожие версии hard-reject-ятся, если не были запрошены.
8. Скачивается лучший допустимый кандидат.
9. После скачивания файл проходит validation.
10. OK-файлы попадают в `accepted/`.
11. Rejected/suspicious/manual-review файлы не попадают в итоговый ZIP.
12. Создаются отчеты.

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
- help-блок с подсказками по TXT, ссылке, iframe и HTML.

Сейчас `История`, `Шаблоны`, `Поддержка` и `Управление тарифом` помечены как будущие функции и не притворяются рабочими кнопками.

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
- запускаться из web и CLI.

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
- `python -m unittest discover -s tests` — успешно, 24 теста.
- `POST /api/import-playlist` с `.txt` файлом — успешно.

## Ключевая мысль

`muzik_maks` — это не просто скачиватель найденных mp3. Это pipeline для сборки личного архива, который пытается выбрать подходящую версию каждого трека, отбраковать clean/radio/censored и спорные варианты, сохранить диагностику и положить в итоговый ZIP только принятые `OK`-файлы.
