# FFmpeg Mirror

独立的 FFmpeg 稳定版分发仓库。Git 只保存脚本、配置、测试与压缩实测数据。

发布流程：**上游 ZIP → 校验 → 合并为 TAR+XZ → GitHub Release 保存完整包 → 生成 ≤10 MB 分片 → EdgeOne 中国站直接上传部署**。

## 首次配置

1. 在 [EdgeOne 中国站控制台](https://console.cloud.tencent.com/edgeone/pages)生成 API Token，见[官方说明](https://cloud.tencent.com/document/product/1552/127422)。
2. 在当前仓库的 Actions Secrets 新增 `EDGEONE_API_TOKEN`，值填上一步的 Token。不要写入代码、日志或聊天。
3. 默认项目名 `ffmpeg-mirror`；CLI 会在不存在时创建「直接上传」项目。已有项目必须是直接上传类型。需要改名时，在仓库 Actions Variables 设置 `EDGEONE_PROJECT_NAME`。
4. 在 Actions → **Publish FFmpeg and deploy EdgeOne** → **Run workflow** 运行。访问地址请在托管平台控制台查看，清单位于部署站点的 `/catalog.json`。
5. 如需自定义域名，在 EdgeOne 控制台绑定。实际中国大陆节点可用性、域名备案等以该项目控制台的接入要求为准。

长期下载需在托管平台绑定自定义域名。仓库、Release 与工作流日志不记录部署地址，域名和 DNS 仅在托管平台管理。中国大陆/含中国大陆区域的默认测试链接仅有效三小时，不能写进长期下载配置，见[域名管理](https://edgeone.cloud.tencent.com/pages/document/162936836982489088)。

不需要连接 EdgeOne 的 Git 仓库构建，也不需要填写静态托管构建命令。Action 使用固定版本 EdgeOne CLI，经 API Token 直接部署目录，固定中国站和 production 环境。官方 [CLI 文档](https://cloud.tencent.com/document/product/1552/127423)。

## 更新与失败重试

- 每天 UTC 03:23（北京时间 11:23）检查五种平台的稳定版；也支持手动运行。
- Windows amd64 使用 [Gyan stable essentials ZIP](https://www.gyan.dev/ffmpeg/builds/)。macOS、Linux 的 amd64/arm64 使用 [Martin Riedl stable ZIP](https://ffmpeg.martin-riedl.de/)。不镜像每日快照。
- 根据上游具体地址、SHA-256 与压缩配方生成确定的 Release tag。已有完整 Release 时复用，不重新压缩或覆盖完整包。
- 新版本先上传到 draft Release，全部完整包和清单上传成功后才发布。下载、校验或压缩失败不会进入 EdgeOne 部署。
- 部署阶段从 Release 拉取完整包，校验包及每个文件，重新分片并核对清单，最后部署整个目录。分片只存在于 Runner 临时目录和 EdgeOne，完整包只在 Release。
- 部署成功后在该 Release 写入匿名状态文件 `deployment-<标识>.json`，只记录版本和完成时间，不记录域名、访问链接、项目名或部署 ID。已成功且无更新时跳过部署，减少部署记录与存储占用。
- 部署失败时 Release 仍可用；重新运行会复用完整包并重试部署。Token 缺失时部署步骤明确失败，不会记录成功。
- 勾选手动输入 `force_deploy` 可强制重新部署当前 Release（例如修改域名、缓存配置后）。
- GitHub 可能因公开仓库长时间没有活动暂停定时工作流；在 Actions 页面重新启用即可。

EdgeOne 当前部署快照只包含该 Release 的分片。`catalog.json` 设置 `Cache-Control: no-store`；带内容哈希的分片地址长期缓存。安装器应在清单更新或分片失效时重新读取清单，不将不同版本的分片混用。平台保留历史部署会占用其账户存储，按需在控制台清理；无更新时不会重复部署。

## 完整包、分片与校验

每个平台将两个工具及上游包内全部其他文件（Windows 还包括 FFplay、文档与许可）一起进行 solid TAR+XZ 压缩，使用 LZMA2 `9e`、128 MiB 字典。仅规范化归档时间戳、所有者和文件模式，**文件内容与程序签名字节不变**。上游 ZIP 的 SHA-256 在解包前检查，压缩后对每个文件再做 SHA-256 比对。

完整包名称示例：`ffmpeg-9.0.2-windows-amd64.tar.xz`。在仓库 Releases 页面下载后可直接使用支持 TAR+XZ 的工具解压。

分片为 `.tar.xz.part0001`、`.part0002`……，每片最大 **10,000,000 字节（十进制 10 MB）**。它们是同一压缩包的连续字节，不是可独立解压的档案。必须按清单顺序拼接后解压。

Python 3.11+，无需额外 Python 依赖：

```sh
# 下载国内站分片，校验每片及完整包，并合并为 TAR+XZ
python3 scripts/restore.py --base https://你的域名 --platform windows-amd64 --output downloads/windows

# 本地构建（生成目录均被 Git 忽略）
python3 scripts/sync_ffmpeg.py --output public --packages packages
python3 scripts/sync_ffmpeg.py --output public --verify
python3 -m unittest discover -s tests -v
```

支持的 `platform`：`windows-amd64`、`darwin-amd64`、`darwin-arm64`、`linux-amd64`、`linux-arm64`。下载合并脚本失败时删除临时文件并保留已有完整包，不执行下载的程序。

## 清单契约 v2

GitHub Release 与 EdgeOne 中的 `catalog.json` 内容相同：

- 根字段：`schemaVersion: 2`、`maxPartBytes: 10000000`、`compressionRecipe`、`releaseTag`、`updatedAt`（UTC）、`platforms`。
- `platforms[platform]`：`version`、`provider`、`homepage`、`sources`（上游 ZIP 的 `url/tools/sha256`）、`artifacts`。
- 每个平台一个 artifact：`filename`、`format: "tar.xz"`、`tools`、`size`、`sha256`、`files`、`parts`。
- `files` 记录 TAR 内每个普通文件的 `path/size/mode/sha256`，包含原包许可与文档。
- `parts` 记录每片的 `path/size/sha256`。`path` 相对于清单目录，没有开头 `/`；数组顺序为拼接顺序。
- 分片路径：`ffmpeg/<platform>/<version>/<完整包SHA256>/<filename>.part0001`。

接入程序需限制清单、包体积、展开体积和片数，拒绝路径穿越、符号链接与重复工具，检查每片、完整包及解压文件哈希后再安装。XZ 解码器需支持至少 128 MiB 字典，解压不依赖本机安装 FFmpeg。

## 压缩实测

FFmpeg 9.0.2，保留所有上游文件，十进制 MB。全部解压文件通过 SHA-256 对比：[原始结果](benchmarks/2026-11-09.json)。

| 平台 | 原 ZIP 总和 | TAR+XZ | 7z | TAR+Zstandard |
| --- | ---: | ---: | ---: | ---: |
| Windows amd64 | 114.8 | 34.0 | 35.2 | 36.9 |
| macOS arm64 | 56.7 | 21.2 | 20.4 | 24.4 |
| Linux amd64 | 66.5 | 26.7 | 26.4 | 29.2 |

选择统一 TAR+XZ，接近最优体积且便于跨平台解压；压缩成本由 Action 承担。本机解压加写文件约 0.8–1.6 秒，实际耗时随设备变化。

## 第三方软件

第三方软件不适用本仓库脚本的 MIT 许可，构建来源见 [THIRD_PARTY.md](THIRD_PARTY.md)。
