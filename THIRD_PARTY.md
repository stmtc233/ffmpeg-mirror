# 第三方构建来源

镜像在上游 ZIP 校验通过后将全部普通文件重新归档为 TAR+XZ，保留文件内容，包括包内的许可、README、构建信息和二进制签名。上游 ZIP 的哈希和每个重新归档文件的哈希均记录在 Release 的 `catalog.json`。

| 构建 | 下载与构建说明 | 源代码入口 |
| --- | --- | --- |
| Windows：Gyan stable essentials | https://www.gyan.dev/ffmpeg/builds/ | 同一页面 release 的 “source code @ github”；依赖版本与构建信息见原包 README |
| macOS/Linux：Martin Riedl stable | https://ffmpeg.martin-riedl.de/ | https://git.martin-riedl.de/ffmpeg/build-script |
| FFmpeg | https://ffmpeg.org/ | https://git.ffmpeg.org/ffmpeg.git |

镜像未修改或重新编译二进制，不将第三方软件重新授权为 MIT。
