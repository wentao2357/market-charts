# 跨资产图表簿

网址：https://wentao2357.github.io/market-charts/

一页看完约 110 张跨资产图：美股大盘与情绪、科技细分与龙头、行业板块、比值图、经济信号行业、全球股指、美债利率与信用、债券 ETF、汇率、大宗商品、加密资产。顶部"今日异动"自动列出 52 周新高/新低、异常波动（σ）、200 日均线突破和背离。

## 如何更新

GitHub Actions 每天 22:35 UTC（香港时间早上 6:35）自动抓一次收盘数据并重新发布，不需要任何操作。也可以在仓库的 **Actions → Update data and publish → Run workflow** 手动触发一次。

## 如何增删图表

编辑 `config/universe.json`：

- 在某个分组的 `series` 里加一行，例如 `{"id": "CRWD", "name": "CrowdStrike", "sym": "CRWD"}`。`sym` 用 Yahoo Finance 的代码。
- 利率类加 `"kind": "rate"`（变动按 bp 显示）。FRED 数据源用 `"src": "fred"`。
- 比值图加在 `ratios` 里：`{"id": "R_X", "name": "名称", "num": "分子id", "den": "分母id", "question": "这张图回答什么"}`。

保存提交后会自动重新抓数据和发布。

## 文件

- `index.html`：页面（读取 `data/market.json`）
- `scripts/fetch_data.py`：抓数据脚本（Yahoo Finance、美国财政部、纽约联储）
- `config/universe.json`：图表清单
- `.github/workflows/update.yml`：每日定时任务

仅供研究参考，不构成投资建议。
