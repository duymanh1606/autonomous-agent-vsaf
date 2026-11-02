class TargetNotFound(RuntimeError):
    """A reviewed UI target was not present in Extension or browser DOM."""

    def __init__(self, target: str):
        self.target = str(target)
        super().__init__(f'TargetNotFound("{self.target}")')

    def as_dict(self):
        return {"type": "TargetNotFound", "target": self.target}
