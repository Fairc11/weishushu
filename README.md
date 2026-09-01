<div align="center">
  <img src="assets/icon.png" alt="微书薯图标" width="104" />
  <h1>微书薯 Weishushu</h1>
  <p><strong>把你的微博，备份成一本可以永远保存的书</strong></p>
  <p>互动 HTML · PDF · Markdown · 本地媒体档案</p>
  <p>
    <a href="../../releases/latest"><img src="https://img.shields.io/badge/版本-v2.0.1-fa7d3c" alt="版本" /></a>
    <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-000000?logo=apple" alt="macOS" />
    <img src="https://img.shields.io/badge/Windows-x64-0078D4?logo=windows" alt="Windows" />
    <img src="https://img.shields.io/badge/免费-无广告%20·%20无云端-brightgreen" alt="免费无广告" />
  </p>
</div>

---

## 界面预览

<table>
  <tr>
    <td><img src="assets/screenshots/book-grid.png" alt="微博书时间轴" /></td>
    <td><img src="assets/screenshots/book-detail.png" alt="微博详情与评论" /></td>
  </tr>
  <tr>
    <td align="center">微博书时间轴 · 九宫格与实况照片</td>
    <td align="center">单条微博详情 · 评论与关联回复</td>
  </tr>
  <tr>
    <td><img src="assets/screenshots/home-light.png" alt="主界面浅色" /></td>
    <td><img src="assets/screenshots/home-dark.png" alt="主界面深色" /></td>
  </tr>
  <tr>
    <td align="center">主界面 · 浅色主题</td>
    <td align="center">主界面 · 深色主题</td>
  </tr>
</table>

## 下载

| 平台 | 文件 | 说明 |
|------|------|------|
| macOS（Apple Silicon，macOS 12+） | [Weishushu-v2.0.1-macOS-arm64.dmg](../../releases/latest/download/Weishushu-v2.0.1-macOS-arm64.dmg) | 拖入「应用程序」即可 |
| Windows 10/11 x64 | [Weishushu_Setup_v2.0.1.exe](../../releases/latest/download/Weishushu_Setup_v2.0.1.exe) | 安装程序，含卸载 |

当前版本 **v2.0.1**（2026-09-01 发布）。每个 Release 都附带对应的 `.sha256` 校验文件，历史版本与完整更新说明见 [Releases](../../releases)。

> **关于安全警告**：安装包当前使用 ad-hoc 签名、未经 Apple 公证与微软代码签名，首次打开时 macOS Gatekeeper / Windows SmartScreen 会提示拦截，这是正常现象。放行方法：
>
> - **macOS**：先正常双击一次（会被拦截），然后打开「系统设置 → 隐私与安全性」，在「安全性」一栏找到 Weishushu 的拦截记录，点「仍要打开」（如下图）。如果弹窗只有「完成 / 移到废纸篓」两个按钮且设置里找不到记录，在「终端」执行一行命令即可永久放行：`xattr -dr com.apple.quarantine /Applications/Weishushu.app`
>
>   <img src="assets/screenshots/gatekeeper-allow.png" alt="macOS 隐私与安全性中的仍要打开按钮位置" width="640" />
>
> - **Windows**：在 SmartScreen 弹窗选择「更多信息 → 仍要运行」。
>
> 下载后可用页面提供的 SHA-256 校验文件确认安装包未被篡改。

## 功能

**归档与呈现**

- **本人微博书**：为当前登录账号生成完整微博书，支持增量更新——只抓新增内容，并自动复查最近 5 条，后续编辑、删除的微博也能同步更正
- **备份任意博主**：输入昵称搜索或粘贴主页链接，即可为其他博主建立同样的微博书，后续一样支持增量更新（暂不含评论与关注资料）
- **仿微博 App 的还原界面**：蓝 V 认证标识、性别符号、转评赞互动图标、话题卡片、引用转发卡片、一至九图宫格，长文保留原始换行，全部按年份—月份组织成时间目录
- **实况照片完整提取**：iPhone 实况照片同时保存静态图和配对视频，在书里原位播放，不丢任何一半
- **混合媒体一条不漏**：同一条微博里图片和视频混发（最多 18 项）也能完整归档、混排展示
- **评论提取**：每条微博归档最新一级评论与关联回复，评论里的图片也离线保存在本地
- **关注资料页签**：归档你关注的博主与超话资料，和时间轴一起翻阅
- **PDF 与 Markdown 导出**：PDF 是带封面、目录和年-月章节的「书版式」（A4 排版，适合打印存档），Markdown 按年-月分节、保留完整评论层级，方便导入笔记软件
- **媒体按年-月整理**：图片、视频、实况照片原样保存在本地，按发布年-月分目录存放，翻文件夹就能按时间找到；旧版档案会自动无损迁移到新目录结构

**安心与可控**

- **扫码登录**：用微博 App 扫码，不输入密码；登录状态只保存在你自己的电脑上，文件权限收紧到仅本人可读，不上传任何服务器
- **本地优先**：微博书、媒体档案、登录状态全部在你选择的本地目录，没有任何云端同步，换电脑只需重新登录
- **中断可续、崩溃不丢**：任务可暂停、可恢复，断网、关电脑、意外崩溃都不丢已归档内容，下次接着跑
- **限流保护**：内置请求限速，触发平台限流时自动暂停等待，不硬闯风控
- **退出登录**：一键清除本机登录状态；卸载时可选择清理全部应用数据，只保留你自己的微博书档案
- **浅色 / 深色双主题**：跟随系统或手动切换

## 快速上手

1. 下载并安装，首次启动会看到一份风险须知，**滚动阅读到底部**后确认继续。
2. 点击「扫码登录」，用微博 App 扫描二维码。
3. 选择「新建微博书」，挑一个保存目录，开始归档。
4. 完成后双击档案里的 `微博书.html`，离线翻阅你的微博书。

## 它不会做什么（10 不做）

微书薯是只读工具，只处理单设备、单登录状态下**本人可见**的数据。它永远不实现：

评论发布 · 点赞自动化 · 关注自动化 · 转发自动化 · 多账号池 · 代理池 / IP 轮询 · Cookie 池 / 账号包 · 验证码绕过 · OAuth 商业用途 · 跨设备同步登录

使用本工具访问微博接口仍可能触发平台风控，账号处理结果不可预测。继续使用即表示你理解并自行承担账号风险。

## 常见问题

**微博书保存在哪里？**
保存在你自己选择的本地目录，登录状态不会随档案复制；换电脑后在新环境重新登录即可。

**会泄露我的账号吗？**
不会。登录 Cookie 只写入当前设备的当前用户环境，文件权限收紧到仅本人可读，不上传任何服务器。

**软件收费吗？**
不收费，也没有广告和任何云端服务。

**为什么首次打开会被系统拦截？**
安装包是 ad-hoc 签名、未购买商业代码签名证书，系统的拦截提示针对的是「未签名」而非「有毒」。用上面「关于安全警告」里的方法放行即可，SHA-256 校验值会随每个 Release 提供。

## 问题反馈

遇到问题或有功能建议，直接在 [GitHub Issues](../../issues) 提交即可。反馈问题时请附上：你的系统版本（macOS / Windows）、操作到哪一步出错、界面上的中文错误提示原文。**不要**在 Issue 里贴 Cookie、账号密码或微博正文等隐私内容。

## 源代码

核心源代码已在本仓库公开，与下载页面的安装包同源，全部位于 `src/` 目录：

```text
src
├── weibo_book/         业务核心：微博提取、媒体抓取、电子书生成
│   └── archive/        本地档案：SQLite 存储、增量同步、断点续跑
├── backend/            FastAPI 服务与前端界面
│   └── app/
│       ├── routers/    接口层
│       ├── services/   任务调度与状态管理
│       ├── templates/  页面模板
│       └── static/     前端样式与脚本
├── desktop/            桌面窗口与内嵌浏览器
├── tests/              回归测试（测试数据均为合成数据）
├── scripts/            构建与发布校验
└── docs/               构建与开发说明
```

克隆后进入 `src/` 目录，按 `docs/DEVELOPMENT.md` 可以从源码自行构建出相同的安装包。

## 许可

源代码以 [MIT 许可](LICENSE)公开。
