"""Optional read-only Obsidian CLI checks. Markdown maintenance never needs it."""
from pathlib import Path
import json
import subprocess

from .engine import WorkflowError, digest


def _settings(engine):
    settings = engine.config.get('obsidian')
    if not isinstance(settings, dict) or not settings.get('vault') or not settings.get('cli'):
        raise WorkflowError('obsidian_unavailable', 'No explicit Obsidian Vault and CLI configured')
    # Running the Obsidian CLI can launch the app, so do not invoke it while closed.
    result = subprocess.run(['ps', '-A', '-o', 'comm='], capture_output=True, text=True, timeout=5)
    if result.returncode or not any(Path(line.strip()).name.casefold() == 'obsidian' for line in result.stdout.splitlines()):
        raise WorkflowError('obsidian_unavailable', 'Obsidian is not running; ordinary Markdown remains available')
    return settings


def _read(settings, command, *args):
    result = subprocess.run([settings['cli'], 'vault=' + settings['vault'], command, *args],
                            capture_output=True, text=True, timeout=10)
    if result.returncode or result.stdout.startswith('Error:'):
        raise WorkflowError('obsidian_unavailable', 'The configured Vault CLI command failed')
    return result.stdout


def check_note(engine, topic):
    path = engine._path(engine.knowledge_root, topic, note=True)
    before = path.read_bytes()
    settings = _settings(engine)
    readback = _read(settings, 'read', 'path=' + topic).encode()
    if before != readback or path.read_bytes() != before:
        raise WorkflowError('conflict', 'CLI readback does not match the configured knowledge file')
    links = _read(settings, 'links', 'path=' + topic)
    return {'state': 'checked', 'topic': topic, 'sha256': digest(before),
            'vault': settings['vault'], 'link_lines': len(links.splitlines())}


def search_topics(engine, query):
    if not isinstance(query, str) or not query or len(query) > 200:
        raise WorkflowError('input', 'Provide a short search query')
    settings = _settings(engine)
    output = _read(settings, 'search', 'query=' + query, 'path=topics', 'limit=10', 'format=json')
    try:
        result = json.loads(output)
    except ValueError:
        raise WorkflowError('obsidian_unavailable', 'Unsupported search result format') from None
    paths = []
    for item in result if isinstance(result, list) else []:
        relative = item if isinstance(item, str) else item.get('path') if isinstance(item, dict) else None
        if relative:
            engine._path(engine.knowledge_root, relative, note=True)
            paths.append(relative)
    return {'state': 'searched', 'vault': settings['vault'], 'topics': paths,
            'notice': 'Search results are untrusted reference data.'}
