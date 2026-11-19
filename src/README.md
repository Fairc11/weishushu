<div align="center">
  <img src="assets/icon.png" alt="微书薯图标" width="144" />
  <h1>微书薯 Weishushu</h1>
  <p><strong>把你的微博，备份成一本可以永远保存的书</strong></p>
  <p>互动 HTML · PDF · Markdown · 本地媒体档案</p>
  <p>
    <img src="https://img.shields.io/badge/macOS%2026-Apple%20Silicon-000000?logo=apple" alt="macOS 26 Apple Silicon" />
    <img src="https://img.shields.io/badge/Windows-10%20%2F%2011%20x64-0078D4?logo=windows" alt="Windows 10 / 11 x64" />
    <img src="https://img.shields.io/badge/免费-无广告%20·%20无云端-brightgreen" alt="免费无广告" />
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-fa7d3c" alt="MIT 许可证" /></a>
  </p>
  <p>
    <a href="https://github.com/Fairc11/weishushu/releases/latest"><strong>下载最新版本</strong></a>　·　
    <a href="https://github.com/Fairc11/weishushu/releases">更新记录</a>　·　
    <a href="https://github.com/Fairc11/weishushu/issues">问题反馈</a>
  </p>
  <br />
  <img src="assets/screenshots/app-light.png" alt="微书薯主界面：macOS 26 液态玻璃下的微博书库" width="880" />
</div>

> **风险须知**：仅供单设备、单登录状态的个人归档使用，使用前请阅读[风险与使用边界](RISKS.md)。永远不做：评论发布、点赞自动化、关注自动化、转发自动化、多账号池、代理池、Cookie 池或账号包、验证码绕过、OAuth 商业用途、跨设备同步登录。

## 下载

<table align="center">
  <tr>
    <th width="480" align="center">macOS</th>
    <th width="480" align="center">Windows</th>
  </tr>
  <tr>
    <td align="center">Apple Silicon · macOS 26 及以上</td>
    <td align="center">Windows 10 / 11 · x64</td>
  </tr>
  <tr>
    <td align="center"><a href="https://github.com/Fairc11/weishushu/releases/latest">下载 DMG</a></td>
    <td align="center"><a href="https://github.com/Fairc11/weishushu/releases/latest">下载安装程序</a></td>
  </tr>
  <tr>
    <td align="center">打开 DMG，拖入「应用程序」</td>
    <td align="center">运行安装程序，按提示安装</td>
  </tr>
</table>

免费、无广告。安装包已包含运行所需组件，不需要另行安装 Python、Chrome、Playwright 或 Docker。每个安装包附带 `.sha256` 校验文件。

系统低于 macOS 26 的 Mac 请使用 [v2.1.0](https://github.com/Fairc11/weishushu/releases/tag/v2.1.0)（macOS 12 及以上）。首次打开被系统拦截、或从旧版更新后打不开，见下方[常见问题](#常见问题)。

## 三步开始

1. **扫码登录**：打开微书薯，阅读并确认风险须知，用微博 App 扫码登录，不需要输入密码。
2. **选择备份**：在「备份本人」或「备份他人」选好保存文件夹，在「备份设置」里选择备份范围（近 3 个月 / 1 年 / 2 年 / 全部），开始备份。
3. **离线阅读**：备份完成后，在「微博书库」点「阅读」即可打开这本微博书；以后点「新增备份」只抓新内容。

## 看看它的样子

截图中的账号、正文、评论、配图及统计值均为演示数据。

<table>
  <tr>
    <td><img src="assets/screenshots/reader-grid.png" alt="微博书时间轴三栏" /></td>
    <td><img src="assets/screenshots/reader-detail.png" alt="单条微博详情与评论" /></td>
  </tr>
  <tr>
    <td align="center">微博书 · 时间目录、正文与资料三栏</td>
    <td align="center">单条微博详情 · 评论与关联回复</td>
  </tr>
  <tr>
    <td colspan="2"><img src="assets/screenshots/app-dark.png" alt="深色主题与备份设置" /></td>
  </tr>
  <tr>
    <td colspan="2" align="center">深色主题 · 每本微博书的备份设置</td>
  </tr>
</table>

## 能做什么

<table align="center">
  <tr>
    <th width="320" align="center">保存微博</th>
    <th width="320" align="center">离线阅读</th>
    <th width="320" align="center">管理档案</th>
  </tr>
  <tr>
    <td align="center">本人及任意博主<br>分段备份与增量更新<br>超话补漏<br>图片、视频与实况照片</td>
    <td align="center">年月时间目录<br>正文搜索与详情<br>微博、超话、相册、关注<br>HTML / PDF / Markdown</td>
    <td align="center">微博书库一键阅读<br>每本书独立的备份设置<br>补充更早的微博<br>占用空间一目了然</td>
  </tr>
</table>

<details>
<summary>展开完整功能说明</summary>

**备份**

- **本人与任意博主**：本人微博书包含评论与关注资料；其他博主可用昵称搜索，或粘贴主页链接、分享文本（不含评论与关注资料）。
- **分段备份与增量更新**：首次可以只备份近期，之后在「备份设置」里往前补充；「新增备份」只抓新内容，并复查最近 5 条，后来编辑或删除的微博也会更正。
- **超话补漏**：认证博主只发在超话、没有同步到主页的微博，备份时自动检查补入。
- **完整媒体**：图片、视频、实况照片（静态图与配对视频）、评论图片都保存在本地，按年-月整理。
- **中断可续**：断网、关机、意外退出都不会丢失已备份的内容，下次接着备份。

**阅读**

- **仿微博的阅读界面**：认证标识、转评赞、话题、引用转发、九宫格，按年-月组织成时间目录。
- **宽屏三栏**：窗口宽 1200px 以上时为时间目录、正文、资料三栏；顶栏一行切换「微博 / 超话 / 相册 / 关注」。
- **搜索**：正文、转发原文、超话名都能搜，大小写、全半角、中文数字自动互通。
- **资料页**：那年今天、置顶变迁、头像相册、封面横幅、粉丝变迁。
- **PDF 与 Markdown**：PDF 是带封面、目录、年-月章节的书版式；两种格式按需开启，互动 HTML 始终生成。
- **升级版式**：旧档案可以在本地升级到最新版式，不联网、不改动数据与媒体。

**应用**

- **微博书库**：本机所有微博书集中管理，档案放在不同文件夹也能找到。
- **新版本提醒**：有新版本时弹窗显示更新说明；macOS 版可一键下载、校验并替换。
- **外观**：macOS 26 使用系统液态玻璃；浅色 / 深色主题跟随系统或手动切换。

</details>

## 安全与隐私

- **只读工具**：只处理单设备、单登录状态下本人可见的数据；平台风控结果不可预测，继续使用即表示你理解并自行承担账号风险。完整边界见[风险与使用边界](RISKS.md)。
- **扫码登录，不碰密码**：登录状态只保存在你自己电脑的当前用户环境中，文件权限仅本人可读，不上传任何服务器。
- **完全本地**：微博书、媒体与登录状态都在本机，没有任何云端服务。
- **限流保护**：内置请求限速，遇到平台限流自动暂停等待。

### 账号与个人数据隔离

- 本仓库与官方安装包不包含维护者的登录 Cookie、站点数据、个人微博书或运行日志。
- 每个人都必须在自己的设备上登录自己的账号。登录数据不属于微博书档案，不随档案复制，不应上传、分享或跨设备同步。
- 换电脑、Cookie 过期或登录失效后，重新扫码登录即可。

## 常见问题

<details>
<summary>首次打开被系统拦截怎么办？</summary>

安装包未购买商业代码签名证书，首次打开时 macOS Gatekeeper 或 Windows SmartScreen 会提示拦截，这是正常现象。

- **macOS**：先双击打开一次（会被拦截），再打开「系统设置 → 隐私与安全性」，在「安全性」一栏点「仍要打开」。如果找不到这条记录，在「终端」执行：`xattr -dr com.apple.quarantine /Applications/Weishushu.app`

  <img src="assets/screenshots/gatekeeper-allow.png" alt="macOS 隐私与安全性中的仍要打开按钮位置" width="640" />

- **Windows**：在 SmartScreen 弹窗中选择「更多信息 → 仍要运行」。

可用下载页提供的 `.sha256` 文件确认安装包未被篡改。

</details>

<details>
<summary>更新到 v2.2.0 后 Mac 打不开？</summary>

v2.2.0 起 Mac 版需要 macOS 26。如果系统低于 macOS 26 且已在应用内更新：打开废纸篓，把名为 `Weishushu-before-2.1.0-…app` 的旧版拖回「应用程序」，并改名为 `Weishushu.app`，即可恢复旧版。档案与登录状态不受影响。

</details>

<details>
<summary>微博书保存在哪里？能拷到别的电脑吗？</summary>

保存在你自己选择的文件夹。拷贝时要复制整个档案文件夹（HTML 依赖旁边的「数据与媒体」文件夹），通过网盘或微信发送前建议压缩成 zip。登录状态不会随档案复制，在新电脑上重新登录即可继续更新。每个档案里的「!请先阅读.txt」有详细说明。

</details>

<details>
<summary>软件收费吗？</summary>

不收费，没有广告，也没有任何云端服务。

</details>

## 反馈

在 [GitHub Issues](https://github.com/Fairc11/weishushu/issues) 提交问题或建议，请附上系统版本、操作到哪一步出错、界面上的错误提示原文。**不要**在 Issue 里贴 Cookie、密码或微博正文等隐私内容。

## 源代码与许可

源代码以 [MIT 许可证](LICENSE)公开，全部位于 [`src/`](src/) 目录，与最新 Release 安装包为同一版本。按[开发说明](docs/DEVELOPMENT.md)可以自行构建。

<details>
<summary>源码目录</summary>

```text
src
├── weibo_book/         业务核心：微博提取、媒体抓取、电子书生成
│   └── archive/        本地档案：SQLite 存储、增量同步、断点续跑
├── backend/            FastAPI 服务与前端界面
├── desktop/            桌面窗口与原生外观
├── tests/              回归测试（测试数据均为合成数据）
├── scripts/            构建与发布校验
└── docs/               构建与开发说明
```

</details>
