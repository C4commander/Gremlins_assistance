# Gremlins Assistance

Gremlins Assistance 是一个面向《Gremlins, Inc.》的 Windows 本地 OCR 辅助工具原型。

## 功能

- 手动选择并锁定目标游戏窗口。
- 按窗口句柄后台截图，即使切换到其他窗口也会继续监控。
- 支持“下方卡牌区(按比例)”识别区域，窗口大小变化时自动按比例换算 ROI。
- 支持“预览识别范围”，用红框显示当前实际监控区域。
- 使用本地 RapidOCR 识别中文文本。
- 使用 `card_image_text_map.csv` 作为卡牌名称词表，按卡牌名称统计出现次数，例如 `稀释珍宝: 1`，不做单字计数。
- 抓图失败或后台渲染不可用时会保留上一次结果并继续重试。
- 支持导出 CSV 统计和 JSON 识别记录。

## 安装依赖

```powershell
pip install -r requirements.txt
```

## 运行

```powershell
python app.py
```

## 打包

```powershell
.\build.ps1
```

打包产物会覆盖生成到：

```text
dist\Gremlins_assistance.exe
```

`build.ps1` 会在打包前自动关闭正在运行的旧版 `Gremlins_assistance.exe`，避免 Windows 文件占用导致覆盖失败。

## 使用流程

1. 打开游戏窗口。
2. 点击“手动选择并锁定窗口”，选择 `Gremlins, Inc.` 窗口。
3. 选择“下方卡牌区(按比例)”或手动填写 ROI。
4. 点击“预览识别范围”，确认红框覆盖目标区域。
5. 点击“单次识别”测试 OCR 和卡牌名称匹配。
6. 点击“开始监控”持续统计卡牌名称出现次数。

## 已知限制

- 如果游戏窗口最小化，后台截图通常会失败。
- 部分 GPU/全屏渲染模式可能不支持 `PrintWindow` 后台截图。
- 如果预览窗口尺寸明显小于真实游戏窗口，通常是 DPI 缩放问题；程序已启用 DPI awareness，但仍建议使用窗口化或无边框窗口模式。
