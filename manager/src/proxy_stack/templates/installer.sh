#!/usr/bin/env bash
# Generic proxy-stack V1 installer. Prompts deliberately bypass piped stdin.
set -euo pipefail
umask 077
tool_image=ghcr.io/silaswei/proxy-stack:V1
domain= node_name= directory= mode=auto
role= local_node=true join_code_file= join_code=
prepare_only=false
non_interactive=false
fail() { printf 'proxy-stack: %s\n' "$*" >&2; exit 1; }
usage() {
  printf '%s\n' 'Usage: bash installer.sh [--domain DOMAIN --name NAME] [--directory PATH]' \
    '  [--mode auto|standalone|existing-caddy] [--role hub|hub-only|node] [--join-code-file PATH] [--prepare-only] [--non-interactive]'
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --domain|--name|--directory|--mode|--role|--join-code-file)
      [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 requires a value"
      case "$1" in
        --domain) domain=$2 ;; --name) node_name=$2 ;;
        --directory) directory=$2 ;; --mode) mode=$2 ;;
        --role) role=$2 ;; --join-code-file) join_code_file=$2 ;;
      esac
      shift 2 ;;
    --prepare-only) prepare_only=true; shift ;;
    --non-interactive) non_interactive=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done
case "$mode" in auto|standalone|existing-caddy) ;; *) fail 'invalid --mode' ;; esac
command -v docker >/dev/null 2>&1 || fail '需要已安装 Docker Engine 和 Compose >= 2.24.4；请先安装后重试。'
docker info >/dev/null 2>&1 || fail 'Docker 不可用；请启动 Docker 并确认当前用户有访问权限。'
compose_version=$(docker compose version --short 2>/dev/null) || fail '需要 Docker Compose >= 2.24.4（docker compose）。'
compose_version=${compose_version#v}
if [[ "$compose_version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
  major=${BASH_REMATCH[1]}; minor=${BASH_REMATCH[2]}; patch=${BASH_REMATCH[3]}
  (( major > 2 || (major == 2 && (minor > 24 || (minor == 24 && patch >= 4))) )) || fail '需要 Docker Compose >= 2.24.4。'
else
  fail '无法确认 Docker Compose 版本；需要 >= 2.24.4。'
fi
if [ -z "$directory" ]; then
  if [ "$EUID" -eq 0 ]; then directory=/opt/proxy-stack; else directory=$HOME/proxy-stack; fi
fi
case "$directory" in /*) ;; *) directory=$PWD/$directory ;; esac
# Reject traversal before checking every path component. Never follow symlinks.
case "$directory/" in */../*|*/./*) fail '目录不能包含 . 或 .. 路径段；请提供直接路径。' ;; esac
[[ "$directory" != *$'\n'* && "$directory" != *$'\r'* ]] || fail 'invalid directory'
while [ "$directory" != / ] && [ "${directory%/}" != "$directory" ]; do directory=${directory%/}; done
component=$directory
while [ -n "$component" ]; do
  [ ! -L "$component" ] || fail '拒绝符号链接目录（包括父目录）。'
  component=${component%/*}
done
marker=.proxy-stack-managed
if [ -e "$directory/$marker" ] || [ -L "$directory/$marker" ]; then
  [ -f "$directory/$marker" ] && [ ! -L "$directory/$marker" ] && \
    [ "$(cat "$directory/$marker")" = 'proxy-stack-installer-v1' ] || fail 'invalid installer marker'
  for file in .env proxyctl compose.yaml compose.external.yaml; do
    [ -f "$directory/$file" ] && [ ! -L "$directory/$file" ] || fail "managed installation missing/unsafe $file; restore it from backup"
  done
  printf '已有受管安装：%s；保留全部配置、数据和凭据。\n' "$directory"
  printf '打开菜单：%q menu\n' "$directory/proxyctl"
  if $prepare_only; then exit 0; fi
  if ! $non_interactive && { exec 3<>/dev/tty; } 2>/dev/null; then
    exec "$directory/proxyctl" menu
  fi
  "$directory/proxyctl" links --labelled || "$directory/proxyctl" status
  exit $?
fi
if [ -e "$directory" ]; then
  [ -d "$directory" ] || fail '安装目标不是目录。'
  shopt -s nullglob dotglob
  entries=("$directory"/*)
  [ "${#entries[@]}" -eq 0 ] || fail '拒绝非空且未受管的目录；请选择新的空目录。'
  shopt -u nullglob dotglob
fi
# Refresh the mutable major tag once, before any helper or Compose manager runs.
# Repeated managed installs returned above deliberately do not pull or upgrade.
docker pull "$tool_image" || fail '无法拉取 V1 工具镜像；安装未写入，请稍后重试。'
prompt() {
  $non_interactive && fail "缺少 $1；非交互模式必须显式提供输入。"
  if ! { exec 3<>/dev/tty; } 2>/dev/null; then fail "缺少 $1；无终端请使用 --non-interactive 和显式参数。"; fi
  printf '%s' "$2" >&3
  IFS= read -r reply <&3 || fail '输入已结束。'
}
if [ -z "$role" ]; then
  if $non_interactive; then role=hub; else
    prompt --role '此服务器：1) 创建订阅中心并提供节点 2) 仅订阅中心 3) 加入已有中心 [1]: '
    case "${reply:-1}" in 1) role=hub ;; 2) role=hub-only ;; 3) role=node ;; *) fail '请选择 1、2 或 3。' ;; esac
  fi
fi
case "$role" in hub) ;; hub-only) role=hub; local_node=false ;; node) ;; *) fail 'invalid --role' ;; esac
if [ "$role" = node ]; then
  if [ -n "$join_code_file" ]; then
    [ -f "$join_code_file" ] && [ ! -L "$join_code_file" ] || fail 'unsafe join code file'
    IFS= read -r join_code < "$join_code_file" || [ -n "$join_code" ] || fail 'empty join code'
  else
    $non_interactive && fail '加入节点需 --join-code-file；密钥不会放入命令参数或 .env。'
    { exec 3<>/dev/tty; } 2>/dev/null || fail '加入码需要终端或 --join-code-file'
    printf '粘贴限时加入码（输入隐藏）: ' >&3
    trap 'stty echo <&3' EXIT HUP INT TERM
    stty -echo <&3
    IFS= read -r join_code <&3 || { stty echo <&3; fail '输入已结束。'; }
    stty echo <&3
    trap - EXIT HUP INT TERM
    printf '\n' >&3
  fi
fi
if [ -z "$domain" ]; then
  detected_ip=$(docker run --rm --read-only --cap-drop=ALL --network bridge "$tool_image" detect-public-ip) || detected_ip=
  if $non_interactive; then
    [ -n "$detected_ip" ] || fail '无法检测公网 IP；请使用 --domain 指定域名或公网 IP。'
    domain=$detected_ip
  else
    prompt --domain "代理域名或公网 IP（回车使用 ${detected_ip:-探测失败，请显式输入}）: "
    domain=${reply:-$detected_ip}
    [ -n "$domain" ] || fail '无法检测公网 IP；请使用 --domain 指定域名或公网 IP。'
  fi
fi
if [ -z "$node_name" ]; then
  # Run hostname on the HOST, then sanitize inside the tool. Never use the
  # ephemeral container's hostname, and never source/eval a derived value.
  host_node_name=$(hostname 2>/dev/null) || host_node_name=
  host_node_name=${host_node_name:0:1024}
  default_name=$(docker run --rm --read-only --cap-drop=ALL --network none \
    -e "HOST_NODE_NAME=$host_node_name" "$tool_image" default-node-name) || fail '无法生成默认节点名称。'
  if $non_interactive; then
    node_name=$default_name
  else
    prompt --name "节点名称（回车使用 $default_name）: "
    node_name=${reply:-$default_name}
  fi
fi
# The existing Python model is authoritative; the host needs neither Python nor git.
domain=$(printf '%s' "$domain" | tr '[:upper:]' '[:lower:]')
docker run --rm --read-only --cap-drop=ALL --network none \
  -e "DOMAIN=$domain" -e "NODE_NAME=$node_name" "$tool_image" validate-input || fail '域名、公网 IP 或节点名称无效；请修正输入后重试。'

# Ask the Docker daemon to reserve each intended host binding briefly. This also
# detects host listeners without requiring ss/lsof/Python or mounting docker.sock.
probe_name=proxy-stack-port-check-$$-$RANDOM
cleanup_probe() { docker rm -f "$probe_name" >/dev/null 2>&1 || :; }
trap cleanup_probe EXIT
port_available() {
  local binding=$1
  if docker run --rm -d --name "$probe_name" --read-only --cap-drop=ALL --network bridge \
    -p "$binding:8080" --entrypoint python "$tool_image" -c 'import time; time.sleep(60)' >/dev/null 2>&1; then
    cleanup_probe
    return 0
  fi
  cleanup_probe
  return 1
}
if [ "$mode" != existing-caddy ]; then
  if port_available 80 && port_available 443; then
    mode=standalone
  elif [ "$mode" = standalone ]; then
    fail '无法绑定 80/443（端口占用或 Docker 绑定失败）；不会停止现有服务。可使用 --mode existing-caddy。'
  else
    printf '80/443 无法绑定；不会停止或覆盖现有服务。可使用已有反代模式（回环 18090）。\n'
    prompt --mode '使用已有反代模式？需手动为该地址配置 HTTPS 路由。[y/N]: '
    case "$reply" in y|Y) mode=existing-caddy ;; *) fail '已取消；可使用 --mode existing-caddy 重试。' ;; esac
  fi
fi
if [ "$mode" = existing-caddy ]; then
  port_available 127.0.0.1:18090 || fail '无法绑定回环网关 127.0.0.1:18090；请先处理占用或 Docker 绑定错误。'
fi
trap - EXIT
mkdir -p -- "$directory"
chmod 700 "$directory"
cd -- "$directory"
directory=$PWD
# EXPORT_TEMPLATES
chmod 700 proxyctl
{
  while IFS= read -r line; do
    case "$line" in DOMAIN=*|NODE_NAME=*|MODE=*|ROLE=*|LOCAL_NODE=*) ;; *) printf '%s\n' "$line" ;; esac
  done < .env.example
  # Model validation excludes quotes, expansion characters and newlines.
  printf "DOMAIN='%s'\nNODE_NAME='%s'\nMODE='%s'\n" "$domain" "$node_name" "$mode"
  printf "ROLE='%s'\nLOCAL_NODE=%s\n" "$role" "$local_node"
  # Distinct installations must never adopt another directory's Compose services.
  printf 'COMPOSE_PROJECT_NAME=proxy-stack-%s-%s-%s\n' "$$" "$RANDOM" "$RANDOM"
  if [ "$mode" = existing-caddy ]; then printf 'COMPOSE_FILE=compose.yaml:compose.external.yaml\n'; fi
} > .env
printf 'proxy-stack-installer-v1\n' > "$marker"
./proxyctl init
# Capture intentional operator disclosure before updater obtains its refresh lock.
links=
if [ "$role" = node ]; then
  if ! printf '%s\n' "$join_code" | ./proxyctl join; then
    printf '加入未完成，当前安装和配置已保留。请在中心重新生成加入码，然后执行：\n' >&2
    printf '  %q node join\n  %q up\n' "$directory/proxyctl" "$directory/proxyctl" >&2
    exit 1
  fi
  unset join_code
else
  links=$(./proxyctl links --labelled)
fi
if $prepare_only; then
  printf '初始化完成；--prepare-only 未启动服务。\n'
  printf '启动服务：%q up\n' "$directory/proxyctl"
else
  ./proxyctl up
  printf '已提交 Docker 启动；这不代表公网可达或证书签发成功。\n'
fi
if [ "$role" = hub ]; then
  printf '\nowner 私密订阅（请勿公开）：\n%s\n' "$links"
  printf '其他服务器加入：在本机执行 proxyctl node code，再在其他服务器的向导选择加入已有中心。\n'
else
  printf '节点已加入；统一订阅链接请从中心获取。中心 node list 可查看实际同步状态。\n'
fi
if [ "$mode" = existing-caddy ]; then
  printf '待处理：审查 %s/data/runtime/host-caddy.snippet，将地址 HTTPS 路由指向 127.0.0.1:18090；公共链接在配置完成前不可用。\n' "$directory"
fi
case "$domain" in
  *:*) ip_mode=true ;;
  *[!0-9.]* ) ip_mode=false ;;
  *) ip_mode=true ;;
esac
if $ip_mode; then
  printf 'TLS 尚未验证：公网 IP 证书签发待完成（并非就绪），无需域名/DNS；公网 80/443 必须可达且持续支持自动续期。使用 Let’s Encrypt shortlived（约160小时）证书；已有反代需 Caddy >= 2.11 并合并生成的 TLS/global 配置。\n'
else
  printf 'TLS 尚未验证：确认域名 A/AAAA 指向本机，公网 80/443 可达且 CAA 允许 ACME。\n'
fi
printf '规则初始为 minimal-bootstrap；updater 将刷新完整规则。稍后在菜单查看状态，rules=upstream 后再日常使用。\n'
printf '路由、DNS 和证书就绪后可执行：%q verify\n' "$directory/proxyctl"
printf '打开中文管理菜单：%q menu\n' "$directory/proxyctl"
