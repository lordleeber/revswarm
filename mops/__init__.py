# MOPS 交叉驗證子系統：用公開資訊觀測站的官方申報日當黃金基準，抽驗/回填 Yahoo 抓到的公布日。
#   mops_validate.py   產生基準 → mops_baseline.csv，逐筆比對 → mops_validation.csv
#   mops_fill.py       用基準回填缺漏（執行前自動備份 revswarm.db）
#   mops_overwrite.py  覆蓋 Yahoo 抓到的遲交日期（同上會先備份）
# 一律從 repo 根目錄以模組形式執行，根目錄才留在 sys.path 上（mops_fill/mops_overwrite 需 import revlib）：
#   python3 -m mops.mops_validate --codes 2330
