"""Explicit, exact-file access to private release reports; no directory discovery."""
from __future__ import annotations

from pathlib import Path

from .errors import WorkflowError


def report_path(engine, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.startswith('.local/'):
        raise WorkflowError('path', 'Select one project report under .local/')
    value = Path(relative)
    if (value.as_posix() != relative or value.is_absolute() or '..' in value.parts
            or any(ord(ch) < 32 for ch in relative)
            or any(part.startswith('.') for part in value.parts[1:])
            or value.suffix.lower() not in {'.md', '.json', '.txt'}):
        raise WorkflowError('path', 'Select a canonical Markdown, JSON or text report path')
    path = engine.project_root / value
    if any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(engine.project_root)):
        raise WorkflowError('path', 'Symlink report paths are not allowed')
    if not path.resolve().is_relative_to(engine.project_root):
        raise WorkflowError('path', 'Report escapes its configured project')
    if path.resolve().is_relative_to(engine.knowledge_root) or path.resolve().is_relative_to(engine.state_dir):
        raise WorkflowError('path', 'Knowledge and state are not independent reports')
    return path


def select_report(engine, relative: str) -> dict:
    from .engine import digest, now, safe_text
    path = report_path(engine, relative)
    if not path.is_file() or path.stat().st_size > 16000:
        raise WorkflowError('report', 'Report unavailable or exceeds 16000 bytes; select a smaller report')
    data = path.read_bytes()
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise WorkflowError('report', 'Report must be UTF-8 text') from None
    safe_text(text)
    identity = digest(relative.encode())[:24]
    selection = {'selection_id': identity, 'path': relative, 'sha256': digest(data), 'selected_at': now()}
    with engine._writer():
        engine._put('report_selection', identity, selection)
    return {'state': 'selected', **selection, 'evidence_kind': 'report', 'scope': 'report_content'}


def report_evidence(engine, item: dict) -> dict:
    from .engine import digest, safe_text
    selection = engine._get('report_selection', item.get('selection_id', ''))
    if not selection:
        raise WorkflowError('pending_evidence', 'Explicitly select this report with select-report --path first')
    path = report_path(engine, selection['path'])
    if not path.is_file() or path.stat().st_size > 16000:
        raise WorkflowError('pending_evidence', 'Selected report unavailable or exceeds 16000 bytes')
    data = path.read_bytes()
    if digest(data) != selection['sha256']:
        raise WorkflowError('pending_evidence', 'Selected report changed; select the exact report again')
    excerpt = item.get('excerpt')
    if not isinstance(excerpt, str) or not excerpt or len(excerpt) > 2000:
        raise WorkflowError('pending_evidence', 'Report needs a short exact excerpt')
    safe_text(data.decode('utf-8'))
    if excerpt.encode() not in data:
        raise WorkflowError('pending_evidence', 'Report excerpt does not match selected source')
    return {'kind': 'report', **selection, 'excerpt': excerpt, 'scope': 'report_content'}
