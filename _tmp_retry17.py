import json
from datetime import datetime
from pathlib import Path
from download_service import load_download_config_from_env
from downloader_core import run_playlist_download

playlist_path = Path(r'downloaded_music/reruns/rerun_20260624_183522_failed17.txt')
lines = playlist_path.read_text(encoding='utf-8').splitlines()
run_name = 'retry17_' + datetime.now().strftime('%Y%m%d_%H%M%S')
run_root = Path('downloaded_music') / 'reruns' / run_name
music_dir = run_root / 'music'
log_file = run_root / 'job.log'
run_root.mkdir(parents=True, exist_ok=True)

cfg = load_download_config_from_env()
cfg.search_mode = 'balanced'
cfg.enable_selenium_fallback = False
cfg.skysound_fallback_enabled = True
cfg.lmusic_fallback_enabled = True
cfg.zvyki_fallback_enabled = True
cfg.search_cache_enabled = False
cfg.download_concurrency = min(cfg.download_concurrency or 5, 5)
cfg.resolve_parallel_workers = min(cfg.resolve_parallel_workers or 4, 4)

print('RUN_ROOT', run_root)
print('TRACKS', len(lines))

def on_progress(idx, total, raw, phase):
    label = (raw or '').encode('unicode_escape').decode()
    print(f'PROGRESS {idx}/{total} {phase} {label}', flush=True)

result = run_playlist_download(lines, str(music_dir), log_file=str(log_file), cfg=cfg, on_progress=on_progress)
(run_root / 'result_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print('DONE')
print(json.dumps({
    'run_root': str(run_root),
    'ok': len(result.get('ok', [])),
    'failed': len(result.get('failed', [])),
    'skipped': len(result.get('skipped', [])),
}, ensure_ascii=False), flush=True)
