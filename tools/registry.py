class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, name, handler):
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = handler

    def exists(self, name):
        return name in self._tools

    @property
    def names(self):
        return tuple(self._tools)

    async def execute(self, action):
        handler = self._tools.get(action.name)

        if handler is None:
            raise RuntimeError(f"Unknown tool: {action.name}")

        return await handler(**action.args)


class BusinessToolRegistry:
    """Explicit whitelist for model-requested business operations."""

    def __init__(self, allowed_names=()):
        self._allowed = frozenset(allowed_names)
        self._tools = {}

    def register(self, tool):
        if tool.name not in self._allowed:
            raise ValueError(f"Business tool is not allowlisted: {tool.name}")
        if tool.name in self._tools:
            raise ValueError(f"Business tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def exists(self, name):
        return name in self._allowed and name in self._tools

    @property
    def names(self):
        return tuple(self._tools)

    async def execute(self, name: str, arguments: dict):
        if name not in self._allowed:
            raise PermissionError(f"Business tool is not allowlisted: {name}")
        tool = self._tools.get(name)
        if tool is None:
            raise RuntimeError(f"Business tool is not configured: {name}")
        return await tool.execute(dict(arguments))
