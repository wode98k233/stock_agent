from typing import Any, List, Optional

from langchain_core.callbacks.base import BaseCallbackHandler, Callbacks
from langchain_core.language_models import BaseLanguageModel
from langchain_core.outputs import LLMResult
from langchain_core.prompt_values import PromptValue


class TracedLLM(BaseLanguageModel):
    def __init__(self, llm: BaseLanguageModel, recorder: BaseCallbackHandler):
        super().__init__()
        self._llm = llm
        self._recorder = recorder

    @property
    def _llm_type(self) -> str:
        return self._llm._llm_type

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        callbacks = run_manager.get_child() if run_manager else None
        if callbacks:
            callbacks.add_handler(self._recorder)
        return self._llm._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        callbacks = run_manager.get_child() if run_manager else None
        if callbacks:
            callbacks.add_handler(self._recorder)
        return await self._llm._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def generate_prompt(
        self,
        prompts: list[PromptValue],
        stop: list[str] | None = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> LLMResult:
        return self._llm.generate_prompt(
            prompts, stop=stop, callbacks=callbacks, **kwargs
        )

    async def agenerate_prompt(
        self,
        prompts: list[PromptValue],
        stop: list[str] | None = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> LLMResult:
        return await self._llm.agenerate_prompt(
            prompts, stop=stop, callbacks=callbacks, **kwargs
        )

    def invoke(self, input, config=None, **kwargs):
        config = config or {}
        existing = (config.get("callbacks") or []) if isinstance(config.get("callbacks"), list) else []
        if isinstance(existing, list):
            config["callbacks"] = existing + [self._recorder]
        else:
            config["callbacks"] = [self._recorder]
        return self._llm.invoke(input, config=config, **kwargs)

    def bind_tools(self, tools, **kwargs):
        self._llm = self._llm.bind_tools(tools, **kwargs)
        return self

    def with_structured_output(self, schema, **kwargs):
        self._llm = self._llm.with_structured_output(schema, **kwargs)
        return self

    @property
    def _identifying_params(self):
        return self._llm._identifying_params

    def get_num_tokens(self, text: str) -> int:
        return self._llm.get_num_tokens(text)


def patch_llm(llm: BaseLanguageModel,
              recorder: BaseCallbackHandler) -> BaseLanguageModel:
    if isinstance(llm, TracedLLM):
        return llm
    return TracedLLM(llm, recorder)
