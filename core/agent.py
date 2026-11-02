class Agent:
    def __init__(self, graph):
        self.graph = graph
        self.loop = graph  # Backward-compatible handle for existing integrations.

    @property
    def state(self):
        """The graph's AgentState.

        The control plane (pause, status snapshot, shutdown, health) probes
        `agent.state` with `getattr`, so without this the flags it writes land
        nowhere and every status field reports `None`.
        """
        return self.graph.state

    async def run(self):
        await self.graph.run()

    async def run_once(self):
        return await self.graph.run_once()
