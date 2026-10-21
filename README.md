# 图文生成短视频流水线（POC）

这条流水线把商品图片和卖点变成一条可以直接发 TikTok 的 15 秒竖屏短视频：

1. **M3（MiniMax-M3.1-Flash）写分镜**，并检查分镜质量
2. **用户在网页上修改、确认**
3. **H3（MiniMax-H3-Max）并行生成三个镜头**，M3 逐个看视频做质检
4. **ffmpeg 合成成片**：统一 1080×1920、加中文字幕、做转场

测试用的商品见 [samples/](samples/README.md)。

## 功能对照

| 要求 | 实现 |
|---|---|
| M3 把商品图片和卖点转成分镜脚本 | `core/storyboard.py`：M3 读图，写 3 个镜头（画面、运镜、音效、中文字幕） |
| 用户确认后调用 H3 生成视频 | 网页上编辑分镜，点“确认分镜”后分镜锁定，开始生成（`pipeline.approve` → `pipeline.run`） |
| 分镜级重试 | 每个镜头固定一列，从分镜到视频到重试都在同一列。可以直接在这一列里改画面、运镜、音效，再点“按修改重新生成”；只重做这一个镜头（`pipeline.retry_shot`）。字幕改完只需重新合成（`pipeline.edit_caption`）。底部灰色的“原提示词”是上一次实际发给 H3 的内容，只读 |
| 失败续跑 | 三个镜头并行生成。每次状态变化都立即写入 `state.json`。“生成 / 继续”会跳过已完成的镜头；生成中的镜头用原 task_id 继续查询，**不会重复付费**；失败的镜头会重新提交 |
| M3 质量检查 | **分镜 QC**：确认前检查一次，不通过就自动修订一次。**视频 QC**：M3 直接看 mp4 和原图对比，不通过就带着修正提示自动重试 1 次，仍不通过就标记出来交给用户 |
| 多个分镜整合输出 | `core/assemble.py`：输出 1080×1920、30fps 的 MP4，带 H3 生成的音轨；中文字幕用 Pillow 渲染（粗体描边，【】里的关键词显示为黄色高亮，淡入），镜头之间 0.3 秒交叉淡化，可以在网页上下载 |

## 本地运行

需要 **Python 3.12 或以上**。

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

然后在 `.env` 里填写：
- `MINIMAX_API_KEY`
- `MINIMAX_API_BASE`：国际站 key 用 `https://api.minimax.io`，国内站 key 用 `https://api.minimaxi.com`
- `APP_PASSWORD`（可选）

此外还需要系统里装了 `ffmpeg`（macOS：`brew install ffmpeg`）。

```bash
.venv/bin/streamlit run app.py
```

> 修改了 `core/` 里的代码后，需要重启 Streamlit。它只会自动重新加载 `app.py`。

## 命令行（和网页调用的是同一套代码）

```bash
.venv/bin/python cli.py demo --sample red_hightop_sneaker
```

`demo` 会自动跑完全程：新建 → 分镜 → 自动确认 → 生成 → 合成。分步命令如下：

```bash
.venv/bin/python cli.py new --sample ceramic_mug
```

```bash
.venv/bin/python cli.py storyboard <id>
```

```bash
.venv/bin/python cli.py approve <id> --resolution 480P
```

```bash
.venv/bin/python cli.py run <id>
```

```bash
.venv/bin/python cli.py retry <id> 2 --visual "new English visual for shot 2"
```

```bash
.venv/bin/python cli.py assemble <id>
```

- `show <id>` 查看分镜和每个镜头的状态，`list` 列出所有项目。
- 每个项目的所有文件都在 `data/projects/<id>/` 下：`storyboard.json`、`state.json`、`shots/`、`final.mp4`。

## 测试和代码检查

```bash
.venv/bin/python -m unittest discover tests
```

这些是离线测试，不调用 API、不花钱。覆盖了：
- 正常流程和合成
- QC 自动重试一次、重试用完后标记交给用户
- 超时或下载失败后续跑（不重复提交）
- 失败后重新提交
- 内容审核拦截
- 手动重试只影响一个镜头
- 分镜自动修订
- 字幕长度和【】格式
- 静音视频判为不通过
- 上传图片校验

代码风格用 ruff 检查，配置在 `pyproject.toml`：

```bash
ruff check .
```

## 部署到 Streamlit Community Cloud

1. 把仓库推到 GitHub。
2. 在 share.streamlit.io 新建 app，入口文件选 `app.py`；在 **Advanced settings** 里把 Python 版本选为 **3.12 或以上**。
3. 在 app 的 **Secrets** 里填写：
   ```toml
   MINIMAX_API_KEY = "..."
   MINIMAX_API_BASE = "https://api.minimaxi.com"  # 国内站 key；国际站 key 用 https://api.minimax.io
   APP_PASSWORD = "..."                            # 可选：访问密码，不填则不需要密码
   ```
4. `packages.txt` 会让平台自动安装 `ffmpeg` 和中文字体 `fonts-noto-cjk`。

Streamlit Cloud 的文件系统是临时的，服务器重启后已生成的项目会丢失，POC 阶段可以接受。
