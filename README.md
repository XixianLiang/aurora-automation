# 极光输入采集（本地 Android 模拟器）

通过 uv + uiautomator2 操作 Windy 和 My Aurora Forecast，保存结构化输入、截图和界面 XML。目前不做极光预测；本地运行与 GitHub Actions 入口见下方。

## 当前模拟器

- AVD：`Pixel_9`；ADB：`emulator-5554`；ARM64 Google Play 系统镜像。
- 系统首选语言：`ru-RU`；时区：`Europe/Minsk`（UTC+3）；自动时区关闭。
- 本机 HTTP 代理：`10.0.2.2:7890`，对应宿主机现有代理。
- Windy 界面为俄语；提供的 Aurora 安装包使用英文界面。

## 安装 Aurora：解决跳转 Google Play 的问题

26.7.4 的 APK 含 Google Play 安装来源检查。普通 ADB 安装时 `installer=null`，会进入 `com.pairip.licensecheck.LicenseActivity` 并跳商店。
在当前模拟器上，将安装来源标记为 `com.android.vending` 后，未登录 Google 账号也能进入 Welcome 和首页。

Google 对这个机制的说明：[installer check](https://support.google.com/googleplay/android-developer/answer/15621622?hl=en-AU)。这个来源字段并不表示真的通过 Play 商店下载。

```sh
uv sync
uv run install_aurora.py --serial emulator-5554 \
  --source com.jrustonapps.myauroraforecast_26.7.4.xapk --fresh
```

`--fresh` 会删除模拟器中已有的 Aurora 及其数据后重装，用于验证首次启动。不带 `--fresh` 则尝试覆盖安装；系统可能拒绝覆盖时变更来源或安装位置。
脚本先用 Android SDK 的 aapt 验证所有 APK 的包名，避免把 Aptoide 安装器误当成 Aurora；记录 APK 哈希和安装结果到 `artifacts/aurora-install.json`。

对应的 ADB 安装命令：

```sh
adb -s emulator-5554 install-multiple -i com.android.vending aurora-xapk/*.apk
adb -s emulator-5554 shell pm list packages -i com.jrustonapps.myauroraforecast
```

安装包未修改；当前验证只适用于提供的版本和模拟器，不保证新版保护机制仍接受相同方式。

## 采集

Windy 顺序：菜单 → Wind 基线 → Clouds / Облака → 搜索地点 → 选择地点 → 关闭详情 → 保存云图。
脚本自动处理已观察到的首次引导：m/s、°C、跳过定位与兴趣；Aurora 的 Welcome、Next、Finish 和权限弹窗也会处理并保存截图。

### 正常搜索模式

```sh
uv run main.py --serial emulator-5554 --location Murmansk --timeout 45
```

Windy 选择第一条地理地点结果，排除分类、摄像头和站点。Aurora 选择第一条地点建议。同名地点可用 `--windy-result` 和 `--aurora-result` 指定完整结果文本。
`--app windy` / `--app aurora` 可单独采集。

当前模拟器测试中，Aurora 的自定义地点候选未返回；其 autocomplete 接口的独立请求得到 HTTP 403。此问题与安装来源检查分开记录，搜索失败不会假报成功。

### Aurora 使用模拟器 GPS

这条路径仍采集 App 页面：把显式坐标注入模拟器 GPS，授予前台定位权限，再在 Aurora 选择 CURRENT LOCATION。Windy 继续用输入地点搜索。

```sh
uv run main.py --serial emulator-5554 --location Мурманск \
  --aurora-gps 68.9707 33.0827 --timeout 45
```

`--aurora-gps` 参数顺序为纬度、经度；仅接受 Android 模拟器。脚本核对模拟器 GPS/fused 的最后定位是否匹配。
JSON 记录 requested_coordinates 和 emulator_location_fix；Aurora 首页只显示“current location”，因此不会声称 App 已显示城市名称。
该方式不等同于 Aurora 的自定义地点搜索通过，也不自动把其他城市名称转换成坐标。

## 输出与限制

每次运行输出到 `artifacts/时间戳/`，也可指定 `--output 目录`。

- `result.json`：输入、设备语言/时区/时间、安装来源、各 App 状态、地点/坐标、Aurora 当前 KP 和概率、图层、原始页面文本。
- `windy-layer.png/xml`、`windy-search.png/xml`、`windy-location.png/xml`、`windy.png/xml`：图层、搜索、详情、最终云图。
- `aurora-search.png/xml`（搜索模式）、`aurora.png/xml`：搜索和首页。
- `*-onboarding-*.png/xml`：首次引导。
- `*-failure.png/xml`：失败证据。

单个 App 失败不阻止另一个采集；部分成功状态为 `partial`，进程仍返回非零退出码。
采集时间使用带偏移的 Asia/Shanghai；设备时区另行记录。Windy 预报时间保留原文，不凭设备时区推断完整日期。
云图目前保留截图，不从颜色推算云量百分比。Aurora 指标来自当前首页，不代表未来逐小时预测。

## 根目录安装文件

- `aurora-apks.zip` / `aurora-apks/`：从手机导出的 Aurora 拆分包。
- `com.jrustonapps.myauroraforecast_26.7.4.xapk` / `aurora-xapk/`：用户提供的包，主 APK 与手机导出主包完全一致。
- `windy-apks/`：手机导出的 ARM64 Windy 包。
- 原有 `windy.apk` 实际为包含 32 位原生库的拆分 ZIP，无法直接安装到当前 ARM64-only 模拟器。
- `my-aurora-forecast.apk` 实际包名为 `cm.aptoide.pt`，是 Aptoide 商店安装器，不是 Aurora。

旧的 `emulator-ru-final` / `emulator-xapk-run` 结果记录的是修复安装来源前的失败，保留供核对。

## 本地验证结果

`artifacts/emulator-fresh-success/result.json` 是使用 `install_aurora.py --fresh` 删除旧安装、重装后，从 Aurora 首次引导开始的完整双 App 运行；整体状态为 `success`。
Aurora 使用明确指定的 GPS 坐标；Windy 搜索「Мурманск」并选择俄语 Clouds 图层。
`artifacts/emulator-gps-complete/result.json` 是此前另一次成功运行。自定义搜索模式的失败证据保留在 `artifacts/aurora-after-installer/`。

## GitHub Actions

实际 workflow 位于 `.github/workflows/android-collect.yml`；根目录 `android-quicktest.yml` 保留为原始 kea2 参考样例。
采用 `reactivecircus/android-emulator-runner@v2`、Ubuntu、API 35 Google Play x86_64 镜像。ARM64 APK 的兼容性在云端实际运行中验证。

手动触发时可输入 Windy 地点和 Aurora GPS 经纬度。首次部署会自动设置俄语、Europe/Minsk、安装 App 并处理引导。
Google Play 首次自动更新最多等待 10 分钟，再确认后台授权服务可用。模拟器设置和采集失败时各最多执行 3 次，失败证据保留在 `setup-attempt-*` / `collection-attempt-*`；最终采集仍失败则 workflow 返回失败。
安装包从同仓库 `android-apps` release 获取，不放入 Git 历史；release 文件为 `aurora-apks.zip` 和 `windy-apks.zip`，仅包含 APK。
无论运行成功与否，上传 `artifacts/ci/` 中的 JSON、截图、界面 XML 和日志，保存 7 天。当前没有启用定时调度。

```sh
gh workflow run android-collect.yml --repo XixianLiang/aurora-automation \
  -f location=Мурманск -f latitude=68.9707 -f longitude=33.0827
```
