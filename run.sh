#!/usr/bin/env bash
# 一行跑完整条流水线：拉取 → 香港探测 → 探活/校验 → 发布。
#
# 这是项目在服务器上的入口。GitHub Actions 用的是同一批脚本，所以两条路径行为一致；
# 区别只在于「探活从哪台机器发出」—— 那决定了校验结果的参考价值（见 README 的校准章节）。
#
# 用法：
#   ./run.sh                 # 用 .env（如果有）或环境变量
#   PUBLISH=none ./run.sh    # 只在本机产出 v2.txt，不提交
#
# 退出码：0 成功；非 0 表示本轮没有更新（保留上一版产物）。
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
[ -f .env ] && set -a && . ./.env && set +a

SINGBOX_BIN="${SINGBOX_BIN:-./sing-box}"
SINGBOX_VERSION="${SINGBOX_VERSION:-1.14.4}"
PUBLISH="${PUBLISH:-none}"

mkdir -p logs
LOG="logs/run-$(date -u '+%Y%m%d-%H%M%S').log"
exec > >(tee -a "$LOG") 2>&1

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

# 单实例锁：定时任务重叠时不要把两个 sing-box 叠在一起跑
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

# --- 流水线 ----------------------------------------------------------------
log "[1/3] 拉取并解析订阅"
python3 fetch_subscriptions.py || { log "[错误] 拉取失败"; exit 1; }

log "[2/3] 香港探测点测延迟（可选，失败不影响发布）"
python3 hk_probe.py || log "[警告] 香港探测失败，继续"

log "[3/3] 探活 + 协议级校验 + 输出"
python3 speed_test.py || { log "[错误] 探活/校验失败，本轮不产出"; exit 1; }

# --- 可选：出口 IP 纯净度标注 + 选优选（结合 NZNL31/ipquality）--------------
if [ "${PURITY:-0}" = "1" ]; then
  log "[附加] 出口 IP 纯净度标注"
  if python3 purity.py; then
    log "[附加] 纯净度标注完成"
    if [ "${TOP_N:-0}" -gt 0 ]; then
      log "[附加] 按纯净度选优选 top${TOP_N}"
      python3 select_top.py || log "[警告] 选优选失败，只保留全量 v2.txt"
      log "[附加] 优选文件留在本机（不进仓库）：$(pwd)/top${TOP_N}.txt"
    fi
  else
    log "[警告] 纯净度标注失败，保留未标注的 v2.txt"
  fi
fi

# --- 发布 ------------------------------------------------------------------
case "$PUBLISH" in
  git)
    log "[发布] 提交并推送"
    git add v2.txt hk_latency.json node_health.json 2>/dev/null || true
    if git diff --cached --quiet; then
      log "[发布] 没有变化，跳过提交"
    else
      git -c user.name="${GIT_AUTHOR_NAME:-node-bot}" \
          -c user.email="${GIT_AUTHOR_EMAIL:-node-bot@localhost}" \
          commit -m "chore: 更新订阅 $(date -u '+%Y-%m-%d %H:%M UTC')"
      git push origin HEAD:main
    fi
    ;;
  none)
    log "[发布] PUBLISH=none，产物只留在本机: $(pwd)/v2.txt"
    ;;
  *)
    log "[错误] 未知 PUBLISH=$PUBLISH（可选 none / git）"; exit 1 ;;
esac

# 只保留最近 30 个日志
ls -1t logs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f
log "[完成] v2.txt $(grep -c '' v2.txt 2>/dev/null || echo 0) 行"
