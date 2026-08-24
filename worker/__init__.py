# 接 server 佇列的爬蟲 worker（/lease → 抓取 → /result）。
# repo 內執行一律用模組形式，讓根目錄留在 sys.path 上以匯入 revlib：
#   python3 -m worker.yahoo_worker  --server http://<IP>:8000
#   python3 -m worker.google_worker --server http://<IP>:8000
# worker 機器同樣是 git clone 本 repo 後從根目錄這樣跑，不需要 DB（只跟 server 走 HTTP）。
# goodinfo_worker.py 不在這裡：它不跟 server 租任務，屬 goodinfo/ 子系統。
