"""Daily data-only collection and a five-session mainboard rollout gate."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.finsight.daily"
PILOT_LABEL = "com.finsight.mainboard-pilot"


def job_definition(data_dir: Path, python: str | None = None, universe: str = "sample", label: str = LABEL) -> dict:
    package_source = Path(__file__).resolve().parents[1]
    root = Path.cwd().resolve()
    if (root / 'src/finsight/__init__.py').exists():
        package_source = root / 'src'
    args = [python or sys.executable, '-m', 'finsight', '--data-dir', str(data_dir.resolve()), 'watch', '--once']
    if universe == 'mainboard':
        args += ['--universe', 'mainboard']
    return {'Label': label, 'ProgramArguments': args, 'WorkingDirectory': str(root),
        'EnvironmentVariables': {'PYTHONPATH': str(package_source)}, 'RunAtLoad': label == LABEL,
        'StartCalendarInterval': {'Hour': 18, 'Minute': 5 if label == LABEL else 15},
        'StandardOutPath': str(data_dir.resolve() / ('scheduler.log' if label == LABEL else 'mainboard-pilot.log')),
        'StandardErrorPath': str(data_dir.resolve() / ('scheduler.log' if label == LABEL else 'mainboard-pilot.log'))}


def rollout_status(store) -> dict:
    from finsight.data.universe import universe_id
    uid = universe_id(True)
    latest = store.latest(uid)
    with store.connect() as db:
        records = {r['trade_date']: dict(r) for r in db.execute('SELECT * FROM publications WHERE universe_id=?', (uid,))}
    consecutive = 0
    sessions = []
    if latest:
        calendar = store.load_snapshot(latest.snapshot_id, table_names=('calendar',))['calendar']
        sessions = sorted(d for d in calendar['trade_date'] if d <= str(latest.as_of))
        for day in reversed(sessions):
            if day not in records or records[day]['coverage'] < .98:
                break
            consecutive += 1
    return {'universe_id': uid, 'consecutive_days': consecutive, 'required_days': 5,
            'promotion_eligible': consecutive >= 5, 'published_dates': sorted(records),
            'latest_session': sessions[-1] if sessions else None,
            'note': 'Only distinct real mainboard trading sessions count; previews, batches and synthetic data do not count.'}


def install(data_dir: Path, universe: str = 'sample', *, _label: str = LABEL) -> dict:
    if sys.platform != 'darwin':
        raise ValueError('automatic schedule installation supports macOS; use watch --once with your system scheduler')
    if universe not in {'sample', 'mainboard'} or _label not in {LABEL, PILOT_LABEL}:
        raise ValueError('invalid collector schedule')
    if universe == 'mainboard' and _label == LABEL:
        from finsight.data.store import Store
        if not rollout_status(Store(data_dir))['promotion_eligible']:
            raise ValueError('mainboard needs five consecutive published sessions before replacing the daily sample job')
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    folder = Path.home() / 'Library/LaunchAgents'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{_label}.plist'
    old = path.read_bytes() if path.exists() else None
    definition = job_definition(data_dir, universe=universe, label=_label)
    if old and plistlib.loads(old).get('WorkingDirectory') != definition['WorkingDirectory']:
        raise ValueError('a FinSight schedule for another checkout exists; inspect it before replacing it')
    if old == plistlib.dumps(definition):
        loaded = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{_label}'], capture_output=True).returncode == 0
        if loaded:
            return {'status': 'loaded', 'label': _label, 'universe': universe}
    domain = f'gui/{os.getuid()}'
    target = f'{domain}/{_label}'
    loaded = subprocess.run(['launchctl', 'print', target], capture_output=True).returncode == 0
    if loaded:
        subprocess.run(['launchctl', 'bootout', target], check=True, capture_output=True)
    path.write_bytes(plistlib.dumps(definition))
    path.chmod(0o600)
    try:
        subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        if old:
            path.write_bytes(old)
            if loaded:
                subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True)
        else:
            path.unlink(missing_ok=True)
        raise ValueError('launchd could not start the collector; previous schedule was restored') from exc
    return {'status': 'installed', 'label': _label, 'universe': universe, 'plist': str(path),
            'time': '18:05' if _label == LABEL else '18:15', 'model_calls': False}


def update_collection_rollout(store) -> dict:
    result = rollout_status(store)
    if sys.platform == 'darwin':
        if result['promotion_eligible']:
            install(store.root, 'mainboard')
            path = Path.home() / 'Library/LaunchAgents' / f'{PILOT_LABEL}.plist'
            if path.exists() and plistlib.loads(path.read_bytes()).get('WorkingDirectory') == str(Path.cwd().resolve()):
                subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{PILOT_LABEL}'], capture_output=True)
                path.unlink()
            result['stage'] = 'promoted'
        else:
            install(store.root, 'mainboard', _label=PILOT_LABEL)
            result['stage'] = 'pilot'
    else:
        result['stage'] = 'manual-system-scheduler'
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO collector_control VALUES ('rollout',?)", (json.dumps(result),))
    return result


def status() -> dict:
    if sys.platform != 'darwin':
        return {'status': 'unsupported_platform'}
    loaded = {}
    for label in (LABEL, PILOT_LABEL):
        loaded[label] = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True).returncode == 0
    return {'status': 'loaded' if loaded[LABEL] else 'not_loaded', 'label': LABEL, 'jobs': loaded}
