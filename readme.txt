市场指数发布服务。现有报价合同、单位整理、异常处理、商户面板和链式指标模块产生带版本的发布记录。

环境：Linux，Python 3.11.2 或更新的兼容版本。所有依赖来自 Python 标准库，无需安装第三方包。

测试：python3 -m unittest discover -s tests/index_vintages -p 'test_*.py'
构建：python3 -m compileall -q market_index_bureau tests/index_vintages
验收：python3 -m market_index_bureau.publisher.cli fixtures/demo.json --database /tmp/market_index_bureau-acceptance.sqlite3
所有输入均为本地合成数据，运行不需要网络。

目录：market_index_bureau 保存业务处理与存储模块；tests/index_vintages 保存业务回归；fixtures/demo.json 为原有业务样例，fixtures/analysis 为经营分析资料及字段说明。

原有验收命令每次使用新的 SQLite 路径，避免复用上次状态。新增输入完整性检查：
python3 -m unittest discover -s tests/analysis_inputs -v

重述与桥接分析（回答“涨出来的是经济变化还是口径/样本变化”）：
python3 -m market_index_bureau.restatement --input fixtures/analysis/input.json \
  --database /tmp/market_index_bureau-restatement.sqlite3 \
  --output /tmp/market_index_bureau-restatement.json
输出含 publication_id、vintages、contributions、bridge_status、reconciliation；
同一输入重复运行走幂等重放，不新增版本行。两个协会回归例位于
fixtures/analysis/regressions：sample_replacement_no_price_change.json
（价格没变但样本换了）与 late_correction_after_publication.json
（原发布日期之后才收到更正），边界例 gap_conflict_invalid_price.json
覆盖样本断档、类别映射冲突与零价格排除。

重述方法摘要（完整取舍见输出 method_notes）：
- 权重基期：系统时点可见的最早月期，基期在样商户标准数量份额固定；新入样商户
  以入样期数量相对基期总量的规模引入，不回写历史。
- 新口径水平 headline_level：当期篮子固定基期刻度权重的加权平均价格，含样本
  更替与分类改版影响；旧口径可比变化 matched_index：仅用相邻两期共同在样且无
  映射冲突的匹配键计算加权价格相对数并链式接续，锚定原发布水平。
- 桥接：Bennet 恒等分解为 matched_price / sample_entry / sample_exit /
  weight_mix / category_migration（迟到修订在跨 vintage 桥中归 late_revision），
  贡献以未舍入值求和，残差超过 1e-9 即判不平衡，没有“其他”兜底项。
- 断档或映射冲突明确标记 not_comparable，沿用上一可比水平，不补平滑增长率；
  缺报价/非正价格排除而不是当零价格；异常报价只标记不删除。
- 双时态：每个 vintage 只使用 known_at <= as_of 的报价、membership、mapping
  版本；迟到更正产生新重述版，原发布与链式历史只插入不覆盖。同一 observation_id
  的 revision 取最高版本，旧 revision 不重复计权。贡献经 evidence 与
  restatement_evidence 表可追到原报价 observation_id、revision 与 mapping_id。

容器运行可使用已经预装的 ccusr/prepared:3f007a95fcee-4441968e5ff6 镜像；将当前仓库只读挂载至 /source，在容器内复制到 /tmp/product 后运行上述命令。依赖准备和正式运行分离，运行过程中不需要网络访问。
