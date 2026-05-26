import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, DragEvent, SetStateAction } from "react";
import "./App.css";

const PRODUCT_NAME = "Музыка Свободы";
const PRODUCT_SUBTITLE = "Оригинальные версии треков в одном архиве";
const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
const PREFS_KEY = "playlistWeb.v2";

type Limits = {
  max_playlist_lines: number | null;
  max_concurrent_jobs: number;
  session_active?: boolean;
};

type QualityCounts = {
  downloaded_ok?: number;
  not_found?: number;
  download_failed?: number;
  low_confidence?: number;
  rejected_clean_or_censored?: number;
  rejected_radio?: number;
  version_mismatch?: number;
  duration_mismatch?: number;
  suspicious?: number;
  manual_review?: number;
};

type PlaylistReport = {
  counts: { ok: number; failed: number; skipped: number };
  quality_counts?: QualityCounts;
  failed: string[];
  skipped: string[];
  failed_truncated?: boolean;
  skipped_truncated?: boolean;
};

type ErrBody = {
  detail?: string;
  saved_run?: string;
  counts?: { ok: number; failed: number; skipped: number };
  quality_counts?: QualityCounts;
  failed?: string[];
  skipped?: string[];
  failed_truncated?: boolean;
  skipped_truncated?: boolean;
};

type JobInfo = {
  job_id: string;
  status: "queued" | "running" | "completed" | "partial_success" | "failed";
  phase: "queued" | "phase1" | "phase2" | "done";
  detail?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  file_name?: string | null;
  delivery?: "zip" | "tracks";
  total_lines?: number;
  current_index?: number;
  current_track?: string | null;
  counts?: { ok: number; failed: number; skipped: number };
  quality_counts?: QualityCounts;
  report?: PlaylistReport | null;
  available_reports?: string[];
  download_ready?: boolean;
  download_name?: string | null;
  saved_run?: string | null;
};

type ImportPreviewTrack = {
  artist?: string | null;
  title: string;
  duration_ms?: number | null;
  line: string;
};

type ImportResponse = {
  ok: boolean;
  source_service: string;
  title?: string | null;
  source_url?: string | null;
  owner?: string | null;
  kind?: string | null;
  profile_name?: string | null;
  profile_url?: string | null;
  track_count: number;
  warnings: string[];
  import_id?: string;
  files?: { txt?: string; json?: string; csv?: string; warnings?: string };
  preview?: ImportPreviewTrack[];
  job?: JobInfo;
};

type ImportTab = "text" | "link" | "file";
type QualitySettings = {
  preferOriginal: boolean;
  preferExplicit: boolean;
  preferUncensored: boolean;
  rejectClean: boolean;
  rejectRadio: boolean;
  rejectCensored: boolean;
  rejectRemix: boolean;
  rejectLive: boolean;
};
type ParallelTasks = "auto" | 3 | 5 | 10;
type NoticeTone = "status" | "warning";
type ImportSourceKind = "text" | "link" | "iframe" | "html" | "file" | "unknown";
type ImportAnalysis = {
  kind: ImportSourceKind;
  label: string;
  sourceUrl?: string | null;
  messages: string[];
};

const PREVIEW_SOFT_LIMIT = 120;

const fetchApi = (input: string, init?: RequestInit) =>
  fetch(input, { ...init, credentials: "include" });

function readUiPrefs(): { delivery: "zip" | "tracks"; parallelTasks: ParallelTasks } | null {
  try {
    const s = localStorage.getItem(PREFS_KEY);
    if (!s) return null;
    const o = JSON.parse(s) as { delivery?: string; parallelTasks?: string | number };
    const delivery = o.delivery === "tracks" ? "tracks" : "zip";
    const parallelTasks =
      o.parallelTasks === 3 || o.parallelTasks === 5 || o.parallelTasks === 10 || o.parallelTasks === "auto"
        ? o.parallelTasks
        : "auto";
    return { delivery, parallelTasks };
  } catch {
    return null;
  }
}

function writeUiPrefs(delivery: "zip" | "tracks", parallelTasks: ParallelTasks) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({ delivery, parallelTasks }));
  } catch {
    return;
  }
}

function decodePlaylistReportHeader(b64: string): PlaylistReport | null {
  const s = b64.trim();
  if (!s) return null;
  try {
    const norm = s.replace(/-/g, "+").replace(/_/g, "/");
    const pad = norm.length % 4 === 0 ? "" : "=".repeat(4 - (norm.length % 4));
    const bin = atob(norm + pad);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const text = new TextDecoder("utf-8").decode(bytes);
    const j = JSON.parse(text) as PlaylistReport;
    if (!j.counts || !Array.isArray(j.failed) || !Array.isArray(j.skipped)) return null;
    return j;
  } catch {
    return null;
  }
}

function errBodyToReport(j: ErrBody): PlaylistReport | null {
  if (!j.counts) return null;
  return {
    counts: j.counts,
    quality_counts: j.quality_counts,
    failed: Array.isArray(j.failed) ? j.failed : [],
    skipped: Array.isArray(j.skipped) ? j.skipped : [],
    failed_truncated: j.failed_truncated,
    skipped_truncated: j.skipped_truncated,
  };
}

function progressPercent(job: JobInfo | null) {
  if (!job) return 0;
  const total = job.total_lines ?? 0;
  if (!total) return 0;
  if (job.status === "completed" || job.status === "partial_success" || job.status === "failed") {
    return 100;
  }
  const current = Math.max(0, Math.min(job.current_index ?? 0, total));
  return Math.round((current / total) * 100);
}

function jobPhaseLabel(phase?: JobInfo["phase"]) {
  switch (phase) {
    case "phase1":
      return "Поиск и проверка треков";
    case "phase2":
      return "Скачивание и валидация";
    case "done":
      return "Готово";
    case "queued":
      return "В очереди";
    default:
      return "Ожидание запуска";
  }
}

function jobStatusLabel(status?: JobInfo["status"]) {
  switch (status) {
    case "queued":
      return "В очереди";
    case "running":
      return "В работе";
    case "completed":
      return "Успешно";
    case "partial_success":
      return "Частично готово";
    case "failed":
      return "Ошибка";
    default:
      return "Не запущено";
  }
}

function formatMs(ms?: number | null) {
  if (!ms) return "";
  const total = Math.round(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function formatDurationClock(valueMs?: number | null) {
  if (!valueMs || valueMs < 0) return "—";
  const totalSeconds = Math.round(valueMs / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function splitTrackLine(line: string) {
  const normalized = line.trim();
  if (!normalized) return { artist: "", title: "" };
  if (normalized.includes(" - ")) {
    const [artist, ...rest] = normalized.split(" - ");
    return { artist: artist.trim(), title: rest.join(" - ").trim() };
  }
  if (normalized.includes(" — ")) {
    const [artist, ...rest] = normalized.split(" — ");
    return { artist: artist.trim(), title: rest.join(" — ").trim() };
  }
  return { artist: "", title: normalized };
}

function extractIframeSrc(text: string) {
  const match = text.match(/<iframe[^>]+src=["']([^"']+)["']/i);
  return match?.[1] ?? null;
}

function detectImportAnalysis(tab: ImportTab, content: string, file: File | null): ImportAnalysis {
  if (file) {
    const lower = file.name.toLowerCase();
    if (lower.endsWith(".html") || lower.endsWith(".htm")) {
      return {
        kind: "html",
        label: "HTML-файл плейлиста",
        messages: [
          "HTML-файл выбран. После распознавания список треков появится, если в сохраненной странице есть данные плейлиста.",
        ],
      };
    }
    return {
      kind: "file",
      label: "Файл плейлиста",
      messages: ["Файл выбран и готов к распознаванию."],
    };
  }

  const trimmed = content.trim();
  if (!trimmed) {
    return { kind: "unknown", label: "Источник не выбран", messages: [] };
  }

  const iframeSrc = extractIframeSrc(trimmed);
  if (iframeSrc) {
    return {
      kind: "iframe",
      label: "Iframe Яндекс Музыки",
      sourceUrl: iframeSrc,
      messages: [
        "Iframe распознан. Если треки не удастся извлечь, загрузите сохраненный HTML-файл страницы или обычный txt-файл.",
      ],
    };
  }

  const linkMatch = trimmed.match(/https?:\/\/music\.yandex\.ru\/[^\s"'<>]+/i);
  if (linkMatch) {
    return {
      kind: "link",
      label: "Ссылка на плейлист",
      sourceUrl: linkMatch[0],
      messages: [
        "Ссылка распознана. Если по ссылке нельзя получить список треков, сохраните страницу плейлиста как HTML или используйте txt-файл.",
      ],
    };
  }

  if (tab === "link" && (/<html/i.test(trimmed) || /__NEXT_DATA__/i.test(trimmed))) {
    return {
      kind: "html",
      label: "HTML-код плейлиста",
      messages: [
        "HTML распознан как страница плейлиста. После распознавания появится список треков, если в коде есть данные страницы.",
      ],
    };
  }

  return {
    kind: "text",
    label: "Текстовый список",
    messages: ["Одна строка — один трек."],
  };
}

function isTextPreviewFile(file: File | null) {
  if (!file) return false;
  const lower = file.name.toLowerCase();
  return lower.endsWith(".txt") || lower.endsWith(".m3u") || lower.endsWith(".m3u8");
}

function toPreviewRows(sourceText: string, limit = PREVIEW_SOFT_LIMIT) {
  return sourceText
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, limit)
    .map((line) => {
      const parsed = splitTrackLine(line);
      return { ...parsed, duration: "", line };
    });
}

function Sidebar() {
  const nav: Array<{ label: string; href: string; active?: boolean }> = [
    { label: "Новый архив", href: "#dashboard", active: true },
    { label: "История архивов", href: "#results" },
    { label: "Настройки", href: "#archive-settings" },
  ];
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="logo-mark" aria-hidden="true">
          <span />
          <span />
          <span />
          <span />
        </div>
        <div>
          <strong>{PRODUCT_NAME}</strong>
          <small>личный архив</small>
        </div>
      </div>

      <nav className="sidebar-nav" aria-label="Разделы">
        {nav.map((item) => (
          <a className={item.active ? "active" : ""} href={item.href} key={item.label}>
            <span className="nav-dot" aria-hidden="true" />
            {item.label}
          </a>
        ))}
      </nav>

      <div className="sidebar-bottom">
        <div className="side-links">
          <a href="#help">Помощь</a>
          <span className="disabled-link">Поддержка</span>
        </div>
        <div className="profile-chip">
          <span>DE</span>
          <div>
            <strong>Пользователь</strong>
            <small>demo</small>
          </div>
        </div>
      </div>
    </aside>
  );
}

function Hero() {
  return (
    <section className="hero-panel">
      <div className="hero-copy">
        <p className="eyebrow">{PRODUCT_SUBTITLE}</p>
        <h1>Собери плейлист в архив</h1>
        <p>
          Загрузите список треков любым удобным способом. Мы найдем подходящие версии,
          отфильтруем clean, radio edit и цензуру, а затем упакуем результат в архив.
        </p>
        <div className="hero-badges">
          <span>Автоматический парсинг</span>
          <span>Резервные источники</span>
          <span>Explicit и без цензуры</span>
        </div>
      </div>
      <div className="hero-art" aria-hidden="true">
        <div className="vinyl">
          <span />
        </div>
        <div className="wave-stack">
          {Array.from({ length: 18 }).map((_, i) => (
            <i key={i} style={{ height: `${20 + ((i * 13) % 54)}px` }} />
          ))}
        </div>
      </div>
    </section>
  );
}

function ImportCard({
  tab,
  setTab,
  importContent,
  setImportContent,
  importFile,
  setImportFile,
  analysis,
  importResult,
  importing,
  isBusy,
  submitImport,
}: {
  tab: ImportTab;
  setTab: (tab: ImportTab) => void;
  importContent: string;
  setImportContent: (value: string) => void;
  importFile: File | null;
  setImportFile: (file: File | null) => void;
  analysis: ImportAnalysis;
  importResult: ImportResponse | null;
  importing: boolean;
  isBusy: boolean;
  submitImport: (runNow: boolean) => Promise<void>;
}) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [dragActive, setDragActive] = useState(false);

  const tabCopy = {
    text: {
      label: "Текст",
      placeholder: "Вставьте список треков...\nKizaru - Дежавю\nMiyagi & Эндшпиль - I Got Love",
      hint: "Одна строка — один трек. Например: Исполнитель - Название трека",
    },
    link: {
      label: "Ссылка / iframe",
      placeholder: "Вставьте ссылку на плейлист, iframe или URL...\nhttps://music.yandex.ru/...\n<iframe ...></iframe>",
      hint: "Вставьте ссылку на плейлист Яндекс Музыки или iframe-код из кнопки «Поделиться».",
    },
    file: {
      label: "TXT / HTML-файл",
      placeholder: "",
      hint: "TXT, M3U, M3U8, HTML — до 50 МБ. TXT остается обычным способом загрузки плейлиста.",
    },
  } satisfies Record<ImportTab, { label: string; placeholder: string; hint: string }>;

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    const dropped = event.dataTransfer.files?.[0];
    if (dropped) setImportFile(dropped);
  };

  return (
    <section className="card import-card" id="dashboard">
      <div className="card-head">
        <div>
          <p className="card-kicker">Источник</p>
          <h2>Импорт плейлиста</h2>
        </div>
        <span className="soft-badge">Yandex HTML / TXT</span>
      </div>

      <div className="tabs" role="tablist" aria-label="Тип импорта">
        {(Object.keys(tabCopy) as ImportTab[]).map((key) => (
          <button
            className={tab === key ? "active" : ""}
            key={key}
            type="button"
            onClick={() => setTab(key)}
          >
            {tabCopy[key].label}
          </button>
        ))}
      </div>

      {((analysis.kind !== "text" && analysis.messages.length > 0) || Boolean(analysis.sourceUrl) || Boolean(importResult?.warnings?.length)) && (
        <div className="import-notice">
          <strong>{analysis.label}</strong>
          {analysis.sourceUrl ? <code>{analysis.sourceUrl}</code> : null}
          {analysis.messages.map((message) => (
            <p key={message}>{message}</p>
          ))}
          {importResult?.track_count === 0 && (analysis.kind === "iframe" || analysis.kind === "link" || analysis.kind === "html") ? (
            <p>
              Плейлист распознан как ссылка или iframe, но список треков не найден. Загрузите сохраненный HTML-файл
              страницы или обычный txt-файл.
            </p>
          ) : null}
          {importResult?.warnings?.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </div>
      )}

      {tab !== "file" ? (
        <label className="field-block">
          <span>{tabCopy[tab].hint}</span>
          {tab === "link" && (
            <div className="input-help">
              <strong>Как это работает</strong>
              <p>
                Можно вставить обычную ссылку вида <code>music.yandex.ru/users/.../playlists/...</code> или весь
                iframe-код. Ссылка и iframe помогают распознать плейлист, но не всегда содержат полный список треков.
                Для полного импорта откройте плейлист, прокрутите его до конца, сохраните страницу как HTML и загрузите
                файл во вкладке «TXT / HTML-файл».
              </p>
            </div>
          )}
          <textarea
            className="textarea"
            value={importContent}
            onChange={(ev) => setImportContent(ev.target.value)}
            placeholder={tabCopy[tab].placeholder}
            rows={9}
          />
          {tab === "text" && (
            <button className="inline-link-button" type="button" onClick={() => setTab("file")}>
              Загрузить готовый .txt файл
            </button>
          )}
        </label>
      ) : (
        <div
          className={`dropzone ${dragActive ? "drag-active" : ""}`}
          onClick={() => fileInputRef.current?.click()}
          onDragOver={(ev) => {
            ev.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={() => setDragActive(false)}
          onDrop={onDrop}
          role="button"
          tabIndex={0}
          onKeyDown={(ev) => {
            if (ev.key === "Enter" || ev.key === " ") fileInputRef.current?.click();
          }}
        >
          <input
            ref={fileInputRef}
            className="visually-hidden"
            type="file"
            accept=".html,.htm,.txt,.m3u,.m3u8,text/html,text/plain"
            onChange={(ev) => setImportFile(ev.target.files?.[0] ?? null)}
          />
          <div className="drop-icon" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <strong>{importFile ? importFile.name : "Перетащите TXT или HTML-файл сюда"}</strong>
          <small>или нажмите для выбора. {tabCopy.file.hint}</small>
        </div>
      )}

      <div className="button-row">
        <button className="button secondary" type="button" disabled={importing || isBusy} onClick={() => void submitImport(false)}>
          {importing ? "Распознаем..." : "Распознать плейлист"}
        </button>
        <button className="button ghost" type="button" disabled={importing || isBusy} onClick={() => void submitImport(true)}>
          Распознать и запустить
        </button>
      </div>
    </section>
  );
}

function QualityCard({
  settings,
  setSettings,
}: {
  settings: QualitySettings;
  setSettings: Dispatch<SetStateAction<QualitySettings>>;
}) {
  const prefer = [
    ["preferOriginal", "Оригинал"],
    ["preferExplicit", "Explicit"],
    ["preferUncensored", "Без цензуры"],
  ] as const;
  const reject = [
    ["rejectClean", "Чистые clean-версии"],
    ["rejectRadio", "Радио версии"],
    ["rejectCensored", "Цензурированные версии"],
    ["rejectRemix", "Remix, если remix не указан во входном треке"],
    ["rejectLive", "Live, если live не указан во входном треке"],
  ] as const;

  return (
    <section className="card quality-card">
      <div className="card-head">
        <div>
          <p className="card-kicker">Правила отбора</p>
          <h2>Качество результата</h2>
        </div>
        <span className="recommend-badge">Рекомендуется</span>
      </div>

      <div className="quality-section">
        <h3>Предпочитать при поиске</h3>
        <div className="pill-row">
          {prefer.map(([key, label]) => (
            <button
              className={settings[key] ? "pill active" : "pill"}
              key={key}
              type="button"
              onClick={() => setSettings((prev) => ({ ...prev, [key]: !prev[key] }))}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="quality-section">
        <h3>Исключать из результатов</h3>
        <div className="toggle-list">
          {reject.map(([key, label]) => (
            <label key={key} className="toggle-row">
              <input
                type="checkbox"
                checked={settings[key]}
                onChange={() => setSettings((prev) => ({ ...prev, [key]: !prev[key] }))}
              />
              <span />
              {label}
            </label>
          ))}
        </div>
      </div>

      <p className="quality-footnote">
        Настройки уже подготовлены для quality-слоя. Пока они сохраняются на фронтенде и не ломают текущий сценарий запуска.
      </p>
    </section>
  );
}

function PlaylistPreviewCard({
  importResult,
  importContent,
  importFile,
  importFileText,
  analysis,
}: {
  importResult: ImportResponse | null;
  importContent: string;
  importFile: File | null;
  importFileText: string | null;
  analysis: ImportAnalysis;
}) {
  const localPreview = useMemo(() => {
    if (importFileText) return toPreviewRows(importFileText);
    if (analysis.kind === "text") return toPreviewRows(importContent);
    return [];
  }, [analysis.kind, importContent, importFileText]);

  const preview = useMemo(() => {
    if (importResult?.preview?.length && localPreview.length <= importResult.preview.length) {
      return importResult.preview.map((track) => ({
        artist: track.artist || splitTrackLine(track.line).artist,
        title: track.title || splitTrackLine(track.line).title,
        duration: formatMs(track.duration_ms),
        line: track.line,
      }));
    }
    if (localPreview.length > 0) return localPreview;
    if (importResult?.preview?.length) {
      return importResult.preview.map((track) => ({
        artist: track.artist || splitTrackLine(track.line).artist,
        title: track.title || splitTrackLine(track.line).title,
        duration: formatMs(track.duration_ms),
        line: track.line,
      }));
    }
    return [];
  }, [importResult, localPreview]);

  const count = importResult?.track_count ?? localPreview.length ?? preview.length;
  const shown = preview.length;
  const isTrimmed = count > shown;

  return (
    <section className="card preview-card">
      <div className="card-head">
        <div>
          <p className="card-kicker">Проверка перед стартом</p>
          <h2>Предпросмотр плейлиста</h2>
        </div>
        <span className="count-badge">Найдено треков: {count}</span>
      </div>

      {isTrimmed ? <p className="preview-caption">Показаны первые {shown} из {count} треков</p> : null}

      {preview.length > 0 ? (
        <div className="track-list">
          {preview.map((track, index) => (
            <div className="track-row" key={`${track.line}-${index}`}>
              <div className="cover-placeholder">
                <span>{index + 1}</span>
              </div>
              <div className="track-meta">
                <strong>{track.title || track.line}</strong>
                <small>{track.artist || "Исполнитель не указан"}</small>
              </div>
              {track.duration ? <time>{track.duration}</time> : null}
              <button type="button" aria-label="Дополнительно">...</button>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-preview">
          <strong>{importFile ? importFile.name : "Плейлист пока не распознан"}</strong>
          <span>
            {analysis.kind === "iframe" || analysis.kind === "link" || analysis.kind === "html"
              ? "Источник распознан, но треки пока не извлечены. Попробуйте распознать плейлист или загрузить HTML/txt-файл."
              : "Вставьте список или загрузите файл, чтобы увидеть треки перед запуском."}
          </span>
        </div>
      )}
    </section>
  );
}

function ArchiveSettingsCard({
  delivery,
  setDelivery,
  parallelTasks,
  setParallelTasks,
  maxParallelTasks,
  isBusy,
  importing,
  hasImportInput,
  submitImport,
}: {
  delivery: "zip" | "tracks";
  setDelivery: (delivery: "zip" | "tracks") => void;
  parallelTasks: ParallelTasks;
  setParallelTasks: (value: ParallelTasks) => void;
  maxParallelTasks: number | null;
  isBusy: boolean;
  importing: boolean;
  hasImportInput: boolean;
  submitImport: (runNow: boolean) => Promise<void>;
}) {
  return (
    <section className="card archive-card" id="archive-settings">
      <div className="card-head">
        <div>
          <p className="card-kicker">Финальный пакет</p>
          <h2>Настройки архива</h2>
        </div>
      </div>

      <div className="segmented" role="radiogroup" aria-label="Формат архива">
        <button className={delivery === "zip" ? "active" : ""} type="button" onClick={() => setDelivery("zip")}>
          ZIP <span>рекомендуется</span>
        </button>
        <button className={delivery === "tracks" ? "active" : ""} type="button" onClick={() => setDelivery("tracks")}>
          ZIP без сжатия MP3
        </button>
      </div>

      <div className="setting-list">
        <div>
          <span>Структура архива</span>
          <strong>Треки в корне</strong>
        </div>
        <div className="setting-control">
          <label htmlFor="parallel-tasks">
            <span>Параллельные задачи</span>
            <small>Больше задач может ускорить обработку, но увеличивает нагрузку и риск ошибок источников.</small>
          </label>
          <select
            id="parallel-tasks"
            className="setting-select"
            value={String(parallelTasks)}
            onChange={(event) => {
              const value = event.target.value;
              if (value === "3" || value === "5" || value === "10") {
                setParallelTasks(Number(value) as 3 | 5 | 10);
              } else {
                setParallelTasks("auto");
              }
            }}
          >
            <option value="auto">Авто</option>
            <option value="3">3</option>
            <option value="5">5</option>
            <option value="10">10</option>
          </select>
        </div>
        <div className="disabled-setting">
          <span>Разбивать на части</span>
          <strong>Скоро</strong>
        </div>
      </div>

      <p className="setting-note">
        {maxParallelTasks
          ? `Сейчас сервис позволяет до ${maxParallelTasks} параллельных задач. Эта настройка пока сохраняется в интерфейсе и не меняет backend-лимит автоматически.`
          : "Настройка параллельности пока сохраняется в интерфейсе и не меняет backend-лимит автоматически."}
      </p>

      <button
        className="button primary-gradient"
        type="button"
        disabled={isBusy || importing || !hasImportInput}
        onClick={() => void submitImport(true)}
      >
        {isBusy ? "Сбор уже идет..." : "Запустить сбор и архивирование"}
      </button>
    </section>
  );
}

function ProgressCard({ job }: { job: JobInfo | null }) {
  const percent = progressPercent(job);
  const current = Math.min(job?.current_index ?? 0, job?.total_lines ?? 0);
  const total = job?.total_lines ?? 0;
  const steps = ["Очередь", "Парсинг", "Поиск", "Скачивание", "Упаковка", "Готово"];
  const activeStep = job?.phase === "phase1" ? 2 : job?.phase === "phase2" ? 3 : job?.phase === "done" ? 5 : job ? 1 : 0;
  const quality = job?.quality_counts;
  const startedAt = job?.created_at ? Date.parse(job.created_at) : null;
  const updatedAt = job?.updated_at ? Date.parse(job.updated_at) : null;
  const now = Date.now();
  const elapsedMs = startedAt ? Math.max(0, (job?.status === "completed" || job?.status === "partial_success" || job?.status === "failed" ? (updatedAt ?? now) : now) - startedAt) : null;
  const etaMs = elapsedMs && current > 0 && total > current ? (elapsedMs / current) * (total - current) : null;
  const foundCount = total > 0 && quality?.not_found != null ? Math.max(0, total - quality.not_found) : null;
  const downloadedCount = quality?.downloaded_ok ?? job?.counts?.ok ?? null;
  const rejectedCount =
    (quality?.rejected_clean_or_censored ?? 0) +
    (quality?.rejected_radio ?? 0) +
    (quality?.version_mismatch ?? 0) +
    (quality?.duration_mismatch ?? 0) +
    (quality?.suspicious ?? 0);

  return (
    <section className="card progress-card">
      <div className="card-head">
        <div>
          <p className="card-kicker">Pipeline</p>
          <h2>Ход выполнения</h2>
        </div>
        <span className={`status-badge status-${job?.status ?? "idle"}`}>{jobStatusLabel(job?.status)}</span>
      </div>

      <div className="progress-summary">
        <div className="progress-summary-copy">
          <strong>{percent}%</strong>
          <span>Общий прогресс</span>
        </div>
        <span>{jobPhaseLabel(job?.phase)}</span>
      </div>
      <div className="progress-bar">
        <span style={{ width: `${percent}%` }} />
      </div>

      <div className="stepper">
        {steps.map((step, index) => (
          <span className={index <= activeStep ? "active" : ""} key={step}>
            {step}
          </span>
        ))}
      </div>

      <dl className="mini-stats">
        <div>
          <dt>Текущая фаза</dt>
          <dd>{jobPhaseLabel(job?.phase)}</dd>
        </div>
        <div>
          <dt>Всего треков</dt>
          <dd>{total || "—"}</dd>
        </div>
        <div>
          <dt>Готово</dt>
          <dd>{current} / {total}</dd>
        </div>
        <div>
          <dt>Найдено</dt>
          <dd>{foundCount ?? "—"}</dd>
        </div>
        <div>
          <dt>Скачано</dt>
          <dd>{downloadedCount ?? "—"}</dd>
        </div>
        <div>
          <dt>Не найдено</dt>
          <dd>{quality?.not_found ?? "—"}</dd>
        </div>
        <div>
          <dt>Отклонено</dt>
          <dd>{quality ? rejectedCount : "—"}</dd>
        </div>
        <div>
          <dt>Manual review</dt>
          <dd>{quality?.manual_review ?? "—"}</dd>
        </div>
        <div>
          <dt>Прошло времени</dt>
          <dd>{formatDurationClock(elapsedMs)}</dd>
        </div>
        <div>
          <dt>Осталось примерно</dt>
          <dd>{formatDurationClock(etaMs)}</dd>
        </div>
      </dl>

      <p className="current-track">
        Текущий трек: <span>{job?.current_track || "—"}</span>
      </p>
    </section>
  );
}

function ResultsCard({
  job,
  report,
}: {
  job: JobInfo | null;
  report: PlaylistReport | null;
}) {
  const counts = job?.counts ?? report?.counts ?? { ok: 0, failed: 0, skipped: 0 };
  const quality = job?.quality_counts ?? report?.quality_counts;
  const rejected = (quality?.rejected_clean_or_censored ?? 0) + (quality?.rejected_radio ?? 0);
  const total = job?.total_lines ?? counts.ok + counts.failed + counts.skipped;
  const ready = job?.download_ready;

  return (
    <section className="card results-card" id="results">
      <div className="card-head">
        <div>
          <p className="card-kicker">Итоги</p>
          <h2>Результаты</h2>
        </div>
        {ready ? <span className="ready-badge">Архив готов</span> : null}
      </div>

      <div className="result-grid">
        <div><span>Всего</span><strong>{total}</strong></div>
        <div><span>OK</span><strong>{quality?.downloaded_ok ?? counts.ok}</strong></div>
        <div><span>Не найдено</span><strong>{quality?.not_found ?? 0}</strong></div>
        <div><span>Отклонено</span><strong>{rejected}</strong></div>
        <div><span>Manual review</span><strong>{quality?.manual_review ?? 0}</strong></div>
        <div><span>Ошибки</span><strong>{counts.failed}</strong></div>
      </div>

      {ready && (
        <div className="download-panel">
          <strong>Архив готов к скачиванию</strong>
          <span>Формат: {job?.download_name?.endsWith(".zip") ? "ZIP" : "архив"}</span>
          <a className="button primary-gradient link-button" href={`${API_BASE}/api/jobs/${job.job_id}/download`}>
            Скачать архив
          </a>
        </div>
      )}

      {job?.available_reports && job.available_reports.length > 0 && (
        <div className="report-actions">
          {job.available_reports.map((name) => (
            <a key={name} href={`${API_BASE}/api/jobs/${job.job_id}/reports/${name}`}>
              Скачать {name}
            </a>
          ))}
        </div>
      )}

      {report?.failed?.length ? (
        <details className="problem-list">
          <summary>Проблемные треки</summary>
          <ul>
            {report.failed.map((line, index) => <li key={`${line}-${index}`}>{line}</li>)}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

export default function App() {
  const savedPrefs = useMemo(() => readUiPrefs(), []);
  const [limits, setLimits] = useState<Limits | null>(null);
  const [delivery, setDelivery] = useState<"zip" | "tracks">(() => savedPrefs?.delivery ?? "zip");
  const [parallelTasks, setParallelTasks] = useState<ParallelTasks>(() => savedPrefs?.parallelTasks ?? "auto");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [statusTone, setStatusTone] = useState<NoticeTone>("status");
  const [lastReport, setLastReport] = useState<PlaylistReport | null>(null);
  const [job, setJob] = useState<JobInfo | null>(null);
  const [downloadedJobId, setDownloadedJobId] = useState<string | null>(null);
  const [importTab, setImportTab] = useState<ImportTab>("text");
  const [importContent, setImportContent] = useState("");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importFileText, setImportFileText] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<ImportResponse | null>(null);
  const [qualitySettings, setQualitySettings] = useState<QualitySettings>({
    preferOriginal: true,
    preferExplicit: true,
    preferUncensored: true,
    rejectClean: true,
    rejectRadio: true,
    rejectCensored: true,
    rejectRemix: true,
    rejectLive: true,
  });

  useEffect(() => {
    writeUiPrefs(delivery, parallelTasks);
  }, [delivery, parallelTasks]);

  useEffect(() => {
    let cancelled = false;
    if (!importFile || !isTextPreviewFile(importFile)) {
      setImportFileText(null);
      return () => {
        cancelled = true;
      };
    }

    importFile
      .text()
      .then((text) => {
        if (!cancelled) setImportFileText(text);
      })
      .catch(() => {
        if (!cancelled) setImportFileText(null);
      });

    return () => {
      cancelled = true;
    };
  }, [importFile]);

  useEffect(() => {
    fetchApi(`${API_BASE}/api/limits`)
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (j && typeof j.max_concurrent_jobs === "number") {
          setLimits({
            max_concurrent_jobs: j.max_concurrent_jobs,
            max_playlist_lines: typeof j.max_playlist_lines === "number" ? j.max_playlist_lines : null,
            session_active: j.session_active === true,
          });
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!job) return;
    if (job.status !== "queued" && job.status !== "running") return;

    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetchApi(`${API_BASE}/api/jobs/${job.job_id}`);
        if (!res.ok) {
          throw new Error(`Не удалось получить статус задачи (${res.status})`);
        }
        const next = (await res.json()) as JobInfo;
        if (!cancelled) {
          setJob(next);
          if (next.report) setLastReport(next.report);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Не удалось обновить статус задачи.");
        }
      }
    };

    const timer = window.setInterval(poll, 1200);
    void poll();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [job?.job_id, job?.status]);

  useEffect(() => {
    if (!job?.download_ready) return;
    if (downloadedJobId === job.job_id) return;

    let cancelled = false;
    const startDownload = async () => {
      try {
        const res = await fetchApi(`${API_BASE}/api/jobs/${job.job_id}/download`);
        if (!res.ok) {
          throw new Error(`Не удалось скачать архив (${res.status})`);
        }
        const hdr = res.headers.get("X-Playlist-Report");
        if (hdr) {
          const parsed = decodePlaylistReportHeader(hdr);
          if (parsed) setLastReport(parsed);
        }
        const blob = await res.blob();
        const dispo = res.headers.get("content-disposition");
        let name = job.download_name || (delivery === "zip" ? "playlist.zip" : "tracks.zip");
        const m = dispo?.match(/filename="?([^";]+)"?/i);
        if (m) name = m[1];
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = name;
        a.click();
        URL.revokeObjectURL(url);
        if (!cancelled) {
          setDownloadedJobId(job.job_id);
          setStatus(job.status === "partial_success" ? "Архив сохранен, но часть треков требует проверки." : "Архив сохранен.");
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Не удалось скачать архив задачи.");
        }
      }
    };

    void startDownload();
    return () => {
      cancelled = true;
    };
  }, [job, downloadedJobId, delivery]);

  useEffect(() => {
    if (!job) return;
    if (job.status === "failed" && job.detail) {
      setError(job.detail);
      setStatus(null);
    }
  }, [job]);

  const importAnalysis = useMemo(
    () => detectImportAnalysis(importTab, importContent, importFile),
    [importContent, importFile, importTab],
  );

  const submitImport = useCallback(
    async (runNow: boolean) => {
      setError(null);
      setStatus(null);
      setStatusTone("status");
      if (!importFile && !importContent.trim()) {
        setError("Вставьте список, ссылку, iframe или выберите HTML/TXT-файл.");
        return;
      }
      setSubmitting(runNow);
      setImporting(!runNow);
      try {
        const fd = new FormData();
        if (importFile) {
          fd.append("file", importFile);
        } else {
          fd.append("content", importContent);
        }
        fd.append("run", runNow ? "true" : "false");
        fd.append("delivery", delivery);
        const res = await fetchApi(`${API_BASE}/api/import-playlist`, {
          method: "POST",
          body: fd,
        });
        const data = (await res.json()) as ImportResponse & { detail?: string };
        if (!res.ok) {
          const rep = errBodyToReport(data as ErrBody);
          if (rep) setLastReport(rep);
          throw new Error(data.detail || `Импорт завершился с кодом ${res.status}.`);
        }
        setImportResult(data);
        if (data.job) {
          setJob(data.job);
          setDownloadedJobId(null);
          setLastReport(null);
          setStatus("Плейлист распознан. Сбор архива запущен.");
        } else {
          if (data.track_count > 0) {
            setStatus("Плейлист распознан и сохранен.");
          } else if (importAnalysis.kind === "iframe" || importAnalysis.kind === "link" || importAnalysis.kind === "html") {
            setStatusTone("warning");
            setStatus(
              "Плейлист распознан как ссылка или iframe, но список треков не найден. Загрузите сохраненный HTML-файл страницы или обычный txt-файл.",
            );
          } else {
            setStatusTone("warning");
            setStatus("Плейлист распознан, но треки не найдены.");
          }
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Не удалось импортировать плейлист.");
      } finally {
        setSubmitting(false);
        setImporting(false);
      }
    },
    [importAnalysis.kind, importFile, importContent, delivery],
  );

  const isBusy = submitting || job?.status === "queued" || job?.status === "running";
  const hasImportInput = Boolean(importFile || importContent.trim());

  return (
    <div className="app-shell">
      <Sidebar />
      <main className="main-content">
        <Hero />

        {(status || error) && (
          <div className={`notice ${error ? "error" : statusTone}`} role={error ? "alert" : "status"}>
            {error || status}
          </div>
        )}

        <section className="dashboard-grid">
          <ImportCard
            tab={importTab}
            setTab={setImportTab}
            importContent={importContent}
            setImportContent={setImportContent}
            importFile={importFile}
            setImportFile={setImportFile}
            analysis={importAnalysis}
            importResult={importResult}
            importing={importing}
            isBusy={isBusy}
            submitImport={submitImport}
          />
          <QualityCard settings={qualitySettings} setSettings={setQualitySettings} />
        </section>

        <section className="bottom-grid">
          <PlaylistPreviewCard
            importResult={importResult}
            importContent={importContent}
            importFile={importFile}
            importFileText={importFileText}
            analysis={importAnalysis}
          />
          <ArchiveSettingsCard
            delivery={delivery}
            setDelivery={setDelivery}
            parallelTasks={parallelTasks}
            setParallelTasks={setParallelTasks}
            maxParallelTasks={limits?.max_concurrent_jobs ?? null}
            isBusy={isBusy}
            importing={importing}
            hasImportInput={hasImportInput}
            submitImport={submitImport}
          />
          <ProgressCard job={job} />
        </section>

        <section className="results-grid">
          <ResultsCard job={job} report={lastReport} />
          <section className="card help-card" id="help">
            <p className="card-kicker">Важно</p>
            <h2>Как подготовить плейлист</h2>
            <p>
              TXT-файл можно загрузить во вкладке «TXT / HTML-файл». Внутри обычный список: одна строка — один трек,
              например «Исполнитель - Название».
            </p>
            <p>
              Ссылку или iframe Яндекс Музыки можно взять через «Поделиться» у плейлиста. Если по ссылке треки не
              появились, сохраните страницу плейлиста как HTML после прокрутки списка до конца и загрузите этот файл.
              Яндекс Музыка используется только как источник списка треков и metadata.
            </p>
            <p>
              Сервис помогает собрать личный архив и отфильтровать неподходящие версии треков. Используйте только для
              личных целей.
            </p>
            <div className="help-tags">
              <span>clean rejected</span>
              <span>radio edit rejected</span>
              <span>manual review</span>
            </div>
          </section>
        </section>
      </main>
    </div>
  );
}
