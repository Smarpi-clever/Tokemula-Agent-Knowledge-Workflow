#!/bin/sh
# Find a supported interpreter before loading any Python installation code.
for akf_python in "${AKF_PYTHON:-}" python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3; do
    # An explicit interpreter selection is authoritative, including a failed check.
    if [ -n "${AKF_PYTHON:-}" ] && [ "$akf_python" != "$AKF_PYTHON" ]; then
        break
    fi
    [ -n "$akf_python" ] || continue
    if command -v "$akf_python" >/dev/null 2>&1 && "$akf_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        exec "$akf_python" -B "$@"
    fi
done
if [ "${AKF_BOOTSTRAP_QUIET:-0}" = 1 ]; then
    printf '%s\n' '{}'
    exit 0
fi
printf '%s\n' '需要 Python 3.11 或更高版本。' '请从 https://www.python.org/downloads/macos/ 安装，然后重新双击 Install.command。' '原有配置保持完整。'
if [ -t 0 ]; then
    printf '%s' '按回车关闭。'
    read -r akf_answer
fi
exit 1
