# 全部測試集中在此。一律從 repo 根目錄執行：
#   python3 -m unittest discover -p 'test_*.py'      # 全套
#   python3 -m unittest tests.test_revlib            # 單支
# 是 package 而非散檔，才能讓 discover 遞迴進來（Python 3.11 起不再遞迴 namespace package）。
# CWD 必須是 repo 根目錄：受測模組（revlib/server/…）與 data/date_overrides.csv 都以根目錄為基準解析。
