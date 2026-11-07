#!/usr/bin/env bash
# wb2api 一键安装脚本（Linux / macOS）
#
#   curl -fsSL https://raw.githubusercontent.com/Acr1mony/workbuddy2api/main/scripts/install.sh | bash
#
# 想改参数就把 env 写在前面（管道里也行）：
#   curl -fsSL ... | WB2A_PORT=9000 WB2A_SERVICE=1 bash
#
# 这个脚本只做四件事：把源码取下来、装进独立的 venv、把数据目录建好、
# 需要的话注册成 systemd 服务。它**不会**碰你机器上已有的 Python 环境。
set -euo pipefail

REPO="${WB2A_REPO:-Acr1mony/workbuddy2api}"
REF="${WB2A_REF:-main}"
PREFIX="${WB2A_DIR:-}"                 # 安装到哪里；留空则 ~/.wb2api（root 则 /opt/workbuddy2api）
DATA_DIR="${WB2A_DATA_DIR:-}"          # 数据目录；留空则 <PREFIX>/data
PORT="${WB2A_PORT:-8788}"
SERVICE="${WB2A_SERVICE:-auto}"        # auto | 1 | 0（auto = 是 root 且有 systemd 时装）
BIND="${WB2A_BIND:-127.0.0.1}"         # 监听地址；要对外暴露自己改成 0.0.0.0
TOKEN="${WB2A_TOKEN:-${GITHUB_TOKEN:-}}"  # 私有仓库才需要（fine-grained PAT，contents:read 就够）
TARBALL_URL="https://codeload.github.com/${REPO}/tar.gz/${REF}"
SRC="${WB2A_SRC:-}"                    # 本地源码目录（离线/自测用，给了就不下载）

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- 基本判断
case "$(uname -s)" in
  Linux|Darwin) ;;
  MINGW*|MSYS*|CYGWIN*)
    # Windows 上没有 systemd、venv 布局也不同：只有显式给了本地源码（自测/离线安装）
    # 才继续，否则指回 python -m wb2api serve —— 那条路是真的能跑。
    [ -n "${WB2A_SRC:-}" ] || die "这个脚本只支持 Linux 与 macOS；Windows 请用 python -m wb2api serve"
    warn "Windows 环境：只做安装流程验证（无 systemd）"
    ;;
  *) die "这个脚本只支持 Linux 与 macOS；Windows 请用 python -m wb2api serve" ;;
esac

if [ -z "$PREFIX" ]; then
  if [ "$(id -u)" = "0" ]; then PREFIX="/opt/workbuddy2api"; else PREFIX="$HOME/.wb2api"; fi
fi
[ -z "$DATA_DIR" ] && DATA_DIR="$PREFIX/data"

# 需要写系统目录时自动补 sudo（非 root 用户在 /opt 下安装的场景）
SUDO=""
if [ ! -w "$(dirname "$PREFIX")" ] && [ "$(id -u)" != "0" ]; then
  if have sudo; then SUDO="sudo"; else PREFIX="$HOME/.wb2api"; DATA_DIR="$PREFIX/data"; fi
fi

# ---------------------------------------------------------------- Python
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; }

PY=""
for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
  if have "$c" && py_ok "$c"; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  say "没找到 Python 3.9+，尝试用系统包管理器装一个"
  if have apt-get; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq python3 python3-venv python3-pip
  elif have dnf; then
    $SUDO dnf install -y -q python3 python3-pip
  elif have yum; then
    $SUDO yum install -y -q python3 python3-pip
  elif have apk; then
    $SUDO apk add --no-cache python3 py3-pip
  elif have pacman; then
    $SUDO pacman -Sy --noconfirm python
  elif have brew; then
    brew install python3
  else
    die "装不了 Python：请手动安装 python3.9+ 后重跑（Ubuntu: apt install python3 python3-venv）"
  fi
  for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
    if have "$c" && py_ok "$c"; then PY="$c"; break; fi
  done
  [ -n "$PY" ] || die "装完仍然没有可用的 python3.9+"
fi
say "使用 $($PY -V 2>&1)（$PY）"

# ---------------------------------------------------------------- 取源码
say "取源码 $REPO@$REF → $PREFIX"
$SUDO mkdir -p "$PREFIX"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fetch() {
  # 私有仓库要带 token；公开仓库两种方式都行，优先 tarball（不依赖 git）
  local url="$1" out="$2"
  if have curl; then
    if [ -n "$TOKEN" ]; then
      curl -fsSL -H "Authorization: Bearer $TOKEN" -o "$out" "$url"
    else
      curl -fsSL -o "$out" "$url"
    fi
  elif have wget; then
    if [ -n "$TOKEN" ]; then
      wget -q --header="Authorization: Bearer $TOKEN" -O "$out" "$url"
    else
      wget -q -O "$out" "$url"
    fi
  else
    die "需要 curl 或 wget"
  fi
}

if [ -n "$SRC" ]; then
  [ -d "$SRC" ] || die "WB2A_SRC=$SRC 不是目录"
  say "使用本地源码 $SRC"
  mkdir -p "$WORK/src"
  cp -a "$SRC/." "$WORK/src/"
  rm -rf "$WORK/src/.git" "$WORK/src/data" "$WORK/src/logs"
elif fetch "$TARBALL_URL" "$WORK/src.tar.gz" 2>/dev/null; then
  mkdir -p "$WORK/src"
  tar -xzf "$WORK/src.tar.gz" -C "$WORK/src" --strip-components=1
elif have git; then
  warn "下载源码包失败（私有仓库？），改用 git clone"
  if [ -n "$TOKEN" ]; then
    git clone -q --depth 1 --branch "$REF" "https://x-access-token:$TOKEN@github.com/${REPO}.git" "$WORK/src"
  else
    git clone -q --depth 1 --branch "$REF" "https://github.com/${REPO}.git" "$WORK/src"
  fi
else
  die "下载失败。私有仓库请设置 WB2A_TOKEN=<PAT> 再重跑"
fi

# 只替换代码，data 目录原样保留（升级不能动账号）
$SUDO mkdir -p "$PREFIX"
if have rsync; then
  $SUDO rsync -a --delete --exclude data --exclude logs --exclude .git "$WORK/src/" "$PREFIX/"
else
  $SUDO find "$PREFIX" -mindepth 1 -maxdepth 1 \
    ! -name data ! -name logs ! -name config.json -exec rm -rf {} +
  $SUDO cp -a "$WORK/src/." "$PREFIX/"
  $SUDO rm -rf "$PREFIX/data" "$PREFIX/logs" 2>/dev/null || true
fi
$SUDO mkdir -p "$DATA_DIR" "$DATA_DIR/logs"

# ---------------------------------------------------------------- 装依赖
VENV="$PREFIX/venv"
say "创建虚拟环境 $VENV"
if ! $SUDO "$PY" -m venv "$VENV" 2>/dev/null; then
  warn "创建 venv 失败，尝试补装 python3-venv"
  if have apt-get; then $SUDO apt-get install -y -qq python3-venv; fi
  $SUDO "$PY" -m venv "$VENV" || die "创建 venv 失败：请手动安装 python3-venv（或 -full）后重跑"
fi

# venv 里可执行文件的目录：Linux/macOS 是 bin，Windows 是 Scripts
V_BIN="$VENV/bin"
[ -d "$VENV/Scripts" ] && V_BIN="$VENV/Scripts"
say "安装 wb2api（零外部依赖，只是把自己装进去）"
$SUDO "$V_BIN/python" -m pip install -q --no-cache-dir --upgrade pip >/dev/null 2>&1 || true
# 从临时副本装，而不是从 $PREFIX 装：$PREFIX 里有 venv 和 data，
# 让构建后端去扫它们既慢又容易出意外。
$SUDO "$V_BIN/python" -m pip install -q --no-cache-dir "$WORK/src" || die "pip install 失败"

# ---------------------------------------------------------------- 服务
UNIT=""
if [ "$SERVICE" = "auto" ]; then
  if [ "$(id -u)" = "0" ] && have systemctl; then SERVICE=1; else SERVICE=0; fi
fi

if [ "$SERVICE" = "1" ]; then
  have systemctl || die "没有 systemctl，改 WB2A_SERVICE=0 手动起"
  UNIT=/etc/systemd/system/wb2api.service
  say "注册 systemd 服务 → $UNIT"
  $SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=wb2api gateway (OpenAI-compatible reverse proxy)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=WB_DATA_DIR=$DATA_DIR
ExecStart=$V_BIN/wb2a serve --host $BIND --port $PORT
Restart=always
RestartSec=5
# 只让它碰自己的数据目录
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable wb2api >/dev/null 2>&1 || true
  $SUDO systemctl restart wb2api
  sleep 2
  if $SUDO systemctl is-active --quiet wb2api; then
    say "服务已启动：systemctl status wb2api"
  else
    warn "服务没起来，看日志：journalctl -u wb2api -n 50 --no-pager"
  fi
else
  say "跳过 systemd（WB2A_SERVICE=$SERVICE）。手动启动："
  echo "    WB_DATA_DIR=$DATA_DIR $V_BIN/wb2a serve --host $BIND --port $PORT"
fi

# ---------------------------------------------------------------- 收尾提示
cat <<EOF

$(printf '\033[1;32m安装完成\033[0m')

  安装目录   $PREFIX
  数据目录   $DATA_DIR      （账号、密钥、用量台账、日志都在这里）
  命令       $V_BIN/wb2a
  面板       http://127.0.0.1:$PORT/panel/
  接口       http://127.0.0.1:$PORT/v1

接下来：

  1. 加账号（没有浏览器也行，它会打印授权链接）：
       $V_BIN/wb2a login

  2. 打开面板，把默认密码 admin 改掉，再建一个客户端 API key：
       http://127.0.0.1:$PORT/panel/

  3. 用 OpenAI SDK 指过来：
       base_url = "http://127.0.0.1:$PORT/v1"
       api_key  = "面板里建的那个 key"

数据目录里有 access/refresh token，别提交、别放公共目录。
EOF
