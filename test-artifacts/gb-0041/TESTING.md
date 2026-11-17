# gb-0041 测试与验证

## 口径换版（本次变更）

### 后端离线用例

```bash
python3 -m pytest -q test-artifacts/gb-0041/test_schema_rollover.py \
  test-artifacts/gb-0041/test_schema_concurrency.py
```

覆盖：

- `test_schema_rollover.py`
  - 版本注册表、字段别名、单位换算、必填项拒绝、别名歧义拒绝。
  - 迁移分批换算 → `ready` → 整批切换，换算前后原值保留。
  - 迁移前可建研究；迁移期间混版被拒、可指定口径建分版研究；切换后研究冻结新口径、旧研究快照不变。
  - 缺项样本隔离并阻止切换；按新口径修正后可切换。
  - `max_batches` 模拟中断后续跑，重复执行不重复换算。
  - viewer 不能发起迁移或切换。
- `test_schema_concurrency.py`
  - 扫描完成后才到达的旧口径样本，切换时自动补换算，不漏样。
  - 扫描后到达且不可换算的样本让迁移回到 `blocked`，不混版。
  - 切换后旧口径入样被拒（422），不能二次迁移。
  - 20 万样本：每次批大小 ≤ 500、至少 400 页，`tracemalloc` 峰值 < 40 MiB。
  - worker 重启从 `sweep_cursor` 续跑。
  - 清空物化值后重放全量，结果与一次换算一致。
  - 隔离样本按新口径修正后问题记录被清理。

依赖：`pytest`、`fakeredis`（测试避免真实 Redis）；数据库用临时 SQLite，与 MySQL 共用同一套 SQLAlchemy 语义。

### 前端 DOM 流程

真实浏览器二进制在本环境缺少系统库（`libnss3` 等）无法启动，因此用 jsdom 驱动**真实前端模块与真实后端**（`run_ui_demo.py` 以 fakeredis TCP 服务 + SQLite 起后端，并把 `/api` 反代到前端静态文件）：

```bash
python3 test-artifacts/gb-0041/run_ui_demo.py &   # 监听 18245，后端 8000
node test-artifacts/gb-0041/ui_dom_check.mjs      # 35 项断言
```

覆盖登录、导航出现“口径换版”、注册表展示、页面发起迁移并轮询到可切换、新口径入样、混版研究被拒、指定口径研究、页面整批切换、切换后徽标/入样限制、旧研究快照不变、阻塞原因与隔离样本展示、切换按钮禁用、修正后切换、迁移记录、窄屏布局。渲染快照写入 `screens/*.html`。

`ui_flow.mjs` 为等价 Playwright 脚本，在有浏览器依赖的环境可直接运行并输出 `screens/*.png`。

### 未在本环境执行

- `docker compose up`（无 Docker），以及 MySQL 上的实跑；MySQL 特有路径（`SELECT ... FOR UPDATE` 的 REPEATABLE READ 快照）已在 `switch` 中以“先加锁再读取”方式约束，但仅在 SQLite 上做过功能验证。
- 真实浏览器截图（缺系统库）。
