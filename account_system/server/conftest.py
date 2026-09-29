"""pytest 全局夹具（2026-09-29）：新引入的后台线程默认全关，保证存量/新增离线测试
不被 worker/备份线程干扰（惯例同 CHAGEE_RECONCILE_INTERVAL_SECONDS=0 等，见各
test_*_offline.py 头部；单测需要显式启用时在文件/用例内覆盖本默认即可）。

- CHAGEE_ORDER_WORKERS=0       异步订单中枢 worker 池不启动（测试内直接调 process_once）
- CHAGEE_BACKUP_INTERVAL_SECONDS=0  SQLite 备份线程不启动（防测试期间备份真实 data/ 库）

conftest 在收集阶段先于所有测试模块导入执行，此处赋值后测试模块自身的同名覆盖仍然生效。
"""

import os

os.environ.setdefault("CHAGEE_ORDER_WORKERS", "0")
os.environ.setdefault("CHAGEE_BACKUP_INTERVAL_SECONDS", "0")
