#!/bin/sh
akf_package=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
/bin/sh "$akf_package/scripts/python.sh" "$akf_package/scripts/install.py" "$@"
akf_result=$?
if [ -t 0 ]; then
    printf '%s\n' '安装入口已结束。诊断、暂停、升级、卸载可使用安装结果中的 akf 路径。' '按回车关闭。'
    read -r akf_answer
fi
exit "$akf_result"
