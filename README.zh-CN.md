<!-- en-sha: 24bbcd47b46a -->
# Foldbun Stack Cookbook

[English](README.md) | 简体中文

[Foldbun Stack](https://stack.foldbun.com) 视觉模型 API 的可运行示例工程，提供 Python、TypeScript 和 Go 版本。每个用例端到端地解决一个真实的工程问题——数据集预标注、点击分割、收据转 JSON、图片检索等——并给出在真实数据集上实测的吞吐、质量和成本。

在 **https://stack.foldbun.com/cookbook/zh/** 浏览全部用例。

## 快速开始 <!-- #quick-start -->

不需要密钥，也不需要注册：示例在 Foldbun Stack 的免费层上、以 Cookbook 自己的项目运行（见[示例如何认证](#authentication)）。

```bash
git clone https://github.com/Foldbun/stack-cookbook.git
cd stack-cookbook/use-cases/00-quickstart/python
uv run main.py
```

curl：`bash ../curl/run.sh`。TypeScript：`cd ../typescript && pnpm install && node main.ts`。Go：`cd ../go && go run .`。

每个用例都有一个小的 smoke 运行，用法相同。完整运行规模更大，需要 API 密钥。

## 示例如何认证 <!-- #authentication -->

调用 API 有两种方式，示例根据环境变量自动选择：

- **免费层（默认）。** 没有设置 `FOLDBUN_API_KEY` 时，示例只发送请求头 `X-Foldbun-Project: fbp_2kIj7bWkULQkLAbceHhfLW4eY6O0`，也就是 Cookbook 自己的公共项目。七个模型全部免费，只接受内联 base64 图片（解码后最大 4 MiB）：没有图片上传、没有图片 handle、没有批处理端点，也没有幂等键。限额按项目计，随模型的重量级别而不同，另有按客户端 IP 的份额。Cookbook 的项目由所有读者共用，可能比较忙；免费层排在付费流量之后，不承诺可用性。被拒绝的调用返回 HTTP 429 并带 `Retry-After`，示例会等待后继续。部分地区无法使用免费层（403）。参见[免费层](https://stack.foldbun.com/docs/free-tier/)和[限速](https://stack.foldbun.com/docs/rate-limits/)。
- **你自己的项目。** 在[控制台](https://stack.foldbun.com/console/)里创建项目（每个账户都有一个默认项目），把 `FOLDBUN_PROJECT_ID` 设为它的 ID。示例随后按你项目自己的限额计数，而不是共享的限额：轻量、中等、重型模型每分钟分别为 30、10、2 次请求，每日分别为 2,000、500、50 次。
- **API 密钥（付费）。** 把 `FOLDBUN_API_KEY` 设为控制台里的密钥（`fbs_...`）。此时每次调用都按标价扣费；如果同时设置了项目 ID，该项目会被记为调用的来源。有密钥才能使用上传、handle 和批处理。带密钥的调用没有任何免费额度。

每个用例的 smoke 运行都不需要密钥。完整运行需要 `FOLDBUN_API_KEY`：它们规模大，有些还用到上传、handle 或批处理；没有密钥时，完整运行会说明原因并退出。`FOLDBUN_MAX_CREDITS` 限制带密钥的运行最多花多少：客户端会拒绝可能超出它的调用。在免费层上，示例会打印所运行内容的标价，并标注“免费层，不扣费”。参见[认证](https://stack.foldbun.com/docs/authentication/)。

## 用例 <!-- #use-cases -->

<!-- usecases -->
| 用例 | 层级 | curl | Python | TypeScript | Go |
|---|---|---|---|---|---|
| [00 第一次调用](use-cases/00-quickstart/README.zh-CN.md) | 公共池 | ✓ | ✓ | ✓ | ✓ |
| [01 用文字提示词预标注图像数据集](use-cases/01-dataset-prelabelling/README.zh-CN.md) | 按量扣费 |  | ✓ |  | ✓ |
| [02 点击分割组件与持有密钥的后端](use-cases/02-click-to-segment/README.zh-CN.md) | 已充值 |  | ✓ | ✓ | ✓ |
| [03 收据和发票转为经过校验的 JSON](use-cases/03-receipts-to-json/README.zh-CN.md) | 按量扣费 |  | ✓ | ✓ | ✓ |
| [04 扫描文档转为可入索引的 Markdown 切块](use-cases/04-documents-to-rag-chunks/README.zh-CN.md) | 按量扣费 |  | ✓ |  |  |
| [05 按图、按文字搜索图库，并找出近重复图片](use-cases/05-image-library-search/README.zh-CN.md) | 公共池 |  | ✓ |  | ✓ |
| [06 把商家照片变成统一的商品目录图](use-cases/06-product-photo-pipeline/README.zh-CN.md) | 按量扣费 |  | ✓ |  |  |
| [08 监控线上集成的成本与时延](use-cases/08-cost-and-latency-monitoring/README.zh-CN.md) | 已充值 |  | ✓ |  |  |
<!-- /usecases -->

每个 smoke 运行都在免费层上免费，不需要密钥。完整运行需要 API 密钥，按[价目表](https://stack.foldbun.com/docs/billing/)的标价扣费。

## 语言与工具链 <!-- #languages -->

| 语言 | 测试环境 | 运行方式 |
|---|---|---|
| Python | Python 3.12 与 3.14，[uv](https://docs.astral.sh/uv/) | `uv run main.py` |
| TypeScript | Node 24，pnpm | `node main.ts`（类型剥离；`tsc --noEmit` 只做检查） |
| Go | Go 1.22 及以上 | `go run .` |
| curl | bash、curl、jq | `bash run.sh` |

每个实现目录自成一体：有自己的依赖清单和锁文件，以及从 `clients/<language>/` 复制来的小客户端文件。把目录复制进自己的项目即可运行。客户端文件不是 SDK，也不发布到任何包仓库。

每个实现都是参数相同的命令行程序：`--manifest <path> --out <dir> [--concurrency N] [--limit N] [--resume]`，并按 `contract/` 的说明写出 `<dir>/result.json`。

## 示例如何测试 <!-- #testing -->

`uv run python -m harness check` 在离线状态下运行全部检查，不需要密钥，也不需要项目 ID：schema、静态检查、客户端测试，以及每个实现对本地假 API 的运行；假 API 的应答按[公开的 API 描述](https://stack.foldbun.com/docs/openapi.yaml)校验。每次发版前，维护者用真实密钥运行 `harness live`，其结果就是用例页面上的数字（标价，也就是带密钥运行的花费）。不使用 GitHub Actions。参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 数据与许可证 <!-- #data -->

每张图片和每个数据集都采用允许再分发的许可证，署名表中逐个文件列出。参见 [ATTRIBUTION.md](ATTRIBUTION.md)。

## 帮助与反馈 <!-- #help -->

示例的问题、用例或语言的需求，请提交 [GitHub issue](https://github.com/Foldbun/stack-cookbook/issues)。API 本身的问题请查阅[文档](https://stack.foldbun.com/docs/)。报告安全问题请参见 [SECURITY.md](SECURITY.md)。

## 许可证 <!-- #licence -->

代码以 [MIT 许可证](LICENSE)发布。图片和数据集保留各自的许可证，列在 [ATTRIBUTION.md](ATTRIBUTION.md) 中。
