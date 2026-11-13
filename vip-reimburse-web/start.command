#!/bin/zsh
set -eu
cd "${0:A:h}"
VIP_BUNDLED_PYTHON='/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3'
if [[ -x .venv-paddle/bin/python && -f .runtime/paddle-ready.json ]]; then
  VIP_PYTHON="$PWD/.venv-paddle/bin/python"
elif [[ -x .venv/bin/python ]]; then
  VIP_PYTHON="$PWD/.venv/bin/python"
elif [[ -x "$VIP_BUNDLED_PYTHON" ]]; then
  VIP_PYTHON="$VIP_BUNDLED_PYTHON"
else
  VIP_PYTHON="$(command -v python3)"
fi
if ! "$VIP_PYTHON" -c 'import pypdf, PIL, openpyxl' 2>/dev/null; then
  print '请先安装基础依赖，操作方法见 README.md。'
  print 'python3 -m venv .venv'
  print '.venv/bin/python -m pip install -r requirements.txt'
  read -r '?按回车键退出'
  exit 1
fi
if [[ "$(uname -s)" == Darwin ]] && command -v swiftc >/dev/null; then
  mkdir -p .runtime/module-cache
  if [[ ! -x .runtime/vision-ocr || scripts/vision_ocr.swift -nt .runtime/vision-ocr ]]; then
    print '正在准备本机离线票据识别，首次启动可能需要一点时间…'
    if ! swiftc -module-cache-path .runtime/module-cache scripts/vision_ocr.swift -o .runtime/vision-ocr; then
      print '本机图片识别暂不可用；PDF 文字提取和手动填写仍可使用。'
    fi
  fi
fi
exec "$VIP_PYTHON" server.py "$@"
