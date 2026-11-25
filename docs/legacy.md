# 旧版位置

ContribOS v0.1 保存在 Git 标签 `legacy-v0.1-final` 中。
当前 `redesign/v2` 分支从 v2 骨架重新开始，不直接使用旧版代码。

查看旧版文件：

```sh
git show legacy-v0.1-final:README.md
git show legacy-v0.1-final:app/security.py
```

旧版 `data/`、`.local-demo/` 和 `.env` 本地文件保持原样。
v2 默认使用 `~/.contribos/`，不自动读取或迁移旧数据。

手动导入可运行 `contribos import-legacy --db /path/to/legacy.sqlite`。命令只读打开旧库，导入仓库、机会、最新偏好、收藏和忽略标记，以及实际发布到 GitHub 的 PR。PR 可在「贡献记录」中查看。重复导入时保留 v2 已有记录。
