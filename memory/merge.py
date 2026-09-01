"""
记忆系统 — 合并 / 冲突消解（v2 M3）

Merger: 同标的同主题新记忆写入时标记旧记忆 superseded；检测冲突并记录。

核心规则:
  - _is_same_subject: (stock_code, subject_kind) 相同才可能是"同一话题"
  - sentiment 相同 + 7 天内 → 不 supersede（补充性分析）
  - sentiment 相反 → 标记 superseded + 记录冲突
  - superseeded 的记忆在检索时默认排除（除非 include_superseded=True）

用法:
    from memory.merge import Merger
    m = Merger(backend)
    conflicts = m.detect_conflict(new_entry, old_entries)
    m.mark_superseded(new_entry.entry_id, old_entries, reason="new_analysis")
"""
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

_log = logging.getLogger(__name__)


class Merger:
    """记忆合并管理器。

    在 SDK.archive() 时调用，对同标的旧记忆标记 superseded，
    对矛盾结论记录冲突事件。
    """

    def __init__(self, db_path: str = ""):
        self._db_path = db_path

    # ── 同主题判定 ──────────────────────────────────────────

    @staticmethod
    def _is_same_subject(new, old) -> bool:
        """判断新旧记忆是否属同一话题。

        三元组匹配：(stock_code, subject_kind)
        - stock_code 不同 → False
        - subject_kind 不同 → False（不同维度分析不冲突）
        - sentiment 相同 + 7 天内 → False（补充性，不取代）
        - 其余 → True（新分析取代旧分析）
        """
        # 股票不同
        new_code = getattr(new, "stock_code", "")
        old_code = getattr(old, "stock_code", "")
        if new_code != old_code or not new_code:
            return False

        # 主题类型不同
        new_kind = (new.metadata or {}).get("subject_kind", "")
        old_kind = (old.metadata or {}).get("subject_kind", "")
        if not new_kind or not old_kind or new_kind != old_kind:
            return False

        # sentiment 相同且 7 天内 → 补充分析，不取代
        new_sent = (new.metadata or {}).get("sentiment", "")
        old_sent = (old.metadata or {}).get("sentiment", "")
        if new_sent and old_sent and new_sent == old_sent:
            new_date = (new.metadata or {}).get("date", "")
            old_date = (old.metadata or {}).get("date", "")
            try:
                if new_date and old_date:
                    nd = datetime.fromisoformat(new_date[:10])
                    od = datetime.fromisoformat(old_date[:10])
                    if abs((nd - od).days) < 7:
                        return False
            except (ValueError, TypeError):
                pass

        return True

    # ── 冲突检测 ────────────────────────────────────────────

    def detect_conflict(self, new_entry, old_entries: List) -> List[dict]:
        """检测新旧记忆之间的冲突。

        Returns:
            [{"type": "sentiment_reversal"|"tag_contradiction"|"value_conflict",
              "old_entry_id": str, "old_value": str, "new_value": str}, ...]
        """
        conflicts = []

        for old in old_entries:
            if not self._is_same_subject(new_entry, old):
                continue

            # 1. sentiment 反转
            new_sent = (new_entry.metadata or {}).get("sentiment", "")
            old_sent = (old.metadata or {}).get("sentiment", "")
            if new_sent and old_sent and new_sent != old_sent:
                conflicts.append({
                    "type": "sentiment_reversal",
                    "old_entry_id": getattr(old, "entry_id", ""),
                    "old_value": old_sent,
                    "new_value": new_sent,
                })
                continue  # sentiment 反转是最高级别冲突，不再检查子类型

            # 2. tags 矛盾
            new_tags = set(new_entry.metadata.get("tags", []) or [])
            old_tags = set(old.metadata.get("tags", []) or [])
            # 强势 ↔ 高位风险 矛盾对
            contradiction_pairs = [
                ({"强势", "看多"}, {"高位风险", "看空", "见顶"}),
                ({"资金流入", "主力净流入"}, {"资金流出", "主力净流出"}),
                ({"低估值"}, {"高估值"}),
            ]
            for pos_set, neg_set in contradiction_pairs:
                if (new_tags & pos_set and old_tags & neg_set) or \
                   (new_tags & neg_set and old_tags & pos_set):
                    conflicts.append({
                        "type": "tag_contradiction",
                        "old_entry_id": getattr(old, "entry_id", ""),
                        "old_value": ", ".join(old_tags & (pos_set | neg_set)),
                        "new_value": ", ".join(new_tags & (pos_set | neg_set)),
                    })
                    break

            # 3. 数值冲突（PE 差 >50% 且 sentiment 一致时仍报告）
            new_pe = new_entry.metadata.get("pe")
            old_pe = old.metadata.get("pe")
            if new_pe and old_pe and isinstance(new_pe, (int, float)) and isinstance(old_pe, (int, float)):
                if old_pe > 0 and abs(new_pe - old_pe) / old_pe > 0.5:
                    conflicts.append({
                        "type": "value_conflict",
                        "old_entry_id": getattr(old, "entry_id", ""),
                        "old_value": f"PE={old_pe:.1f}",
                        "new_value": f"PE={new_pe:.1f}",
                    })

        return conflicts

    # ── 标记取代 ────────────────────────────────────────────

    def mark_superseded(self, new_entry_id: str,
                         old_entries: List,
                         reason: str = "new_analysis",
                         db_path: str = "") -> int:
        """对每条同主题旧记忆标记 superseded_by + 写 conflict_log。

        Returns:
            被取代的条目数
        """
        if not old_entries:
            return 0

        now = datetime.now().isoformat()
        count = 0
        marked_entries = []

        # 从 old_entries 中取第一个的 entry_id 作为"新分析"的代表
        # 实际由调用方传入 new_entry_id
        for old in old_entries:
            old_id = getattr(old, "entry_id", "")
            if not old_id or old_id == new_entry_id:
                continue
            if hasattr(old, "superseded_by"):
                old.superseded_by = new_entry_id
            if hasattr(old, "superseded_at"):
                old.superseded_at = now
            if hasattr(old, "supersede_reason"):
                old.supersede_reason = reason
            count += 1
            marked_entries.append(old)

        # 写 conflict_log（如果提供了 db_path）
        if db_path and count > 0:
            self._write_conflict_log(db_path, new_entry_id, marked_entries)

        return count

    def _write_conflict_log(self, db_path: str, new_entry_id: str,
                             old_entries: List):
        """将冲突事件写入 conflict_log 表。"""
        import sqlite3
        try:
            conn = sqlite3.connect(db_path)
            for old in old_entries:
                old_id = getattr(old, "entry_id", "")
                if not old_id:
                    continue
                # 检测冲突类型
                conflict_type = ""
                old_val = ""
                new_val = ""
                new_sent = (getattr(old, "metadata", {}) or {}).get("sentiment", "")
                # 用 old 自身的 sentiment 与实际新 sentiment 对比
                # 简化：标记为 "superseded"
                conflict_type = "superseded"
                conn.execute(
                    "INSERT INTO conflict_log "
                    "(new_entry_id, old_entry_id, conflict_type, old_value, new_value) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (new_entry_id, old_id, conflict_type, old_val, new_val)
                )
            conn.commit()
            conn.close()
        except Exception:
            _log.debug("[merge] conflict_log write failed", exc_info=True)

    # ── 查询 ────────────────────────────────────────────────

    def find_superseded_chain(self, entry_id: str,
                               all_entries: List) -> List:
        """反查演进链：entry → superseded_by → ... → 当前结论。

        Args:
            entry_id: 起始 entry_id
            all_entries: 全量条目列表（调用方自行查询）
        Returns:
            按时间正序的演进链列表
        """
        entry_map = {}
        for e in all_entries:
            eid = getattr(e, "entry_id", "")
            if eid:
                entry_map[eid] = e

        chain = []
        seen = set()
        current_id = entry_id
        while current_id and current_id not in seen:
            seen.add(current_id)
            entry = entry_map.get(current_id)
            if entry:
                chain.append(entry)
                current_id = getattr(entry, "superseded_by", "")
            else:
                break
        return chain

    def get_current_conclusions(self, stock_code: str,
                                  all_entries: List) -> List:
        """返回某标的当前未被 superseded 的结论。

        Args:
            stock_code: 股票代码
            all_entries: 全量条目列表
        """
        return [
            e for e in all_entries
            if getattr(e, "stock_code", "") == stock_code
            and not getattr(e, "superseded_by", "")
        ]
