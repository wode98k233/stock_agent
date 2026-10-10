"""取证层：从 trace / 回答 / 用户问题里抽取可核验的证据。

设计文档 §7。核心原则：
- **直查 `steps.output` 全量**，不走 `collector.collect_run` 的 digest 截断
  （digest 把工具返回裁到 1200 字，核验数字有没有出处在那里做不了）
- 规则能判的绝不交给 LLM；规则产出的是**待复核对象**（`unverified`），不是定罪
"""
