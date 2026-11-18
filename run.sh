#!/usr/bin/env bash
# 服务器入口：消费候选池 → 探活/校验 → 可选纯净度优选 → 发布。
#
# 分工（默认）：
#   CI   每轮只做「拉源 + 解析 + 去重」，把候选池提交成 candidates.yaml，不做探活、不发布；
#   服务器 负责唯一一次真正的探活与发布 —— 探活结论只在「从哪测」这个前提下有意义，
#          所以由你控制的机器来跑。
#
# 服务器不限制地区：CPU 架构自动识别，探活点就是这台机器本身。
#
# 用法：
#   ./run.sh                        # 默认：用仓库里的候选池；过期则自己拉源
#   PUBLISH=none ./run.sh           # 只产出到本机，不提交
#   HK_PROBE=1 ./run.sh             # 额外用 check-host.net 香港探测点补一份数据
#   PURITY=1 TOP_N=80 ./run.sh      # 追加出口 IP 纯净度 + 选优选（产物不入库）
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
[ -f .env ] && set -a && . ./.env && set +a

SINGBOX_BIN="${SINGBOX_BIN:-./sing-box}"
SINGBOX_VERSION="${SINGBOX_VERSION:-1.14.4}"
PUBLISH="${PUBLISH:-none}"
# 候选池超过这么多小时没更新，就自己拉一遍源（CI 挂了也不至于停更）
CANDIDATES_MAX_AGE_H="${CANDIDATES_MAX_AGE_H:-6}"

mkdir -p logs
LOG="logs/run-$(date -u '+%Y%m%d-%H%M%S').log"
exec > >(tee -a "$LOG") 2>&1

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

exec 9>"logs/.lock"
if ! flock -n 9; then
  log "[跳过] 上一轮还在跑"
  exit 0
fi

# --- sing-box ---------------------------------------------------------------
ensure_singbox() {
  [ -x "$SINGBOX_BIN" ] && return 0
  local arch
  case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l|armv7)  arch=armv7 ;;
    *) log "[错误] 不支持的架构: $(uname -m)"; return 1 ;;
  esac
  local url="https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-linux-${arch}.tar.gz"
  log "[准备] 下载 sing-box ${SINGBOX_VERSION} (${arch})"
  curl -fL --retry 3 -o sing-box.tar.gz "$url"
  tar -xzf sing-box.tar.gz
  mv "sing-box-${SINGBOX_VERSION}-linux-${arch}/sing-box" "$SINGBOX_BIN"
  chmod +x "$SINGBOX_BIN"
  rm -rf sing-box.tar.gz "sing-box-${SINGBOX_VERSION}-linux-${arch}"
}

export SINGBOX_BIN
ensure_singbox || { log "[错误] sing-box 准备失败"; exit 1; }

# --- 候选池 ----------------------------------------------------------------
candidates_fresh() {
  [ -f candidates.yaml ] || return 1
  local age=$(( ($(date +%s) - $(date -r candidates.yaml +%s)) / 3600 ))
  [ "$age" -lt "$CANDIDATES_MAX_AGE_H" ]
}

if [ "$PUBLISH" = "git" ]; then
  log "[0/4] 同步仓库"
  git fetch -q origin || log "[警告] fetch 失败，继续用本地状态"
  git merge -q --ff-only origin/main 2>/dev/null || log "[提示] 本地有改动或已分叉，跳过合并"
fi

if candidates_fresh; then
  log "[1/4] 使用仓库里的候选池 candidates.yaml（$(date -r candidates.yaml '+%Y-%m-%d %H:%M')）"
else
  log "[1/4] 候选池缺失或过期，自己拉源"
  python3 fetch_subscriptions.py || { log "[错误] 拉取失败"; exit 1; }
fi

if [ "${HK_PROBE:-0}" = "1" ]; then
  log "[2/4] 香港探测点（可选，失败不影响发布）"
  python3 hk_probe.py || log "[警告] 香港探测失败，继续"
else
  log "[2/4] 跳过香港探测（HK_PROBE=0）；排序只看本机测得的延迟"
fi

log "[3/4] 探活 + 协议级校验 + 输出"
python3 speed_test.py || { log "[错误] 探活/校验失败，本轮不产出"; exit 1; }

if [ "${PURITY:-0}" = "1" ]; then
  log "[4/4] 出口 IP 纯净度标注"
  if python3 purity.py; then
    if [ "${TOP_N:-0}" -gt 0 ] 2>/dev/null; then
      log "[4/4] 按纯净度选优选 top${TOP_N}"
      python3 select_top.py || log "[警告] 选优选失败，只保留 v2.txt"
      log "[提示] 优选文件只在本机：$(pwd)/top${TOP_N}.txt（不入库）"
    fi
  else
    log "[警告] 纯净度标注失败，保留未标注的 v2.txt"
  fi
else
  log "[4/4] 跳过纯净度标注（PURITY=0）"
fi

# --- 发布 ------------------------------------------------------------------
case "$PUBLISH" in
  git)
    log "[发布] 提交并推送 v2.txt"
    git add v2.txt hk_latency.json node_health.json 2>/dev/null || true
    if git diff --cached --quiet; then
      log "[发布] 没有变化，跳过提交"
    else
      git -c user.name="${GIT_AUTHOR_NAME:-node-bot}" \
          -c user.email="${GIT_AUTHOR_EMAIL:-node-bot@localhost}" \
          commit -q -m "chore: 更新订阅 $(date -u '+%Y-%m-%d %H:%M UTC')"
      for i in 1 2 3 4; do
        if git push -q origin HEAD:main; then
          log "[发布] 推送成功（第 ${i} 次）"
          break
        fi
        log "[发布] 推送失败，同步远端后重试（${i}/4）"
        git fetch -q origin || true
        git reset -q --soft origin/main 2>/dev/null || true
        git add v2.txt hk_latency.json node_health.json 2>/dev/null || true
        git commit -q -m "chore: 更新订阅 $(date -u '+%Y-%m-%d %H:%M UTC')" || true
        sleep 5
      done
    fi
    ;;
  none)
    log "[发布] PUBLISH=none，产物只留在本机: $(pwd)/v2.txt"
    ;;
  *)
    log "[错误] 未知 PUBLISH=$PUBLISH（可选 none / git）"; exit 1 ;;
esac

ls -1t logs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f
log "[完成] v2.txt $(wc -c < v2.txt) 字节"
