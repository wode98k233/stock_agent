class AgentContext:
    __slots__ = ('logger', 'memory', 'skill_registry', 'budget',
                 'progress_reporter')

    def __init__(self, logger, memory, skill_registry, **kwargs):
        self.logger = logger
        self.memory = memory
        self.skill_registry = skill_registry
        self.budget = kwargs.get('budget')
        self.progress_reporter = kwargs.get('progress_reporter')
