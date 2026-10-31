#!/bin/sh
akf_plugin=${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}
if [ -z "$akf_plugin" ]; then
    printf '%s\n' '{}'
    exit 0
fi
AKF_BOOTSTRAP_QUIET=1 /bin/sh "$akf_plugin/scripts/python.sh" "$akf_plugin/scripts/plugin_entry.py"
