# 打包 FACAI 桌面客户端（Windows）

Windows 安装包配置已纳入本地 FACAI 服务源码。原生安装包仍需 Windows 实机验收。
首次启动需要系统 Python 3.10+ 和网络安装依赖，不是完整离线包。

## 一步打包（国内网络已配镜像加速）

```powershell
cd packaging\app
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

产物在 `packaging\app\dist\FACAI Setup 1.0.0.exe`。

## 手动步骤（等价）

```powershell
npm install --registry=https://registry.npmmirror.com
# electron 二进制用国内镜像：
$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
npm run dist
```

## 说明

- 已安装客户端默认准备随包服务源码并在本机 127.0.0.1:8080 启动；用户数据独立保存在 Electron userData/data
- 设置 `FACAI_DESKTOP_MODE=remote` 才切换到原远程站点 `https://whstapp.lol`
- 外链自动跳系统浏览器；应用内永远留在工作台
- 版本更新：直接重跑 `build.ps1`（版本号改 `package.json` 的 `version`）
- 当前只有 Linux 模拟资源 + 真实 FACAI 进程测试通过；Windows 原生安装包没有验收

## 另一条更轻的「打包」路径（已上线）

whstapp.lol 本身就是 **PWA（可安装应用）**：
- 手机浏览器打开 → 添加到主屏幕 = 全屏 App（含图标）
- 电脑 Chrome/Edge 地址栏右侧「安装」按钮 = 独立窗口应用
- 零打包成本，与网页版同步更新
