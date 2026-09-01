# 开发说明

本文件仅记录从公开源代码可重复执行的开发、验证和构建流程。开始任务前请先阅读 `RISKS.md`、`AGENTS.md` 和 `.clinerules`。

## 环境要求

- Python 3.12。
- macOS 主线构建需要 Apple Silicon Mac 和系统自带的 Xcode Command Line Tools。
- Windows 验证构建需要可用的 WebView2 Runtime；具体检测逻辑以代码和发布检查为准。
- 首次安装 Playwright Chromium 需要网络连接。

## 安装依赖与运行

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python run.py
```

Windows PowerShell 中应使用虚拟环境对应的 Python 可执行文件，不得将未验证的系统 Python 路径写入脚本。

## 验证

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/release_check.py
.venv/bin/python -m compileall -q backend weibo_book tests desktop desktop_app.py js_api.py run.py
git diff --check
```

涉及前端 JavaScript 时，还应对改动文件执行 `node --check`。只有在全部验证通过后才能进入构建步骤。

## macOS 构建

`scripts/build_mac.sh` 与 `scripts/build_dmg.sh` 会在解析参数前清除外部 `WEISHUSHU_PROFILE`。无参数和显式 `--user` 均固定为正式用户版身份，只有显式 `--dev` 才设置开发 profile；未知参数或两个以上的冲突参数均以非零状态退出。

```bash
# 日常个人版
bash scripts/build_mac.sh
bash scripts/build_dmg.sh
bash scripts/build_mac.sh --user
bash scripts/build_dmg.sh --user

# 开发版
bash scripts/build_mac.sh --dev
bash scripts/build_dmg.sh --dev
```

`scripts/build_mac.sh` 生成 PyInstaller onedir 应用包，`scripts/build_dmg.sh` 校验 arm64 可执行文件、执行临时签名、生成 DMG 和 SHA-256 文件，并以只读方式挂载检查。用户版和开发版分别使用 `com.weishushu.desktop` 与 `com.weishushu.desktop.dev`，不得互相读取运行数据。当前流程不提供 Apple Developer ID 签名或 Apple 公证。

## Windows 验证构建

Windows 链路使用 `build.spec`、`build_exe.bat` 和 `installer.iss`。它不是用户交付主线；GitHub Actions 已验证 Windows x64 回归、发布门禁、PyInstaller onedir、Inno Setup 和工件上传。仍必须在独立 Windows 环境完成 WebView2、安装、启动、`%LOCALAPPDATA%\\Weishushu` 写入、卸载和数据保留的人工验收，才可将安装程序视为已完成端到端验收。

## 运行时数据边界

- 登录 Cookie 文件 `.weibo_book_cookies`、WebKit 站点数据、日志、档案数据库和生成的电子书都是运行时数据，不是源代码。
- 源码开发态与 `WeishushuDev.app` 使用 `.weibo_book_cookies_dev` 和 `WeishushuDev` 目录；日常个人版 `Weishushu.app` 使用 `.weibo_book_cookies` 和 `Weishushu` 目录。两类 profile 不得复制或交叉读取登录状态。
- frozen 运行身份只由精确可执行文件名决定，不读取运行环境中的 `WEISHUSHU_PROFILE`。默认 Cookie 写入通过 `get_cookie_file_path()` 解析；Chrome 导入使用当前 profile 缓存目录下的 `chrome-import-profile`，开发 profile 不读取正式身份的旧 Windows Cookie。
- Mac 日常个人版同窗 WKWebView 使用系统持久化站点数据存储；源码开发态与开发版 frozen 使用非持久化站点数据存储，关闭后不保留 WebKit Cookie、localStorage、IndexedDB 或缓存，登录恢复只使用 `.weibo_book_cookies_dev`。
- 这些数据不得提交到 Git，不得放入 DMG 或 EXE，不得上传到公开渠道。
- 涉及路径、Cookie、日志、浏览器或打包的变更必须同时考虑 frozen/dev 双路径。开发态可从仓库读取资源，封装态只读资源与当前用户可写数据必须分离。
- 测试样本必须使用合成数据，不得从个人档案或登录文件复制内容。
