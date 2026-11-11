# 仓库清理报告

日期：2026-11-11。完整工作区先备份，再按逐文件 SHA256 清理。未使用 git clean、reset、强制丢弃或删除业务数据。原源码提交 884d015 与全部 app/tests 有效源码保留；未提交的 repair-v1 证据、Plan16 原件和索引单独保全。

## 范围、计数和体积

清理前：Git 索引 2,879 文件；工作树（不含 .git，含 .venv/缓存/本地数据）6,573 文件、167,454,090 逻辑字节；du 为仓库 230 MiB，其中 .git 56 MiB。清理完成时从该工作树移出 **3,512 文件，90,425,024 字节**。最终交付口径及清理后实测见下方最终快照。

这两个口径不能混用：测试会重新生成缓存，Wheel 构建有临时副本，新增文档/试用数据库也会改变实际工作树。Git 历史与虚拟环境保留，不能用源码净减少推导 .git 体积下降。

## 分类

| 分类 | 文件/范围 | 处理与理由 |
| --- | --- | --- |
| KEEP | app 的 257 个生产文件；全部有效 Python/JS 测试、fixtures、pyproject、部署容器/Compose、scripts 下实际引用工具 | 运行/构建/测试需要；无生产源码删除 |
| KEEP_HISTORY | Plan14 全部 28 份 Artifact、index 与 final-audit、Plan15 index 与全部 161 份 Artifact、旧 Node command graph；tasks.md/ADR/迁移恢复文档；repair 部署回滚、source review、review resolution | 测试按哈希读取或有独立决策/恢复价值，原字节保留 |
| REMOVE | 2,257 份重复过程证据、执行/观察脚本、原始 JUnit、过程快照 | 已逐文件归档；不再作为当前交付依赖。原始 JUnit 有用户路径，未直接提交 |
| REMOVE | 14 份旧单任务审计、被实现说明替代的提案及设计提示 | 全部已在初始私有备份逐字节核验；当前使用说明已合并 |
| REMOVE | 1,239 份 pycache/pytest/ruff 缓存 | 可再生成，删除前仍做归档核验 |
| REMOVE | agent-harness-reset-v1.zip、payload.json | 未被代码/测试/部署引用的临时压缩包和模型探针输入 |
| REVIEW | .env、data/、.venv、contribos.egg-info，以及根 docs 下仍有实现关联的工程说明 | .env/数据绝不删除；环境与包元数据仍供本机使用；工程说明暂保留避免失去运行契约。均按既有忽略规则管理本地私有文件 |

历史证据主要从 docs/agent-harness-reset-v1 与 docs/acceptance 移出，公开保留必要结论、原始文件哈希和 [恢复索引](HISTORY_INDEX.md)，没有为维护旧报告增加新基础设施。超过 352 个失效 Markdown 链接已导向归档索引，历史结论/Hash/完成 Phase 文本保留。

## 可恢复性与隐私

仓库外私有目录 `~/Documents/contribos-private-archives/contribos-maintenance-20261111T020132Z` 为 0700，文件 0600。workspace-before.tar.gz 包含 Git、完整初始工作树和未提交文件；removed-files.tar.gz 包含第一批 3,526 文件，逐个 SHA256 已验证；追加 14 份旧文档在 workspace-before.tar.gz 中逐字节核验。完整最终文件级分类/哈希见私有 cleanup-actual.json。公开索引保留最终证据原始哈希，未篡改历史 52/2151/184/12 结论。未提交的原始 JUnit 没有进入新 Commit。

恢复先校验归档哈希，解压到新的私有空目录，再按需复制；不要覆盖现有业务目录。已提交原件还可 git show <commit>:<path> 恢复。不要把私有归档公开上传。

## 功能影响检查

- 清理前检索引用；完整回归发现辅助模块间接依赖 Plan14 index/final-audit 与 Artifact 全集，遗漏的 28 文件已从私有备份逐字节恢复并重新归类 KEEP_HISTORY。现存必需 fixtures 哈希未变，相关测试另行重跑，未改断言。
- 有效 app 与既有测试源文件没有删除或编辑；repair-v1 的 257 文件候选源码在 884d015 中保留。
- 新增默认入口固定 Fake、loopback、独立数据/Artifact；未加载 .env 或操作既有 Gateway/Compose。
- import、编译、Wheel/静态资源、实际 HTTP 业务、真浏览器及完整回归结果统一见 [本轮验收](../ACCEPTANCE_REPORT.md)。

## 后续建议

工程参考文档暂保留（REVIEW），下一轮仅在确认调用关系后合并；不要继续堆叠执行快照。私有原始证据按本地备份策略管理，本轮不销毁。没有稳定 Tag，历史恢复依赖保留分支/提交和私有归档；本轮不创建或删除 Tag。

## 最终快照

验收结束、试用实例停止后的快照（2026-11-11T02:34:52.860635+00:00）：Git 索引 **748 文件**（之前 2,879）；工作树不含 .git、包含保留的 .venv 和试用数据，**3091 文件**（之前 6,573），约 **80.94 MB 逻辑数据**（之前 167.45 MB）。整个仓库占盘约 142 MiB（之前约 230 MiB，包含 .git）；Git 历史保留，后续 Commit 对象会带来小幅增长。

初始文件净移出 3,512 份、90,425,024 字节；测试重新生成的 294 份项目缓存另外在私有归档核验后再次清理。新文档/脚本、保留的试用数据库和环境元数据使文件数/体积不等于简单减法。逐文件分类与原始哈希见私有 cleanup-actual.json；功能结果见公开 [验收报告](../ACCEPTANCE_REPORT.md) 和 [机器结果](acceptance-results.json)。
