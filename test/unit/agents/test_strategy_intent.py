"""agents.shared.strategy_intent 单测（纯离线，零 IO 依赖外部状态）。

覆盖计划任务 3 步骤 3.3 的 24 条必测清单（逐条对应，见各用例注释）。
"""
import json
from datetime import date

import pytest

from agents.shared import strategy_intent as si
from agents.shared.strategy_intent import detect_strategy_intent, validate_rules


def _real_rules():
    """读真实词表当合法样本（校验与覆盖用例共用）"""
    with open(si._RULES_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def test_empty_input():
    """1. 空串/空白 → 不判策略，reason=empty"""
    for text in ('', '   ', '\n\t'):
        r = detect_strategy_intent(text)
        assert r['is_strategy'] is False
        assert r['reason'] == 'empty'
        assert r['disambiguate'] is False
        assert r['confidence'] == 0.0


def test_imperative_tail_buy_next_day():
    """2. 显式祈使 + 「尾盘」先于「第二天」命中（词表顺序即优先级）"""
    r = detect_strategy_intent('欸你说，尾盘买入第二天卖出，这个策略如何？')
    assert r['is_strategy'] is True
    assert r['reason'] == 'explicit_imperative'
    assert r['confidence'] >= 0.9
    assert r['slots']['entry_timing'] == 'same_close'   # 「尾盘」在词表里先于「第二天」
    assert r['slots']['entry'].startswith('尾盘买入')


def test_imperative_backtest_please():
    """3. 「帮我回测一下…」命中祈使词「帮我回测」"""
    r = detect_strategy_intent('帮我回测一下 MACD 金叉')
    assert r['is_strategy'] is True
    assert r['reason'] == 'explicit_imperative'
    assert 'macd' in r['signals']['condition_words']


def test_pair_hit_enters():
    """4. 条件词 × 意图词成对命中 → 进会话"""
    r = detect_strategy_intent('MACD 金叉买入靠不靠谱')
    assert r['is_strategy'] is True
    assert r['reason'] == 'condition_intent_pair'
    assert r['confidence'] == pytest.approx(0.7)
    assert r['disambiguate'] is False


def test_condition_only_needs_disambiguation():
    """5. 「MACD 是什么」：只有条件词 + 提问句式 → 不判策略但发分流题"""
    r = detect_strategy_intent('MACD 是什么')
    assert r['is_strategy'] is False
    assert r['disambiguate'] is True
    assert r['reason'] == 'question_form'


def test_condition_only_no_question_marker():
    """6. 「看看 MACD 金叉」：只有条件词且无提问句式 → 仍发分流题"""
    r = detect_strategy_intent('看看 MACD 金叉')
    assert r['is_strategy'] is False
    assert r['disambiguate'] is True
    assert r['signals']['condition_words']


def test_intent_only_needs_disambiguation():
    """7. 「胜率」：只有意图词 → 单项命中不放行"""
    r = detect_strategy_intent('胜率')
    assert r['is_strategy'] is False
    assert r['disambiguate'] is True
    assert r['signals']['intent_words'] == ['胜率']


def test_plain_report_question():
    """8. 最重要的一条：普通提问不能被误判成策略，也不能弹分流题"""
    r = detect_strategy_intent('茅台最近怎么样')
    assert r['is_strategy'] is False
    assert r['disambiguate'] is False
    assert r['signals']['condition_words'] == []
    assert r['signals']['entry_words'] == []
    assert r['signals']['exit_words'] == []


def test_plain_news_question():
    """9. 普通消息类提问同样不判策略、不弹分流题"""
    r = detect_strategy_intent('最近有什么消息')
    assert r['is_strategy'] is False
    assert r['disambiguate'] is False
    # 注：该句不含词表内的提问句式（question_markers 只收「是什么/为什么/怎么算…」），
    # 零命中 ⇒ 不判策略、不弹分流题。断言只用计划口径，不锁 question_marker 取值。


def test_question_marker_suppresses():
    """10. 有提问句式且无意图词 → 提问优先于条件词"""
    r = detect_strategy_intent('MACD 和 KDJ 有什么区别')
    assert r['is_strategy'] is False
    # 计划口径只定 is_strategy=False；实现里仍属「只命中条件词」一侧 ⇒ 分流为 True
    # （与用例 5「MACD 是什么」同口径：先问一句比猜好）。
    assert r['reason'] == 'question_form'


def test_code_extraction():
    """11. 6 位代码 → universe"""
    r = detect_strategy_intent('600519 上用 MACD 金叉回测一下')
    assert r['slots']['universe'] == '600519'
    assert r['signals']['has_universe'] is True


def test_code_not_from_amount():
    """12. 前后连数字不算代码（避免命中金额/年份）"""
    r = detect_strategy_intent('1234567 元的账户回测一下')
    assert r['slots']['universe'] == ''
    assert r['signals']['has_universe'] is False


def test_universe_keyword():
    """13. 关键词兜底映射（沪深300 → 成分股范围，不再是指数单码）"""
    r = detect_strategy_intent('沪深300 上试一下这个策略')
    assert r['slots']['universe'] == 'POOL:index:000300'
    assert r['slots']['universe_source'] == 'keyword'


def test_parse_compare_timing():
    """「开盘卖 or 收盘卖」要识别成两种口径对比，而不是二选一。"""
    from agents.shared.strategy_intent import parse_compare_timing
    assert parse_compare_timing('持有3天后开盘价卖出或者是收盘价卖出')
    assert parse_compare_timing('开盘价和收盘价都对比一下')
    assert parse_compare_timing('开盘或者尾盘卖出都看看')
    assert parse_compare_timing('两种卖法都试试')
    assert not parse_compare_timing('次日开盘卖出')
    assert not parse_compare_timing('持有3个交易日卖出')
    assert not parse_compare_timing('收盘价高于开盘价才买')
    # 单一口径不能误判成「两种卖法」：第一个时点紧跟着「买」= 买入口径。
    # 真机 2026-10-25：「尾盘买入、次日开盘卖出」被当成对比，白跑两遍。
    assert not parse_compare_timing('尾盘买入、次日开盘卖出')
    assert not parse_compare_timing('次日开盘价买入，持有3天后的开盘价卖出')
    assert parse_compare_timing('成交口径次日开盘价买入，然后是持有3-5个交易日后的开盘价卖出 或者是收盘价卖出')


def test_parse_hold_periods():
    """「持有 3-5 个交易日」要给出持有期矩阵 [3,4,5]。"""
    from agents.shared.strategy_intent import parse_hold_periods
    assert parse_hold_periods('持有3-5个交易日卖出') == [3, 4, 5]
    assert parse_hold_periods('持有3、4、5天卖出') == [3, 4, 5]
    assert parse_hold_periods('持有3个交易日卖出') == []
    assert parse_hold_periods('第二天开盘买入') == []


def test_period_absolute_month():
    """「区间改成 2024 年 9 月份开始」必须落成绝对起点，不能静默变成近 3 年。"""
    from datetime import date
    r = detect_strategy_intent('区间改成2024年9月份开始就行了，结束时间到现在')
    assert r['slots']['period_start'] == '2024-09-01'
    assert r['slots']['period_end'] == date.today().isoformat()


def test_period_absolute_month_needs_range_marker():
    """「2024年9月有一次深V」是举例，不是区间，不能当起点。"""
    r = detect_strategy_intent('2024年9月有一次深V，第二天开盘买入')
    assert r['slots']['period_start'] == ''


def test_period_words():
    """14. 区间词 → ISO 日期，跨度约 1095 天"""
    r = detect_strategy_intent('近3年 MACD 金叉回测一下')
    start = r['slots']['period_start']
    end = r['slots']['period_end']
    assert start and end
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    assert abs((d1 - d0).days - 1095) <= 1


def test_timing_word_next_open():
    """15. 「第二天卖出」→ 次日开盘成交。

    ⚠️ 口径按**位置**归属动作词（口径写在动作词前是中文常态），所以「第二天」归卖点，
    不能再塞进 ``entry_timing``。旧实现「命中第一个 timing 词就break」把买点/卖点混为一谈，
    会让「尾盘买入、次日卖出」丢掉整个卖点时点。
    """
    r = detect_strategy_intent('第二天卖出这个策略如何')
    assert r['slots']['exit_timing'] == 'next_open'
    assert r['slots']['entry_timing'] == ''


def test_timing_split_entry_and_exit():
    """15b.买卖口径各自抽取（位置法归属后面最近的动作词）"""
    cases = [
        ('尾盘买入，次日卖出',        'same_close', 'next_open'),
        ('尾盘买入，次日收盘卖出',      'same_close', 'next_close'),
        ('尾盘买入',                'same_close', ''),
        ('次日开盘卖出',                 '',      'next_open'),
        ('尾盘买，次日收盘卖',           'same_close', 'next_close'),
        ('买入尾盘',                   'same_close', ''),
    ]
    for text, entry, exit_ in cases:
        s = detect_strategy_intent(text)['slots']
        assert (s['entry_timing'], s['exit_timing']) == (entry, exit_), text


def test_timing_word_same_close():
    """16. 「收盘价」→ 当日收盘价成交"""
    r = detect_strategy_intent('收盘价买入这个策略如何')
    assert r['slots']['entry_timing'] == 'same_close'


def test_entry_exit_clause():
    """17. 入场/出场原话留档（按标点切句取命中句）"""
    r = detect_strategy_intent('收盘价上穿 5 日均线买入，第二天开盘卖出')
    assert '买入' in r['slots']['entry']
    assert '卖出' in r['slots']['exit']


def test_signals_persistable():
    """18. signals 六个键齐全且可 JSON 序列化（会落 strategy_session.intent_signals）"""
    r = detect_strategy_intent('600519 近3年 MACD 金叉买入回测一下胜率多少')
    for key in ('imperatives', 'intent_words', 'condition_words',
                'entry_words', 'exit_words'):
        assert isinstance(r['signals'][key], list), key
    assert isinstance(r['signals']['question_marker'], bool)
    json.dumps(r, ensure_ascii=False)


def test_rules_override():
    """19. 词表可换：传入自定义 rules 时完全按自定义词表求值"""
    custom = _real_rules()
    custom['imperatives'] = ['自定义祈使句']
    r = detect_strategy_intent('自定义祈使句', custom)
    assert r['is_strategy'] is True
    assert r['reason'] == 'explicit_imperative'
    # 换成自定义祈使词后，原词表的祈使词失效；条件词缺失 ⇒ L2 成对条件不成立
    r2 = detect_strategy_intent('回测一下', custom)
    assert r2['is_strategy'] is False
    assert r2['reason'] == 'single_sided'
    custom['condition_words'] = []
    assert detect_strategy_intent('MACD 金叉', custom)['is_strategy'] is False


def test_missing_rules_file(monkeypatch, tmp_path):
    """20. 词表文件缺失 → FileNotFoundError（显式失败，不静默降级）"""
    monkeypatch.setattr(si, '_RULES_PATH', tmp_path / '不存在.json')
    monkeypatch.setattr(si, '_LOADED', False)
    si._RULES.clear()
    with pytest.raises(FileNotFoundError):
        si._rules()
    monkeypatch.undo()
    assert si.reload_rules()   # 复原，避免影响后续用例


def test_long_input_truncation():
    """21. 超长输入不炸，entry 截断到 40 字"""
    text = '尾盘买入这个策略如何' + '啊' * 1990
    assert len(text) >= 2000
    r = detect_strategy_intent(text)
    assert r['is_strategy'] is True
    assert 0 < len(r['slots']['entry']) <= 40


def test_case_insensitive():
    """22. 大小写不敏感（小写 macd 同样命中）"""
    r = detect_strategy_intent('macd 金叉靠谱吗')
    assert r['is_strategy'] is True
    assert 'macd' in r['signals']['condition_words']


def test_signals_expose_action_words():
    """23. 锁住 R2「可疑带」判据：零命中但有买卖动作词，交 LLM 兜底"""
    r = detect_strategy_intent('早盘买进，第二天冲高卖掉')
    assert r['is_strategy'] is False
    assert r['disambiguate'] is False
    assert r['signals']['condition_words'] == []
    assert r['signals']['intent_words'] == []
    # 词表补了单字动词（买/入/卖/出），用于覆盖「尾盘买，次日收盘卖」这类口语，
    # 同一位置会同时命中「买进」和「买」——这里只锁「动作词被识别出来」这个契约。
    assert '买进' in r['signals']['entry_words']
    assert '卖掉' in r['signals']['exit_words']


def test_advice_timing_question_is_not_strategy():
    """操作/择时问题不能因为出现「买入/条件/止损」就被送进策略会话。"""
    q = ('我现在买入消费ETF（159928）或者中证白酒ETF（512690）是不是好时机？'
         '我感觉会迎来一波反弹，但现在已经开了一小波了。'
         '所以我问你是明天就买入，还是等回踩？什么区间内买入比较好？'
         '请给出明确的操作建议、触发条件和风险控制。')
    r = detect_strategy_intent(q)
    assert r['is_strategy'] is False
    assert r['disambiguate'] is False
    assert r['reason'] == 'advice_question'
    assert r['signals']['advice_words']


def test_how_to_operate_question_is_not_strategy():
    """「…怎么操作？」哪怕带着买卖/涨幅条件，也只是问操作，不弹策略分流题。

    2026-11-02 真机原话：尾盘买入、次日开盘卖出、收盘涨幅 5% 到涨停 —— 命中条件词
    但没意图词，旧口径走 single_sided 分流题（先问「要回测还是只是问问」），
    用户看到的就是「我问操作，你把我往策略上引」。
    """
    r = detect_strategy_intent('尾盘买入，第二天开盘卖出，收盘涨幅在5%到涨停之间，怎么操作？')
    assert r['is_strategy'] is False
    assert r['disambiguate'] is False
    assert r['reason'] == 'advice_question'


def test_stock_picking_question_is_not_strategy():
    """「帮我选几只…股票」是选股，不是策略验证。"""
    r = detect_strategy_intent('帮我选几只低位放量的股票')
    assert r['is_strategy'] is False
    assert r['disambiguate'] is False
    assert r['reason'] == 'advice_question'


def test_explicit_backtest_still_wins_over_advice_words():
    r = detect_strategy_intent('帮我回测一下：明天买还是等回踩，什么区间买入')
    assert r['is_strategy'] is True
    assert r['reason'] in ('explicit_imperative', 'condition_intent_pair')


def test_validate_rules_and_degrade(monkeypatch, tmp_path):
    """24. ① 校验返回中文错误清单 ② 词表 JSON 损坏 → 降级为无意图，不抛"""
    errs = validate_rules({'imperatives': 'x'})
    assert any('imperatives' in e for e in errs)
    assert validate_rules(_real_rules()) == []
    assert validate_rules(['不是对象']) == ['词表根节点必须是 JSON 对象']
    bad = tmp_path / 'broken.json'
    bad.write_text('{', encoding='utf-8')
    monkeypatch.setattr(si, '_RULES_PATH', bad)
    try:
        assert si.reload_rules() == {}
        assert detect_strategy_intent('回测一下')['is_strategy'] is False
    finally:
        monkeypatch.undo()
        si.reload_rules()
    assert detect_strategy_intent('回测一下')['is_strategy'] is True


def test_reason_vocabulary_closed():
    """25. reason 只取自既定词表（可落库、可运营复盘）"""
    allowed = {'empty', 'explicit_imperative', 'condition_intent_pair',
               'question_form', 'single_sided', 'advice_question'}
    samples = ['', '回测一下 MACD 金叉', 'MACD 金叉买入靠不靠谱', 'MACD 是什么',
               '看看 MACD 金叉', '茅台最近怎么样', '早盘买进第二天卖掉']
    for s in samples:
        assert detect_strategy_intent(s)['reason'] in allowed, s
