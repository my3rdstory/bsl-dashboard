#!/usr/bin/env bash
set -o errexit

is_supported_python() {
  local bin="$1"
  local version
  version="$("$bin" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")"
  [ "$version" = "3.12" ] || [ "$version" = "3.13" ] || [ "$version" = "3.14" ]
}

candidates=(
  "${UV_BUILD_PYTHON:-}"
  "python${UV_BUILD_PYTHON:-}"
  "python3.13"
  "python3.12"
  "python3.14"
  "python3"
  "python"
)

PYTHON_BIN=""
for candidate in "${candidates[@]}"; do
  [ -z "$candidate" ] && continue
  if command -v "$candidate" >/dev/null 2>&1; then
    if is_supported_python "$candidate"; then
      PYTHON_BIN="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "오류: 지원 가능한 Python(3.12/3.13/3.14)이 환경에서 발견되지 않았습니다."
  echo "Render에서 Python 자동 설치가 불가하면 3.12/3.13/3.14 런타임 중 하나가 필요합니다."
  exit 1
fi

echo "Python target: $PYTHON_BIN"

uv sync --python "$PYTHON_BIN"
uv run --python "$PYTHON_BIN" python manage.py collectstatic --noinput
uv run --python "$PYTHON_BIN" python manage.py migrate --noinput

echo "Python 인터프리터: $($PYTHON_BIN --version 2>&1)"
