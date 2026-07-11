#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/iKeilo/LdxpM.git"
INSTALL_DIR="${INSTALL_DIR:-/opt/ldxpm}"
SERVICE_NAME="ldxpm"
DEFAULT_PORT="8765"
DEFAULT_IMAGE_TAG="latest"
DEFAULT_ADMIN_USERNAME="admin"
DEFAULT_ADMIN_PASSWORD="admin"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}请使用 root 运行，或用 sudo 执行。${NC}"
    exit 1
  fi
}

detect_ip() {
  hostname -I 2>/dev/null | awk '{print $1}' || ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}' || echo "127.0.0.1"
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
  else
    echo ""
  fi
}

install_deps() {
  if ! command -v docker >/dev/null 2>&1; then
    echo -e "${YELLOW}正在安装 Docker...${NC}"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker || true
  fi
  if [ -z "$(compose_cmd)" ]; then
    echo -e "${YELLOW}正在安装 Docker Compose 插件...${NC}"
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update
      apt-get install -y docker-compose-plugin git curl
    else
      echo -e "${RED}未检测到 Docker Compose，请手动安装后重试。${NC}"
      exit 1
    fi
  fi
  if ! command -v git >/dev/null 2>&1; then
    apt-get update
    apt-get install -y git
  fi
}

write_env() {
  local port="$1"
  local admin_username="$2"
  local admin_password="$3"
  cat > "${INSTALL_DIR}/.env" <<EOF
PORT=${port}
IMAGE_TAG=${IMAGE_TAG:-${DEFAULT_IMAGE_TAG}}
ADMIN_USERNAME=${admin_username}
ADMIN_PASSWORD=${admin_password}
EOF
}

show_url() {
  local port
  port="$(grep '^PORT=' "${INSTALL_DIR}/.env" 2>/dev/null | cut -d= -f2 || echo "${DEFAULT_PORT}")"
  local ip
  ip="$(detect_ip)"
  echo
  echo -e "${GREEN}LdxpM 已就绪。${NC}"
  echo -e "本机地址:  http://127.0.0.1:${port}"
  echo -e "局域网地址: http://${ip}:${port}"
  echo
}

install_app() {
  need_root
  install_deps
  read -rp "请输入 Web 端口 [${DEFAULT_PORT}]: " port
  port="${port:-$DEFAULT_PORT}"
  read -rp "请输入管理员用户名 [${DEFAULT_ADMIN_USERNAME}]: " admin_username
  admin_username="${admin_username:-$DEFAULT_ADMIN_USERNAME}"
  read -rsp "请输入管理员密码 [${DEFAULT_ADMIN_PASSWORD}]: " admin_password
  echo
  admin_password="${admin_password:-$DEFAULT_ADMIN_PASSWORD}"

  if [ -d "${INSTALL_DIR}/.git" ]; then
    echo -e "${YELLOW}检测到已安装目录，正在更新代码...${NC}"
    git -C "${INSTALL_DIR}" pull --ff-only
  else
    mkdir -p "$(dirname "${INSTALL_DIR}")"
    git clone "${REPO_URL}" "${INSTALL_DIR}"
  fi

  mkdir -p "${INSTALL_DIR}/data"
  write_env "${port}" "${admin_username}" "${admin_password}"
  cd "${INSTALL_DIR}"
  $(compose_cmd) pull
  $(compose_cmd) up -d
  show_url
}

update_app() {
  need_root
  if [ ! -d "${INSTALL_DIR}/.git" ]; then
    echo -e "${RED}未找到安装目录：${INSTALL_DIR}${NC}"
    exit 1
  fi
  cd "${INSTALL_DIR}"
  echo -e "${YELLOW}正在从 GitHub 拉取最新版本...${NC}"
  git pull --ff-only
  $(compose_cmd) pull
  $(compose_cmd) up -d
  show_url
}

uninstall_app() {
  need_root
  if [ -d "${INSTALL_DIR}" ]; then
    cd "${INSTALL_DIR}"
    if [ -n "$(compose_cmd)" ]; then
      $(compose_cmd) down || true
    fi
  fi
  read -rp "是否删除数据目录 ${INSTALL_DIR}/data？输入 yes 确认: " confirm
  if [ "${confirm}" = "yes" ]; then
    rm -rf "${INSTALL_DIR}"
    echo -e "${GREEN}已卸载并删除数据。${NC}"
  else
    echo -e "${GREEN}已停止服务，保留安装目录和数据。${NC}"
  fi
}

status_app() {
  if [ -d "${INSTALL_DIR}" ] && [ -n "$(compose_cmd)" ]; then
    cd "${INSTALL_DIR}"
    $(compose_cmd) ps
    show_url
  else
    echo "尚未安装。"
  fi
}

menu() {
  clear || true
  echo "=============================="
  echo " LdxpM Docker 管理脚本"
  echo "=============================="
  echo "1) 安装"
  echo "2) 更新"
  echo "3) 卸载"
  echo "4) 状态"
  echo "0) 退出"
  echo
  read -rp "请选择: " choice
  case "${choice}" in
    1) install_app ;;
    2) update_app ;;
    3) uninstall_app ;;
    4) status_app ;;
    0) exit 0 ;;
    *) echo "无效选择"; exit 1 ;;
  esac
}

case "${1:-menu}" in
  install) install_app ;;
  update) update_app ;;
  uninstall) uninstall_app ;;
  status) status_app ;;
  menu|*) menu ;;
esac
