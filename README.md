# 理赔报价机器人

当前版本：**v3.17.21**（P0：报价处理意见采集与规则建议）

基于 Python、Playwright 和 Tkinter 的理赔报价自动化工具，支持定损同步、零件查价与改价、报价提交、高额案件提交上级、平级移交及工作记录。

## 本次更新

- 新增 P0 智能意见判断：进入报价页后读取「报价处理意见列表」最新一条，给出建议动作并采集语料。
- 判断结果写入审计日志与 Excel 台账备注；P0 只建议不执行，`INTELLIGENCE_MODE=off` 时与旧版完全一致。
- 语料落 SQLite（opinion_corpus / opinion_label / opinion_prediction），支持导出 JSONL 供后续训练。
- 报价任务提交对话框按人员代码分流：已处理过的九级报价案件人员代码自动带入时直接提交，不再填组别代码；首次接收才填组别代码联动带出人员。

## 运行

- Windows：运行 `dist/理赔报价机器人.exe`。
- 源码：安装依赖后执行 `python gui.py`。

## 构建

```powershell
python -m PyInstaller "理赔报价机器人.spec" --clean --noconfirm
```

## 文档索引

- [EXE 打包方法（PyInstaller）](PACKAGING.md)：spec 固化依赖、路径规范、构建与验收清单。

## 数据与记录

- 工作记录：`data/ledger/工作记录_YYYYMM.xlsx`
- 运行、审计与错误日志：`data/logs/`
- 本地价格及处理历史：`data/price_cache.db`

完整历史版本见 [CHANGELOG.md](CHANGELOG.md)。
