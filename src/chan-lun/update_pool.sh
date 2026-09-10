#!/bin/bash
# 重建股票池 pool/pool.tsv : 沪深300(300) + 中证500(500) + 中证1000(1000) 合并去重
# 数据源: 腾讯微证券 WeStock (westock-data-skillhub, 官方 npm 包)
# 注意: 本机 npm registry 被指到华泰内网(502), 必须先切到官方 registry。
# 接口偶发抖动 -> 每指数按预期数量重试(最多5次), 全部达标才落盘。
# 兼容 macOS bash3.2(无关联数组), 用平行数组实现。
set -e
cd "$(dirname "$0")"
export npm_config_registry=https://registry.npmjs.org

TMP=$(mktemp)
trap 'rm -f "$TMP" "$TMP".*' EXIT

# WeStock 查询代码与指数并非同一套: 沪深300用 sh000300, 中证500/1000 用 sz399905/sz399852
IDXS="sh000300 sz399905 sz399852"

for idx in $IDXS; do
  case "$idx" in
    sh000300) want=300; name=沪深300 ;;
    sz399905) want=500; name=中证500 ;;
    sz399852) want=1000; name=中证1000 ;;
  esac
  got=0
  for try in 1 2 3 4 5; do
    npx -y westock-data-skillhub@1.0.5 index constituent "$idx" 2>/dev/null \
      | awk -F'|' '/^\| *[a-z]{2}[0-9]{6} *\|/ {gsub(/ /,"",$2); gsub(/^ +| +$/,"",$3); print $2"\t"$3}' \
      > "$TMP.$idx"
    got=$(wc -l < "$TMP.$idx" | tr -d ' ')
    [ "$got" -ge "$want" ] && break
    echo "[update_pool] $name 第${try}次仅得 ${got}/${want}, 重试..." >&2
    sleep 2
  done
  if [ "$got" -lt "$want" ]; then
    echo "[update_pool] 警告: $name 重试5次仍只得 ${got}/${want}, 池可能偏小" >&2
  fi
  echo "[update_pool] $name = $got 只" >&2
  cat "$TMP.$idx" >> "$TMP"
done

awk -F'\t' '!seen[$1]++ {print}' "$TMP" > pool/pool.tsv
n=$(wc -l < pool/pool.tsv | tr -d ' ')
echo "[update_pool] done: pool/pool.tsv = $n 只"
if [ "$n" -lt 1700 ]; then
  echo "[update_pool] 警告: 池仅 $n 只(<1700), 建议重跑" >&2
fi
