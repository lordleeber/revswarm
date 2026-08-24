# goodinfo 子系統：爬 goodinfo 基本資料頁 → 建 stock_dates.db（上市/上櫃/興櫃/公開發行日）。
# 是 package 而非散檔，才能讓 tests/test_stock_dates.py 用 `from goodinfo import goodinfo_worker` 匯入。
# 一律從 repo 根目錄以模組形式執行（python3 -m goodinfo.goodinfo_worker），相對路徑相對於根目錄。
