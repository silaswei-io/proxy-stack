#!/usr/bin/env bash
# proxy-stack v1.0.0; prompts use /dev/tty, never the piped script's stdin.
set -euo pipefail
umask 077
tool_image=ghcr.io/silaswei-io/proxy-stack:v1.0.0
caddy_image=caddy:2.11.4@sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52
domain= node_name= directory= port= mode=auto role= local_node=true
join_code_file= join_code= prepare_only=false non_interactive=false check_only=false
manage_host=false repeat=false host_route=false http_port=80 http_bind=0.0.0.0
committed=false project= phase=preflight recover_only=false reconfigure=false namespace_change=false
fail() { printf 'proxy-stack: %s\n' "$*" >&2; exit 1; }
usage() {
  printf '%s\n' 'Usage: bash installer.sh [--domain DOMAIN --name NAME --port PORT] [--directory PATH]' \
    '  [--role hub|hub-only|node] [--join-code-file PATH] [--mode auto|standalone|existing-caddy]' \
    '  [--check|--plan|--recover] [--prepare-only] [--non-interactive] [--manage-host-caddy]'
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --domain|--name|--directory|--port|--mode|--role|--join-code-file)
      [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 requires a value"
      case "$1" in
        --domain) domain=$2 ;; --name) node_name=$2 ;; --directory) directory=$2 ;;
        --port) port=$2 ;; --mode) mode=$2 ;; --role) role=$2 ;; --join-code-file) join_code_file=$2 ;;
      esac
      shift 2 ;;
    --prepare-only) prepare_only=true; shift ;;
    --non-interactive) non_interactive=true; shift ;;
    --check|--plan) check_only=true; shift ;;
    --recover) recover_only=true; shift ;;
    --manage-host-caddy) manage_host=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done
requested_domain=$domain requested_name=$node_name requested_port=$port requested_mode=$mode requested_role=$role
case "$mode" in auto|standalone|existing-caddy) ;; *) fail 'invalid --mode' ;; esac
valid_port() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 )); }
if [ -n "$port" ]; then valid_port "$port" || fail '端口需为 1–65535 的数字。'; port=$((10#$port)); fi
# Installer settings never inherit an unrelated shell's Compose/runtime identity.
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE
unset DOMAIN NODE_NAME HOST_NODE_NAME MODE PUBLIC_PORT HTTP_PORT HTTP_BIND HTTP01_ONLY CONTACT_EMAIL UPDATE_INTERVAL
unset TOOL_IMAGE CADDY_IMAGE ROLE LOCAL_NODE LEASE_SECONDS
[ "$(uname -s)" = Linux ] || fail '安装器仅支持 Linux 和本机 rootful Docker（不支持 Docker Desktop 端口预检）。'
for required in docker timeout sha256sum; do command -v "$required" >/dev/null 2>&1 || fail "需要 $required（timeout/sha256sum 由 GNU coreutils 提供）。"; done
timeout 15 docker info >/dev/null 2>&1 || fail 'Docker 不可用；请启动 Docker 并确认当前用户有访问权限。'
endpoint=$(timeout 10 docker context inspect --format '{{.Endpoints.docker.Host}}') || fail '无法确认 Docker endpoint。'
case "$endpoint" in unix://*) ;; *) fail '安装器只支持本机 Unix socket Docker。' ;; esac
security=$(timeout 10 docker info --format '{{json .SecurityOptions}}') || fail '无法确认 Docker 权限。'
case "$security" in *rootless*) fail '安装器需要 rootful Docker。' ;; esac
compose_version=$(timeout 10 docker compose version --short 2>/dev/null) || fail '需要 Docker Compose >= 2.24.4。'
compose_version=${compose_version#v}
if [[ "$compose_version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
  major=${BASH_REMATCH[1]}; minor=${BASH_REMATCH[2]}; patch=${BASH_REMATCH[3]}
  (( major > 2 || (major == 2 && (minor > 24 || (minor == 24 && patch >= 4))) )) || fail '需要 Docker Compose >= 2.24.4。'
else fail '无法确认 Docker Compose 版本；需要 >= 2.24.4。'; fi
if [ -z "$directory" ]; then
  if [ "$EUID" -eq 0 ]; then directory=/opt/proxy-stack; else directory=$HOME/proxy-stack; fi
fi
case "$directory" in /*) ;; *) directory=$PWD/$directory ;; esac
case "$directory/" in */../*|*/./*) fail '目录不能包含 . 或 .. 路径段。' ;; esac
[[ "$directory" != *$'\n'* && "$directory" != *$'\r'* ]] || fail 'invalid directory'
while [ "$directory" != / ] && [ "${directory%/}" != "$directory" ]; do directory=${directory%/}; done
[ "$directory" != / ] || fail '不能安装到根目录。'
component=$directory
while [ -n "$component" ]; do
  [ ! -L "$component" ] || fail '拒绝符号链接目录（包括父目录）。'
  component=${component%/*}
done
marker=.proxy-stack-managed
# EXPORT_HOST_HELPER
# Normal reruns discover interrupted journals. Never execute a user-writable or
# symlinked recovery program, and never recover another live installer.
recover_previous() {
  local previous owner_pid owner_start current_start permissions file
  [ -d "$directory.install-lock" ] && [ ! -L "$directory.install-lock" ] || fail '安装锁不安全。'
  file="$directory.install-lock/owner"
  [ -f "$file" ] && [ ! -L "$file" ] && [ -O "$file" ] || fail '安装锁缺少安全的恢复记录。'
  IFS= read -r previous < "$file" || fail '恢复记录为空。'
  case "$previous" in /*/proxy-stack-transaction.*) ;; *) fail '恢复路径无效。' ;; esac
  for file in "$previous" "$previous/recover.sh" "$previous/pid"; do
    [ -e "$file" ] && [ ! -L "$file" ] && [ -O "$file" ] || fail '恢复文件不安全。'
    permissions=$(stat -c '%a' "$file" 2>/dev/null) || permissions=$(stat -f '%Lp' "$file")
    case "$permissions" in 700|600) ;; *) fail '恢复文件权限不安全。' ;; esac
  done
  owner_pid=$(head -n 1 "$previous/pid")
  [[ "$owner_pid" =~ ^[0-9]+$ ]] || fail '恢复进程记录无效。'
  if kill -0 "$owner_pid" 2>/dev/null; then
    owner_start=$(tail -n 1 "$previous/pid")
    current_start=$(awk '{print $22}' "/proc/$owner_pid/stat" 2>/dev/null) || current_start=unknown
    [ "$current_start" != "$owner_start" ] || fail '另一个安装事务仍在运行。'
  fi
  if $check_only; then fail '检测到未完成事务；请先用相同 --directory 和 --recover 恢复（本次检查未写入）。'; fi
  # Only known packaged bytes may be migrated. Keep the original helper intact
  # for audit/retry, and never execute a helper supplied through a new path.
  file="$previous/host_setup.py"
  [ -f "$file" ] && [ ! -L "$file" ] && [ -O "$file" ] && [ "$(stat -c '%a' "$file")" = 600 ] && [ "$(stat -c '%h' "$file")" = 1 ] || fail '恢复 helper 不安全。'
  [ ! -L "$previous/operator.log" ] || fail '恢复日志不安全。'
  local helper_hash current_hash backup temporary
  helper_hash=$(sha256sum "$file" | cut -d' ' -f1)
  current_hash=$(emit_host_helper | sha256sum | cut -d' ' -f1)
  case "$helper_hash" in
    "$current_hash") ;;
    4e58b55786b068d5f664d6354b9b9a1219514f449a81d71c64aacbdfb31cbaee)
      backup="$previous/host_setup.611082f.original.py"
      if [ -e "$backup" ] || [ -L "$backup" ]; then
        [ -f "$backup" ] && [ ! -L "$backup" ] && [ -O "$backup" ] && [ "$(stat -c '%a' "$backup")" = 600 ] && [ "$(stat -c '%h' "$backup")" = 1 ] && cmp -s "$file" "$backup" || fail '旧 helper 备份冲突；保留原文件。'
      else
        (set -o noclobber; cat "$file" > "$backup") || fail '无法保留旧 helper。'
      fi
      temporary=$(mktemp "$previous/.host-helper.XXXXXXXX") || fail '无法准备恢复 helper。'
      emit_host_helper > "$temporary"
      mv -- "$temporary" "$file"
      ;;
    *) fail '未知恢复 helper；未覆盖或执行，保留备份供人工审查。' ;;
  esac
  printf '正在恢复上次未完成事务…\n'
  if ! bash "$previous/recover.sh" >>"$previous/operator.log" 2>&1; then
    fail "恢复尚未完成；保留私有状态和锁。日志：$previous/operator.log；重试 --recover。"
  fi
  printf '上次事务已恢复；记录：%s\n' "$previous"
}
if [ -e "$directory.install-lock" ] || [ -L "$directory.install-lock" ]; then
  recover_previous
elif $recover_only; then
  printf '没有待恢复事务。\n'
fi
if $recover_only; then exit 0; fi
if [ -e "$directory/$marker" ] || [ -L "$directory/$marker" ]; then
  [ -f "$directory/$marker" ] && [ ! -L "$directory/$marker" ] && [ "$(cat "$directory/$marker")" = proxy-stack-installer-v1 ] || fail 'invalid installer marker'
  for file in .env proxyctl compose.yaml compose.external.yaml; do
    [ -f "$directory/$file" ] && [ ! -L "$directory/$file" ] || fail "managed installation missing/unsafe $file; restore it from backup"
  done
  repeat=true
  # Read only known literal fields, never source/eval user configuration.
  while IFS='=' read -r key value; do
    value=${value#\'}; value=${value%\'}
    case "$key" in
      TOOL_IMAGE) case "$value" in "$tool_image"|ghcr.io/silaswei/proxy-stack:v1.0.0) ;; *) fail '自定义工具镜像需手动迁移；原安装未改变。' ;; esac ;;
      DOMAIN) domain=$value ;; NODE_NAME) node_name=$value ;; PUBLIC_PORT) port=$value ;;
      MODE) mode=$value ;; ROLE) role=$value ;; LOCAL_NODE) local_node=$value ;;
      HTTP_PORT) http_port=$value ;; HTTP_BIND) http_bind=$value ;; COMPOSE_PROJECT_NAME) project=$value ;;
    esac
  done < "$directory/.env"
  installed_image=$(cd -- "$directory" && timeout 10 docker compose config --images manager) || fail '无法验证受管工具镜像。'
  case "$installed_image" in
    "$tool_image") ;;
    ghcr.io/silaswei/proxy-stack:v1.0.0) namespace_change=true ;;
    *) fail '自定义工具镜像需手动迁移；原安装未改变。' ;;
  esac
  previous_mode=$mode
  [ -z "$requested_domain" ] || domain=$requested_domain
  [ -z "$requested_name" ] || node_name=$requested_name
  [ -z "$requested_port" ] || port=$requested_port
  # Roles are identities, never silently reset a hub/node on reconfiguration.
  expected_role=$role; if [ "$role" = hub ] && [ "$local_node" = false ]; then expected_role=hub-only; fi
  [ -z "$requested_role" ] || [ "$requested_role" = "$expected_role" ] || fail '受管安装不支持更改角色；原身份保留。'
  mode=$requested_mode
  # A pending expert install is retried using stack HTTPS, not an endless menu.
  if [ "$previous_mode" = existing-caddy ] && [ "$mode" != existing-caddy ] && [ -z "$requested_port" ]; then port=; fi
  valid_port "${port:-443}" && valid_port "$http_port" || fail '受管端口配置无效；保留原文件。'
  [[ "$project" =~ ^proxy-stack-[a-zA-Z0-9_-]+$ ]] || fail '受管 Compose 标识无效。'
  printf '已有受管 v1.0.0；预检重试/重配，保留身份。\n'
  if $prepare_only && ! $namespace_change && ! $check_only && [ -z "$requested_domain$requested_name$requested_port$requested_role" ] && [ "$requested_mode" = auto ]; then printf '菜单：%q menu\n' "$directory/proxyctl"; exit 0; fi
elif [ -e "$directory" ]; then
  [ -d "$directory" ] || fail '安装目标不是目录。'
  shopt -s nullglob dotglob; entries=("$directory"/*); shopt -u nullglob dotglob
  [ "${#entries[@]}" -eq 0 ] || fail '拒绝非空且未受管的目录；请选择新的空目录。'
fi
parent=$directory
while [ ! -d "$parent" ]; do parent=${parent%/*}; [ -n "$parent" ] || parent=/; done
[ -w "$parent" ] || fail '目标父目录不可写。'
available_kb=$(df -Pk "$parent" | awk 'END {print $4}')
[[ "$available_kb" =~ ^[0-9]+$ ]] && (( available_kb >= 262144 )) || fail '目标文件系统可用空间不足 256 MiB。'
stage=$(mktemp -d "${TMPDIR:-/var/tmp}/proxy-stack-transaction.XXXXXXXX") || fail '无法创建私有事务目录。'
stage=$(CDPATH= cd -P -- "$stage" && pwd)
operator_log=$stage/operator.log
: > "$operator_log"; chmod 600 "$operator_log"
quiet() {
  if "$@" >>"$operator_log" 2>&1; then return 0; fi
  printf '操作失败；诊断日志：%s\n' "$operator_log" >&2
  return 1
}
# Named, short-lived helpers check collisions and the public-address roundtrip.
probe_name=proxy-stack-port-check-$$-$RANDOM
roundtrip_listener=proxy-stack-roundtrip-listener-$$-$RANDOM
roundtrip_probe=proxy-stack-roundtrip-probe-$$-$RANDOM
cleanup_roundtrip() { timeout 5 docker rm -f "$roundtrip_probe" "$roundtrip_listener" >/dev/null 2>&1 || :; }
cleanup_port_probe() { timeout 5 docker rm -f "$probe_name" >/dev/null 2>&1 || :; }
cleanup_probe() { cleanup_port_probe; cleanup_roundtrip; }
trap cleanup_probe EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
port_available() {
  if timeout 10 docker run --rm -d --name "$probe_name" --read-only --cap-drop=ALL --network bridge \
    -p "$1:8080" --entrypoint python "$tool_image" -c 'import time; time.sleep(60)' >/dev/null 2>&1; then
    cleanup_port_probe; return 0
  fi
  cleanup_port_probe; return 1
}
random_port() {
  local attempts candidate
  for ((attempts=0; attempts<24; attempts++)); do
    candidate=$((20000 + (RANDOM * 2 + RANDOM % 2) % 40000))
    if [ "$candidate" != "${port:-}" ] && port_available "${1:-}$candidate"; then reply=$candidate; return 0; fi
  done
  fail '24 次探测未找到空闲端口；请指定 --port。'
}
owns_binding() {
  $repeat || return 1
  local cid identity bindings binding expected_ip=$2
  expected_ip=${expected_ip#\[}; expected_ip=${expected_ip%\]}
  cid=$(cd -- "$directory" && timeout 10 docker compose ps -q caddy) || return 1
  [[ "$cid" =~ ^[a-f0-9]{12,64}$ ]] || return 1
  identity=$(timeout 10 docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{.State.Running}}' "$cid") || return 1
  [ "$identity" = "$project|caddy|$directory|true" ] || return 1
  bindings=$(timeout 10 docker inspect --format '{{range $p, $bs := .NetworkSettings.Ports}}{{range $bs}}{{printf "%s %s %s\n" $p .HostIp .HostPort}}{{end}}{{end}}' "$cid") || return 1
  while IFS= read -r binding; do
    [ "$binding" != "$1/tcp $expected_ip $3" ] || return 0
  done <<< "$bindings"
  return 1
}
public_roundtrip() {
  local nonce result=0
  nonce=$(head -c 32 /dev/urandom | sha256sum | cut -d' ' -f1)
  printf '验证所选 HTTPS 端口的公共地址回环（最多 30 秒；不代表外部入站可达）…\n'
  if ! timeout 10 docker run --rm -d --name "$roundtrip_listener" --read-only --cap-drop=ALL --network bridge \
    -p "$port:8080" -e "ROUNDTRIP_NONCE=$nonce" --entrypoint python "$tool_image" -c '
import http.server, os, signal, time
signal.signal(signal.SIGALRM, signal.SIG_DFL)
signal.alarm(30)  # Absolute lifetime, including trickled request headers.
class Server(http.server.HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = os.environ["ROUNDTRIP_NONCE"].encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass
server = Server(("0.0.0.0", 8080), Handler)
server.timeout = 1
until = time.monotonic() + 30
while time.monotonic() < until: server.handle_request()
server.server_close()
' >>"$operator_log" 2>&1; then result=1
  elif ! timeout 15 docker run --rm --name "$roundtrip_probe" --read-only --cap-drop=ALL --network bridge \
    -e "ROUNDTRIP_NONCE=$nonce" -e "ROUNDTRIP_HOST=$domain" -e "ROUNDTRIP_PORT=$port" --entrypoint python "$tool_image" -c '
import os, signal, time, urllib.request
signal.signal(signal.SIGALRM, signal.SIG_DFL)
signal.alarm(12)  # Includes DNS and reads; independent of the Docker CLI.
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args): return None
host = os.environ["ROUNDTRIP_HOST"]
if ":" in host: host = "[" + host + "]"
url = "http://" + host + ":" + os.environ["ROUNDTRIP_PORT"] + "/"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
for attempt in range(3):
    try:
        with opener.open(url, timeout=2) as response:
            if response.status == 200 and response.read(65) == os.environ["ROUNDTRIP_NONCE"].encode():
                raise SystemExit(0)
    except Exception: pass
    time.sleep(1)
raise SystemExit(1)
' >>"$operator_log" 2>&1; then result=1; fi
  cleanup_roundtrip
  return "$result"
}
prompt() {
  $non_interactive && fail "缺少 $1；无交互模式请提供显式参数。"
  if ! { exec 3<>/dev/tty; } 2>/dev/null; then fail "缺少 $1；无终端请使用 --non-interactive 和显式参数。"; fi
  printf '%s' "$2" >&3
  IFS= read -r reply <&3 || fail '输入已结束。'
}
if ! $repeat || $namespace_change; then
  printf '正在预检 v1.0.0…\n'
  printf '准备工具镜像（最多 120 秒）…\n'
  quiet timeout 120 docker pull "$tool_image" || fail '无法拉取 v1.0.0；安装目录未写入。'
fi
if [ -z "$role" ]; then
  if $non_interactive; then role=hub; else
    prompt --role $'此服务器：\n  1) 中心 + 本机节点（默认）\n  2) 仅订阅中心（节点在其他服务器）\n  3) 加入已有中心（仅运行节点）\n选择 [1]: '
    case "${reply:-1}" in 1) role=hub ;; 2) role=hub-only ;; 3) role=node ;; *) fail '请选择 1、2 或 3。' ;; esac
  fi
fi
case "$role" in hub) ;; hub-only) role=hub; local_node=false ;; node) ;; *) fail 'invalid --role' ;; esac
if [ -z "$domain" ]; then
  detected_ip=$(timeout 15 docker run --rm --read-only --cap-drop=ALL --network bridge "$tool_image" detect-public-ip) || detected_ip=
  if $non_interactive; then
    [ -n "$detected_ip" ] || fail '无法检测公网 IP；请使用 --domain。'; domain=$detected_ip
  else
    prompt --domain "代理域名或公网 IP（回车使用 ${detected_ip:-探测失败，请显式输入}）: "
    domain=${reply:-$detected_ip}; [ -n "$domain" ] || fail '请使用 --domain 指定地址。'
  fi
fi
if [ -z "$node_name" ]; then
  host_node_name=$(hostname 2>/dev/null) || host_node_name=
  host_node_name=${host_node_name:0:1024}
  default_name=$(timeout 10 docker run --rm --read-only --cap-drop=ALL --network none -e "HOST_NODE_NAME=$host_node_name" "$tool_image" default-node-name) || fail '无法生成默认节点名称。'
  if $non_interactive; then node_name=$default_name; else
    prompt --name "节点名称（回车使用 $default_name）: "; node_name=${reply:-$default_name}
  fi
fi
if [ -z "$port" ]; then
  random_port
  selected=$reply
  if $non_interactive; then port=$selected; else
    prompt --port "公共 HTTPS 端口（回车使用空闲随机端口 $selected）: "
    port=${reply:-$selected}
  fi
fi
valid_port "$port" || fail '端口需为 1–65535 的数字。'; port=$((10#$port))
[ "$port" -ne 80 ] || fail '公共 HTTPS 端口不能使用 HTTP-01 所需的 80。'
domain=$(printf '%s' "$domain" | tr '[:upper:]' '[:lower:]')
domain=$(timeout 15 docker run --rm --read-only --cap-drop=ALL --network none -e "DOMAIN=$domain" -e "NODE_NAME=$node_name" -e "PUBLIC_PORT=$port" "$tool_image" validate-input 2>>"$operator_log") || fail '域名、公网 IP、名称或端口无效；安装目录未写入。'
quiet timeout 20 docker run --rm --read-only --cap-drop=ALL --network bridge -e "DOMAIN=$domain" -e "NODE_NAME=$node_name" "$tool_image" check-address || fail '地址映射预检失败；安装目录未写入。'
if port_available "$port"; then
  if ! $prepare_only; then public_roundtrip || fail "所选 HTTPS 端口 $port 的公共地址回环失败；检查防火墙/云安全组、NAT/回环支持。原安装和宿主配置未改变。日志：$operator_log"; fi
elif owns_binding "$port" 0.0.0.0 "$port" || owns_binding "$port" :: "$port"; then
  printf '精确受管 Caddy 已占用所选端口；跳过临时监听，仍须完成 HTTPS 验证。\n'
else fail "公共 HTTPS 端口 $port 已占用；安装目录未写入。"; fi
if [ "$mode" = existing-caddy ]; then
  $prepare_only || fail 'existing-caddy 仅供专家 --prepare-only；正常安装请使用 auto。'
  port_available 127.0.0.1:18090 || owns_binding 8080 127.0.0.1 18090 || fail '回环 18090 已占用；安装目录未写入。'
else
  mode=standalone
  if $repeat && [ "$http_bind" = 127.0.0.1 ]; then
    host_route=true
    port_available "127.0.0.1:$http_port" || owns_binding 80 127.0.0.1 "$http_port" || fail '回环 HTTP-01 端口已被其他服务占用。'
  elif port_available 80 || owns_binding 80 "$http_bind" 80; then
    http_port=80
    case "$domain" in *:*) http_bind='[::]' ;; *) http_bind=0.0.0.0 ;; esac
  else
    host_route=true; http_bind=127.0.0.1
    random_port 127.0.0.1:; http_port=$reply
  fi
fi
mkdir "$stage/install"
cd -- "$stage/install"
# EXPORT_TEMPLATES
emit_host_helper > host_setup.py
chmod 700 proxyctl
if $repeat; then
  mkdir "$stage/original-install"
  for file in .env .env.example proxyctl compose.yaml compose.external.yaml; do
    [ ! -L "$directory/$file" ] || fail '受管文件含符号链接。'
    if [ -f "$directory/$file" ]; then cp -p -- "$directory/$file" "$stage/original-install/$file"; fi
  done
  env_source=$stage/original-install/.env
else
  project=proxy-stack-$$-$RANDOM-$RANDOM
  env_source=.env.example
fi
{
  while IFS= read -r line; do
    case "$line" in
      TOOL_IMAGE=*)
        value=${line#TOOL_IMAGE=}; value=${value#\'}; value=${value%\'}
        case "$value" in "$tool_image"|ghcr.io/silaswei/proxy-stack:v1.0.0) printf 'TOOL_IMAGE=%s\n' "$tool_image" ;; *) fail '自定义工具镜像需手动迁移。' ;; esac
        ;;
      DOMAIN=*|NODE_NAME=*|MODE=*|ROLE=*|LOCAL_NODE=*|PUBLIC_PORT=*|HTTP_PORT=*|HTTP_BIND=*|HTTP01_ONLY=*|COMPOSE_PROJECT_NAME=*|COMPOSE_FILE=*) ;; *) printf '%s\n' "$line" ;; esac
  done < "$env_source"
  printf "DOMAIN='%s'\nNODE_NAME='%s'\nMODE='%s'\nPUBLIC_PORT=%s\n" "$domain" "$node_name" "$mode" "$port"
  printf 'HTTP_BIND=%s\nHTTP_PORT=%s\nHTTP01_ONLY=true\nROLE=%s\nLOCAL_NODE=%s\nCOMPOSE_PROJECT_NAME=%s\n' "$http_bind" "$http_port" "$role" "$local_node" "$project"
  if [ "$mode" = existing-caddy ]; then printf 'COMPOSE_FILE=compose.yaml:compose.external.yaml\n'; fi
} > .env
# Avoid needless rewriting of an identical managed dotenv.
if $repeat; then
  for file in .env .env.example proxyctl compose.yaml compose.external.yaml; do
    cmp -s "$file" "$stage/original-install/$file" || reconfigure=true
  done
fi
state_action() {
  local action=$1 access=rw
  local action_name=proxy-stack-state-$$-$RANDOM
  printf '%s\n' "$action_name" > "$stage/state-runner"
  if [ "$action" = check ]; then access=ro; fi
  if timeout 30 docker run --rm --name "$action_name" --read-only --cap-drop=ALL --network bridge --tmpfs /tmp \
    -v "$directory/data:/data:$access" -v "$stage/state:/transaction" \
    -e "DOMAIN=$domain" -e "NODE_NAME=$node_name" -e "PUBLIC_PORT=$port" -e "MODE=$mode" \
    "$tool_image" install-state "$action"; then rm -f "$stage/state-runner"; return 0; fi
  timeout 5 docker rm -f "$action_name" >/dev/null 2>&1 || return 1
  rm -f "$stage/state-runner"
  return 1
}
if $repeat; then
  mkdir "$stage/state"
  quiet state_action check || fail '受管状态不支持此重配；原配置未改变。'
fi
if $repeat && $reconfigure && $prepare_only; then
  running=$(cd -- "$directory" && timeout 10 docker compose ps --services --status running) || fail '无法确认原运行状态。'
  [ -z "$running" ] || fail '--prepare-only 不能重配正在运行的安装；原服务未改变。'
fi
quiet timeout 15 docker compose config --quiet || fail 'Compose 候选配置无效；安装目录未写入。'
# All needed images and full native Caddy configuration validate before activation.
printf '验证 Caddy 镜像与完整候选配置（最多 120 秒）…\n'
quiet timeout 120 docker pull "$caddy_image" || fail 'Caddy 镜像不可用；安装目录未写入。'
timeout 15 docker run --rm --read-only --cap-drop=ALL --network none -e "DOMAIN=$domain" -e "NODE_NAME=$node_name" -e "PUBLIC_PORT=$port" -e "MODE=$mode" -e "ROLE=$role" -e "LOCAL_NODE=$local_node" -e HTTP01_ONLY=true "$tool_image" preview-caddy > "$stage/Caddyfile" 2>>"$operator_log"
quiet timeout 20 docker run --rm --read-only --cap-drop=ALL --network none --cap-add=NET_BIND_SERVICE --tmpfs /data --tmpfs /config --tmpfs /tmp -v "$stage/Caddyfile:/etc/caddy/Caddyfile:ro" "$caddy_image" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile || fail 'Caddy 候选配置验证失败；安装目录未写入。'
if $host_route; then
  command -v python3 >/dev/null 2>&1 || fail '80 已占用；自动集成需要宿主 Python 3。'
  timeout 45 python3 "$stage/install/host_setup.py" plan --stage "$stage/host" --domain "$domain" --port "$http_port" --identity "$project" || fail '宿主 Caddy 预检未通过；安装目录未写入。'
  if $non_interactive && ! $manage_host && ! $check_only; then fail '此计划需要 --manage-host-caddy 显式授权宿主 HTTP-01 集成。'; fi
fi
printf '计划：%s；HTTPS %s:%s；HTTP-01 %s:%s。\n' "$role" "$domain" "$port" "$http_bind" "$http_port"
printf '目标：%s；公网防火墙/NAT 需允许 80 和 %s。\n' "$directory" "$port"
if $check_only; then printf '预检通过；未写入安装目录，未激活服务。\n计划日志：%s\n' "$operator_log"; exit 0; fi
if { $host_route || { $repeat && $reconfigure; }; } && ! $non_interactive; then
  prompt confirmation '按此计划安装/重配（含计划中的宿主 HTTP 路由）？[y/N]: '
  case "$reply" in y|Y) ;; *) fail '已取消；未激活服务。' ;; esac
fi
if [ "$role" = node ] && ! $repeat; then
  if [ -n "$join_code_file" ]; then
    [ -f "$join_code_file" ] && [ ! -L "$join_code_file" ] || fail 'unsafe join code file'
    IFS= read -r join_code < "$join_code_file" || [ -n "$join_code" ] || fail 'empty join code'
  else
    $non_interactive && fail '加入节点需 --join-code-file。'
    exec 3<>/dev/tty
    printf '粘贴限时加入码（输入隐藏）: ' >&3
    trap 'stty echo <&3; cleanup_probe' EXIT HUP INT TERM
    stty -echo <&3; IFS= read -r join_code <&3 || { stty echo <&3; fail '输入已结束。'; }
    stty echo <&3; printf '\n' >&3
  fi
fi
# Recovery script/journal survive terminal loss. They contain paths/IDs, no tokens.
cd -- "$stage"
cp install/host_setup.py host_setup.py
{
  printf '#!/usr/bin/env bash\nset -u\numask 077\n'
  printf 'directory=%q\nstage=%q\nproject=%q\nrepeat=%q\nreconfigure=%q\ntool_image=%q\n' "$directory" "$stage" "$project" "$repeat" "$reconfigure" "$tool_image"
  cat <<'RECOVERY'
release_lock() {
  if [ -f "$directory.install-lock/owner" ] && [ "$(cat "$directory.install-lock/owner")" = "$stage" ]; then
    rm -f -- "$directory.install-lock/owner" && rmdir -- "$directory.install-lock"
  fi
}
case "$(cat "$stage/phase" 2>/dev/null)" in complete|prepared|rolled-back) release_lock; exit $? ;; esac
failed=0
if [ -f "$stage/state-runner" ]; then
  runner=$(cat "$stage/state-runner")
  case "$runner" in proxy-stack-state-[0-9]*-[0-9]*) timeout 10 docker rm -f "$runner" || failed=1 ;; *) failed=1 ;; esac
  [ "$failed" -ne 0 ] || rm -f "$stage/state-runner"
fi
[ "$failed" -eq 0 ] || exit 1
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE
unset DOMAIN NODE_NAME HOST_NODE_NAME MODE PUBLIC_PORT HTTP_PORT HTTP_BIND HTTP01_ONLY CONTACT_EMAIL UPDATE_INTERVAL TOOL_IMAGE CADDY_IMAGE ROLE LOCAL_NODE LEASE_SECONDS
# Only this transaction's own Compose project/resources may be stopped.
if [ -f "$stage/resources-starting" ] && [ ! -f "$stage/resources-stopped" ]; then
  if [ -f "$directory/.env" ] && [ "$(sha256sum "$directory/.env" | cut -d' ' -f1)" = "$(cat "$stage/env.sha256")" ]; then
    if $repeat && ! $reconfigure; then
      (cd -- "$directory" && timeout 10 docker compose ps --services --status running) > "$stage/after-services" || failed=1
      while IFS= read -r service; do
        case "$service" in hub|xray|updater|caddy) ;; *) failed=1; continue ;; esac
        if ! grep -qxF "$service" "$stage/before-services"; then
          (cd -- "$directory" && timeout 20 docker compose stop "$service") || failed=1
        fi
      done < "$stage/after-services"
      timeout 10 docker ps -aq --filter "label=com.docker.compose.project=$project" > "$stage/after-containers" || failed=1
      while IFS= read -r cid; do
        [[ "$cid" =~ ^[a-f0-9]{12,64}$ ]] || continue
        if ! grep -qxF "$cid" "$stage/before-containers"; then timeout 15 docker rm -f "$cid" || failed=1; fi
      done < "$stage/after-containers"
    elif $repeat; then
      (cd -- "$directory" && timeout 45 docker compose --project-name "$project" down) || failed=1
    else
      (cd -- "$directory" && timeout 45 docker compose --project-name "$project" down --volumes --remove-orphans) || failed=1
    fi
  else failed=1; fi
  if [ "$failed" -eq 0 ]; then : > "$stage/resources-stopped"; fi
fi
if [ -f "$stage/host/journal.json" ]; then
  timeout 45 python3 "$stage/host_setup.py" rollback --stage "$stage/host" || failed=1
fi
recover_state() {
  local action=$1 runner=proxy-stack-state-$$-$RANDOM
  printf '%s\n' "$runner" > "$stage/state-runner"
  if timeout 30 docker run --rm --name "$runner" --read-only --cap-drop=ALL --network bridge --tmpfs /tmp \
    -v "$directory/data:/data" -v "$stage/state:/transaction" "$tool_image" install-state "$action"; then
    rm -f "$stage/state-runner"; return 0
  fi
  timeout 5 docker rm -f "$runner" || return 1
  rm -f "$stage/state-runner"
  return 1
}
restore_repeat() {
  if [ -f "$stage/state-saved" ] && [ ! -f "$stage/state-restored" ]; then
    recover_state restore || return 1
    : > "$stage/state-restored"
  fi
  for file in .env .env.example proxyctl compose.yaml compose.external.yaml; do
    if [ -f "$stage/original-install/$file" ]; then
      cp -p -- "$stage/original-install/$file" "$directory/.$file.restore" && mv -f -- "$directory/.$file.restore" "$directory/$file" || return 1
    elif [ "$file" = .env.example ]; then rm -f -- "$directory/$file" || return 1; fi
  done
  if [ -s "$stage/before-services" ]; then
    services=()
    while IFS= read -r service; do
      case "$service" in hub|xray|updater|caddy) services+=("$service") ;; *) return 1 ;; esac
    done < "$stage/before-services"
    (cd -- "$directory" && timeout 60 docker compose --project-name "$project" up -d --no-deps --no-build --pull never "${services[@]}") || return 1
  fi
  : > "$stage/repeat-restored"
}
if $repeat && [ -f "$stage/repeat-mutating" ] && [ ! -f "$stage/repeat-restored" ]; then
  if [ ! -f "$stage/resources-starting" ] || [ -f "$stage/resources-stopped" ]; then
    restore_repeat || failed=1
  else failed=1; fi
fi

if ! $repeat && [ -f "$stage/enrollment-attempted" ] && [ "$failed" -eq 0 ]; then
  # Revoke scoped authorization BEFORE discarding a newly enrolled runtime.
  # A lost/offline response keeps the private peer + journal and lock for retry.
  mkdir -p "$stage/state"
  recover_state cancel-enrollment || failed=1
fi
if ! $repeat && [ -f "$stage/created" ] && [ -d "$directory" ] && [ "$failed" -eq 0 ]; then
  if [ -f "$directory/.proxy-stack-transaction" ] && [ "$(cat "$directory/.proxy-stack-transaction")" = "$stage" ]; then
    mv -- "$directory" "$stage/failed-install" || failed=1
  else failed=1; fi
fi
if [ "$failed" -eq 0 ]; then
  printf 'rolled-back\n' > "$stage/phase"
  release_lock || failed=1
else printf 'recovery-required\n' > "$stage/phase"; fi
exit "$failed"

RECOVERY
} > recover.sh
chmod 700 recover.sh
printf '%s\n' "$$" > pid
awk '{print $22}' "/proc/$$/stat" >> pid 2>/dev/null || printf 'unknown\n' >> pid
# Record the pre-existing project containers before starting any new ones.
timeout 10 docker ps -aq --filter "label=com.docker.compose.project=$project" > before-containers
if $repeat; then
  (cd -- "$directory" && timeout 10 docker compose ps --services --status running) > before-services
  while IFS= read -r cid; do
    [[ "$cid" =~ ^[a-f0-9]{12,64}$ ]] || fail '受管容器标识无效。'
    identity=$(timeout 10 docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{index .Config.Labels "com.docker.compose.service"}}' "$cid") || fail '无法验证受管容器身份。'
    case "$identity" in "$directory|hub"|"$directory|xray"|"$directory|updater"|"$directory|caddy") ;; *) fail 'Compose 项目标识属于其他目录/服务；未改变安装。' ;; esac
  done < before-containers
fi
if ! $repeat; then
  [ ! -s before-containers ] || fail '随机项目标识已存在；未创建安装。'
  for resource in network volume; do
    occupied=$(timeout 10 docker "$resource" ls -q --filter "label=com.docker.compose.project=$project") || fail '无法检查项目资源。'
    [ -z "$occupied" ] || fail '随机项目资源已存在；未创建安装。'
  done
fi
rollback_on_exit() {
  local result=$?
  cleanup_probe
  if $committed; then
    if [ -f "$directory.install-lock/owner" ] && [ "$(cat "$directory.install-lock/owner")" = "$stage" ]; then rm -f -- "$directory.install-lock/owner"; rmdir -- "$directory.install-lock"; fi
  else
    # Capture only this transaction's Caddy diagnostics BEFORE recovery removes
    # its container. Never stream these potentially sensitive bytes to the UI.
    if [ -f "$stage/resources-starting" ] && [ -f "$directory/.env" ] && [ -f "$stage/env.sha256" ] && \
       [ "$(sha256sum "$directory/.env" | cut -d' ' -f1)" = "$(cat "$stage/env.sha256")" ]; then
      (cd -- "$directory" && timeout 10 docker compose --project-name "$project" logs --no-color --tail 200 caddy) > "$stage/caddy-diagnostics.log" 2>&1 || :
      chmod 600 "$stage/caddy-diagnostics.log"
    fi
    if bash "$stage/recover.sh" >>"$operator_log" 2>&1; then
      printf '安装未完成；本次变更已回滚。备份/日志：%s\n' "$stage" >&2
    else
      printf '安装未完成；私有状态保留。恢复：重新运行安装器 --directory %q --recover；日志：%s/operator.log\n' "$directory" "$stage" >&2
    fi
  fi
  exit "$result"
}
trap rollback_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
printf 'approved\n' > phase
mkdir -p -- "$(dirname -- "$directory")"
mkdir -- "$directory.install-lock" || fail '另一个安装事务持有目录锁；先完成或恢复该事务。'
printf '%s\n' "$stage" > "$directory.install-lock/owner"
if $repeat; then
  for file in .env proxyctl compose.yaml compose.external.yaml; do
    [ ! -L "$directory/$file" ] && cmp -s "$directory/$file" "$stage/original-install/$file" || fail '受管配置在预检后改变；未启动或重置服务。'
  done
fi
# Recheck every selected public/gateway binding just before any installation write.
port_available "$port" || owns_binding "$port" 0.0.0.0 "$port" || owns_binding "$port" :: "$port" || fail 'HTTPS 端口在预检后被占用。'
if [ "$mode" = existing-caddy ]; then binding=127.0.0.1:18090; target_port=8080; expected_ip=127.0.0.1; expected_port=18090
else binding="$http_bind:$http_port"; target_port=80; expected_ip=$http_bind; expected_port=$http_port; fi
port_available "$binding" || owns_binding "$target_port" "$expected_ip" "$expected_port" || fail 'HTTP 网关端口在预检后被占用。'
if $host_route; then timeout 30 python3 "$stage/host_setup.py" recheck --stage "$stage/host" || fail '宿主配置在预检后已变化。'; fi
if $repeat && $reconfigure; then
  # Journal intent precedes stopping/writing. A crash can always restart the
  # prior services, even if state snapshotting had not yet completed.
  while IFS= read -r service; do
    case "$service" in hub|xray|updater|caddy) ;; *) fail '发现未识别的受管运行服务。' ;; esac
  done < "$stage/before-services"
  : > "$stage/repeat-mutating"
  quiet timeout 60 "$directory/proxyctl" down || fail '无法停止本安装资源；将恢复原运行状态。'
  quiet state_action save || fail '无法保存私有状态备份。'
  : > "$stage/state-saved"
  for file in .env .env.example proxyctl compose.yaml compose.external.yaml; do
    cp -- "$stage/install/$file" "$directory/.$file.new"
    mv -f -- "$directory/.$file.new" "$directory/$file"
  done
  chmod 700 "$directory/proxyctl"
  quiet state_action apply || fail '运行配置再生成失败；将恢复原状态。'
fi
if ! $repeat; then
  mkdir -p -- "$directory"; chmod 700 "$directory"
  printf '%s\n' "$stage" > "$directory/.proxy-stack-transaction"
  : > "$stage/created"
  for file in .env .env.example proxyctl compose.yaml compose.external.yaml; do cp -- "$stage/install/$file" "$directory/$file"; done
  chmod 700 "$directory/proxyctl"
  printf 'proxy-stack-installer-v1\n' > "$directory/$marker"
  # Compose init can create the project's network/one-off manager even when
  # init or enrollment fails. Persist intent before the FIRST Compose run.
  sha256sum "$directory/.env" | cut -d' ' -f1 > "$stage/env.sha256"
  : > "$stage/resources-starting"
  quiet timeout 30 "$directory/proxyctl" init
fi
sha256sum "$directory/.env" | cut -d' ' -f1 > "$stage/env.sha256"
if [ "$role" = node ] && ! $repeat; then
  # Join output never enters logs, and the invitation is stdin only.
  : > "$stage/enrollment-attempted"
  runner=proxy-stack-state-$$-$RANDOM
  printf '%s\n' "$runner" > "$stage/state-runner"
  if ! printf '%s\n' "$join_code" | timeout 20 docker run --rm -i --name "$runner" --read-only --cap-drop=ALL --network bridge --tmpfs /tmp -v "$directory/data:/data" "$tool_image" join >/dev/null; then
    # Enrollment can be retried with the preserved private state from the journal.
    fail '加入失败；本次安装将回滚，私有状态保存在事务备份。'
  fi
  rm -f "$stage/state-runner"
  unset join_code
fi
if $prepare_only; then
  committed=true; printf 'prepared\n' > phase
  printf '初始化完成；未启动服务，未检查公网就绪。\n'
  if [ "$mode" = existing-caddy ]; then printf '专家反代片段：%s/data/runtime/host-caddy.snippet（保留 TLS/global 要求）。\n' "$directory"; fi
  printf '启动：%q up\n完成检查：%q finish\n' "$directory/proxyctl" "$directory/proxyctl"
else
  : > "$stage/resources-starting"
  port_available "$port" || owns_binding "$port" 0.0.0.0 "$port" || owns_binding "$port" :: "$port" || fail '启动前 HTTPS 端口已被占用。'
  port_available "$binding" || owns_binding "$target_port" "$expected_ip" "$expected_port" || fail '启动前 HTTP 端口已被占用。'
  if $repeat && ! $reconfigure; then quiet timeout 60 "$directory/proxyctl" up --no-recreate; else quiet timeout 60 "$directory/proxyctl" up --force-recreate; fi
  if $host_route; then timeout 45 python3 "$stage/host_setup.py" apply --stage "$stage/host" || fail '宿主路由激活失败。'; fi
  # Completion is separate from usability: a hub-only can bootstrap enrollment.
  printf '等待公共 HTTPS 验证（最多 120 秒；不会提前显示私密链接）…\n'
  if timeout 120 "$directory/proxyctl" finish --wait 120 --https-only --silent --progress; then :
  else
    finish_status=$?
    if [ "$finish_status" -eq 124 ]; then printf '完成检查失败（timeout）：120 秒上限已到；根据上方进度分类检查本机状态、服务、HTTPS 或节点同步。\n' >&2; fi
    fail '公共 HTTPS / 节点运行同步待完成；回滚本次配置和资源，保留原服务。'
  fi
  committed=true; printf 'complete\n' > phase
  "$directory/proxyctl" finish || :
fi
if $prepare_only; then printf '菜单：%q menu\n' "$directory/proxyctl"; fi
printf '安装日志/恢复记录：%s\n' "$stage"
