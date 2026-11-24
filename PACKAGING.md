# EXE 打包方法（PyInstaller）

本文档总结本项目的打包方式，供 `D:\Auto-Trading` 等同类项目参考。
核心目标：**打包一次固化配置，不再每次临场排查缺文件、缺模块。**

- 项目：Auto-Quotation（理赔报价机器人）
- 入口：`gui.py`（Tkinter 桌面程序，无控制台窗口）
- 规格文件：`理赔报价机器人.spec`（纳入版本控制）
- 环境：Windows 11 + Python 3.12.10 + PyInstaller 6.15.0
- 产物：`dist/理赔报价机器人.exe`（onefile，约 72.9 MB）

---

## 一、为什么本项目不需要"每次临时补依赖"

因为依赖被分成三类，全部**提前固化**，而不是靠打包报错后逐个补：

| 依赖类型 | 表现 | 固化手段 |
|---|---|---|
| 显式 import | PyInstaller 能自动分析到 | 无需处理 |
| 动态 / 延迟 import | 字符串拼模块名、`importlib`、可选插件，自动分析会漏 | `hiddenimports` 写死在 spec |
| 随包数据文件 | html / 模板 / 配置 / 静态资源，不会自动进包 | `datas` 写死在 spec |
| 第三方大依赖自带二进制 | playwright 的 driver、node、协议文件 | `collect_all('playwright')` 一次性收全 |
| 运行期外部程序 | Chrome / Edge 浏览器内核 | **不打包**，运行时探测本机浏览器，不依赖包内文件 |

关键认知：报错"打包后才缺 `xxx`"，几乎都落在第 2、3、4 类。把这三类写进 spec，就一劳永逸。

---

## 二、打包链路总览

```
gui.py
  └─ PyInstaller 6.x
       ├─ Analysis：分析 import 图（含 hiddenimports、collect_all）
       ├─ PYZ / PKG：压缩打包 Python 模块
       └─ EXE：套 bootloader + 图标 + manifest（高 DPI）
            └─ dist/理赔报价机器人.exe
                 └─ 运行时解包到 sys._MEIPASS（临时目录，只读）
                      ├─ mock_web/        ← datas 带进来的静态页
                      └─ playwright/      ← collect_all 带进来的 driver
                 └─ exe 同目录 data/       ← 可写数据（日志、profile、SQLite、台账）
```

---

## 三、三类依赖的一次性盘点方法

打包前做一次盘点，比打包后补 5 次更省时间。

### 1. 动态 / 延迟 import

搜索项目中的间接导入写法：

```powershell
git grep -nE "importlib|__import__|import_module|importlib.util"
git grep -nE "import .* as .*#|TYPE_CHECKING"
```

本项目实测需要写死的有：

```python
hiddenimports = [
    'bot.capture', 'bot.capture.snapshot', 'bot.capture.actions',
    'bot.runners.capture',   # 运行模式下拉框按字符串动态加载
    'openpyxl',              # 写工作记录，occasionally 延迟导入
]
```

规律：**凡是"按配置名/模式名动态加载的模块"，都必须逐个写进 hiddenimports**；新增一个动态模块，就同步加一行。

### 2. 随包数据文件

```powershell
# 找代码里按相对路径读取的文件/目录
git grep -nE "open\(|read_text|read_bytes|Path\(.*\) / "
```

本项目只有一处需要进包：`mock_web`（本地模拟门户页面）。

```python
datas = [('mock_web', 'mock_web')]   # (源路径, 包内路径)
```

注意：`data/` 这种**会被写入**的目录**绝对不能**放进 datas，否则写的是临时解包目录，退出即丢。

### 3. 大依赖自带二进制（playwright）

不要手写一堆 playwright 子模块，直接：

```python
from PyInstaller.utils.hooks import collect_all
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]
```

同类适用：`numpy`、`PIL`、`openpyxl`、`lxml`、`pandas` 等有明显二进制/数据文件的包，优先查是否有官方 hook；没有再用 `collect_all`。

---

## 四、完整 spec 模板（可直接复制改）

```python
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# ① 随包静态资源（只读）
datas = [('mock_web', 'mock_web')]
binaries = []

# ② 动态/延迟导入的模块
hiddenimports = [
    'bot.capture', 'bot.capture.snapshot', 'bot.capture.actions',
    'bot.runners.capture', 'openpyxl',
]

# ③ 大型依赖一次性收全
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='理赔报价机器人',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                      # GUI 程序：不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest='app.manifest',            # 高 DPI 清单
)
```

`app.manifest`（解决 Windows 缩放模糊）：

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings xmlns:ws2005="http://schemas.microsoft.com/SMI/2005/WindowsSettings">
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">permonitorv2</dpiAwareness>
    </windowsSettings>
  </application>
</assembly>
```

---

## 五、运行时路径规范（最容易踩坑的地方）

**只读资源**用 `sys._MEIPASS`，**可写数据**用 exe 同目录。

集中在 `bot/config.py` 一处实现，其它模块只引用，禁止各自写 `Path(__file__)`：

```python
def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)          # 打包后解包目录（只读、临时）
    return Path(__file__).resolve().parent.parent

PROJECT_ROOT = _base_dir()
MOCK_WEB_DIR = PROJECT_ROOT / "mock_web"

# 可写数据：profile、日志、SQLite、Excel 台账
if getattr(sys, "frozen", False):
    _exe_dir = Path(sys.executable).resolve().parent
    DATA_DIR = _exe_dir / "data"
else:
    DATA_DIR = PROJECT_ROOT / "data"
```

日志模块同理：

```python
def app_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent.parent / "data"
```

判断原则：

- 程序**读**的资源（html、模板、图标）→ `_MEIPASS`
- 程序**写**的东西（日志、Excel、SQLite、浏览器 profile）→ `exe 同目录/data`
- 判断依据只看"是否会被写"，不看"是不是运行时才用"

---

## 六、外部浏览器：不打包内核

Playwright 自带 Chromium 有 150 MB 以上，onefile 打包会让体积失控且易被杀软拦截。本项目策略：

1. **不打包浏览器内核**；
2. 运行时按 `Chrome → Edge → 360 → 内置 Chromium` 顺序探测本机浏览器，找到就用 `executable_path` 启动；
3. 打包版把"内置 Chromium 可用"恒定为 `False`，避免误判后启动失败；

```python
def bundled_available() -> bool:
    if getattr(sys, "frozen", False):
        return False   # onefile 不打包浏览器内核
    ...
```

4. 一个都找不到时给**明确可操作**的提示（"请在控制台指定浏览器路径"），而不是抛底层异常。

如果你的项目也依赖本机浏览器，务必让"打包后无内置浏览器"成为已知状态，而不是运行时的意外。

---

## 七、标准构建流程

```powershell
# 1. 先跑源码自检，源码跑不通不要打包
python -m compileall -q bot
python -c "import gui"        # 或运行一次冒烟脚本

# 2. 同步版本号（本项目的版本写入模块）
python -m bot.version --write

# 3. 全量重建：删旧产物 + --clean，避免增量缓存掩盖缺文件问题
Remove-Item -Recurse -Force build -ErrorAction SilentlyContinue
Remove-Item -Force "dist\理赔报价机器人.exe" -ErrorAction SilentlyContinue
python -m PyInstaller "理赔报价机器人.spec" --clean --noconfirm

# 4. 记录产物元数据，便于确认"打的是最新代码"
$f = Get-Item "dist\理赔报价机器人.exe"
[PSCustomObject]@{
  Version       = (python -c "from bot.version import current; print(current())")
  SizeMB        = [math]::Round($f.Length/1MB, 2)
  LastWriteTime = $f.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
  SHA256        = (Get-FileHash $f.FullName -Algorithm SHA256).Hash
}
```

要点：

- **不要省略 `--clean`**。PyInstaller 有 Analysis 缓存，改了 hiddenimports/datas 后不清缓存，可能出现"改了 spec 但产物没变"。
- **不要只看"Build complete"**。必须核对 exe 的修改时间与 SHA256，确认不是旧产物。
- 用 spec 文件构建（`python -m PyInstaller xxx.spec`），不要在命令行堆 `--add-data`；否则配置分散在历史命令里，换台机器就复现不了。

---

## 八、产物验收清单（打包成功 ≠ 能用）

在**没有 Python 环境**的目录（或另一台机器）逐项验证：

- [ ] 双击 exe 能正常启动 GUI，无黑框、无缺 DLL 报错
- [ ] 静态资源可读：mock 首页 / 采集页面能打开（验证 `datas` 与 `_MEIPASS`）
- [ ] 可写目录正确：exe 同目录生成 `data/logs/`，日志有内容
- [ ] 动态模块可加载：切换到"采集/录制"等运行模式不报 `ModuleNotFoundError`
- [ ] 核心业务链路跑通一次（本项目：mock 流程 + 台账 Excel 落盘）
- [ ] 浏览器可启动：自动探测到 Chrome/Edge 或用自定义路径
- [ ] 关闭程序后 `data/` 下的文件仍在（验证没写进临时目录）

---

## 九、"每次都要临时补依赖"的成因对照表

| 现象 | 根因 | 对策 |
|---|---|---|
| `ModuleNotFoundError: bot.xxx` | 按配置名动态 import | 加进 `hiddenimports` |
| 运行时报找不到 `xxx.html` / 图标 | 静态资源没进 `datas` | `datas=[('dir','dir')]` |
| playwright 启动报 driver 缺失 | 只装了主包没带 driver | `collect_all('playwright')` |
| 打包版启动即崩，源码正常 | 打包版找不到内置 Chromium | 运行时探测本机浏览器 + `executable_path` |
| 日志/Excel 退出后消失 | 写进了 `_MEIPASS` 临时目录 | 写 `exe 同目录/data` |
| 改了 spec 但产物没变化 | 增量缓存 | 删 `build/` + `--clean` |
| 每次换个模块名就要重打补丁 | 依赖清单没固化 | 一律写 spec，提交版本控制 |
| 打包成功但闪退无提示 | `console=False` 吞掉 traceback | 临时改 `console=True` 定位，或写文件日志 |

---

## 十、迁移到 D:\Auto-Trading 的落地步骤

1. **固化 spec**：把当前命令行参数整理成 `xxx.spec` 并提交；之后只用 spec 构建。
2. **做一次动态 import 盘点**：`importlib` / 字符串模块名 / 插件式加载，逐个写进 `hiddenimports`。
3. **做一次数据文件盘点**：代码里所有 `open(...)`、`Path(...)/` 读取的静态文件，全部进 `datas`。
4. **统一路径模块**：区分只读 `_MEIPASS` 与可写 `exe/data`，其它模块禁止自行拼路径。
5. **处理外部依赖**：浏览器/驱动等大二进制不打包，运行时探测并给出可操作提示。
6. **加"构建后自检"脚本**：编译检查 → 全量重建 → 打印版本/大小/时间/SHA256 → 冒烟清单。

完成这 6 步后，日常发版只需一条命令，不再需要"打包-报错-补依赖-再打包"循环。

---

## 十一、维护约定

- 新增动态加载模块 → 同步加 `hiddenimports`；
- 新增静态资源目录 → 同步加 `datas`；
- 新增可写数据 → 一律放 `exe 同目录/data`，不进 `datas`；
- 发版固定流程：改代码 → 升版本 → `compileall` → 删 `build`/旧 exe → `--clean` 重建 → 核对元数据 → 冒烟。
