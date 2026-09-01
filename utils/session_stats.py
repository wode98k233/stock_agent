import time
from utils.logger import RequestContext


class SessionStats:
    def __init__(self):
        self.queries = 0
        self.total_tokens = 0
        self.total_cached_tokens = 0
        self.total_llm_calls = 0
        self.total_tool_calls = 0
        self.start_time = time.time()

    def accumulate(self, ctx: RequestContext):
        self.queries += 1
        self.total_tokens += ctx.metrics['llm_tokens_in'] + ctx.metrics['llm_tokens_out']
        self.total_cached_tokens += ctx.metrics.get('llm_cached_tokens', 0)
        self.total_llm_calls += ctx.metrics['llm_calls']
        self.total_tool_calls += ctx.metrics['tool_calls']

    def summary(self) -> str:
        elapsed = time.time() - self.start_time
        cache_part = f" | 缓存命中 {self.total_cached_tokens}" if self.total_cached_tokens else ""
        return (f"📊 会话统计: {self.queries} 次查询 | "
                f"{self.total_tokens} tokens{cache_part} | "
                f"{self.total_llm_calls} 次 LLM 调用 | "
                f"{self.total_tool_calls} 次工具调用 | "
                f"耗时 {elapsed:.0f}s")
