# goodinfo 子系統：爬 goodinfo 基本資料頁 → 建 stock_dates.db（上市/上櫃/興櫃/公開發行日）。
# 是 package 而非散檔，才能讓 tests/test_stock_dates.py 用 `from goodinfo import goodinfo_worker` 匯入。
# 腳本仍從 repo 根目錄執行（python3 goodinfo/goodinfo_worker.py），相對路徑一律相對於根目錄。
