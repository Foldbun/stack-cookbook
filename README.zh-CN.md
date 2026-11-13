<!-- en-sha: e5792e2ccd1e -->
# Foldbun Stack Cookbook

[English](README.md) | 简体中文

[Foldbun Stack](https://stack.foldbun.com) 视觉模型 API 的可运行示例工程，提供 Python、TypeScript 和 Go 版本。每个用例端到端地解决一个真实的工程问题——数据集预标注、点击分割、收据转 JSON、图片检索等——并给出在真实数据集上实测的吞吐、质量和成本。

在 **https://stack.foldbun.com/cookbook/zh/** 浏览全部用例。

## 快速开始 <!-- #quick-start -->

以下命令需要 API 密钥，而密钥尚未开放（见 [API 密钥](#api-keys)）。除调用 API 之外的部分无需密钥即可运行：`uv run python -m harness check` 会用本地的 API 仿真服务运行全部实现。

```bash
git clone https://github.com/Foldbun/stack-cookbook.git
cd stack-cookbook/use-cases/00-quickstart/python
export FOLDBUN_API_KEY="fbs_..."
uv run main.py
```

curl：`bash ../curl/run.sh`。TypeScript：`cd ../typescript && pnpm install && node main.ts`。Go：`cd ../go && go run .`。

## API 密钥 <!-- #api-keys -->

示例使用在 Foldbun Stack 控制台中创建的密钥调用 API。API 密钥即将推出。在此之前，可以在 [Playground](https://stack.foldbun.com/playground/) 中无需账户试用全部模型；每个用例页面都展示了一次真实运行的实测结果和费用，没有密钥也可以阅读示例。参见[认证](https://stack.foldbun.com/docs/authentication/)。

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
| [08 监控线上集成的成本与时延](use-cases/08-cost-and-latency-monitoring/README.zh-CN.md) | 已充值 |  | ✓ |  |  |
<!-- /usecases -->

公共池：新账户即可运行。已充值：需要充值过的账户，免费池内的调用不扣费。按量扣费：按[价目表](https://stack.foldbun.com/docs/billing/)从余额扣费。

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

`uv run python -m harness check` 在离线状态下运行全部检查，不需要密钥：schema、静态检查、客户端测试，以及每个实现对本地假 API 的运行；假 API 的应答按[公开的 API 描述](https://stack.foldbun.com/docs/openapi.yaml)校验。每次发版前，维护者用真实密钥运行 `harness live`，其结果就是用例页面上的数字。不使用 GitHub Actions。参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 数据与许可证 <!-- #data -->

每张图片和每个数据集都采用允许再分发的许可证，署名表中逐个文件列出。参见 [ATTRIBUTION.md](ATTRIBUTION.md)。

## 帮助与反馈 <!-- #help -->

示例的问题、用例或语言的需求，请提交 [GitHub issue](https://github.com/Foldbun/stack-cookbook/issues)。API 本身的问题请查阅[文档](https://stack.foldbun.com/docs/)。报告安全问题请参见 [SECURITY.md](SECURITY.md)。

## 许可证 <!-- #licence -->

代码以 [MIT 许可证](LICENSE)发布。图片和数据集保留各自的许可证，列在 [ATTRIBUTION.md](ATTRIBUTION.md) 中。
