# 接 server 佇列的爬蟲 worker（/lease → 抓取 → /result）。
# repo 內執行一律用模組形式，讓根目錄留在 sys.path 上以匯入 revlib：
#   python3 -m worker.yahoo_worker  --server http://<IP>:8000
#   python3 -m worker.google_worker --server http://<IP>:8000
# 遠端 worker 機器仍是平鋪部署（yahoo_worker.py 與 revlib.py 同層），照舊 python3 yahoo_worker.py。
# goodinfo_worker.py 不在這裡：它不跟 server 租任務，屬 goodinfo/ 子系統。
